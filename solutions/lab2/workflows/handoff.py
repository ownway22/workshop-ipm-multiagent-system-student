"""四代理 Handoff 工作流（Lab 2 的核心）。

由 `src/main.py` 匯入；本模組不自行啟動任何伺服器。

## 拓撲

以 Primary 為起點的**單層星狀**：Primary ↔ 三位專家，專家之間**沒有**直接邊。

```text
              ┌──▶ qvn-coding-agent ────┐
              │                         │
使用者 ──▶ qvn-primary-agent ◀──────────┤
              │                         │
              ├──▶ qvn-architect-agent ─┤
              │                         │
              └──▶ qvn-spec-agent ──────┘
```

這是「複合需求逐次交接、禁止平行分派」的**結構性**保證——不是靠 instructions
拜託模型照做，而是圖上根本不存在「專家 A → 專家 B」的路徑。

## 為什麼參與者要重新建構，而不是沿用持久化 agent

`registry/create_agents.py` 在 Foundry 專案裡建立的是**持久化 agent**（portal 看得到、
可依前置詞清理）。`HandoffBuilder` 需要的則是**執行期參與者**：它會 clone 參與者、
注入交接工具、掛上 middleware，因此要求參與者支援本地工具呼叫。

兩者由同一份定義檔（`src/agents/`）產生，角色與資料契約一致但物件不同。
Handoff runtime 另附內部控制規則與 specialists-only 回交政策；Lab 1 的角色文字不變。
`.env` 換一個專案端點，這裡就會連到另一個專案，不需要改任何程式碼。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING

from agents import build_agent_roles
from models.agent_role import AgentRole

if TYPE_CHECKING:  # 只為型別標註；執行期不匯入，維持本模組「用到才 import」的一致風格
    from agent_framework import (
        Agent, AgentResponse, AgentResponseUpdate, ChatResponse, CheckpointStorage, ResponseStream,
    )

logger = logging.getLogger(__name__)

_DEGRADED_RESULT = "handoff_degraded"


class HandoffRoutingError(RuntimeError):
    """專家回合沒有完成合法回交；不能當成正常審查或使用者等待。"""

#: 專家角色的 key（Primary 以外的三個）。順序即 `add_handoff` 的建邊順序。
SPECIALIST_KEYS: tuple[str, ...] = ("coding", "architect", "spec")

#: Workflow 名稱。與 Lab 3 的 hosted agent 名稱一致，方便對照兩者是同一套拓撲。
WORKFLOW_NAME = "qvn-ipm-review"

#: DevUI 上「workflow 類別」實體的顯示名稱。
#:
#: 必須與 `WORKFLOW_NAME` 不同：DevUI 的實體名稱直接取自物件的 `.name`，兩者同名時
#: 清單上會出現兩個一模一樣的項目，學員無法分辨自己正在跟哪一個對話。
#: `WORKFLOW_NAME` 本身**不能**改——它同時綁定 `deploy/agent.yaml` 的 `name` 與 azd 的
#: 服務名稱，三者不一致會讓部署後的查詢與呼叫全部 404。
WORKFLOW_ENTITY_NAME = f"{WORKFLOW_NAME}-workflow"

#: workflow 類別實體的入口節點 id。
#:
#: 這個範點只接受 `str`，作用是讓 DevUI 產生**單一文字輸入框**而不是 `Message` 的六欄表單。
#: 詳細理由見 `_build_workflow_entity()` 的說明。
WORKFLOW_ENTRY_EXECUTOR_ID = "user-input"

#: 單一 agent 單次執行的逾時（秒）。
#:
#: 降級機制要求「不崩潰、也不長時間無回應」——後半句只靠 try/except 是達不到的：
#: 服務端沒回應時連線可以掛很久。因此逾時本身就是降級條件之一。
AGENT_TIMEOUT_SECONDS = 180


def _degraded_text(agent_name: str, reason: str, remediation: str) -> str:
    """組出降級回覆的文字。

    刻意寫成「講給使用者聽」而不是拋堆疊：學員在 DevUI 看到的應該是一段看得懂的說明，
    而不是紅色的 traceback。同時必須說清楚**哪一位**專家不可用，否則使用者會誤以為
    整個系統壞了。
    """
    return (
        f"⚠️ 我原本要把這一段交給「{agent_name}」，但目前無法使用（{reason}）。\n"
        f"\n"
        f"這一段分析我先跳過，**不會**由我代寫——那樣會產生沒有依據的內容。\n"
        f"\n"
        f"你可以：\n"
        f"1. {remediation}\n"
        f"2. 或告訴我略過這一段，我接著處理其餘部分。"
    )


#: HTTP 狀態碼 → `(原因, 修復建議)`。涵蓋三種常見的不可用情境。
_STATUS_REASONS: dict[int, tuple[str, str]] = {
    401: (
        "驗證失敗（HTTP 401）",
        "執行 `az login --tenant <你的租用戶 ID>` 重新登入後重試。",
    ),
    403: (
        "權限不足（HTTP 403）",
        "確認你在該 Foundry 專案上具備 Foundry Project Manager 角色。",
    ),
    404: (
        "找不到對應的 agent 或模型部署，可能已被刪除",
        "執行 `cd src && uv run python -m preflight` 確認六項檢查，"
        "再執行 `uv run python -m registry.create_agents` 重新建立。",
    ),
    429: (
        "觸發流量限制（HTTP 429）",
        "等一分鐘後重試；若整場 workshop 都很頻繁，請調高模型部署的 TPM 配額。",
    ),
}


def _unwrap(error: BaseException) -> list[BaseException]:
    """展開例外鏈，回傳由外而內的所有例外。

    SDK 會把底層錯誤層層包裝：`ChatClientException` → `NotFoundError`，
    狀態碼只掛在最內層。只看最外層那一個會全部落到「未知」分類。
    （2026-07-27 實測：模型部署不存在時外層是 `ChatClientException`，404 在內層。）
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    queue: list[BaseException] = [error]
    while queue:
        current = queue.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        queue.extend(
            item
            for item in (current.__cause__, current.__context__, *current.args)
            if isinstance(item, BaseException)
        )
    return chain


