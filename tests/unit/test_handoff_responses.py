import asyncio
import json
import os
import tempfile
import unittest
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient, MockTransport, Response
from openai import AsyncOpenAI
from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Executor,
    FinishReason,
    FunctionInvocationLayer,
    FunctionTool,
    Message,
    ResponseStream,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowEvent,
    agent_middleware,
    chat_middleware,
    function_middleware,
    handler,
    response_handler,
)
from agent_framework.orchestrations import HandoffAgentUserRequest, HandoffBuilder, HandoffSentEvent
from agent_framework.openai import OpenAIChatClient, OpenAIContinuationToken
from azure.ai.agentserver.responses.models import (
    CreateResponse, ItemMessage, MessageContentInputTextContent,
)

from agents import build_agent_roles
from models.specialist_review import SpecialistReview
from workflows.responses import HandoffRequestError, HandoffResponsesHost
from workflows.handoff import (
    HandoffRoutingError,
    _current_reviews,
    _render_primary_summary,
    build_checkpoint_safe_middleware,
    build_primary_summary_middleware,
    build_participants,
    build_workflow,
)


class Confirmation(Executor):
    @handler
    async def start(self, messages: list[Message], ctx: WorkflowContext) -> None:
        reply = AgentResponse(messages=[Message("assistant", [messages[0].text])])
        await ctx.yield_output(reply)
        await ctx.request_info(HandoffAgentUserRequest(reply), list[Message])

    @response_handler
    async def resume(
        self, original: HandoffAgentUserRequest, response: list[Message], ctx: WorkflowContext
    ) -> None:
        reply = AgentResponse(
            messages=[Message("assistant", [f"{original.agent_response.text} -> {response[0].text}"])]
        )
        await ctx.yield_output(reply)
        await ctx.request_info(HandoffAgentUserRequest(reply), list[Message])


def workflow():
    return WorkflowBuilder(
        name="confirmation-test", start_executor=Confirmation(id="confirm")
    ).build()


class LoggedConfirmation(Confirmation):
    @handler
    async def start(
        self,
        messages: list[Message],
        ctx: WorkflowContext[Never, AgentResponseUpdate | AgentResponse | dict[str, str]],
    ) -> None:
        await ctx.add_event(WorkflowEvent(
            "handoff_sent",
            data=HandoffSentEvent(source="qvn-primary-agent", target="qvn-coding-agent"),
        ))
        await ctx.add_event(WorkflowEvent(
            "handoff_sent", data={"source": messages[0].text, "token": "private-token"}
        ))
        await ctx.yield_output({"body": "private-unrecognized-output"})
        for text in ("private-coding-first", "private-coding-second"):
            await ctx.yield_output(AgentResponseUpdate(
                contents=[Content.from_text(text)],
                role="assistant",
                author_name="qvn-coding-agent",
                agent_id="ignored-stream-agent",
                response_id="private-provider-response",
            ))
        await ctx.add_event(WorkflowEvent(
            "handoff_sent",
            data=HandoffSentEvent(source="qvn-coding-agent", target="qvn-primary-agent"),
        ))
        reply = AgentResponse(
            messages=[Message(
                "assistant", ["private-primary-summary"], author_name="ignored-message-author"
            )],
            agent_id="qvn-primary-agent",
        )
        await ctx.yield_output(reply)
        await ctx.request_info(HandoffAgentUserRequest(reply), list[Message])


def logging_workflow(*, intermediate=False):
    executor = LoggedConfirmation(id="logged-confirm")
    return WorkflowBuilder(
        name="logging-test",
        start_executor=executor,
        output_from=[] if intermediate else [executor],
        intermediate_output_from=[executor] if intermediate else [],
    ).build()


class ScriptedHandoffClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """Replace only the model; framework middleware still handles the handoff tools."""

    def __init__(
        self, name: str, script: Sequence[tuple[tuple[str, ...], str | None]]
    ) -> None:
        super().__init__()
        self.name = name
        self.script = list(script)
        self.calls = 0
        self.requests = []
        self.options_seen = []
        self.response_updates = []
        self.finalized_responses = []
        self.hook_counts = {"finalizer": 0, "transform": 0, "result": 0, "cleanup": 0}

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        if self.calls >= len(self.script):
            raise AssertionError(f"Unexpected model call for {self.name}")
        chunks, target = self.script[self.calls]
        self.calls += 1
        self.requests.append([message.to_dict() for message in messages])
        self.options_seen.append(dict(options))
        message_id = f"{self.name}-{self.calls}"
        updates = [
            ChatResponseUpdate(
                role="assistant",
                author_name=self.name,
                message_id=message_id,
                response_id=message_id,
                continuation_token=OpenAIContinuationToken(response_id=message_id),
                additional_properties={"provider_event": event},
            )
            for event in ("response.created", "response.in_progress")
        ]
        updates.extend(
            ChatResponseUpdate(
                contents=[Content.from_text(text)],
                role="assistant",
                author_name=self.name,
                message_id=message_id,
            )
            for text in chunks
        )
        if target is not None:
            tool_name = f"handoff_to_{target}"
            if not any(
                isinstance(tool, FunctionTool) and tool.name == tool_name
                for tool in options.get("tools", [])
            ):
                raise AssertionError(f"Framework did not inject {tool_name}")
            updates.append(ChatResponseUpdate(
                contents=[Content.from_function_call(
                    call_id=message_id, name=tool_name, arguments={}
                )],
                role="assistant",
                author_name=self.name,
                message_id=message_id,
            ))
        updates.append(ChatResponseUpdate(
            role="assistant",
            author_name=self.name,
            message_id=message_id,
            response_id=message_id,
            finish_reason="tool_calls" if target is not None else "stop",
        ))
        self.response_updates.append(updates)

        async def generate():
            for update in updates:
                yield update

        def finalize(items):
            self.hook_counts["finalizer"] += 1
            response = ChatResponse.from_updates(
                items, output_format_type=options.get("response_format")
            )
            response.additional_properties["synthetic_finalizer"] = self.name
            self.finalized_responses.append(response)
            return response

        def transform(update):
            self.hook_counts["transform"] += 1
            return update

        def result_hook(response):
            self.hook_counts["result"] += 1
            response.additional_properties["synthetic_result_hook"] = True
            return response

        def cleanup():
            self.hook_counts["cleanup"] += 1

        if stream:
            return (
                ResponseStream(generate(), finalizer=finalize)
                .with_transform_hook(transform)
                .with_result_hook(result_hook)
                .with_cleanup_hook(cleanup)
            )

        async def complete():
            return finalize(updates)

        return complete()


class ScriptedRoleClients:
    def __init__(self, scripts, *, client_factory=None, middleware_factory=None):
        self.scripts = scripts
        self.clients = {}
        self.agents = {}
        self.client_factory = client_factory or ScriptedHandoffClient
        self.middleware_factory = middleware_factory

    def as_agent(self, **options):
        name = options["name"]
        client = self.client_factory(name, self.scripts.get(name, []))
        self.clients[name] = client
        if self.middleware_factory is not None:
            options = dict(options)
            options["middleware"] = [
                *self.middleware_factory(name), *options.get("middleware", []),
            ]
        agent = client.as_agent(**options)
        self.agents[name] = agent
        return agent


class FaultHandoffClient(ScriptedHandoffClient):
    def __init__(self, name, script, fault):
        super().__init__(name, script)
        self.fault = fault

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.calls += 1
        tool = next(item for item in options["tools"] if isinstance(item, FunctionTool))
        if self.fault == "empty":
            contents = []
        elif self.fault == "unknown_tool":
            contents = [Content.from_function_call(
                call_id="fault-call", name="not_a_handoff", arguments={}
            )]
        elif self.fault == "invalid_arguments":
            contents = [Content.from_function_call(
                call_id="fault-call", name=tool.name, arguments={"unexpected": True}
            )]
        elif self.fault == "user_action":
            contents = [Content(
                "function_approval_request", id="synthetic-approval", user_input_request=True
            )]
        elif self.fault == "error_content":
            contents = [Content("error", error_code="synthetic_error", message="Synthetic failure")]
        else:
            contents = [Content.from_text("SYNTHETIC-PARTIAL\n")]

        async def generate():
            if self.fault == "technical":
                raise RuntimeError("Synthetic provider failure")
            if self.fault == "timeout":
                await asyncio.sleep(1)
            if self.fault == "missing_terminal":
                yield ChatResponseUpdate(
                    role="assistant",
                    continuation_token=OpenAIContinuationToken(response_id="resp_pending"),
                )
            if contents:
                yield ChatResponseUpdate(
                    contents=contents, role="assistant",
                )
            if self.fault == "midstream":
                raise RuntimeError("Synthetic midstream failure")
            if self.fault == "cancel":
                raise asyncio.CancelledError()
            if self.fault != "missing_terminal":
                yield ChatResponseUpdate(
                    role="assistant",
                    finish_reason=FinishReason(
                        "length" if self.fault == "unfinished"
                        else "cancelled" if self.fault == "cancel_reason"
                        else "incomplete" if self.fault == "incomplete"
                        else "content_filter" if self.fault == "content_filter"
                        else "tool_calls" if self.fault in ("unknown_tool", "invalid_arguments")
                        else "stop"
                    ),
                    continuation_token=(
                        OpenAIContinuationToken(response_id="resp_pending")
                        if self.fault == "pending" else None
                    ),
                )

        if stream:
            return ResponseStream(generate(), finalizer=ChatResponse.from_updates)

        async def complete():
            return ChatResponse.from_updates([update async for update in generate()])

        return complete()


class ServiceDefaultHandoffClient(ScriptedHandoffClient):
    STORES_BY_DEFAULT = True


