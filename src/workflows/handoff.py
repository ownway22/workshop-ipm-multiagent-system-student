"""四代理 Handoff 工作流（Lab 2 的核心）。

由 `src/main.py` 匯入；本模組不自行啟動任何伺服器。

## 拓撲（research.md R05）

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

這是 FR-016「複合需求逐次交接、禁止平行分派」的**結構性**保證——不是靠 instructions
拜託模型照做，而是圖上根本不存在「專家 A → 專家 B」的路徑。

## 為什麼參與者要重新建構，而不是沿用持久化 agent

`registry/create_agents.py` 在 Foundry 專案裡建立的是**持久化 agent**（portal 看得到、
可依前置詞清理）。`HandoffBuilder` 需要的則是**執行期參與者**：它會 clone 參與者、
注入交接工具、掛上 middleware，因此要求參與者支援本地工具呼叫。

兩者由同一份定義檔（`src/agents/`）產生，內容一致但物件不同（FR-061）。
`.env` 換一個專案端點，這裡就會連到另一個專案，不需要改任何程式碼。
"""

import asyncio
import logging
from collections.abc import AsyncIterable
from typing import TYPE_CHECKING

from agents import build_agent_roles
from models.agent_role import AgentRole

if TYPE_CHECKING:  # 只為型別標註；執行期不匯入，維持本模組「用到才 import」的一致風格
    from agent_framework import CheckpointStorage

logger = logging.getLogger(__name__)

#: 專家角色的 key（Primary 以外的三個）。順序即 `add_handoff` 的建邊順序。
SPECIALIST_KEYS: tuple[str, ...] = ("coding", "architect", "spec")

#: Workflow 名稱。與 Lab 3 的 hosted agent 名稱一致，方便對照兩者是同一套拓撲。
WORKFLOW_NAME = "qvn-ipm-review"

#: DevUI 上「workflow 類別」實體的顯示名稱（spec 002 的 FR-103）。
#:
#: MUST 與 `WORKFLOW_NAME` 不同：DevUI 的實體名稱直接取自物件的 `.name`，兩者同名時
#: 清單上會出現兩個一模一樣的項目，學員無法分辨自己正在跟哪一個對話。
#: `WORKFLOW_NAME` 本身**不能**改——它同時綁定 `deploy/agent.yaml` 的 `name` 與 azd 的
#: 服務名稱，三者不一致會讓部署後的查詢與呼叫全部 404。
WORKFLOW_ENTITY_NAME = f"{WORKFLOW_NAME}-workflow"

#: workflow 類別實體的入口節點 id。
#:
#: 這個節點只接受 `str`，作用是讓 DevUI 產生**單一文字輸入框**而不是 `Message` 的六欄表單。
#: 詳細理由見 `_build_workflow_entity()` 的說明。
WORKFLOW_ENTRY_EXECUTOR_ID = "user-input"

#: 單一 agent 單次執行的逾時（秒）。
#:
#: FR-021 要求「MUST NOT 崩潰或**長時間無回應**」——後半句只靠 try/except 是達不到的：
#: 服務端沒回應時連線可以掛很久。因此逾時本身就是降級條件之一。
AGENT_TIMEOUT_SECONDS = 180


