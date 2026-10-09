import asyncio
import json
import tempfile
import unittest
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Executor,
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
from azure.ai.agentserver.responses.models import CreateResponse, ItemMessage

from agents import build_agent_roles
from models.specialist_review import SpecialistReview
from workflows.responses import HandoffRequestError, HandoffResponsesHost
from workflows.handoff import (
    HandoffRoutingError,
    build_checkpoint_safe_middleware,
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
                contents=[Content.from_text(text)],
                role="assistant",
                author_name=self.name,
                message_id=message_id,
            )
            for text in chunks
        ]
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
            if contents:
                yield ChatResponseUpdate(
                    contents=contents, role="assistant",
                    finish_reason=(
                        "length" if self.fault == "unfinished"
                        else "cancelled" if self.fault == "cancel_reason" else None
                    ),
                )
            if self.fault == "midstream":
                raise RuntimeError("Synthetic midstream failure")
            if self.fault == "cancel":
                raise asyncio.CancelledError()

        if stream:
            return ResponseStream(generate(), finalizer=ChatResponse.from_updates)

        async def complete():
            return ChatResponse.from_updates([update async for update in generate()])

        return complete()


class ServiceDefaultHandoffClient(ScriptedHandoffClient):
    STORES_BY_DEFAULT = True


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


class HandoffResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.host = HandoffResponsesHost(workflow, checkpoint_root=self.root)

    async def asyncTearDown(self):
        self.directory.cleanup()

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
        host = HandoffResponsesHost(logging_workflow, checkpoint_root=self.root)
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
        host = HandoffResponsesHost(logging_workflow, checkpoint_root=self.root)
        payloads = [
            event.as_dict()
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
        restarted = HandoffResponsesHost(workflow, checkpoint_root=self.root)
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
            event.as_dict()
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

        host = HandoffResponsesHost(broken_factory, checkpoint_root=self.root)
        events = [
            event.as_dict()
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
            payloads = [event.as_dict() for event in events]
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

    async def test_http_preserves_real_handoff_specialist_text(self):
        roles = build_agent_roles("contract-")
        primary_name = roles["primary"].agent_name
        coding_name = roles["coding"].agent_name
        preface = "<<<PRIMARY_PREFACE>>>\n"
        summary = "<<<PRIMARY_SUMMARY>>>\nSummary: offline fixture reviewed.\n"
        coding_text = (
            "<<<CODING_BEGIN_OUTPUT_CONTRACT>>>\n"
            "Synthetic specialist review.\n"
            '{"category":"code","summary":"offline fixture"}\n'
            "```python\n"
            'value = {"fixture": True}\n'
            "```\n"
            "<<<CODING_END_OUTPUT_CONTRACT>>>\n"
        )
        coding_chunks = (
            "<<<CODING_BEGIN_OUTPUT_CONTRACT>>>\nSynthetic specialist review.\n",
            '{"category":"code",',
            '"summary":"offline fixture"}\n```py',
            'thon\nvalue = {"fixture": True}\n',
            "```\n<<<CODING_END_OUTPUT_CONTRACT>>>\n",
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
                            event.as_dict() async for event in host._respond(
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
                event.as_dict() async for event in host._respond(
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
                    "PREFACE\n" + coding_text + "SUMMARY\n",
                )
                self.assertEqual(len(inner_results), 1)
                self.assertEqual(len(outer_results), 1)
                self.assertIs(outer_results[0], inner_results[0])
                result = outer_results[0]
                self.assertEqual(result.value, SpecialistReview.model_validate(review))
                self.assertEqual(result.additional_properties["synthetic_agent_metadata"], "kept")
                client = source.clients[coding]
                self.assertEqual(client.hook_counts["finalizer"], 1)
                self.assertEqual(client.finalized_responses[0].value, SpecialistReview.model_validate(review))
                self.assertEqual(client.finalized_responses[0].additional_properties["synthetic_finalizer"], coding)
                if stream:
                    self.assertEqual(client.hook_counts, {
                        "finalizer": 1, "transform": 2, "result": 1, "cleanup": 1,
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
                        event.as_dict() async for event in host._respond(
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
            self.assertNotIn("store", source.clients[primary].options_seen[0])
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
                event.as_dict() async for event in host._respond(
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