def _classify(error: BaseException) -> tuple[str, str]:
    """把例外歸類成 `(原因, 修復建議)`。

    分類必須以型別與 HTTP 狀態碼判斷，不可比對錯誤訊息字串——訊息會隨服務端改版變動。
    """
    from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError

    chain = _unwrap(error)

    if any(isinstance(item, asyncio.TimeoutError) for item in chain):
        return (
            f"超過 {AGENT_TIMEOUT_SECONDS} 秒沒有回應",
            "稍後重試；若持續發生，請確認 Foundry 專案所在區域的服務狀態。",
        )

    for item in chain:
        status = getattr(item, "status_code", None)
        if isinstance(status, int) and status in _STATUS_REASONS:
            return _STATUS_REASONS[status]

    for item in chain:
        if isinstance(item, ResourceNotFoundError):
            return _STATUS_REASONS[404]
        if isinstance(item, ClientAuthenticationError):
            return _STATUS_REASONS[401]

    return (type(error).__name__, "檢查終端機的完整錯誤訊息後重試。")


def _degraded_response(agent_name: str, error: BaseException):
    """建立非串流用的降級 `AgentResponse`。"""
    from agent_framework import AgentResponse, Message

    reason, remediation = _classify(error)
    return AgentResponse(
        messages=[Message("assistant", [_degraded_text(agent_name, reason, remediation)])],
        additional_properties={_DEGRADED_RESULT: True},
    )


def _degraded_update(agent_name: str, error: BaseException):
    """建立串流用的降級 `AgentResponseUpdate`。"""
    from agent_framework import AgentResponseUpdate, Message

    reason, remediation = _classify(error)
    message = Message("assistant", [_degraded_text(agent_name, reason, remediation)])
    return AgentResponseUpdate(
        contents=message.contents, role="assistant",
        additional_properties={_DEGRADED_RESULT: True},
    )


