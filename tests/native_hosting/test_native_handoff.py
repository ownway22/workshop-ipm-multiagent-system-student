"""以真正 Handoff 與 native Responses ASGI endpoint 驗證遷移門檻。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import GenericAlias, TracebackType
from typing import Any, Self
from uuid import uuid4

import pytest
from agent_framework import (
    Agent,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    CheckpointStorage,
    Content,
    FunctionInvocationLayer,
    FunctionTool,
    Message,
    ResponseStream,
    Workflow,
)
from agent_framework.orchestrations import HandoffAgentUserRequest
from agent_framework_foundry_hosting import (
    CheckpointStoreProvider,
    FoundryRequestScope,
    HostedResponseRequest,
    ResponsesHostServer,
    WorkflowTurn,
    response_input_messages,
)
from azure.ai.agentserver.core import AgentConfig, FoundryAgentRequestContext
from azure.ai.agentserver.responses.models import MessageRole
from httpx import ASGITransport, AsyncClient, Response

from agents import build_agent_roles
from models.specialist_review import SpecialistReview
from workflows.handoff import WORKFLOW_NAME, build_participants, build_workflow

INITIAL_TEXT = "請健檢這段固定材料：fixture_value = 42"
PREFACE = "<<<PRIMARY_PREFACE>>>\n交給 Coding。\n"
SUMMARY = "<<<PRIMARY_SUMMARY>>>\n已收到 Coding 的完整結果；請提供下一步。\n"
SECOND_TEXT = "<<<SECOND_TURN>>>\n保留原材料 fixture_value = 42，等待下一次追問。\n"
THIRD_TEXT = "<<<THIRD_TURN>>>\n原材料與上一輪追問都仍在 history。\n"
CODING_TEXT = (
    "<<<CODING_BEGIN>>>\n"
    + SpecialistReview(
        category="code",
        summary="固定材料的離線健檢",
        findings=["變數使用固定值。"],
        severity="LOW",
        evidence=["fixture_value = 42（來源：使用者訊息）"],
        recommendations=["以測試確認實際用途。"],
        limitations=["未執行材料中的程式碼。"],
    ).model_dump_json()
    + "\n```python\nfixture_value = 42\n```\n<<<CODING_END>>>\n"
)
FIRST_TEXT = PREFACE + CODING_TEXT + SUMMARY
CHECKPOINT_TYPES = [
    f"{kind.__module__}:{kind.__qualname__}"
    for kind in (HandoffAgentUserRequest, GenericAlias, MessageRole)
]


@pytest.fixture(autouse=True)
def isolated_state() -> Iterator[None]:
    assert not AgentConfig.from_env().is_hosted, "此測試只允許本機離線執行。"
    root = Path(__file__).resolve().parents[2] / ".cache" / "native-hosting-tests"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="case-", dir=root) as directory:
        key = "AGENTSERVER_STATE_ROOT"
        previous = os.environ.get(key)
        os.environ[key] = directory
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


class DeterministicHandoffClient(
    FunctionInvocationLayer,
    ChatMiddlewareLayer,
    BaseChatClient,
    AbstractAsyncContextManager["DeterministicHandoffClient"],
):
    """只替換模型；交接、middleware、history 與 checkpoint 仍由 SDK 執行。"""

    STORES_BY_DEFAULT = True

    def __init__(self, name: str, primary_name: str, coding_name: str) -> None:
        super().__init__()
        self.name = name
        self.primary_name = primary_name
        self.coding_name = coding_name
        self.calls = 0
        self.entered = 0
        self.closed = 0
        self.requests: list[list[Message]] = []

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed += 1

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        assert options.get("store") is False
        self.calls += 1
        self.requests.append(list(messages))
        users = [message.text for message in messages if message.role == "user"]
        assert users.count(INITIAL_TEXT) == 1, "原材料缺失或 history 被重複 replay。"
        target = None
        if self.name == self.coding_name:
            text = CODING_TEXT
        elif self.name == self.primary_name:
            if users[-1] == "繼續":
                text = SECOND_TEXT
            elif users[-1] == "再繼續":
                assert any(SECOND_TEXT in message.text for message in messages)
                text = THIRD_TEXT
            elif any(CODING_TEXT in message.text for message in messages):
                text = SUMMARY
            else:
                text, target = PREFACE, self.coding_name
        else:
            raise AssertionError(f"不應呼叫未指定的專家：{self.name}")

        message_id = f"{self.name}-{uuid4().hex}"
        contents = [Content.from_text(text)]
        if target is not None:
            tool_name = f"handoff_to_{target}"
            assert any(
                isinstance(tool, FunctionTool) and tool.name == tool_name
                for tool in options.get("tools", ())
            )
            contents.append(
                Content.from_function_call(
                    call_id=message_id, name=tool_name, arguments={}
                )
            )

        async def updates() -> AsyncIterator[ChatResponseUpdate]:
            for offset in range(0, len(text), 17):
                yield ChatResponseUpdate(
                    contents=[Content.from_text(text[offset : offset + 17])],
                    role="assistant",
                    author_name=self.name,
                    message_id=message_id,
                )
            if target is not None:
                yield ChatResponseUpdate(
                    contents=contents[1:],
                    role="assistant",
                    author_name=self.name,
                    message_id=message_id,
                )
            yield ChatResponseUpdate(
                role="assistant",
                author_name=self.name,
                message_id=message_id,
                finish_reason="tool_calls" if target is not None else "stop",
            )

        if stream:
            return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

        async def complete() -> ChatResponse:
            return ChatResponse.from_updates([update async for update in updates()])

        return complete()


class HandoffFactory:
    def __init__(self) -> None:
        self.roles = build_agent_roles("qvn-")
        self.clients: list[DeterministicHandoffClient] = []
        self.graphs: list[Workflow] = []

    def as_agent(self, **options: Any) -> Agent:
        client = DeterministicHandoffClient(
            options["name"],
            self.roles["primary"].agent_name,
            self.roles["coding"].agent_name,
        )
        self.clients.append(client)
        return client.as_agent(**options)

    def __call__(self, request: HostedResponseRequest) -> Workflow:
        workflow = build_workflow(
            build_participants(self, self.roles, checkpoint_safe=True)
        )
        self.graphs.append(workflow)
        return workflow


class ObservedCheckpointProvider(CheckpointStoreProvider):
    """只觀察公開 provider 回傳的 store，不介入 host 的 binding 或鎖。"""

    def __init__(self) -> None:
        super().__init__(allowed_checkpoint_types=CHECKPOINT_TYPES)
        self.stores: dict[str, CheckpointStorage] = {}

    def get_store_for_scope(
        self,
        *,
        scope: FoundryRequestScope | None,
        context_id: str,
        platform_context: FoundryAgentRequestContext,
    ) -> CheckpointStorage:
        store = super().get_store_for_scope(
            scope=scope, context_id=context_id, platform_context=platform_context
        )
        self.stores[context_id] = store
        return store


async def parse_text(request: HostedResponseRequest) -> WorkflowTurn[list[Message]]:
    return WorkflowTurn(input=await response_input_messages(request))


async def parse_with_public_replies(
    request: HostedResponseRequest,
) -> WorkflowTurn[list[Message]]:
    items = await request.get_input_items()
    if request.request.get("previous_response_id") or any(
        item.get("type") == "function_call_output" for item in items
    ):
        return WorkflowTurn(responses=await request.get_workflow_responses())
    return await parse_text(request)


def output_text(payload: Mapping[str, Any]) -> str:
    return "".join(
        part["text"]
        for item in payload["output"]
        if item["type"] == "message"
        for part in item["content"]
        if part["type"] == "output_text"
    )


def response_payload(response: Response, *, stream: bool) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    if not stream:
        return response.json()
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert events[0]["type"] == "response.created", events
    assert events[1]["type"] == "response.in_progress", events
    assert events[-1]["type"] in ("response.completed", "response.failed"), events
    payload = events[-1]["response"]
    deltas = "".join(
        event["delta"] for event in events if event["type"] == "response.output_text.delta"
    )
    assert deltas == output_text(payload)
    return payload


def assert_completed(payload: Mapping[str, Any], expected: str) -> None:
    assert payload["status"] == "completed", json.dumps(
        payload.get("error"), ensure_ascii=False
    )
    assert payload.get("error") is None, payload
    assert output_text(payload) == expected, payload


async def pending_handoff(
    provider: ObservedCheckpointProvider, *, expected: str = SUMMARY
) -> str:
    assert len(provider.stores) == 1
    store = next(iter(provider.stores.values()))
    checkpoint = await store.get_latest(workflow_name=WORKFLOW_NAME)
    assert checkpoint is not None
    pending = checkpoint.pending_request_info_events
    assert len(pending) == 1
    event = next(iter(pending.values()))
    assert isinstance(event.data, HandoffAgentUserRequest)
    assert event.data.agent_response.text == expected
    return event.request_id


def make_host(
    factory: HandoffFactory,
    provider: ObservedCheckpointProvider,
    *,
    public_replies: bool = False,
) -> ResponsesHostServer:
    return ResponsesHostServer(
        workflow=factory,
        parse_response=parse_with_public_replies if public_replies else parse_text,
        checkpoint_store_provider=provider,
        configure_observability=None,
    )


@asynccontextmanager
async def endpoint(host: ResponsesHostServer) -> AsyncIterator[AsyncClient]:
    async with host.router.lifespan_context(host):
        async with AsyncClient(
            transport=ASGITransport(app=host), base_url="http://localhost"
        ) as client:
            yield client


async def post_turn(
    client: AsyncClient,
    text: str | list[dict[str, Any]],
    *,
    stream: bool,
    previous: str | None = None,
    conversation: str | None = None,
    store: bool = True,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "input": text,
        "stream": stream,
        "store": store,
        "agent_session_id": "offline-native-handoff",
    }
    if previous is not None:
        request["previous_response_id"] = previous
    if conversation is not None:
        request["conversation"] = conversation
    response = await client.post("/responses", json=request)
    return response_payload(response, stream=stream)


def function_reply(payload: Mapping[str, Any], text: str) -> list[dict[str, Any]]:
    calls = [
        item
        for item in payload["output"]
        if item["type"] == "function_call" and item["name"] == "request_info"
    ]
    assert len(calls) == 1, payload
    return [{
        "type": "function_call_output",
        "call_id": calls[0]["call_id"],
        "output": json.dumps([Message("user", [text]).to_dict()], ensure_ascii=False),
    }]


def assert_resources_closed(factory: HandoffFactory) -> None:
    assert all(client.entered == client.closed == 1 for client in factory.clients)
    assert len({graph.graph_signature_hash for graph in factory.graphs}) == 1
    assert all(graph.name == WORKFLOW_NAME for graph in factory.graphs)


async def three_plain_text_turns(*, stream: bool, public_replies: bool) -> None:
    factory = HandoffFactory()
    provider = ObservedCheckpointProvider()
    host = make_host(factory, provider, public_replies=public_replies)
    async with endpoint(host) as client:
        previous = None
        for text, expected in (
            (INITIAL_TEXT, FIRST_TEXT),
            ("繼續", SECOND_TEXT),
            ("再繼續", THIRD_TEXT),
        ):
            payload = await post_turn(client, text, stream=stream, previous=previous)
            assert_completed(payload, expected)
            if previous is None:
                await pending_handoff(provider)
                assert output_text(payload).count(CODING_TEXT) == 1
            previous = payload["id"]
    assert len(factory.graphs) == 3
    assert_resources_closed(factory)


@pytest.mark.gate
@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize(
    "public_replies", [False, True], ids=["typed-input", "public-replies"]
)
def test_three_plain_text_handoff_turns(stream: bool, public_replies: bool) -> None:
    asyncio.run(three_plain_text_turns(stream=stream, public_replies=public_replies))


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize(
    "public_replies", [False, True], ids=["typed-input", "public-replies"]
)
def test_plain_text_rejection_does_not_consume_pending_handoff(
    stream: bool, public_replies: bool
) -> None:
    async def scenario() -> None:
        factory, provider = HandoffFactory(), ObservedCheckpointProvider()
        host = make_host(factory, provider, public_replies=public_replies)
        async with endpoint(host) as client:
            first = await post_turn(client, INITIAL_TEXT, stream=stream)
            assert_completed(first, FIRST_TEXT)
            assert output_text(first).count(CODING_TEXT) == 1
            pending = await pending_handoff(provider)
            for text in ("繼續", "再繼續"):
                rejected = await post_turn(
                    client, text, stream=stream, previous=first["id"]
                )
                assert rejected["status"] == "failed"
                assert rejected["error"] == {
                    "code": "server_error",
                    "message": (
                        "Pending workflow replies cannot be mixed with new input."
                        if public_replies else
                        "Native workflow input validation failed. Validate the typed input "
                        "and current scoped continuation, or start a fresh workflow lineage."
                    ),
                }
                assert output_text(rejected) == ""
                assert await pending_handoff(provider) == pending
        assert [client.calls for client in factory.clients] == [
            2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        ]
        assert_resources_closed(factory)

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("recreate", [False, True], ids=["same-host", "rebuilt-host"])
@pytest.mark.parametrize("named", [False, True], ids=["previous-response", "conversation"])
def test_correlated_function_results_are_only_a_control(
    stream: bool, recreate: bool, named: bool
) -> None:
    """function result 對照組不是 Playground 純文字門檻的替代品。"""

    async def scenario() -> None:
        factory, provider = HandoffFactory(), ObservedCheckpointProvider()
        host = make_host(factory, provider, public_replies=True)
        previous = None
        payload: dict[str, Any] | None = None
        conversation = "conv_native_control" if named else None
        request: str | list[dict[str, Any]] = INITIAL_TEXT
        for index, (text, expected) in enumerate((
            (INITIAL_TEXT, FIRST_TEXT),
            ("繼續", SECOND_TEXT),
            ("再繼續", THIRD_TEXT),
        )):
            if index:
                assert payload is not None
                request = function_reply(payload, text)
            if recreate and index:
                host = make_host(factory, provider, public_replies=True)
            async with endpoint(host) as client:
                payload = await post_turn(
                    client, request, stream=stream,
                    previous=None if named else previous,
                    conversation=conversation,
                )
            assert_completed(payload, expected)
            await pending_handoff(provider, expected=SUMMARY if index == 0 else expected)
            previous = payload["id"]
        assert len(factory.graphs) == 3
        assert_resources_closed(factory)

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize(
    "contract", ["branch", "conversation-switch", "store-false"]
)
def test_native_contract_differences(stream: bool, contract: str) -> None:
    async def scenario() -> None:
        factory, provider = HandoffFactory(), ObservedCheckpointProvider()
        host = make_host(factory, provider, public_replies=True)
        async with endpoint(host) as client:
            if contract == "store-false":
                failed = await post_turn(client, INITIAL_TEXT, stream=stream, store=False)
                assert not provider.stores
                expected_error = (
                    "Native workflow execution failed. Validate the typed input "
                    "and current scoped continuation, or start a fresh workflow lineage."
                )
            else:
                first = await post_turn(
                    client, INITIAL_TEXT, stream=stream,
                    conversation="conv_native_no_fork"
                    if contract == "conversation-switch" else None,
                )
                assert_completed(first, FIRST_TEXT)
                reply = function_reply(first, "繼續")
                if contract == "branch":
                    second = await post_turn(
                        client, reply, stream=stream, previous=first["id"]
                    )
                    assert_completed(second, SECOND_TEXT)
                    expected_error = (
                        "This workflow continuation is stale or forked; "
                        "use its current response."
                    )
                else:
                    expected_error = (
                        "Native workflow scoped continuation failed. Validate the typed input "
                        "and current scoped continuation, or start a fresh workflow lineage."
                    )
                failed = await post_turn(
                    client, reply, stream=stream, previous=first["id"]
                )
                assert output_text(failed) == ""
                assert [client.calls for client in factory.clients[-4:]] == [0, 0, 0, 0]
            assert failed["status"] == "failed"
            assert failed["error"] == {"code": "server_error", "message": expected_error}
        assert_resources_closed(factory)

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize(
    "failure", ["checkpoint-missing", "checkpoint-mismatch", "reply-mismatch"]
)
def test_exact_checkpoint_or_reply_failure_does_not_restart(
    stream: bool, failure: str
) -> None:
    async def scenario() -> None:
        factory, provider = HandoffFactory(), ObservedCheckpointProvider()
        host = make_host(factory, provider, public_replies=True)
        async with endpoint(host) as client:
            first = await post_turn(client, INITIAL_TEXT, stream=stream)
            assert_completed(first, FIRST_TEXT)
            await pending_handoff(provider)
            store = next(iter(provider.stores.values()))
            checkpoint = await store.get_latest(workflow_name=WORKFLOW_NAME)
            assert checkpoint is not None
            reply = function_reply(first, "繼續")
            if failure == "checkpoint-missing":
                await store.delete(checkpoint.checkpoint_id)
                assert await store.get_latest(workflow_name=WORKFLOW_NAME) is not None
            elif failure == "checkpoint-mismatch":
                checkpoint.workflow_name = "not-the-bound-workflow"
                await store.save(checkpoint)
            else:
                reply[0]["call_id"] = "call_not_in_this_checkpoint"
            rejected = await post_turn(
                client, reply, stream=stream, previous=first["id"]
            )
            assert rejected["status"] == "failed"
            assert rejected["error"]["code"] == "server_error"
            assert rejected["error"]["message"] == (
                "A reply must match its wire ID in the exact pending workflow checkpoint."
                if failure == "reply-mismatch" else
                "Native workflow scoped continuation failed. Validate the typed input "
                "and current scoped continuation, or start a fresh workflow lineage."
            )
            assert output_text(rejected) == ""
            assert [client.calls for client in factory.clients[-4:]] == [0, 0, 0, 0]
        assert_resources_closed(factory)

    asyncio.run(scenario())