def _degraded_text(agent_name: str, reason: str, remediation: str) -> str:
    """組出降級回覆的文字（FR-021）。

    刻意寫成「講給使用者聽」而不是拋堆疊：學員在 DevUI 看到的應該是一段看得懂的說明，
    而不是紅色的 traceback。同時 MUST 說清楚**哪一位**專家不可用——否則使用者會誤以為
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


#: HTTP 狀態碼 → `(原因, 修復建議)`。涵蓋 FR-021 明列的三種情境。
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

    分類 MUST 以型別與 HTTP 狀態碼判斷，MUST NOT 比對錯誤訊息字串——訊息會隨服務端改版變動。
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
        messages=[Message("assistant", [_degraded_text(agent_name, reason, remediation)])]
    )


def _degraded_update(agent_name: str, error: BaseException):
    """建立串流用的降級 `AgentResponseUpdate`。"""
    from agent_framework import AgentResponseUpdate, Message

    reason, remediation = _classify(error)
    message = Message("assistant", [_degraded_text(agent_name, reason, remediation)])
    return AgentResponseUpdate(contents=message.contents, role="assistant")


def build_degraded_reply_middleware(agent_name: str):
    """建立單一 agent 的降級 middleware（FR-021、data-model.md 1.4 節狀態轉移）。

    掛上之後，`handed_off(X)` 遇到 X 不可用時會走 `degraded_reply → idle`：
    工作流照常收束、發出 `request_info` 等使用者，而不是整條 workflow 拋例外中斷。

    **為什麼串流與非串流要分開處理**：`await call_next()` 在串流模式下**很快就返回**，
    此時 `context.result` 只是一個尚未消費的 `ResponseStream`，真正的錯誤要等到迭代時才發生
    ——那已經在 middleware 之外了。因此串流路徑 MUST 另外包一層，在迭代時攔截。
    """
    from agent_framework import AgentResponse, ResponseStream, agent_middleware

    def _guard(inner: AsyncIterable[object]) -> AsyncIterable[object]:
        async def generate():
            try:
                async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
                    async for update in inner:
                        yield update
            except Exception as error:  # noqa: BLE001 — 降級的目的就是攔下所有失敗
                logger.warning("%s 不可用，改以降級回覆：%r", agent_name, error)
                yield _degraded_update(agent_name, error)

        return generate()

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
            # MUST NOT 用 `.updates` —— 那是「已收集的更新」串列，不是可迭代來源。
            inner = context.result
            context.result = ResponseStream(
                _guard(inner), finalizer=AgentResponse.from_updates
            )

    return degraded_reply


def build_participants(client, roles: dict[str, AgentRole]) -> dict[str, object]:
    """依定義檔建構四個 Handoff 執行期參與者（FR-061）。

    `require_per_service_call_history_persistence=True` 是**必要**的：漏掉任何一個，
    `HandoffBuilder.build()` 會直接以 `ValueError` 失敗，訊息會列出缺少設定的 agent
    （research.md R04 的實測證據）。這個旗標讓本機保存的對話歷史，與服務端在交接工具呼叫
    短路後的狀態保持一致。

    `response_format` 一律取自定義檔（Primary 為 `None`，三位專家為 `SpecialistReview`）。
    這裡 MUST NOT 另外指定，否則就多出一份會與定義檔分歧的設定。

    來源 M04（microsoft/agent-framework 官方 handoff sample 與原始碼）：
    https://github.com/microsoft/agent-framework
    —— 本旗標的強制性與失敗訊息的內容以該 repository 的實際實作為準。
    """
    participants: dict[str, object] = {}
    for key, role in roles.items():
        default_options: dict[str, object] = {}
        if role.response_format is not None:
            default_options["response_format"] = role.response_format
        participants[key] = client.as_agent(
            name=role.agent_name,
            description=role.description,
            instructions=role.instructions,
            default_options=default_options or None,
            require_per_service_call_history_persistence=True,
            middleware=[build_degraded_reply_middleware(role.agent_name)],
        )
    return participants


def build_workflow(
    participants: dict[str, object],
    *,
    name: str = WORKFLOW_NAME,
    checkpoint_storage: "CheckpointStorage | None" = None,
):
    """以 `HandoffBuilder` 建立單層星狀拓撲（FR-012、FR-016、FR-017）。

    Args:
        participants: `build_participants()` 產生的四個執行期參與者。
        name: workflow 名稱。DevUI 的實體顯示名稱直接取自它，因此同一個行程要註冊兩個實體時
            MUST 各自給不同的名稱（spec 002 的 FR-103）。
        checkpoint_storage: 建構期的 checkpoint 儲存體。給值才會**真的產生** checkpoint。

    **checkpoint 為什麼一定要在建構期給**（spec 002 research.md R04 實測）：
    DevUI 執行 workflow 類別實體時，會替每一段對話配一份 storage 並在 `run()` 時當成
    runtime override 傳入。但只有 runtime override 是**不夠**的——實測第一輪跑完後
    checkpoint 數為 `0`；建構期啟用後才變成 `4`。而 DevUI 的續接分支沒有 checkpoint 就
    直接回錯誤字串，所以漏掉這個參數的症狀不是「慢」，是「一送出就報錯」。

    `add_handoff` MUST 明確呼叫。省略的話每位參與者的可交接目標會變成**全連通（mesh）**，
    專家之間互相可達，FR-016 的結構性保證就沒了。2026-07-27 實測對照：

        # 有明確 add_handoff
        qvn-coding-agent -> {'qvn-primary-agent'}
        # 省略 add_handoff
        qvn-coding-agent -> {'qvn-architect-agent', 'qvn-primary-agent', 'qvn-spec-agent'}

    **限制落在哪裡**：在 `HandoffAgentExecutor._handoff_targets`——它決定框架替該 agent
    注入哪幾個交接工具。`workflow.edge_groups` 則**無論如何都是全連通**，那只是訊息傳遞的
    底層線路，不代表可交接的對象。驗證拓撲 MUST 看 `_handoff_targets`，看 `edge_groups`
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
        # Primary → 三位專家（FR-013）
        .add_handoff(primary, specialists)
    )
    # 每位專家只有一條出邊：交回 Primary（FR-017）。
    # 專家之間不建邊，「同時分派給多位專家」因此在圖上不可能發生（FR-016）。
    for specialist in specialists:
        builder = builder.add_handoff(specialist, [primary])
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
        credential: 開發者憑證（FR-005；MUST NOT 使用 API key）。
    """
    client = _create_client(endpoint, model_deployment_name, credential)
    return build_workflow(build_participants(client, build_agent_roles()))


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
    """建立 DevUI 要註冊的兩個實體（spec 002 的 FR-101、FR-102、FR-106）。

    Returns:
        `(agent_entity, workflow_entity)`——前者是「整套工作流包成單一 agent」，
        在 DevUI 上顯示為 **agent** 類別；後者顯示為 **workflow** 類別。
        DevUI 以 duck typing 判定類別（有 `get_executors_list` 或 `executors` 就算 workflow），
        所以不需要任何額外參數。

    **為什麼要建兩份，而不是同一份註冊兩次**（research.md R03 實測）：
    `Workflow.run()` 有同步的併發防護，同一個實例正在跑時再跑一次會直接拋出

        WorkflowException: Workflow is already running; concurrent runs are not allowed
        on the same instance.

    學員只要在一邊的回覆還在串流時去點另一邊，共用實例就會炸。
    `Workflow.clone()` 也不行——它的實作是 `copy.deepcopy()`，會連同聊天用戶端與憑證
    一起深拷貝；用同一份定義檔重新 build 一次反而更便宜（不發任何網路請求）也更好懂。

    兩個實體共用同一份 `src/agents/` 定義檔（FR-102、延續 spec 001 的 FR-061），
    因此四位 agent 的職責、instructions 與結構化輸出完全一致。
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
        # 不動它才能讓「既有行為不變」（FR-107）保持成立。
        checkpoint_storage=InMemoryCheckpointStorage(),
    )
    return agent_entity, workflow_entity