def build_degraded_reply_middleware(agent_name: str):
    """建立單一 agent 的降級 middleware。

    沒有回交 guard 時，不可用的 agent 會產生降級回覆並等待使用者。
    Handoff specialists 的外層 guard 則將此 metadata 標記視為明確失敗，
    不冒充正常審查或 completed 的專家等待；Primary 的既有降級行為保留。

    **為什麼串流與非串流要分開處理**：`await call_next()` 在串流模式下**很快就返回**，
    此時 `context.result` 只是一個尚未消費的 `ResponseStream`，真正的錯誤要等到疊代時才發生
    ——那已經在 middleware 之外了。因此串流路徑必須另外包一層，在疊代時攬截。
    """
    from agent_framework import AgentResponse, AgentResponseUpdate, ResponseStream, agent_middleware

    def _guard(
        inner: "ResponseStream[AgentResponseUpdate, AgentResponse]",
    ) -> "ResponseStream[AgentResponseUpdate, AgentResponse]":
        failed = False

        async def generate() -> AsyncIterator[AgentResponseUpdate]:
            nonlocal failed
            try:
                async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
                    async for update in inner:
                        yield update
            except Exception as error:  # noqa: BLE001 — 降級的目的就是攔下所有失敗
                failed = True
                logger.warning("%s 不可用，改以降級回覆：%r", agent_name, error)
                yield _degraded_update(agent_name, error)

        async def finalize(updates: Sequence[AgentResponseUpdate]) -> AgentResponse:
            if failed:
                return AgentResponse.from_updates(updates)
            return await inner.get_final_response()

        return ResponseStream(generate(), finalizer=finalize)

    @agent_middleware
    async def degraded_reply(context, call_next) -> None:
        try:
            async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
                await call_next()
        except Exception as error:  # noqa: BLE001
            logger.warning("%s 不可用，改以降級回覆：%r", agent_name, error)
            context.result = _degraded_response(agent_name, error)
            return

        if context.stream and isinstance(context.result, ResponseStream):
            # `ResponseStream` 本身就是 AsyncIterable，直接迭代即可。
            # 不可用 `.updates`——那是「已收集的更新」串列，不是可疊代來源。
            context.result = _guard(context.result)

    return degraded_reply


def build_checkpoint_safe_middleware():
    """在寫進 checkpoint 前，清掉 SDK 附帶的 `raw_representation` 除錯物件。

    `raw_representation` 是模型供應商的原始回應物件，不一定能序列化，留著可能讓 checkpoint
    寫入失敗。這裡只清掉它，文字、工具呼叫與結構化結果都保留。
    只用在 Responses 主機（`create_workflow_factory()`）；DevUI 的行為不變。
    """
    from agent_framework import AgentResponse, AgentResponseUpdate, ResponseStream, agent_middleware

    def clear_raw(response: AgentResponse | AgentResponseUpdate) -> None:
        response.raw_representation = None
        messages = response.messages if isinstance(response, AgentResponse) else [response]
        for message in messages:
            message.raw_representation = None
            for content in message.contents:
                content.raw_representation = None

    @agent_middleware
    async def checkpoint_safe(context, call_next):
        await call_next()
        if context.stream and isinstance(context.result, ResponseStream):
            context.result.with_transform_hook(clear_raw)
            context.result.with_result_hook(clear_raw)
        elif isinstance(context.result, AgentResponse):
            clear_raw(context.result)

    return checkpoint_safe


def _return_description(primary_name: str) -> str:
    return (
        f"將本回合控制權交回協調者 {primary_name}，由協調者彙整或釐清。"
        "這是框架內部交接，不查詢或執行外部系統；不重複已輸出的專家文字。"
    )


def _runtime_instructions(primary_name: str) -> str:
    return (
        "\n\n## Handoff 執行期控制規則\n"
        "角色定義中的「沒有任何工具」指沒有外部查詢或執行工具；"
        "本工作流另有框架提供的內部交接控制工具。\n"
        "先依原資料契約輸出完整專家結果；回交 call 不是專家分析，不能代替結果。"
        f"程式會在結果正常完成後呼叫內部工具，將控制權交回 {primary_name}，"
        "由協調者彙整或向使用者釐清；不要自行呼叫交接工具。\n"
        "原審查結果只輸出一次；需要補充材料的說明不代表審查完成，"
        "不得捏造審查結果。業務與 schema 的原有限制不變。"
    )


def _empty_arguments(arguments: object) -> bool:
    if arguments is None or arguments == "":
        return True
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return False
    return isinstance(arguments, Mapping) and not arguments


