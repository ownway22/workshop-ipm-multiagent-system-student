import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Never

from httpx import ASGITransport, AsyncClient
from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    Content,
    Executor,
    Message,
    ResponseStream,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowEvent,
    handler,
    response_handler,
)
from agent_framework.orchestrations import HandoffAgentUserRequest, HandoffSentEvent
from azure.ai.agentserver.responses.models import CreateResponse, ItemMessage

from workflows.responses import HandoffRequestError, HandoffResponsesHost
from workflows.handoff import build_checkpoint_safe_middleware


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


if __name__ == "__main__":
    unittest.main()