def provider_responses_events(
    chunks: Sequence[str], *, target: str | None = None,
    status: str | None = "completed", incomplete_reason: str | None = None,
):
    text = "".join(chunks)
    response = {
        "id": "resp_offline_provider", "object": "response", "created_at": 1,
        "model": "offline-responses", "parallel_tool_calls": False,
        "tool_choice": "auto", "tools": [], "output": [], "status": "in_progress",
    }
    part = {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
    message = {
        "id": "msg_offline_provider", "type": "message", "role": "assistant",
        "status": "completed", "content": [part],
    }
    location = {"item_id": message["id"], "output_index": 0, "content_index": 0}
    events = [
        {"type": "response.created", "response": response},
        {"type": "response.in_progress", "response": response},
        {
            "type": "response.output_item.added", "output_index": 0,
            "item": {**message, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added", **location,
            "part": {**part, "text": ""},
        },
        *[
            {"type": "response.output_text.delta", **location, "delta": chunk, "logprobs": []}
            for chunk in chunks
        ],
        {"type": "response.output_text.done", **location, "text": text, "logprobs": []},
        {"type": "response.content_part.done", **location, "part": part},
        {"type": "response.output_item.done", "output_index": 0, "item": message},
    ]
    output = [message]
    if target is not None:
        call = {
            "type": "function_call", "id": "fc_offline_provider",
            "call_id": "call_offline_provider", "name": f"handoff_to_{target}",
            "arguments": "{}", "status": "completed",
        }
        call_location = {"item_id": call["id"], "output_index": 1}
        events.extend([
            {
                "type": "response.output_item.added", "output_index": 1,
                "item": {**call, "arguments": "", "status": "in_progress"},
            },
            {"type": "response.function_call_arguments.delta", **call_location, "delta": "{}"},
            {
                "type": "response.function_call_arguments.done", **call_location,
                "name": call["name"], "arguments": "{}",
            },
            {"type": "response.output_item.done", "output_index": 1, "item": call},
        ])
        output.append(call)
    if status is not None:
        events.append({
            "type": f"response.{status}",
            "response": {
                **response, "status": status, "output": output,
                "error": (
                    {"code": "server_error", "message": "Synthetic provider failure"}
                    if status == "failed" else None
                ),
                "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
            },
        })
    return "\n\n".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': index})}"
        for index, event in enumerate(events)
    ) + "\n\n"


def context(text, conversation="conversation-a", user="user-a", response=None):
    async def get_input_items():
        return [ItemMessage(role="user", content=text)]

    async def get_history():
        return []

    return SimpleNamespace(
        get_input_items=get_input_items,
        get_history=get_history,
        conversation_id=conversation,
        # 與實際平台一致：每一輪都有不同的 response_id。
        response_id=response or f"resp_{uuid.uuid4().hex}",
        platform_context=SimpleNamespace(user_id_key=user, call_id="test-call"),
    )


def event_payload(event):
    """保留相同 wire 斷言，只轉換舊 model／新版 dict 的公開表示法。"""
    return event.as_dict() if hasattr(event, "as_dict") else dict(event)


class HandoffResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "handoff"
        self.root.mkdir()
        environment = patch.dict(os.environ, {
            "AGENTSERVER_STATE_ROOT": str(Path(self.directory.name) / "agentserver"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.host = HandoffResponsesHost(
            workflow, checkpoint_root=self.root, configure_observability=None
        )

    async def turn(self, text, *, host=None, previous=None, **kwargs):
        request = CreateResponse(input=text, stream=True, previous_response_id=previous)
        return "".join([
            chunk async for chunk in (host or self.host)._text_chunks(
                request, context(text, **kwargs), asyncio.Event()
            )
        ])

    def production_host(
        self, scripts, *, suffix="production", client_factory=None, middleware_factory=None
    ):
        roles = build_agent_roles("policy-")
        sources = []

        def factory():
            named_scripts = {
                roles[key].agent_name: [
                    (chunks, roles[target].agent_name if target is not None else None)
                    for chunks, target in steps
                ]
                for key, steps in scripts.items()
            }
            source = ScriptedRoleClients(
                named_scripts, client_factory=client_factory,
                middleware_factory=middleware_factory,
            )
            sources.append(source)
            return build_workflow(
                build_participants(source, roles, checkpoint_safe=True),
                name="production-policy-test",
            )

        return (
            HandoffResponsesHost(
                factory, checkpoint_root=self.root / suffix, configure_observability=None
            ),
            roles,
            sources,
        )

    def test_handoff_log_source_priority(self):
        cases = (
            (AgentResponseUpdate(author_name="author", agent_id="agent"), "author"),
            (AgentResponseUpdate(author_name="", agent_id="agent"), "agent"),
            (AgentResponseUpdate(), "unknown"),
            (AgentResponse(
                agent_id="agent",
                messages=[Message("assistant", ["private-body"], author_name="author")],
            ), "agent"),
            (AgentResponse(messages=[
                Message("assistant", ["private-body"]),
                Message("assistant", ["private-body"], author_name=""),
                Message("assistant", ["private-body"], author_name="first-author"),
                Message("assistant", ["private-body"], author_name="second-author"),
            ]), "first-author"),
            (AgentResponse(), "unknown"),
            (Message("assistant", ["private-body"], author_name="author"), "author"),
            (Message("assistant", ["private-body"]), "unknown"),
            ({"body": "private-body"}, "unknown"),
        )
        for data, expected in cases:
            with self.subTest(data_type=type(data).__name__, expected=expected):
                self.assertEqual(self.host._event_source(data), expected)

    async def test_handoff_logs_are_metadata_only_for_output_and_intermediate(self):
        for intermediate in (False, True):
            with self.subTest(intermediate=intermediate):
                host = HandoffResponsesHost(
                    lambda: logging_workflow(intermediate=intermediate),
                    checkpoint_root=self.root,
                    configure_observability=None,
                )
                with self.assertLogs("workflows.responses", level="INFO") as captured:
                    result = await self.turn(
                        "private-input-body",
                        host=host,
                        conversation=f"private-conversation-{intermediate}",
                        user="private-user",
                        response=f"private-response-{intermediate}",
                    )
                self.assertEqual(
                    result, "private-coding-firstprivate-coding-secondprivate-primary-summary"
                )
                messages = [record.getMessage() for record in captured.records]
                self.assertEqual(messages[:4], [
                    "Handoff transition: source=qvn-primary-agent target=qvn-coding-agent",
                    "Handoff output: source=qvn-coding-agent",
                    "Handoff transition: source=qvn-coding-agent target=qvn-primary-agent",
                    "Handoff output: source=qvn-primary-agent",
                ])
                self.assertEqual(len(messages), 5)
                waiting = captured.records[-1]
                self.assertEqual(
                    waiting.msg, "Handoff waiting for user input: source=%s request_id=%s"
                )
                checkpoint = await host._latest_checkpoint(
                    host._response_dir("private-user", f"private-response-{intermediate}")
                )
                self.assertIsNotNone(checkpoint)
                self.assertEqual(
                    waiting.args,
                    ("qvn-primary-agent", next(iter(checkpoint.pending_request_info_events))),
                )
                self.assertTrue(all(record.levelname == "INFO" for record in captured.records))
                for forbidden in (
                    "private-input-body", "private-coding-first", "private-coding-second",
                    "private-primary-summary", "private-unrecognized-output", "private-token",
                    "private-provider-response", "private-user", "private-conversation",
                    "private-response", str(self.root), ".handoff-checkpoints",
                ):
                    self.assertNotIn(forbidden, "\n".join(messages))

    async def test_streaming_output_is_logged_once_per_source_per_turn(self):
        host = HandoffResponsesHost(
            logging_workflow, checkpoint_root=self.root, configure_observability=None
        )
        with self.assertLogs("workflows.responses", level="INFO") as captured:
            for conversation in ("first-turn", "second-turn"):
                await self.turn("private-input-body", host=host, conversation=conversation)
        messages = [record.getMessage() for record in captured.records]
        self.assertEqual(messages.count("Handoff output: source=qvn-coding-agent"), 2)
        self.assertEqual(messages.count("Handoff output: source=qvn-primary-agent"), 2)

    async def test_unattributed_output_and_waiting_log_unknown(self):
        with self.assertLogs("workflows.responses", level="INFO") as captured:
            self.assertEqual(await self.turn("private-input-body"), "private-input-body")
        self.assertEqual(captured.records[0].getMessage(), "Handoff output: source=unknown")
        self.assertEqual(captured.records[-1].args[0], "unknown")
        self.assertNotIn("private-input-body", "\n".join(captured.output))

    async def test_logging_keeps_responses_event_order_and_text(self):
        host = HandoffResponsesHost(
            logging_workflow, checkpoint_root=self.root, configure_observability=None
        )
        payloads = [
            event_payload(event)
            async for event in host._respond(
                CreateResponse(input="private-input-body", stream=True),
                context("private-input-body"),
                asyncio.Event(),
            )
        ]
        self.assertEqual([payload["type"] for payload in payloads], [
            "response.created",
            "response.in_progress",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.delta",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ])
        self.assertEqual(
            payloads[-1]["response"]["output"][0]["content"][0]["text"],
            "private-coding-firstprivate-coding-secondprivate-primary-summary",
        )

    async def test_plain_text_resumes_after_host_recreation(self):
        self.assertEqual(await self.turn("review architecture next"), "review architecture next")
        restarted = HandoffResponsesHost(
            workflow, checkpoint_root=self.root, configure_observability=None
        )
        self.assertEqual(
            await self.turn("no, go ahead", host=restarted),
            "review architecture next -> no, go ahead",
        )
        self.assertEqual(
            await self.turn("more detail", host=restarted),
            "review architecture next -> no, go ahead -> more detail",
        )

    async def test_conversations_and_users_are_isolated(self):
        await self.turn("private-a")
        self.assertEqual(await self.turn("new-b", conversation="conversation-b"), "new-b")
        self.assertEqual(await self.turn("other-user", user="user-b"), "other-user")
        self.assertEqual(await self.turn("continue"), "private-a -> continue")

    async def test_previous_response_chain(self):
        await self.turn("first", conversation=None, response="resp_one")
        self.assertEqual(
            await self.turn("second", previous="resp_one", conversation=None, response="resp_two"),
            "first -> second",
        )
        self.assertEqual(
            await self.turn("third", previous="resp_two", conversation=None, response="resp_three"),
            "first -> second -> third",
        )

    async def test_missing_previous_response_fails(self):
        with self.assertRaisesRegex(HandoffRequestError, "previous_response_id"):
            await self.turn("continue", previous="resp_missing", conversation=None)

    async def test_empty_input_fails(self):
        with self.assertRaisesRegex(HandoffRequestError, "非空白"):
            await self.turn(" ")

    async def test_model_and_mapping_inputs_preserve_user_text(self):
        cases = [
            ([ItemMessage(role="user", content="完整文字\n```python\nvalue = 42\n```")],
             ["完整文字\n```python\nvalue = 42\n```"]),
            ([{"type": "message", "role": "user", "content": "原樣保留"}],
             ["原樣保留"]),
            ([{"role": "user", "content": "type 可省略"}], ["type 可省略"]),
            ([ItemMessage(role="user", content=[
                MessageContentInputTextContent(type="input_text", text="第一段"),
                MessageContentInputTextContent(type="input_text", text="第二段"),
            ])], ["第一段\n第二段"]),
            ([{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "程式碼"},
                {"type": "input_text", "text": "```python\nvalue = 42\n```"},
            ]}], ["程式碼\n```python\nvalue = 42\n```"]),
            ([{"role": "user", "content": " \t"}, {"role": "user", "content": "有效材料"}],
             ["有效材料"]),
        ]
        for items, expected in cases:
            with self.subTest(items=items):
                async def get_input_items():
                    return items

                result = await self.host._input_messages(SimpleNamespace(
                    get_input_items=get_input_items,
                ))
                self.assertEqual([message.text for message in result], expected)
                self.assertTrue(all(message.role == "user" for message in result))

    async def test_mapping_inputs_reject_non_user_or_non_text_items(self):
        cases = [
            (["not-an-item"], "文字訊息"),
            ([{"role": "assistant", "content": "不得接受 assistant"}], "文字訊息"),
            ([{"role": "system", "content": "不得接受 system"}], "文字訊息"),
            ([{"type": "function_call_output", "role": "user", "output": "不得配對"}],
             "文字訊息"),
            ([{"role": "user", "content": None}], "不支援附件"),
            ([{"role": "user", "content": {"text": "不是 content list"}}], "不支援附件"),
            ([{"role": "user", "content": [
                {"type": "input_text", "text": "不能只取這段"},
                {"type": "input_image", "image_url": "https://example.invalid/image"},
            ]}], "不支援附件"),
            ([{"role": "user", "content": [{"text": "缺少 part type"}]}], "不支援附件"),
            ([{"role": "user", "content": [{"type": "input_text", "text": 42}]}],
             "文字訊息"),
            ([{"role": "user", "content": []}], "非空白"),
        ]
        for items, reason in cases:
            with self.subTest(items=items):
                async def get_input_items():
                    return items

                with self.assertRaisesRegex(HandoffRequestError, reason):
                    await self.host._input_messages(SimpleNamespace(
                        get_input_items=get_input_items,
                    ))

    async def test_previous_response_after_conversation_turn(self):
        # 前一輪用 conversation 送出，下一輪改用 previous_response_id 也要接得上。
        await self.turn("first", conversation="conversation-x", response="resp_conv_1")
        self.assertEqual(
            await self.turn("second", previous="resp_conv_1", conversation=None),
            "first -> second",
        )

    async def test_each_turn_keeps_only_latest_checkpoint(self):
        for text in ("first", "second", "third"):
            await self.turn(text)
        response_dirs = list(self.root.glob("*/responses/*"))
        self.assertEqual(len(response_dirs), 3)
        for directory in response_dirs:
            self.assertEqual(len(list(directory.glob("*.json"))), 1, directory)

    async def test_forks_from_same_response_leave_it_untouched(self):
        await self.turn("root", conversation=None, response="resp_root")
        root_dir = next(self.root.glob("*/responses/*"))
        before = {path.name: path.read_bytes() for path in root_dir.glob("*.json")}
        self.assertEqual(
            await self.turn("left", previous="resp_root", conversation=None), "root -> left"
        )
        self.assertEqual(
            await self.turn("right", previous="resp_root", conversation=None), "root -> right"
        )
        after = {path.name: path.read_bytes() for path in root_dir.glob("*.json")}
        self.assertEqual(before, after)

    async def test_other_conversations_are_not_blocked(self):
        key = self.host._lock_key("user-a", "conversation-a")
        lock = self.host._lock(key)
        async with lock:
            # conversation-a 被佔住時，conversation-b 仍應立即完成。
            result = await asyncio.wait_for(
                self.turn("independent", conversation="conversation-b"), timeout=10
            )
        self.assertEqual(result, "independent")

    async def test_invalid_input_reports_reason_not_server_error(self):
        events = [
            event_payload(event)
            async for event in self.host._respond(
                CreateResponse(input=" ", stream=True), context(" "), asyncio.Event()
            )
        ]
        self.assertEqual(events[-1]["type"], "response.failed")
        error = events[-1]["response"]["error"]
        self.assertEqual(error["code"], "invalid_prompt")
        self.assertIn("非空白", error["message"])

    async def test_server_errors_stay_generic(self):
        def broken_factory():
            raise RuntimeError("secret internal detail")

        host = HandoffResponsesHost(
            broken_factory, checkpoint_root=self.root, configure_observability=None
        )
        events = [
            event_payload(event)
            async for event in host._respond(
                CreateResponse(input="hello", stream=True), context("hello"), asyncio.Event()
            )
        ]
        error = events[-1]["response"]["error"]
        self.assertEqual(error["code"], "server_error")
        self.assertNotIn("secret", error["message"])

    async def test_provider_raw_objects_are_not_retained(self):
        import pickle

        raw = lambda: None
        middleware = build_checkpoint_safe_middleware()
        for streaming in (True, False):
            content = Content.from_text("keep text")
            content.raw_representation = raw
            if streaming:
                async def updates():
                    yield AgentResponseUpdate(
                        contents=[content], role="assistant", raw_representation=raw
                    )
                result = ResponseStream(updates(), finalizer=AgentResponse.from_updates)
            else:
                result = AgentResponse(
                    messages=[Message("assistant", [content], raw_representation=raw)],
                    raw_representation=raw,
                )
            call_context = SimpleNamespace(stream=streaming, result=result)

            async def next_call():
                pass

            await middleware(call_context, next_call)
            if streaming:
                response = AgentResponse.from_updates([item async for item in call_context.result])
            else:
                response = call_context.result
            self.assertEqual(response.text, "keep text")
            self.assertIn(response.raw_representation, (None, [None]))
            self.assertIsNone(response.messages[0].raw_representation)
            self.assertIsNone(response.messages[0].contents[0].raw_representation)
            pickle.dumps(response)

    async def test_corrupt_checkpoint_does_not_restart_conversation(self):
        await self.turn("first")
        for path in self.root.rglob("*.json"):
            path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(Exception):
            await self.turn("continue")

    async def test_concurrent_followups_keep_both_turns(self):
        await self.turn("first")
        results = await asyncio.gather(self.turn("second"), self.turn("third"))
        self.assertEqual(results, ["first -> second", "first -> second -> third"])

    async def test_stream_and_nonstream_complete_without_function_call(self):
        for stream in (True, False):
            events = [
                event async for event in self.host._respond(
                    CreateResponse(input="hello", stream=stream),
                    context("hello", conversation=f"stream-{stream}"),
                    asyncio.Event(),
                )
            ]
            payloads = [event_payload(event) for event in events]
            self.assertEqual(payloads[-1]["type"], "response.completed")
            output = payloads[-1]["response"]["output"]
            self.assertEqual(output[0]["type"], "message")
            self.assertEqual(output[0]["content"][0]["text"], "hello")

    async def test_http_nonstream_and_stream_followup(self):
        async with AsyncClient(
            transport=ASGITransport(app=self.host), base_url="http://localhost"
        ) as client:
            first = await client.post(
                "/responses", json={"input": "code reviewed", "stream": False}
            )
            self.assertEqual(first.status_code, 200, first.text)
            response = first.json()
            self.assertEqual(response["status"], "completed")
            second = await client.post(
                "/responses",
                json={
                    "input": "no, go ahead",
                    "stream": True,
                    "previous_response_id": response["id"],
                },
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertIn("code reviewed -> no, go ahead", second.text)
            self.assertIn("response.completed", second.text)
            self.assertNotIn("response.failed", second.text)

    async def test_http_users_and_conversations_are_isolated(self):
        class EchoHandoffClient(ScriptedHandoffClient):
            def _inner_get_response(self, *, messages, stream, options, **kwargs):
                material = next(message.text for message in messages if message.role == "user")
                chunks, target = self.script[self.calls]
                self.script[self.calls] = (
                    tuple(chunk.replace("{material}", material) for chunk in chunks), target,
                )
                return super()._inner_get_response(
                    messages=messages, stream=stream, options=options, **kwargs
                )

        host, roles, sources = self.production_host({
            "primary": [(("START\n",), "coding"), (("SUMMARY:{material}\n",), None)],
            "coding": [(("REVIEW:{material}\n",), None)],
        }, suffix="http-isolation", client_factory=EchoHandoffClient)
        checkpoint_root = self.root / "http-isolation"
        primary = roles["primary"].agent_name
        async with AsyncClient(
            transport=ASGITransport(app=host), base_url="http://localhost"
        ) as client:
            for user, conversation, text, material in (
                ("user-a", "conv_same", "private-a", "private-a"),
                ("user-b", "conv_same", "private-b attempt", None),
                ("user-a", "conv_separate", "separate-a", "separate-a"),
                ("user-b", "conv_own_b", "private-b", "private-b"),
                ("user-a", "conv_same", "continue-a", "private-a"),
                ("user-b", "conv_own_b", "continue-b", "private-b"),
                ("user-a", "conv_separate", "continue", "separate-a"),
                ("user-b", "conv_same", "continue-b attempt", None),
                ("user-a", "conv_same", "再繼續", "private-a"),
            ):
                with self.subTest(user=user, conversation=conversation, text=text):
                    before = {
                        path.relative_to(checkpoint_root): path.read_bytes()
                        for path in checkpoint_root.rglob("*") if path.is_file()
                    }
                    previous_calls = [
                        (model, model.calls)
                        for source in sources for model in source.clients.values()
                    ]
                    pointer = host._conversation_pointer("user-a", "conv_same")
                    previous = await host._latest_checkpoint(host._response_dir(
                        "user-a", pointer.read_text(encoding="utf-8")
                    )) if pointer.is_file() else None
                    response = await client.post(
                        "/responses",
                        headers={"x-agent-user-id": user},
                        json={"input": text, "conversation": conversation, "stream": False},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = response.json()
                    if material is None:
                        self.assertEqual(payload["status"], "failed")
                        self.assertEqual(payload["error"]["code"], "invalid_prompt")
                        self.assertIn("已有歷史紀錄", payload["error"]["message"])
                        self.assertEqual("".join(
                            part["text"] for item in payload["output"]
                            if item["type"] == "message"
                            for part in item["content"] if part["type"] == "output_text"
                        ), "")
                        self.assertNotIn("private-a", response.text)
                        self.assertTrue(all(
                            model.calls == 0 for model in sources[-1].clients.values()
                        ))
                        self.assertTrue(all(
                            model.calls == calls for model, calls in previous_calls
                        ))
                        self.assertEqual({
                            path.relative_to(checkpoint_root): path.read_bytes()
                            for path in checkpoint_root.rglob("*") if path.is_file()
                        }, before)
                        pending = await host._latest_checkpoint(host._response_dir(
                            "user-a", pointer.read_text(encoding="utf-8")
                        ))
                        self.assertIsNotNone(previous)
                        self.assertIsNotNone(pending)
                        self.assertEqual(
                            set(pending.pending_request_info_events),
                            set(previous.pending_request_info_events),
                        )
                        for request_id, event in pending.pending_request_info_events.items():
                            self.assertIsInstance(event.data, HandoffAgentUserRequest)
                            self.assertEqual(
                                event.data.agent_response.to_dict(),
                                previous.pending_request_info_events[request_id]
                                .data.agent_response.to_dict(),
                            )
                        self.assertFalse(host._conversation_pointer(user, conversation).exists())
                        continue
                    self.assertEqual(payload["status"], "completed", payload)
                    self.assertIsNone(payload.get("error"))
                    self.assertEqual(
                        payload["output"][0]["content"][0]["text"],
                        f"START\nREVIEW:{material}\nSUMMARY:{material}\n",
                    )
                    model = sources[-1].clients[primary]
                    self.assertEqual(model.calls, 2)
                    users = [
                        message.text for message in map(Message.from_dict, model.requests[0])
                        if message.role == "user"
                    ]
                    self.assertEqual(users.count(material), 1)
                    self.assertEqual(users[-1], text)
                    foreign = {"private-a", "private-b", "separate-a"} - {material}
                    forbidden = foreign | {"private-b attempt", "continue-b attempt"}
                    self.assertTrue(
                        forbidden.isdisjoint(users)
                    )
                    for agent in sources[-1].clients.values():
                        for messages in agent.requests:
                            history_text = "\n".join(
                                Message.from_dict(message).text for message in messages
                            )
                            for private_text in forbidden:
                                self.assertNotIn(private_text, history_text)

    async def test_http_input_errors_keep_invalid_prompt_contract(self):
        for stream in (False, True):
            for input_value, reason in (
                (" ", "非空白"),
                ([{"role": "assistant", "content": "private-assistant-text"}], "文字訊息"),
                ([{"role": "user", "content": [
                    {"type": "input_image", "image_url": "https://example.invalid/image"},
                ]}], "不支援附件"),
            ):
                with self.subTest(stream=stream, reason=reason):
                    async with AsyncClient(
                        transport=ASGITransport(app=self.host), base_url="http://localhost"
                    ) as client:
                        response = await client.post(
                            "/responses", json={"input": input_value, "stream": stream}
                        )
                    self.assertEqual(response.status_code, 200, response.text)
                    if stream:
                        events = [
                            json.loads(line.removeprefix("data: "))
                            for line in response.text.splitlines() if line.startswith("data: {")
                        ]
                        self.assertEqual(events[0]["type"], "response.created")
                        self.assertEqual(events[-1]["type"], "response.failed")
                        self.assertFalse(any(
                            event["type"] == "response.completed" for event in events
                        ))
                        payload = events[-1]["response"]
                    else:
                        payload = response.json()
                    self.assertEqual(payload["status"], "failed")
                    self.assertEqual(payload["error"]["code"], "invalid_prompt")
                    self.assertIn(reason, payload["error"]["message"])
                    self.assertNotIn("private-assistant-text", payload["error"]["message"])

    async def test_http_preserves_real_handoff_specialist_text(self):
        roles = build_agent_roles("contract-")
        primary_name = roles["primary"].agent_name
        coding_name = roles["coding"].agent_name
        preface = "<<<PRIMARY_PREFACE>>>\n"
        review = SpecialistReview(
            category="code", summary="Offline fixture reviewed.", findings=["Synthetic finding."],
            severity="LOW", evidence=["Synthetic evidence."], recommendations=["Synthetic advice."],
            limitations=["Offline fixture only."],
        )
        summary = _render_primary_summary([(coding_name, review)], "")
        coding_text = (
            "<<<CODING_BEGIN_OUTPUT_CONTRACT>>>\n"
            "Synthetic specialist review.\n"
            + review.model_dump_json() + "\n"
            "```python\n"
            'value = {"fixture": True}\n'
            "```\n"
            "<<<CODING_END_OUTPUT_CONTRACT>>>\n"
        )
        coding_chunks = tuple(
            coding_text[offset:offset + 17] for offset in range(0, len(coding_text), 17)
        )
        expected_text = preface + coding_text + summary
        clients: list[tuple[ScriptedHandoffClient, ScriptedHandoffClient]] = []

        def factory():
            source = ScriptedRoleClients({
                primary_name: [((preface,), coding_name), ((summary,), None)],
                coding_name: [(coding_chunks, None)],
            })
            participants = build_participants(source, roles, checkpoint_safe=True)
            clients.append((source.clients[primary_name], source.clients[coding_name]))
            return build_workflow(participants, name="offline-output-contract")

        for stream in (True, False):
            with self.subTest(stream=stream):
                host = HandoffResponsesHost(
                    factory,
                    checkpoint_root=self.root / f"output-contract-{stream}",
                    configure_observability=None,
                )
                async with AsyncClient(
                    transport=ASGITransport(app=host), base_url="http://localhost"
                ) as client:
                    with self.assertLogs("workflows.responses", level="INFO") as captured:
                        response = await client.post(
                            "/responses",
                            json={
                                "input": "Synthetic output-contract fixture input.",
                                "conversation": f"conv_offline_contract_{stream}",
                                "stream": stream,
                            },
                        )
                self.assertEqual(response.status_code, 200, response.text)
                transitions = [
                    record.args for record in captured.records
                    if record.msg == "Handoff transition: source=%s target=%s"
                ]
                self.assertEqual(transitions, [
                    (primary_name, coding_name), (coding_name, primary_name)
                ])
                waiting = [
                    record.args[0] for record in captured.records
                    if record.msg == "Handoff waiting for user input: source=%s request_id=%s"
                ]
                self.assertEqual(waiting, [primary_name])
                primary_client, coding_client = clients[-1]
                self.assertEqual((primary_client.calls, coding_client.calls), (2, 1))

                if stream:
                    events = [
                        json.loads(line.removeprefix("data: "))
                        for line in response.text.splitlines() if line.startswith("data: {")
                    ]
                    self.assertFalse(any(
                        event["type"] == "response.failed" for event in events
                    ))
                    self.assertEqual(events[-1]["type"], "response.completed")
                    deltas = "".join(
                        event["delta"] for event in events
                        if event["type"] == "response.output_text.delta"
                    )
                    done = "".join(
                        event["text"] for event in events
                        if event["type"] == "response.output_text.done"
                    )
                    self.assertEqual(deltas, expected_text)
                    self.assertEqual(done, deltas)
                    payload = events[-1]["response"]
                else:
                    payload = response.json()

                self.assertEqual(payload["status"], "completed")
                self.assertIsNone(payload.get("error"))
                actual_text = "".join(
                    part["text"]
                    for item in payload["output"] if item["type"] == "message"
                    for part in item["content"] if part["type"] == "output_text"
                )
                self.assertEqual(actual_text, expected_text)
                self.assertEqual(actual_text.count(coding_text), 1)
                self.assertEqual(actual_text.count("<<<CODING_BEGIN_OUTPUT_CONTRACT>>>"), 1)
                self.assertEqual(actual_text.count("<<<CODING_END_OUTPUT_CONTRACT>>>"), 1)

        self.assertEqual(len(clients), 2)
        self.assertIsNot(clients[0][0], clients[1][0])
        self.assertIsNot(clients[0][1], clients[1][1])

    async def test_http_real_handoff_three_plain_text_turns_and_forks(self):
        roles = build_agent_roles("continuity-")
        primary, coding = roles["primary"].agent_name, roles["coding"].agent_name
        material = "固定材料：fixture_value = 42"
        prefaces = [f"<<<PRIMARY_{index}>>>\n" for index in range(3)]
        review_values = [
            SpecialistReview(
                category="code", summary=f"第 {index} 輪固定材料健檢",
                findings=["材料使用固定值。"], severity="LOW",
                evidence=["fixture_value = 42（來源：使用者訊息）"],
                recommendations=["以測試確認用途。"], limitations=["未執行程式碼。"],
            )
            for index in range(3)
        ]
        summaries = [_render_primary_summary([(coding, review)], "") for review in review_values]
        reviews = [
            f"<<<CODING_BEGIN_{index}>>>\n"
            + review_values[index].model_dump_json()
            + f"\n```python\nfixture_value = 42\n```\n<<<CODING_END_{index}>>>\n"
            for index in range(3)
        ]
        for stream in (False, True):
            for mode in ("conversation", "chain", "fork"):
                for recreate in (False, True):
                    with self.subTest(stream=stream, mode=mode, recreate=recreate):
                        sources = []
                        checkpoint_root = self.root / f"{stream}-{mode}-{recreate}"

                        def factory():
                            index = len(sources)
                            source = ScriptedRoleClients({
                                primary: [
                                    ((prefaces[index],), coding),
                                    ((summaries[index],), None),
                                ],
                                coding: [(
                                    tuple(
                                        reviews[index][offset:offset + 17]
                                        for offset in range(0, len(reviews[index]), 17)
                                    ), None,
                                )],
                            })
                            sources.append(source)
                            return build_workflow(
                                build_participants(source, roles, checkpoint_safe=True),
                                name="plain-text-continuity",
                            )

                        host = HandoffResponsesHost(
                            factory, checkpoint_root=checkpoint_root,
                            configure_observability=None,
                        )
                        first_id = previous_id = None
                        original_files = None
                        for index, text in enumerate((material, "繼續", "再繼續")):
                            if recreate and index:
                                host = HandoffResponsesHost(
                                    factory, checkpoint_root=checkpoint_root,
                                    configure_observability=None,
                                )
                            request = {"input": text, "stream": stream}
                            if mode == "conversation":
                                request["conversation"] = f"conv_{stream}_{recreate}"
                            elif index:
                                request["previous_response_id"] = (
                                    first_id if mode == "fork" else previous_id
                                )
                            async with AsyncClient(
                                transport=ASGITransport(app=host), base_url="http://localhost"
                            ) as client:
                                response = await client.post("/responses", json=request)
                            self.assertEqual(response.status_code, 200, response.text)
                            if stream:
                                events = [
                                    json.loads(line.removeprefix("data: "))
                                    for line in response.text.splitlines()
                                    if line.startswith("data: {")
                                ]
                                self.assertEqual(events[0]["type"], "response.created")
                                self.assertEqual(events[-1]["type"], "response.completed")
                                self.assertFalse(any(
                                    event["type"] == "response.failed" for event in events
                                ))
                                payload = events[-1]["response"]
                            else:
                                payload = response.json()
                            expected = prefaces[index] + reviews[index] + summaries[index]
                            actual = "".join(
                                part["text"]
                                for item in payload["output"]
                                for part in item["content"]
                                if part["type"] == "output_text"
                            )
                            self.assertEqual(payload["status"], "completed")
                            self.assertIsNone(payload.get("error"))
                            self.assertTrue(all(
                                item["type"] == "message" for item in payload["output"]
                            ))
                            self.assertEqual(actual, expected)
                            self.assertEqual(actual.count(reviews[index]), 1)
                            if stream:
                                for event_type, key in (
                                    ("response.output_text.delta", "delta"),
                                    ("response.output_text.done", "text"),
                                ):
                                    self.assertEqual("".join(
                                        event[key] for event in events
                                        if event["type"] == event_type
                                    ), expected)
                            primary_client = sources[index].clients[primary]
                            self.assertEqual(primary_client.calls, 2)
                            self.assertEqual(sources[index].clients[coding].calls, 1)
                            history = [
                                Message.from_dict(item) for item in primary_client.requests[0]
                            ]
                            users = [message.text for message in history if message.role == "user"]
                            self.assertEqual(users.count(material), 1)
                            self.assertEqual(users[-1], text)
                            if index:
                                prior = 0 if mode == "fork" else index - 1
                                self.assertIn(summaries[prior], "\n".join(
                                    message.text for message in history
                                ))
                                if mode == "fork" and index == 2:
                                    self.assertNotIn("繼續", users)
                                    self.assertNotIn(summaries[1], "\n".join(
                                        message.text for message in history
                                    ))
                            directory = host._response_dir(None, payload["id"])
                            checkpoint = await host._latest_checkpoint(directory)
                            self.assertIsNotNone(checkpoint)
                            pending = checkpoint.pending_request_info_events
                            self.assertEqual(len(pending), 1)
                            waiting = next(iter(pending.values())).data
                            self.assertIsInstance(waiting, HandoffAgentUserRequest)
                            self.assertEqual(waiting.agent_response.text, summaries[index])
                            if index == 0:
                                first_id = payload["id"]
                                original_files = {
                                    path.name: path.read_bytes() for path in directory.glob("*.json")
                                }
                            previous_id = payload["id"]
                        self.assertEqual(len(sources), 3)
                        for name in (primary, coding):
                            self.assertEqual(len({
                                id(source.clients[name]) for source in sources
                            }), 3)
                        first_dir = host._response_dir(None, first_id)
                        self.assertEqual({
                            path.name: path.read_bytes() for path in first_dir.glob("*.json")
                        }, original_files)

    async def test_production_return_policy_for_all_specialists(self):
        roles = build_agent_roles("routing-")
        primary_name = roles["primary"].agent_name
        for key in ("coding", "architect", "spec"):
            for already_returned in (False, True):
                with self.subTest(specialist=key, already_returned=already_returned):
                    specialist_name = roles[key].agent_name
                    source = ScriptedRoleClients({
                        primary_name: [(("PREFACE\n",), specialist_name), (("SUMMARY\n",), None)],
                        specialist_name: [
                            (("REVIEW-BEGIN\n", "REVIEW-END\n"), primary_name if already_returned else None)
                        ],
                    })
                    participants = build_participants(source, roles, checkpoint_safe=True)
                    host = HandoffResponsesHost(
                        lambda: build_workflow(participants, name="production-routing-test"),
                        checkpoint_root=self.root / f"{key}-{already_returned}",
                        configure_observability=None,
                    )
                    with self.assertLogs("workflows.responses", level="INFO") as captured:
                        self.assertEqual(
                            await self.turn("Synthetic review input.", host=host),
                            "PREFACE\nREVIEW-BEGIN\nREVIEW-END\nSUMMARY\n",
                        )
                    transitions = [
                        record.args for record in captured.records
                        if record.msg == "Handoff transition: source=%s target=%s"
                    ]
                    self.assertEqual(transitions, [
                        (primary_name, specialist_name), (specialist_name, primary_name)
                    ])
                    self.assertEqual(captured.records[-1].args[0], primary_name)
                    self.assertEqual(source.clients[specialist_name].calls, 1)
                    self.assertEqual(source.clients[primary_name].calls, 2)
                    self.assertEqual(source.clients[specialist_name].options_seen[0]["tool_choice"], "none")

    async def test_primary_summary_preserves_reviews_without_echo_or_extra_model_call(self):
        review = SpecialistReview(
            category="code", summary="Verified summary.",
            findings=["Exact finding."], severity="LOW", evidence=["Exact evidence."],
            recommendations=["Exact advice."], limitations=["Exact limitation."],
        )
        text = review.model_dump_json()
        for streaming in (False, True):
            for primary_reply in (text, "Invented HIGH finding.", "### 待釐清事項\n請提供缺少的架構材料？"):
                with self.subTest(stream=streaming, reply=primary_reply):
                    host, roles, sources = self.production_host({
                        "primary": [(("PREFACE\n",), "coding"), ((primary_reply,), None)],
                        "coding": [((text,), None)],
                    }, suffix=f"faithful-{streaming}-{len(primary_reply)}")
                    async with AsyncClient(transport=ASGITransport(app=host), base_url="http://localhost") as client:
                        response = await client.post("/responses", json={"input": "Synthetic material.", "stream": streaming})
                    if streaming:
                        events = [
                            json.loads(line[6:]) for line in response.text.splitlines()
                            if line.startswith("data: {")
                        ]
                        payload = events[-1]["response"]
                        self.assertEqual(events[-1]["type"], "response.completed")
                    else:
                        payload = response.json()
                    actual = "".join(
                        part["text"] for item in payload["output"] for part in item["content"]
                        if part["type"] == "output_text"
                    )
                    self.assertEqual(actual.count(text), 1)
                    summary = actual[len("PREFACE\n" + text):]
                    self.assertIn(f"1. `{roles['coding'].agent_name}`", summary)
                    self.assertIn("**嚴重度：LOW**", summary)
                    for value in ("Exact finding.", "Exact evidence.", "Exact advice.", "Exact limitation."):
                        self.assertIn(value, summary)
                    self.assertNotIn("Invented", summary)
                    self.assertNotIn("HIGH", summary)
                    if primary_reply.startswith("###"):
                        self.assertIn(primary_reply, summary)
                    source = sources[0]
                    self.assertEqual(source.clients[roles["primary"].agent_name].calls, 2)
                    self.assertEqual(source.clients[roles["coding"].agent_name].calls, 1)
                    self.assertNotIn("response_format", source.clients[roles["primary"].agent_name].options_seen[1])
                    checkpoint = await host._latest_checkpoint(host._response_dir(None, payload["id"]))
                    waiting = next(iter(checkpoint.pending_request_info_events.values())).data.agent_response
                    self.assertEqual(waiting.text, summary)
                    if streaming:
                        self.assertEqual("".join(
                            event["delta"] for event in events if event["type"] == "response.output_text.delta"
                        ), actual)

    async def test_primary_summary_does_not_repeat_prior_review_on_plain_text_followup(self):
        review = SpecialistReview(
            category="code", summary="Verified summary.", findings=["Exact finding."],
            severity="LOW", evidence=["Exact evidence."], recommendations=["Exact advice."],
            limitations=["Exact limitation."],
        )
        host, roles, _ = self.production_host({
            "primary": [(("START\n",), "coding"), (("Untrusted summary",), None)],
            "coding": [((review.model_dump_json(),), None)],
        }, suffix="faithful-followup")
        await self.turn("Synthetic first turn.", host=host)
        restarted, _, sources = self.production_host({
            "primary": [(("LOW: Exact advice.",), None)],
        }, suffix="faithful-followup")
        self.assertEqual(await self.turn("Only repeat the original advice.", host=restarted), "LOW: Exact advice.")
        self.assertEqual(sources[0].clients[roles["primary"].agent_name].calls, 1)
        self.assertEqual(sources[0].clients[roles["coding"].agent_name].calls, 0)

    async def test_primary_summary_preserves_composite_routing_and_source_order(self):
        reviews = [
            SpecialistReview(category=category, summary="Verified summary.", findings=[finding],
                severity=severity, evidence=["Exact evidence."], recommendations=["Exact advice."],
                limitations=["Exact limitation."])
            for category, finding, severity in (
                ("specification", "Spec finding.", "LOW"), ("architecture", "Architect finding.", "HIGH")
            )
        ]
        host, roles, sources = self.production_host({
            "primary": [
                (("START\n",), "spec"),
                ((reviews[0].model_dump_json() + "\nNEXT\n",), "architect"),
                (("Wrong summary",), None),
            ],
            "spec": [((reviews[0].model_dump_json(),), None)],
            "architect": [((reviews[1].model_dump_json(),), None)],
        })
        with self.assertLogs("workflows.responses", level="INFO") as captured:
            actual = await self.turn("Synthetic complete composite materials.", host=host)
        summary = actual[actual.index("## handled_by"):]
        self.assertEqual(actual.count(reviews[0].model_dump_json()), 1)
        self.assertEqual(actual.count(reviews[1].model_dump_json()), 1)
        self.assertLess(summary.index(roles["spec"].agent_name), summary.index(roles["architect"].agent_name))
        self.assertIn("Spec finding.", summary)
        self.assertIn("Architect finding.", summary)
        self.assertIn("**嚴重度：LOW**", summary)
        self.assertIn("**嚴重度：HIGH**", summary)
        self.assertEqual([
            record.args for record in captured.records if record.msg == "Handoff transition: source=%s target=%s"
        ], [(roles["primary"].agent_name, roles["spec"].agent_name),
            (roles["spec"].agent_name, roles["primary"].agent_name),
            (roles["primary"].agent_name, roles["architect"].agent_name),
            (roles["architect"].agent_name, roles["primary"].agent_name)])
        self.assertEqual(sources[0].clients[roles["primary"].agent_name].calls, 3)

    def test_primary_review_scope_excludes_old_user_and_primary_payloads(self):
        review = SpecialistReview(
            category="code", summary="Verified summary.", findings=["Exact finding."],
            severity="LOW", evidence=["Exact evidence."], recommendations=["Exact advice."],
            limitations=["Exact limitation."],
        )
        text = review.model_dump_json()
        names = {"qvn-coding-agent": "code"}
        messages = [
            Message("assistant", [text], author_name="qvn-coding-agent"),
            Message("user", [text], author_name="qvn-coding-agent"),
            Message("assistant", [text], author_name="qvn-primary-agent"),
        ]
        self.assertEqual(_current_reviews(messages, names), [])
        messages.append(Message("assistant", [text], author_name="qvn-coding-agent"))
        self.assertEqual(_current_reviews(messages, names), [("qvn-coding-agent", review)])
        with self.assertRaises(HandoffRoutingError):
            _current_reviews(messages, {"qvn-coding-agent": "architecture"})

    async def test_primary_buffered_stream_keeps_normal_second_handoff_updates(self):
        review = SpecialistReview(
            category="specification", summary="Verified summary.", findings=["Exact finding."],
            severity="LOW", evidence=["Exact evidence."], recommendations=["Exact advice."],
            limitations=["Exact limitation."],
        )

        class RawPrimary(ChatMiddlewareLayer, BaseChatClient):
            def _inner_get_response(self, *, messages, stream, options, **kwargs):
                async def updates():
                    yield ChatResponseUpdate(
                        contents=[Content.from_text("NEXT"), Content.from_function_call(
                            call_id="normal-next", name="handoff_to_qvn-architect-agent", arguments={},
                        )], role="assistant", author_name="qvn-primary-agent", message_id="next-message",
                    )
                    yield ChatResponseUpdate(role="assistant", finish_reason="tool_calls")
                return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

        client = RawPrimary(middleware=[build_primary_summary_middleware(
            "qvn-primary-agent", {"qvn-spec-agent": "specification", "qvn-architect-agent": "architecture"},
        )])
        stream = client.get_response([
            Message("user", ["Both materials."]),
            Message("assistant", [review.model_dump_json()], author_name="qvn-spec-agent"),
        ], stream=True, options={"store": False})
        updates = [update async for update in stream]
        self.assertTrue(updates)
        self.assertEqual("".join(update.text for update in updates), "NEXT")
        self.assertEqual([
            content.name for update in updates for content in update.contents if content.type == "function_call"
        ], ["handoff_to_qvn-architect-agent"])
        self.assertEqual((await stream.get_final_response()).finish_reason, "tool_calls")

    async def test_primary_partial_review_survives_clarification_round_trip(self):
        spec = SpecialistReview(
            category="specification", summary="Spec summary.", findings=["Spec finding."],
            severity="LOW", evidence=["Spec evidence."], recommendations=["Spec advice."],
            limitations=["Spec limitation."],
        )
        architecture = SpecialistReview(
            category="architecture", summary="Architecture summary.", findings=["Architecture finding."],
            severity="HIGH", evidence=["Architecture evidence."], recommendations=["Architecture advice."],
            limitations=["Architecture limitation."],
        )
        question = "### 待釐清事項\n請提供缺少的架構材料？"
        host, roles, _ = self.production_host({
            "primary": [(("START\n",), "spec"), ((question,), None)],
            "spec": [((spec.model_dump_json(),), None)],
        }, suffix="partial-clarification")
        first = await self.turn("Spec material; architecture requested but missing.", host=host)
        self.assertIn("Spec finding.", first)
        self.assertIn(question, first)
        restarted, _, sources = self.production_host({
            "primary": [(("NEXT\n",), "architect"), (("Finished.",), None)],
            "architect": [((architecture.model_dump_json(),), None)],
        }, suffix="partial-clarification")
        final = await self.turn("Architecture material.", host=restarted)
        summary = final[final.index("## handled_by"):]
        self.assertIn("Spec finding.", summary)
        self.assertIn("Architecture finding.", summary)
        self.assertLess(summary.index(roles["spec"].agent_name), summary.index(roles["architect"].agent_name))
        self.assertEqual(sources[0].clients[roles["spec"].agent_name].calls, 0)

    async def test_http_specialist_control_only_does_not_complete_review(self):
        for key in ("coding", "architect", "spec"):
            for stream in (True, False):
                with self.subTest(specialist=key, stream=stream):
                    requests = []

                    def provider(request):
                        body = json.loads(request.content)
                        requests.append(body)
                        self.assertEqual(body["tool_choice"], "none")
                        self.assertTrue(body["text"]["format"]["strict"])
                        self.assertEqual(
                            [tool["name"] for tool in body["tools"]],
                            [f"handoff_to_{roles['primary'].agent_name}"],
                        )
                        payload = provider_responses_events((), target=roles["primary"].agent_name)
                        events = [
                            json.loads(line.removeprefix("data: "))
                            for line in payload.splitlines() if line.startswith("data: ")
                        ]
                        events = [
                            event for event in events
                            if event["type"] in (
                                "response.created", "response.in_progress", "response.completed",
                            )
                            or event["type"].startswith("response.function_call")
                            or event["type"] in ("response.output_item.added", "response.output_item.done")
                            and event["item"]["type"] == "function_call"
                        ]
                        for event in events:
                            if "output_index" in event:
                                event["output_index"] = 0
                            if event["type"] == "response.completed":
                                event["response"]["output"] = [
                                    item for item in event["response"]["output"]
                                    if item["type"] == "function_call"
                                ]
                        return Response(
                            200, headers={"content-type": "text/event-stream"},
                            text="\n\n".join(
                                "event: " + event["type"] + "\ndata: " + json.dumps(event)
                                for event in events
                            ) + "\n\n",
                        )

                    async with AsyncClient(transport=MockTransport(provider)) as provider_http:
                        async with AsyncOpenAI(
                            api_key="offline-fixture-not-a-real-key",
                            base_url="http://offline.invalid/v1",
                            http_client=provider_http, max_retries=0,
                        ) as openai_client:
                            def client_factory(name, script):
                                if name == roles[key].agent_name:
                                    return OpenAIChatClient(
                                        model="offline-responses", async_client=openai_client,
                                    )
                                return ScriptedHandoffClient(name, script)

                            host, roles, sources = self.production_host({
                                "primary": [(("PREFACE\n",), key)],
                            }, suffix=f"control-only-{key}-{stream}", client_factory=client_factory)
                            async with AsyncClient(
                                transport=ASGITransport(app=host), base_url="http://localhost"
                            ) as client:
                                with self.assertLogs("workflows.responses", level="INFO") as captured:
                                    response = await client.post(
                                        "/responses", json={"input": "Synthetic material.", "stream": stream}
                                    )
                    payload = (
                        json.loads([
                            line.removeprefix("data: ")
                            for line in response.text.splitlines() if line.startswith("data: {")
                        ][-1])["response"]
                        if stream else response.json()
                    )
                    self.assertEqual(payload["status"], "failed")
                    self.assertEqual(payload["error"]["code"], "server_error")
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(sources[0].clients[roles["primary"].agent_name].calls, 1)
                    self.assertEqual([
                        record.args for record in captured.records
                        if record.msg == "Handoff transition: source=%s target=%s"
                    ], [(roles["primary"].agent_name, roles[key].agent_name)])

    async def test_http_provider_responses_lifecycle(self):
        review = {
            "category": "code", "summary": "Synthetic parser review.",
            "findings": ["Synthetic finding."], "severity": "LOW",
            "evidence": ["Synthetic original code."], "recommendations": ["Synthetic advice."],
            "limitations": ["Offline fixture only."],
        }
        text = json.dumps(review, indent=2) + "\n"
        chunks = (text[:32], text[32:75], text[75:])
        cases = (
            ("result_only", False, "completed", None),
            ("already_returned", True, "completed", None),
            ("failed", False, "failed", None),
            ("incomplete", False, "incomplete", None),
            ("length", False, "incomplete", "max_output_tokens"),
            ("content_filter", False, "incomplete", "content_filter"),
            ("pending", False, "in_progress", None),
            ("missing_terminal", False, None, None),
        )
        for stream in (True, False):
            for case, already_returned, status, incomplete_reason in cases:
                with self.subTest(stream=stream, case=case):
                    requests = []
                    updates = []
                    final_responses = []
                    invoked_tools = []

                    def provider(request):
                        body = json.loads(request.content)
                        self.assertIs(body["stream"], True)
                        self.assertIs(body["store"], False)
                        self.assertEqual(body["tool_choice"], "none")
                        self.assertEqual(
                            [tool["name"] for tool in body["tools"]],
                            [f"handoff_to_{roles['primary'].agent_name}"],
                        )
                        requests.append(body)
                        self.assertEqual(len(requests), 1)
                        return Response(
                            200,
                            headers={"content-type": "text/event-stream"},
                            text=provider_responses_events(
                                chunks,
                                target=roles["primary"].agent_name if already_returned else None,
                                status=status, incomplete_reason=incomplete_reason,
                            ),
                        )

                    @chat_middleware
                    async def observe_parser(context, call_next):
                        await call_next()
                        context.result.with_transform_hook(updates.append)
                        context.result.with_result_hook(final_responses.append)

                    @function_middleware
                    async def observe_tool(context, call_next):
                        invoked_tools.append(context.function.name)
                        await call_next()

                    async with AsyncClient(transport=MockTransport(provider)) as provider_http:
                        async with AsyncOpenAI(
                            api_key="offline-fixture-not-a-real-key",
                            base_url="http://offline.invalid/v1",
                            http_client=provider_http, max_retries=0,
                        ) as openai_client:
                            def client_factory(name, script):
                                if name == roles["coding"].agent_name:
                                    return OpenAIChatClient(
                                        model="offline-responses", async_client=openai_client,
                                        middleware=[observe_parser, observe_tool],
                                    )
                                return ScriptedHandoffClient(name, script)

                            host, roles, sources = self.production_host({
                                "primary": [
                                    (("PREFACE\n",), "coding"), (("SUMMARY\n",), None),
                                ],
                            }, suffix=f"parser-{stream}-{case}", client_factory=client_factory)
                            async with AsyncClient(
                                transport=ASGITransport(app=host), base_url="http://localhost"
                            ) as client:
                                with self.assertLogs("workflows.responses", level="INFO") as captured:
                                    response = await client.post(
                                        "/responses", json={"input": "Synthetic material.", "stream": stream}
                                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    events: list[dict[str, Any]] = []
                    if stream:
                        events = [
                            json.loads(line.removeprefix("data: "))
                            for line in response.text.splitlines() if line.startswith("data: {")
                        ]
                        payload = events[-1]["response"]
                    else:
                        payload = response.json()
                    completed = status == "completed"
                    self.assertEqual(
                        payload["status"], "completed" if completed else "failed",
                        "\n".join(captured.output),
                    )
                    primary = roles["primary"].agent_name
                    coding = roles["coding"].agent_name
                    self.assertEqual([
                        record.args for record in captured.records
                        if record.msg == "Handoff transition: source=%s target=%s"
                    ], [(primary, coding), (coding, primary)] if completed else [(primary, coding)])
                    self.assertEqual([
                        record.args[0] for record in captured.records
                        if record.msg == "Handoff waiting for user input: source=%s request_id=%s"
                        and isinstance(record.args, tuple)
                    ], [primary] if completed else [])
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(sources[0].clients[primary].calls, 2 if completed else 1)
                    self.assertEqual(invoked_tools, [f"handoff_to_{primary}"] if completed else [])
                    if not completed:
                        self.assertEqual(payload["error"]["code"], "server_error")
                        continue
                    self.assertEqual(updates[0].contents, [])
                    self.assertEqual(updates[1].contents, [])
                    self.assertIsNotNone(updates[0].continuation_token)
                    self.assertIsNotNone(updates[1].continuation_token)
                    self.assertIsNone(payload.get("error"))
                    expected = (
                        "PREFACE\n" + text
                        + _render_primary_summary([(coding, SpecialistReview.model_validate(review))], "")
                    )
                    actual = "".join(
                        part["text"]
                        for item in payload["output"] if item["type"] == "message"
                        for part in item["content"] if part["type"] == "output_text"
                    )
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual.count(text), 1)
                    if stream:
                        self.assertEqual("".join(
                            event["delta"] for event in events
                            if event["type"] == "response.output_text.delta"
                        ), expected)
                        self.assertEqual("".join(
                            event["text"] for event in events
                            if event["type"] == "response.output_text.done"
                        ), expected)
                    self.assertEqual(len(final_responses), 1)
                    self.assertIsNone(final_responses[0].continuation_token)
                    self.assertEqual(
                        final_responses[0].finish_reason, "tool_calls" if already_returned else "stop"
                    )
                    self.assertEqual(final_responses[0].value, SpecialistReview.model_validate(review))

    async def test_production_return_policy_composite_sequences(self):
        for first in ("coding", "spec"):
            with self.subTest(first=first):
                host, roles, sources = self.production_host({
                    "primary": [
                        (("START\n",), first),
                        (("NEXT\n",), "architect"),
                        (("SUMMARY\n",), None),
                    ],
                    first: [(("FIRST-REVIEW\n",), None)],
                    "architect": [(("ARCHITECT-REVIEW\n",), None)],
                }, suffix=first)
                with self.assertLogs("workflows.responses", level="INFO") as captured:
                    text = await self.turn("Synthetic composite material.", host=host)
                self.assertEqual(
                    text, "START\nFIRST-REVIEW\nNEXT\nARCHITECT-REVIEW\nSUMMARY\n"
                )
                primary = roles["primary"].agent_name
                first_name = roles[first].agent_name
                architect = roles["architect"].agent_name
                self.assertEqual([
                    record.args for record in captured.records
                    if record.msg == "Handoff transition: source=%s target=%s"
                ], [
                    (primary, first_name), (first_name, primary),
                    (primary, architect), (architect, primary),
                ])
                self.assertEqual(captured.records[-1].args[0], primary)
                self.assertEqual(sources[0].clients[primary].calls, 3)
                self.assertEqual(sources[0].clients[first_name].calls, 1)
                self.assertEqual(sources[0].clients[architect].calls, 1)

    async def test_production_return_policy_fresh_factory_continuation_and_history(self):
        host, roles, sources = self.production_host({
            "primary": [
                (("START\n",), "coding"), (("SUMMARY\n",), None),
                (("NEXT-START\n",), "coding"), (("NEXT-SUMMARY\n",), None),
            ],
            "coding": [(("REVIEW\n",), None), (("NEXT-REVIEW\n",), None)],
        })
        self.assertEqual(await self.turn("Synthetic first turn.", host=host), "START\nREVIEW\nSUMMARY\n")
        # 新 factory 的 script 只表示下一輪必要呼叫，不重播前輪文字。
        host2, _, restarted_sources = self.production_host({
            "primary": [(("NEXT-START\n",), "coding"), (("NEXT-SUMMARY\n",), None)],
            "coding": [(("NEXT-REVIEW\n",), None)],
        })
        with self.assertLogs("workflows.responses", level="INFO") as captured:
            text = await self.turn("Synthetic follow-up.", host=host2)
        self.assertEqual(text, "NEXT-START\nNEXT-REVIEW\nNEXT-SUMMARY\n")
        self.assertEqual(captured.records[-1].args[0], roles["primary"].agent_name)
        primary = roles["primary"].agent_name
        coding = roles["coding"].agent_name
        self.assertIsNot(sources[0].clients[coding], restarted_sources[0].clients[coding])
        for name in (primary, coding):
            messages = restarted_sources[0].clients[name].requests[0]
            calls = [
                part["call_id"] for message in messages for part in message["contents"]
                if part["type"] == "function_call"
            ]
            results = [
                part["call_id"] for message in messages for part in message["contents"]
                if part["type"] == "function_result"
            ]
            self.assertTrue(calls)
            self.assertEqual(set(calls), set(results))
        self.assertEqual(restarted_sources[0].clients[coding].calls, 1)
        self.assertEqual(restarted_sources[0].clients[primary].calls, 2)

    async def test_production_return_policy_nonstream_agent_path(self):
        roles = build_agent_roles("nonstream-")
        primary = roles["primary"].agent_name
        coding = roles["coding"].agent_name
        source = ScriptedRoleClients({
            primary: [(("PREFACE\n",), coding), (("SUMMARY\n",), None)],
            coding: [(("CODING\n",), None)],
        })
        workflow = build_workflow(build_participants(source, roles, checkpoint_safe=True))
        result = await workflow.run("Synthetic nonstream material.")
        self.assertEqual([
            (event.data.source, event.data.target)
            for event in result if event.type == "handoff_sent"
        ], [(primary, coding), (coding, primary)])
        self.assertEqual(
            "".join(output.text for output in result.get_outputs()), "PREFACE\nCODING\nSUMMARY\n"
        )
        self.assertEqual(source.clients[coding].calls, 1)
        self.assertEqual(source.clients[primary].calls, 2)

    async def test_production_return_policy_primary_and_lab1_isolation(self):
        for text in ("Synthetic clarification question?", "Synthetic capability boundary."):
            with self.subTest(text=text):
                host, roles, sources = self.production_host({
                    "primary": [((text,), None)],
                })
                with self.assertLogs("workflows.responses", level="INFO") as captured:
                    self.assertEqual(await self.turn("Synthetic request.", host=host), text)
                self.assertFalse(any(
                    record.msg == "Handoff transition: source=%s target=%s"
                    for record in captured.records
                ))
                self.assertEqual(captured.records[-1].args[0], roles["primary"].agent_name)
                self.assertEqual(
                    sources[0].agents[roles["primary"].agent_name].default_options["instructions"],
                    roles["primary"].instructions,
                )
                self.assertTrue(all(
                    sources[0].clients[roles[key].agent_name].calls == 0
                    for key in ("coding", "architect", "spec")
                ))
        roles = build_agent_roles("standalone-")
        role = roles["coding"]
        original = role.instructions
        client = ScriptedHandoffClient(role.agent_name, [(("Synthetic standalone reply.",), None)])
        agent = client.as_agent(
            name=role.agent_name, instructions=role.instructions,
            default_options={"response_format": role.response_format},
        )
        result = await agent.run("Synthetic standalone material.")
        self.assertEqual(result.text, "Synthetic standalone reply.")
        self.assertEqual(agent.default_options["instructions"], original)
        self.assertEqual(role.instructions, original)
        self.assertEqual(role.response_format, SpecialistReview)
        self.assertFalse(client.options_seen[0].get("tools"))

    async def test_production_return_policy_technical_failures_do_not_complete(self):
        for fault in (
            "empty", "unknown_tool", "invalid_arguments", "user_action",
            "technical", "midstream", "timeout", "error_content", "unfinished",
            "pending", "missing_terminal", "incomplete", "content_filter",
        ):
            with self.subTest(fault=fault):
                def client_factory(name, script):
                    if name.endswith("-coding-agent"):
                        return FaultHandoffClient(name, script, fault)
                    return ScriptedHandoffClient(name, script)

                host, roles, sources = self.production_host({
                    "primary": [(("START\n",), "coding")],
                    "coding": [],
                }, suffix=fault, client_factory=client_factory)
                with patch("workflows.handoff.AGENT_TIMEOUT_SECONDS", 0.05 if fault == "timeout" else 180):
                    with self.assertLogs("workflows.responses", level="INFO") as captured:
                        events = [
                            event_payload(event) async for event in host._respond(
                                CreateResponse(input="Synthetic failure material.", stream=True),
                                context("Synthetic failure material."),
                                asyncio.Event(),
                            )
                        ]
                self.assertEqual(events[-1]["type"], "response.failed")
                self.assertEqual(events[-1]["response"]["error"]["code"], "server_error")
                self.assertFalse(any(event["type"] == "response.completed" for event in events))
                self.assertEqual([
                    record.args for record in captured.records
                    if record.msg == "Handoff transition: source=%s target=%s"
                ], [(roles["primary"].agent_name, roles["coding"].agent_name)])
                self.assertFalse(any(
                    record.msg == "Handoff waiting for user input: source=%s request_id=%s"
                    for record in captured.records
                ))
                self.assertEqual(sources[0].clients[roles["coding"].agent_name].calls, 1)
                self.assertEqual(sources[0].clients[roles["primary"].agent_name].calls, 1)

    async def test_production_return_policy_cancellation_is_not_degraded(self):
        for fault in ("cancel", "cancel_reason"):
            with self.subTest(fault=fault):
                def client_factory(name, script):
                    return (
                        FaultHandoffClient(name, script, fault)
                        if name.endswith("-coding-agent") else ScriptedHandoffClient(name, script)
                    )

                host, roles, sources = self.production_host({
                    "primary": [(("START\n",), "coding")],
                }, suffix=fault, client_factory=client_factory)
                with self.assertLogs("workflows.responses", level="INFO") as captured:
                    with self.assertRaises(asyncio.CancelledError):
                        await self.turn("Synthetic cancelled request.", host=host)
                self.assertEqual([
                    record.args for record in captured.records
                    if record.msg == "Handoff transition: source=%s target=%s"
                ], [(roles["primary"].agent_name, roles["coding"].agent_name)])
                self.assertEqual(sources[0].clients[roles["coding"].agent_name].calls, 1)

    async def test_production_return_policy_tool_failure_does_not_retry(self):
        @function_middleware
        async def failing_tool(context, call_next):
            if context.function.name.endswith(roles["primary"].agent_name):
                raise RuntimeError("Synthetic handoff tool failure")
            await call_next()

        def extra(name):
            return [failing_tool] if name.endswith("-coding-agent") else []

        host, roles, sources = self.production_host({
            "primary": [(("START\n",), "coding")],
            "coding": [(("REVIEW\n",), "primary")],
        }, middleware_factory=extra)
        with self.assertLogs("workflows.responses", level="INFO"):
            events = [
                event_payload(event) async for event in host._respond(
                    CreateResponse(input="Synthetic tool failure.", stream=True),
                    context("Synthetic tool failure."), asyncio.Event(),
                )
            ]
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertEqual(events[-1]["response"]["error"]["code"], "server_error")
        self.assertEqual(sources[0].clients[roles["coding"].agent_name].calls, 1)

    async def test_production_return_policy_preserves_hooks_value_and_metadata(self):
        roles = build_agent_roles("hooks-")
        primary = roles["primary"].agent_name
        coding = roles["coding"].agent_name
        review = {
            "category": "code", "summary": "Synthetic review.",
            "findings": ["Synthetic finding."], "severity": "LOW",
            "evidence": ["Synthetic evidence."], "recommendations": ["Synthetic advice."],
            "limitations": ["Synthetic offline-only data."],
        }
        coding_text = json.dumps(review, separators=(",", ":")) + "\n"
        for stream in (True, False):
            with self.subTest(stream=stream):
                inner_results = []
                outer_results = []

                @agent_middleware
                async def original_result(context, call_next):
                    await call_next()

                    def tagged(response):
                        response.additional_properties["synthetic_agent_metadata"] = "kept"
                        inner_results.append(response)
                        return response

                    if context.stream:
                        context.result.with_result_hook(tagged)
                    else:
                        context.result = tagged(context.result)

                @agent_middleware
                async def observe_result(context, call_next):
                    await call_next()
                    if context.stream:
                        context.result.with_result_hook(outer_results.append)
                    else:
                        outer_results.append(context.result)

                class ObservedClients(ScriptedRoleClients):
                    def as_agent(self, **options):
                        if options["name"] == coding:
                            options = dict(options)
                            options["middleware"] = [
                                observe_result, *options["middleware"], original_result,
                            ]
                        return super().as_agent(**options)

                source = ObservedClients({
                    primary: [(("PREFACE\n",), coding), (("SUMMARY\n",), None)],
                    coding: [((coding_text[:40], coding_text[40:]), None)],
                })
                original_instructions = {key: role.instructions for key, role in roles.items()}
                workflow = build_workflow(build_participants(source, roles, checkpoint_safe=True))
                if stream:
                    events = [
                        event async for event in workflow.run("Synthetic hooks material.", stream=True)
                    ]
                else:
                    events = await workflow.run("Synthetic hooks material.")
                self.assertEqual([
                    (event.data.source, event.data.target)
                    for event in events if event.type == "handoff_sent"
                ], [(primary, coding), (coding, primary)])
                self.assertEqual(
                    "".join(event.data.text for event in events if event.type == "output"),
                    "PREFACE\n" + coding_text
                    + _render_primary_summary([(coding, SpecialistReview.model_validate(review))], ""),
                )
                self.assertEqual(len(inner_results), 1)
                self.assertEqual(len(outer_results), 1)
                self.assertIs(outer_results[0], inner_results[0])
                result = outer_results[0]
                self.assertEqual(result.value, SpecialistReview.model_validate(review))
                self.assertEqual(result.additional_properties["synthetic_agent_metadata"], "kept")
                client = source.clients[coding]
                self.assertEqual(client.hook_counts["finalizer"], 1)
                self.assertTrue(all(
                    update.continuation_token is not None
                    for update in client.response_updates[0][:2]
                ))
                self.assertIsNone(client.response_updates[0][-1].continuation_token)
                self.assertIsNone(client.finalized_responses[0].continuation_token)
                self.assertEqual(client.finalized_responses[0].finish_reason, "stop")
                self.assertEqual(client.finalized_responses[0].value, SpecialistReview.model_validate(review))
                self.assertEqual(client.finalized_responses[0].additional_properties["synthetic_finalizer"], coding)
                if stream:
                    self.assertEqual(client.hook_counts, {
                        "finalizer": 1, "transform": 5, "result": 1, "cleanup": 1,
                    })
                    self.assertTrue(client.finalized_responses[0].additional_properties["synthetic_result_hook"])
                self.assertEqual(
                    {key: role.instructions for key, role in roles.items()}, original_instructions
                )
                for key in ("coding", "architect", "spec"):
                    self.assertEqual(
                        source.agents[roles[key].agent_name].default_options["response_format"],
                        roles[key].response_format,
                    )

    async def test_production_return_policy_rejects_nonunique_or_wrong_tool_binding(self):
        for fault in ("missing", "duplicate", "wrong_description", "wrong_parameters"):
            with self.subTest(fault=fault):
                @chat_middleware
                async def invalid_binding(context, call_next):
                    tools = list(context.options.get("tools", []))
                    original = next(tool for tool in tools if isinstance(tool, FunctionTool))
                    context.options = dict(context.options)
                    if fault == "missing":
                        context.options["tools"] = []
                    elif fault == "duplicate":
                        context.options["tools"] = [original, original]
                    else:
                        context.options["tools"] = [FunctionTool(
                            name=original.name,
                            description="Wrong target binding." if fault == "wrong_description" else original.description,
                            input_model={"type": "object", "properties": {"unexpected": {"type": "string"}}}
                            if fault == "wrong_parameters" else {"type": "object", "properties": {}},
                        )]
                    await call_next()

                def extra(name):
                    return [invalid_binding] if name.endswith("-coding-agent") else []

                host, roles, sources = self.production_host({
                    "primary": [(("START\n",), "coding")],
                    "coding": [(("REVIEW\n",), None)],
                }, suffix=fault, middleware_factory=extra)
                with self.assertLogs("workflows.responses", level="INFO"):
                    events = [
                        event_payload(event) async for event in host._respond(
                            CreateResponse(input="Synthetic binding check.", stream=True),
                            context("Synthetic binding check."), asyncio.Event(),
                        )
                    ]
                self.assertEqual(events[-1]["type"], "response.failed")
                self.assertEqual(events[-1]["response"]["error"]["code"], "server_error")
                self.assertEqual(sources[0].clients[roles["coding"].agent_name].calls, 0)

    async def test_production_return_policy_clarification_is_not_a_review(self):
        question = "Synthetic specialist needs one missing detail."
        host, roles, sources = self.production_host({
            "primary": [(("START\n",), "coding"), (("Synthetic Primary clarification?",), None)],
            "coding": [((question,), None)],
        })
        with self.assertLogs("workflows.responses", level="INFO") as captured:
            actual = await self.turn("Synthetic partial material.", host=host)
        self.assertEqual(actual, "START\n" + question + "Synthetic Primary clarification?")
        self.assertEqual(captured.records[-1].args[0], roles["primary"].agent_name)
        self.assertEqual(sources[0].clients[roles["coding"].agent_name].calls, 1)
        self.assertNotIn('"category"', actual)

    async def test_production_return_policy_conversations_do_not_share_state(self):
        host, roles, sources = self.production_host({
            "primary": [(("START\n",), "coding"), (("SUMMARY\n",), None)],
            "coding": [(("REVIEW\n",), None)],
        })
        with self.assertLogs("workflows.responses", level="INFO") as captured:
            results = await asyncio.gather(
                self.turn("Synthetic first conversation.", host=host, conversation="first"),
                self.turn("Synthetic second conversation.", host=host, conversation="second"),
            )
        self.assertEqual(results, ["START\nREVIEW\nSUMMARY\n"] * 2)
        self.assertEqual(len(sources), 2)
        for key in ("primary", "coding"):
            name = roles[key].agent_name
            self.assertIsNot(sources[0].clients[name], sources[1].clients[name])
            self.assertEqual(
                [source.clients[name].calls for source in sources], [2, 2] if key == "primary" else [1, 1]
            )
        self.assertEqual(sum(
            record.msg == "Handoff transition: source=%s target=%s"
            for record in captured.records
        ), 4)

    async def test_production_return_policy_nonstream_history_pairs(self):
        roles = build_agent_roles("nonstream-history-")
        primary = roles["primary"].agent_name
        coding = roles["coding"].agent_name
        source = ScriptedRoleClients({
            primary: [
                (("START\n",), coding), (("SUMMARY\n",), None),
                (("NEXT\n",), coding), (("NEXT-SUMMARY\n",), None),
            ],
            coding: [(("REVIEW\n",), None), (("NEXT-REVIEW\n",), None)],
        })
        workflow = build_workflow(build_participants(source, roles))
        first = await workflow.run("Synthetic first nonstream turn.")
        requests = [event for event in first if event.type == "request_info"]
        self.assertEqual(len(requests), 1)
        second = await workflow.run(responses={
            requests[0].request_id: HandoffAgentUserRequest.create_response("Synthetic next turn.")
        })
        self.assertEqual([
            (event.data.source, event.data.target)
            for event in second if event.type == "handoff_sent"
        ], [(primary, coding), (coding, primary)])
        for name in (primary, coding):
            messages = source.clients[name].requests[2 if name == primary else 1]
            calls = {
                part["call_id"] for message in messages for part in message["contents"]
                if part["type"] == "function_call"
            }
            results = {
                part["call_id"] for message in messages for part in message["contents"]
                if part["type"] == "function_result"
            }
            self.assertTrue(calls)
            self.assertEqual(calls, results)
        self.assertEqual(source.clients[coding].calls, 2)

    async def test_production_return_policy_service_default_client_uses_local_history(self):
        scripts = {
            "primary": [(("START\n",), "coding"), (("SUMMARY\n",), None)],
            "coding": [(("REVIEW\n",), None)],
        }
        host, roles, sources = self.production_host(
            scripts, client_factory=ServiceDefaultHandoffClient
        )
        self.assertEqual(
            await self.turn("Synthetic service-default first turn.", host=host),
            "START\nREVIEW\nSUMMARY\n",
        )
        restarted, _, next_sources = self.production_host(
            scripts, client_factory=ServiceDefaultHandoffClient
        )
        self.assertEqual(
            await self.turn("Synthetic service-default follow-up.", host=restarted),
            "START\nREVIEW\nSUMMARY\n",
        )
        coding = roles["coding"].agent_name
        primary = roles["primary"].agent_name
        for source in (sources[0], next_sources[0]):
            effective = source.clients[coding].options_seen[0]
            self.assertIs(effective["store"], False)
            self.assertFalse(any(
                effective.get(key)
                for key in ("previous_response_id", "conversation", "conversation_id")
            ))
            self.assertIs(source.clients[primary].options_seen[0]["store"], False)
        messages = next_sources[0].clients[coding].requests[0]
        calls = {
            part["call_id"] for message in messages for part in message["contents"]
            if part["type"] == "function_call"
        }
        results = {
            part["call_id"] for message in messages for part in message["contents"]
            if part["type"] == "function_result"
        }
        self.assertTrue(calls)
        self.assertEqual(calls, results)
        self.assertTrue(any(
            "REVIEW" in part.get("text", "")
            for message in messages for part in message["contents"]
        ))
        self.assertEqual(next_sources[0].clients[coding].calls, 1)
        self.assertEqual(next_sources[0].clients[primary].calls, 2)

    async def test_production_return_policy_rejects_legacy_service_session_without_erasing_it(self):
        sessions = []

        @chat_middleware
        async def legacy_session(context, call_next):
            context.session.service_session_id = "resp_synthetic_legacy"
            sessions.append(context.session)
            await call_next()

        def extra(name):
            return [legacy_session] if name.endswith("-coding-agent") else []

        host, roles, sources = self.production_host({
            "primary": [(("START\n",), "coding")],
            "coding": [(("REVIEW\n",), None)],
        }, middleware_factory=extra, client_factory=ServiceDefaultHandoffClient)
        with self.assertLogs("workflows.responses", level="INFO"):
            events = [
                event_payload(event) async for event in host._respond(
                    CreateResponse(input="Synthetic legacy history.", stream=True),
                    context("Synthetic legacy history."), asyncio.Event(),
                )
            ]
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertEqual(events[-1]["response"]["error"]["code"], "server_error")
        self.assertEqual(sessions[0].service_session_id, "resp_synthetic_legacy")
        self.assertEqual(sources[0].clients[roles["coding"].agent_name].calls, 0)


if __name__ == "__main__":
    unittest.main()