def _check_response_actions(response: "ChatResponse | AgentResponse | AgentResponseUpdate") -> None:
    from agent_framework import AgentResponseUpdate

    if response.finish_reason == "cancelled":
        raise asyncio.CancelledError()
    if (response.additional_properties or {}).get(_DEGRADED_RESULT):
        raise HandoffRoutingError("專家已降級，不能當成正常回交。")
    if response.finish_reason in (
        "length", "content_filter", "error", "incomplete", "failed",
    ):
        raise HandoffRoutingError("專家回合未正常完成，不能補上回交。")
    messages = [response] if isinstance(response, AgentResponseUpdate) else response.messages
    for message in messages:
        for content in message.contents:
            if (
                content.user_input_request or content.type == "error"
                or content.exception or content.error_code or content.status in ("failed", "incomplete")
            ):
                raise HandoffRoutingError("專家有錯誤或未完成的使用者動作。")


def _check_response_complete(response: "ChatResponse | AgentResponse") -> None:
    _check_response_actions(response)
    if response.continuation_token is not None:
        raise HandoffRoutingError("專家回合尚未完成，不能補上回交。")
    if response.finish_reason not in ("stop", "tool_calls"):
        raise HandoffRoutingError("專家回合未正常完成，不能補上回交。")


def build_completion_return_middleware(primary_name: str):
    """模型先輸出結果，再於原 result hook 回交；保留工具、finalizer 與 hooks。"""
    from agent_framework import (
        ChatResponse, Content, FunctionTool, ResponseStream, chat_middleware,
    )

    expected_name = f"handoff_to_{primary_name}"
    expected_description = _return_description(primary_name)

    @chat_middleware
    async def completion_return(context, call_next):
        if (context.options or {}).get("store") is not False:
            raise HandoffRoutingError("專家回交需要本機模型 history，store 必須為 False。")
        if context.session is not None and context.session.service_session_id:
            raise HandoffRoutingError(
                "既有服務端 conversation 無法安全轉成本機 history，請建立新的 session／conversation。"
            )
        if any((context.options or {}).get(key) for key in ("previous_response_id", "conversation")):
            raise HandoffRoutingError("專家回交不能引用既有服務端 response 或 conversation。")
        candidates = [
            tool for tool in (context.options or {}).get("tools", []) or []
            if isinstance(tool, FunctionTool) and tool.name == expected_name
        ]
        if len(candidates) != 1:
            raise HandoffRoutingError("無法唯一辨識框架的 Primary 回交工具。")
        tool = candidates[0]
        parameters = tool.parameters()
        if (
            tool.description != expected_description
            or tool.approval_mode != "never_require"
            or parameters.get("type") != "object"
            or parameters.get("properties") != {}
            or parameters.get("required", []) != []
        ):
            raise HandoffRoutingError("Primary 回交工具的目標或參數契約不符。")

        def return_control(response: ChatResponse) -> ChatResponse:
            _check_response_complete(response)
            if not response.text.strip():
                raise HandoffRoutingError("專家沒有審查結果或釐清文字，不能只靠控制 call 回交。")
            calls = [
                content for message in response.messages for content in message.contents
                if content.type == "function_call"
            ]
            if calls:
                if (
                    len(calls) != 1 or calls[0].name != tool.name
                    or not calls[0].call_id or calls[0].informational_only
                    or not _empty_arguments(calls[0].arguments)
                ):
                    raise HandoffRoutingError("專家要求的工具或參數不是合法回交。")
                return response
            assistants = [message for message in response.messages if message.role == "assistant"]
            if not assistants:
                raise HandoffRoutingError("專家正常回覆缺少 assistant 訊息。")
            assistants[-1].contents.append(Content.from_function_call(
                call_id=f"handoff_{uuid.uuid4().hex}", name=tool.name, arguments={},
            ))
            return response

        # 保留工具供 SDK 執行程式補的 call，但不讓模型以工具選擇略過 structured output。
        context.options = dict(context.options or {}, tool_choice="none")
        await call_next()
        if context.stream and isinstance(context.result, ResponseStream):
            # 沿用原 stream 的 hooks；控制 call 留在同一份訊息中供本機 history 配對。
            context.stream_result_hooks.insert(0, return_control)
        elif isinstance(context.result, ChatResponse):
            context.result = return_control(context.result)
        else:
            raise HandoffRoutingError("專家 chat 結果型別不符合回交契約。")

    return completion_return


def build_return_guard_middleware(primary_name: str):
    """位於降級處理外層，消費完 stream 後仍核對 SDK 產生的回交結果。"""
    from agent_framework import AgentResponse, AgentResponseUpdate, ResponseStream, agent_middleware

    def verify(response: AgentResponse) -> None:
        _check_response_complete(response)
        if not response.messages:
            raise HandoffRoutingError("專家沒有 SDK 回交結果。")
        results = [
            content for content in response.messages[-1].contents
            if content.type == "function_result"
        ]
        if len(results) != 1 or not results[0].call_id:
            raise HandoffRoutingError("專家沒有唯一的 SDK 回交結果。")
        payload = results[0].result
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                raise HandoffRoutingError("SDK 回交結果格式不符。") from None
        if not isinstance(payload, Mapping) or dict(payload) != {"handoff_to": primary_name}:
            raise HandoffRoutingError("SDK 回交結果未指向 Primary。")

    @agent_middleware
    async def return_guard(context, call_next):
        await call_next()
        if context.stream and isinstance(context.result, ResponseStream):
            inner = context.result

            async def checked_updates() -> AsyncIterator[AgentResponseUpdate]:
                async for update in inner:
                    if not isinstance(update, AgentResponseUpdate):
                        raise HandoffRoutingError("專家 stream 更新型別不符。")
                    _check_response_actions(update)
                    yield update
                verify(await inner.get_final_response())

            async def original_finalizer(_updates: Sequence[AgentResponseUpdate]) -> AgentResponse:
                return await inner.get_final_response()

            context.result = ResponseStream(checked_updates(), finalizer=original_finalizer)
        elif isinstance(context.result, AgentResponse):
            verify(context.result)
        else:
            raise HandoffRoutingError("專家 agent 結果型別不符合回交契約。")

    return return_guard


def build_participants(
    client, roles: dict[str, AgentRole], *, checkpoint_safe: bool = False
) -> "dict[str, Agent]":
    """依定義檔建構四個 Handoff 執行期參與者。

    `require_per_service_call_history_persistence=True` 是**必要**的：漏掉任何一個，
    `HandoffBuilder.build()` 會直接以 `ValueError` 失敗，訊息會列出缺少設定的 agent。
    這個旗標讓本機保存的對話歷史，與服務端在交接工具呼叫短路後的狀態保持一致。

    `response_format` 一律取自定義檔（Primary 為 `None`，三位專家為 `SpecialistReview`）。
    這裡不可另外指定，否則就多出一份會與定義檔分歧的設定。
    只有 specialists 新建的 options 加上 `store=False`，由 SDK 本機 history 保存
    程式補的控制 call；不修改共用 client、角色定義或 Primary 的 storage 選擇。

    來源 M04（microsoft/agent-framework 官方 handoff sample 與原始碼）：
    https://github.com/microsoft/agent-framework
    —— 本旗標的強制性與失敗訊息的內容以該 repository 的實際實作為準。
    """
    participants: dict[str, Agent] = {}
    primary_name = roles["primary"].agent_name
    for key, role in roles.items():
        default_options: dict[str, object] = {}
        if role.response_format is not None:
            default_options["response_format"] = role.response_format
        if key in SPECIALIST_KEYS:
            default_options["store"] = False
        participants[key] = client.as_agent(
            name=role.agent_name,
            description=role.description,
            instructions=(
                role.instructions + _runtime_instructions(primary_name)
                if key in SPECIALIST_KEYS else role.instructions
            ),
            default_options=default_options or None,
            require_per_service_call_history_persistence=True,
            middleware=[
                *([build_return_guard_middleware(primary_name)] if key in SPECIALIST_KEYS else []),
                *([build_checkpoint_safe_middleware()] if checkpoint_safe else []),
                build_degraded_reply_middleware(role.agent_name),
                *([build_completion_return_middleware(primary_name)] if key in SPECIALIST_KEYS else []),
            ],
        )
    return participants


def build_workflow(
    participants: "dict[str, Agent]",
    *,
    name: str = WORKFLOW_NAME,
    checkpoint_storage: "CheckpointStorage | None" = None,
):
    """以 `HandoffBuilder` 建立單層星狀拓撲。

    Args:
        participants: `build_participants()` 產生的四個執行期參與者。
        name: workflow 名稱。DevUI 的實體顯示名稱直接取自它，因此同一個行程要註冊兩個實體時
            必須各自給不同的名稱。
        checkpoint_storage: 建構期的 checkpoint 儲存體。給值才會**真的產生** checkpoint。

    **checkpoint 為什麼一定要在建構期給**（2026-07-28 實測）：
    DevUI 執行 workflow 類別實體時，會替每一段對話配一份 storage 並在 `run()` 時當成
    runtime override 傳入。但只有 runtime override 是**不夠**的——實測第一輪跑完後
    checkpoint 數為 `0`；建構期啟用後才變成 `4`。而 DevUI 的續接分支沒有 checkpoint 就
    直接回錯誤字串，所以漏掉這個參數的症狀不是「慢」，是「一送出就報錯」。

    `add_handoff` 必須明確呼叫。省略的話，每位參與者的可交接目標會變成**全連通（mesh）**，
    專家之間互相可達，「禁止平行分派」的結構性保證就沒了。2026-07-27 實測對照：

        # 有明確 add_handoff
        qvn-coding-agent -> {'qvn-primary-agent'}
        # 省略 add_handoff
        qvn-coding-agent -> {'qvn-architect-agent', 'qvn-primary-agent', 'qvn-spec-agent'}

    **限制落在哪裡**：在 `HandoffAgentExecutor._handoff_targets`——它決定框架替該 agent
    注入哪幾個交接工具。`workflow.edge_groups` 則**無論如何都是全連通**，那只是訊息傳遞的
    底層線路，不代表可交接的對象。驗證拓撲要看 `_handoff_targets`，看 `edge_groups`
    會得到「怎麼都是 mesh」的錯誤結論。

    來源 M03（Microsoft Agent Framework Workflows，協調模式的概念文件）：
    https://learn.microsoft.com/en-us/agent-framework/workflows/
    —— 說明 handoff 與 sequential、concurrent、magentic 的差別；本專案只實作 handoff。
    """
    from agent_framework.orchestrations import HandoffBuilder

    primary = participants["primary"]
    specialists = [participants[key] for key in SPECIALIST_KEYS]

    builder = (
        HandoffBuilder(name=name)
        .participants([primary, *specialists])
        .with_start_agent(primary)
        # Primary → 三位專家
        .add_handoff(primary, specialists)
    )
    # 每位專家只有一條出邊：交回 Primary。
    # 專家之間不建邊，「同時分派給多位專家」因此在圖上不可能發生。
    for specialist in specialists:
        builder = builder.add_handoff(
            specialist, [primary], description=_return_description(primary.name or primary.id)
        )
    if checkpoint_storage is not None:
        builder = builder.with_checkpointing(checkpoint_storage)
    return builder.build()


def _create_client(endpoint: str, model_deployment_name: str, credential):
    """建立 Foundry 聊天用戶端。建構時不發任何網路請求。"""
    from agent_framework.foundry import FoundryChatClient

    return FoundryChatClient(
        project_endpoint=endpoint,
        model=model_deployment_name,
        credential=credential,
    )


def create_workflow(endpoint: str, model_deployment_name: str, credential):
    """一步建立可執行的工作流，給 `src/main.py` 的 Responses 模式使用。

    Args:
        endpoint: Foundry 專案端點。
        model_deployment_name: 模型部署名稱。
        credential: 開發者憑證；不可使用 API key。
    """
    client = _create_client(endpoint, model_deployment_name, credential)
    return build_workflow(build_participants(client, build_agent_roles()))


def create_workflow_factory(endpoint: str, model_deployment_name: str, credential):
    """建立工作流工廠，給 `src/main.py` 的 Responses 模式使用。

    聊天用戶端只建一次、共用；工作流則**每一輪重新建立**，讓不同使用者與不同對話的
    executor 狀態互不干擾，也避免共用實例時撞上「Workflow is already running」。
    建構工作流不發任何網路請求，所以每輪重建的成本很低。
    """
    client = _create_client(endpoint, model_deployment_name, credential)

    def factory():
        return build_workflow(
            build_participants(client, build_agent_roles(), checkpoint_safe=True)
        )

    return factory


def _build_workflow_entity(
    client,
    roles: dict[str, AgentRole],
    *,
    name: str,
    checkpoint_storage: "CheckpointStorage | None",
):
    """建立 DevUI 專用的 workflow 類別實體：在 handoff 拓撲前面加一個字串入口節點。

    **為什麼要多這個節點**：DevUI 會依「起點 executor 宣告接受的型別」決定輸入介面。
    `HandoffAgentExecutor` 的型別清單第一個是 `Message`，DevUI 因此產生一張有
    `role`／`contents`／`author_name`／`message_id`／`additional_properties`／
    `raw_representation` 六個欄位的表單——學員只想貼一段題目，卻要先搞懂這些欄位；
    而且只要在 `additional_properties` 打了字，解析就會失敗並回退成 `dict`，
    最後以 `Executor HandoffAgentExecutor cannot handle message of type <class 'dict'>`
    收場（2026-07-28 實測）。

    改以一個**只接受 `str`** 的入口節點當起點後，DevUI 會產生
    `Input Type: Simple Text` 的單一輸入框，上述錯誤在結構上不可能發生。

    **順帶修好的事**：DevUI 的拓撲圖畫的是工作流宣告的邊。沿用 `HandoffBuilder` 的
    圖時，畫出來的是底層全連通的訊息線路（專家之間也有線），與「專家之間沒有直接邊」
    的教學重點衝突。這裡的邊由本函式**明確宣告**，因此圖上呈現的就是真正的單層星狀。

    交接行為完全沿用 `HandoffBuilder` 建好的參與者，**不重新實作 handoff**：
    交接工具、middleware 與結構化輸出都掛在同一批 executor 物件上。
    """
    from agent_framework import Message, WorkflowBuilder, WorkflowContext, executor

    @executor(id=WORKFLOW_ENTRY_EXECUTOR_ID)
    async def user_input(prompt: str, ctx: WorkflowContext[Message]) -> None:
        """把使用者輸入的純文字包成 `Message`，交給 Primary。"""
        await ctx.send_message(Message("user", [prompt]))

    handoff = build_workflow(
        build_participants(client, roles),
        name=name,
        checkpoint_storage=checkpoint_storage,
    )
    executors = {item.id: item for item in handoff.get_executors_list()}
    primary = executors[roles["primary"].agent_name]

    builder = WorkflowBuilder(
        name=name,
        start_executor=user_input,
        checkpoint_storage=checkpoint_storage,
    ).add_edge(user_input, primary)
    # 只宣告 Primary ↔ 各專家；專家之間不建邊，圖上就看得到真正的拓撲。
    for key in SPECIALIST_KEYS:
        specialist = executors[roles[key].agent_name]
        builder = builder.add_edge(primary, specialist).add_edge(specialist, primary)
    return builder.build()


def create_devui_entities(endpoint: str, model_deployment_name: str, credential):
    """建立 DevUI 要註冊的兩個實體。

    Returns:
        `(agent_entity, workflow_entity)`——前者是「整套工作流包成單一 agent」，
        在 DevUI 上顯示為 **agent** 類別；後者顯示為 **workflow** 類別。
        DevUI 以 duck typing 判定類別（有 `get_executors_list` 或 `executors` 就算 workflow），
        所以不需要任何額外參數。

    **為什麼要建兩份，而不是同一份註冊兩次**（2026-07-28 實測）：
    `Workflow.run()` 有同步的併發防護，同一個實例正在跑時再跑一次會直接拋出

        WorkflowException: Workflow is already running; concurrent runs are not allowed
        on the same instance.

    學員只要在一邊的回覆還在串流時去點另一邊，共用實例就會炸。
    `Workflow.clone()` 也不行——它的實作是 `copy.deepcopy()`，會連同聊天用戶端與憑證
    一起深拷貝；用同一份定義檔重新 build 一次反而更便宜（不發任何網路請求）也更好懂。

    兩個實體共用同一份 `src/agents/` 定義檔，因此四位 agent 的職責、instructions 與
    結構化輸出完全一致。
    """
    from agent_framework import InMemoryCheckpointStorage

    client = _create_client(endpoint, model_deployment_name, credential)
    roles = build_agent_roles()

    agent_entity = build_workflow(build_participants(client, roles)).as_agent(
        name=WORKFLOW_NAME
    )
    workflow_entity = _build_workflow_entity(
        client,
        roles,
        name=WORKFLOW_ENTITY_NAME,
        # 只有 workflow 類別實體需要 checkpoint；agent 類別實體的執行路徑用不到，
        # 不動它才能讓既有行為保持不變。
        checkpoint_storage=InMemoryCheckpointStorage(),
    )
    return agent_entity, workflow_entity
