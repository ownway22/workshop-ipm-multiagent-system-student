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
Handoff runtime 另附 outcome、需求進度與內部控制規則；Lab 1 的角色文字不變。
`.env` 換一個專案端點，這裡就會連到另一個專案，不需要改任何程式碼。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agents import build_agent_roles
from models.agent_role import AgentRole
from models.specialist_review import ReviewCategory, SpecialistReview

if TYPE_CHECKING:  # 只為型別標註；執行期不匯入，維持本模組「用到才 import」的一致風格
    from agent_framework import (
        Agent, AgentResponse, AgentResponseUpdate, AgentSession, ChatResponse, CheckpointStorage, Content,
        Message, ResponseStream, WorkflowCheckpoint,
    )

logger = logging.getLogger(__name__)

_DEGRADED_RESULT = "handoff_degraded"


class HandoffRoutingError(RuntimeError):
    """專家回合沒有完成合法回交；不能當成正常審查或使用者等待。"""


SpecialistKey = Literal["coding", "architect", "spec"]
_LEDGER_KEY = "handoff.requirements.v1"
_LEDGER_MARKER = "handoff.requirements_initialized"
_DISPATCH_KEY = "handoff.dispatch.v1"
_RECEIPT_KEY = "handoff.outcome_receipt.v1"
_ACCEPT_PLAN = "accept_review_plan"
_RESUME_PLAN = "resume_review_plan"
_CATEGORIES = {
    "coding": ReviewCategory.CODE,
    "architect": ReviewCategory.ARCHITECTURE,
    "spec": ReviewCategory.SPECIFICATION,
}


class SpecialistOutcome(BaseModel):
    """Handoff-only 結果；控制 call 本身不是審查結果。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["review", "needs_input", "unavailable"]
    review: SpecialistReview | None = None
    detail: str | None = None
    question: str | None = None

    @model_validator(mode="after")
    def valid_outcome(self) -> Self:
        if self.kind == "review":
            if self.review is None or self.detail is not None or self.question is not None:
                raise ValueError("review requires only a validated SpecialistReview")
        else:
            if self.review is not None or not self.detail or not self.detail.strip():
                raise ValueError("blocked outcomes require a reason, not invented findings")
            if self.kind == "needs_input":
                if not self.question or not self.question.strip():
                    raise ValueError("needs_input requires one clarification question")
            elif self.question is not None:
                raise ValueError("unavailable is a reason, not a clarification question")
        return self


class ReviewPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    roles: list[SpecialistKey] = Field(min_length=1, max_length=3)
    replace: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def valid_plan(self) -> Self:
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("a finite plan cannot repeat a role")
        if self.replace and (not self.reason or not self.reason.strip()):
            raise ValueError("replacing unfinished work requires an explicit reason")
        if not self.replace and self.reason is not None:
            raise ValueError("replacement reasons belong to replacement revisions")
        return self


class HandoffDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(min_length=1)
    step: int = Field(ge=0, le=2)
    role: SpecialistKey
    attempt: int = Field(ge=1, le=2)
    dispatch_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)


class OutcomeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatch: HandoffDispatch
    source: str = Field(min_length=1)
    handoff_call_id: str = Field(min_length=1)
    outcome: SpecialistOutcome


class ReviewTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: SpecialistKey
    status: Literal["pending", "running", "reviewed", "needs_input", "unavailable"] = "pending"
    attempts: int = Field(default=0, ge=0, le=2)
    dispatch: HandoffDispatch | None = None
    receipt_id: str | None = None
    outcome: SpecialistOutcome | None = None


class PlanRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    number: int = Field(ge=1)
    accepted_turn: int = Field(ge=1)
    retired_turn: int | None = Field(default=None, ge=1)
    replacement_reason: str | None = None
    tasks: list[ReviewTask] = Field(min_length=1, max_length=3)
    receipts: dict[str, OutcomeReceipt] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_progress(self) -> Self:
        ReviewPlan(roles=[task.role for task in self.tasks])
        unfinished = False
        for index, task in enumerate(self.tasks):
            if unfinished and task.status != "pending":
                raise ValueError("later tasks cannot run before the current task finishes")
            unfinished = unfinished or task.status != "reviewed"
            if task.status == "pending":
                if task.dispatch is not None or task.outcome is not None or task.receipt_id is not None:
                    raise ValueError("pending tasks cannot claim completed work")
                continue
            dispatch = task.dispatch
            if (
                dispatch is None or dispatch.revision_id != self.id or dispatch.step != index
                or dispatch.role != task.role or dispatch.attempt != task.attempts
            ):
                raise ValueError("task progress requires the matching dispatched attempt")
            if task.status == "running":
                if task.outcome is not None or task.receipt_id is not None:
                    raise ValueError("running tasks cannot claim a result")
                continue
            receipt = self.receipts.get(task.receipt_id or "")
            expected = "review" if task.status == "reviewed" else task.status
            if (
                receipt is None or receipt.dispatch != dispatch or receipt.outcome != task.outcome
                or receipt.outcome.kind != expected
            ):
                raise ValueError("completed or blocked tasks require their executed outcome receipt")
        dispatches: set[str] = set()
        for call_id, receipt in self.receipts.items():
            dispatch = receipt.dispatch
            if (
                call_id != receipt.handoff_call_id or dispatch.revision_id != self.id
                or dispatch.step >= len(self.tasks) or self.tasks[dispatch.step].role != dispatch.role
                or dispatch.attempt > self.tasks[dispatch.step].attempts
                or dispatch.dispatch_id in dispatches
            ):
                raise ValueError("receipt is not a unique executed step in this revision")
            if receipt.outcome.review is not None and receipt.outcome.review.category != _CATEGORIES[dispatch.role]:
                raise ValueError("receipt category disagrees with the executed role")
            dispatches.add(dispatch.dispatch_id)
        for index, task in enumerate(self.tasks):
            attempts = sorted(
                receipt.dispatch.attempt for receipt in self.receipts.values()
                if receipt.dispatch.step == index
            )
            completed_attempts = task.attempts - (task.status == "running")
            if attempts != list(range(1, completed_attempts + 1)):
                raise ValueError("task attempts do not match executed outcome receipts")
        return self

    def next_task(self) -> ReviewTask | None:
        return next((task for task in self.tasks if task.status != "reviewed"), None)


class ReviewTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    id: str = Field(min_length=1)
    decision: Literal["undecided", "plan", "resume", "recap", "clarify", "out_of_scope"] = "undecided"
    model_calls: int = Field(default=0, ge=0, le=8)
    closed: bool = False


class ReviewLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    turn: ReviewTurn
    revisions: list[PlanRevision] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_revisions(self) -> Self:
        identifiers: set[str] = set()
        calls: set[str] = set()
        for index, revision in enumerate(self.revisions):
            if (
                revision.number != index + 1 or revision.id in identifiers
                or revision.accepted_turn > self.turn.number
                or revision.retired_turn is not None and revision.retired_turn > self.turn.number
                or revision.retired_turn is not None and revision.retired_turn <= revision.accepted_turn
                or index < len(self.revisions) - 1 and revision.retired_turn is None
                or index == len(self.revisions) - 1 and revision.retired_turn is not None
            ):
                raise ValueError("revision chronology is inconsistent")
            if calls.intersection(revision.receipts):
                raise ValueError("handoff receipt cannot complete multiple revisions")
            identifiers.add(revision.id)
            calls.update(revision.receipts)
        return self

    def current(self) -> PlanRevision | None:
        return self.revisions[-1] if self.revisions else None

    def progress(self) -> tuple[list[SpecialistKey], list[SpecialistKey]]:
        revision = self.current()
        tasks = revision.tasks if revision is not None else []
        return (
            [task.role for task in tasks if task.status == "reviewed"],
            [task.role for task in tasks if task.status != "reviewed"],
        )


class PrimaryReply(BaseModel):
    """結束 disposition 與進度必須符合 runtime 的已驗證狀態。"""

    model_config = ConfigDict(extra="forbid")

    disposition: Literal["complete", "recap", "clarify", "needs_input", "unavailable", "out_of_scope"]
    message: str = Field(min_length=1)
    question: str | None = None
    reviewed: list[SpecialistKey] = Field(default_factory=list)
    unfinished: list[SpecialistKey] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonempty_reply(self) -> Self:
        if not self.message.strip():
            raise ValueError("empty output is not completion")
        if self.disposition in ("clarify", "needs_input"):
            if not self.question or not self.question.strip():
                raise ValueError("clarification requires one question")
        elif self.question is not None:
            raise ValueError("this disposition does not ask for user input")
        return self


def load_review_ledger(session: "AgentSession") -> ReviewLedger | None:
    if _LEDGER_KEY not in session.state:
        if session.state:
            raise HandoffRoutingError("既有 Handoff 狀態缺少需求 ledger，不能安全重置。")
        return None
    if session.state.get(_LEDGER_MARKER) is not True:
        raise HandoffRoutingError("Handoff ledger 初始化標記不符。")
    try:
        return ReviewLedger.model_validate_json(
            json.dumps(session.state[_LEDGER_KEY], allow_nan=False), strict=True,
        )
    except (ValidationError, TypeError, ValueError):
        raise HandoffRoutingError("Handoff 需求 ledger 損壞或矛盾。") from None


def build_review_checkpoint_validator(
    roles: dict[str, AgentRole] | None = None,
) -> "Callable[[WorkflowCheckpoint], None]":
    """在 SDK 套用 session 缺省值之前，驗證已存在 checkpoint 的 ledger owner。"""
    from agent_framework import AgentSession

    selected_roles = roles if roles is not None else build_agent_roles()
    primary_name = selected_roles["primary"].agent_name

    def validate(checkpoint: "WorkflowCheckpoint") -> None:
        executor_states = checkpoint.state.get("_executor_state")
        owner = executor_states.get(primary_name) if isinstance(executor_states, dict) else None
        payload = owner.get("agent_session") if isinstance(owner, dict) else None
        if (
            not isinstance(payload, dict) or payload.get("type") != "session"
            or not isinstance(payload.get("session_id"), str) or not payload["session_id"].strip()
            or "service_session_id" not in payload or payload["service_session_id"] is not None
            or not isinstance(payload.get("state"), dict) or _LEDGER_KEY not in payload["state"]
        ):
            raise HandoffRoutingError("既有 checkpoint 缺少有效的 Primary owner session/state/ledger。")
        session = AgentSession.from_dict(payload)
        ledger = load_review_ledger(session)
        if ledger is None or not ledger.turn.closed:
            raise HandoffRoutingError("既有 checkpoint 的 Primary ledger 不是已結束的使用者回合。")
        revision = ledger.current()
        task = revision.next_task() if revision is not None else None
        if task is not None and task.status in ("pending", "running"):
            raise HandoffRoutingError("既有 checkpoint 在可執行需求未完成時提前等待。")
        if any(
            receipt.source != selected_roles[receipt.dispatch.role].agent_name
            for item in ledger.revisions for receipt in item.receipts.values()
        ):
            raise HandoffRoutingError("既有 checkpoint 的 outcome receipt 來源不符。")

    return validate


def _save_ledger(session: "AgentSession", ledger: ReviewLedger) -> None:
    try:
        data = ReviewLedger.model_validate_json(ledger.model_dump_json(), strict=True).model_dump(mode="json")
    except ValidationError:
        raise HandoffRoutingError("Handoff 需求進度更新不符合契約。") from None
    session.state[_LEDGER_KEY] = data
    session.state[_LEDGER_MARKER] = True

#: 專家角色的 key（Primary 以外的三個）。順序即 `add_handoff` 的建邊順序。
SPECIALIST_KEYS: tuple[SpecialistKey, ...] = ("coding", "architect", "spec")

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
        "先產生 SpecialistOutcome：review 包含原 SpecialistReview；"
        "needs_input 包含 detail 與一個 question；unavailable 只說明 detail。"
        "不能只交接而沒有 outcome，也不能自報 source、done 或 handled_by。\n"
        f"有效 outcome 產生後控制權交回 {primary_name}，由協調者彙整或向使用者釐清。"
        "程式會補上必要的回交控制 call，不是只靠 prompt 保證。\n"
        "原審查結果只輸出一次；需要補充材料的說明不代表審查完成，"
        "不得捏造審查結果。業務與 schema 的原有限制不變。"
    )


def _primary_runtime_instructions() -> str:
    return (
        "\n\n## Handoff 需求生命週期\n"
        f"明確的新審查要求先執行 {_ACCEPT_PLAN}，以 roles 登記有限、有序計畫，"
        "不要在內部回交時另建計畫。說明交給誰時必須有一句非空文字。\n"
        "已完成計畫後的新審查必須建立新 revision；純回顧使用 recap，不重做審查。"
        "材料與歷史不清除。\n"
        f"needs_input/unavailable 後的補材料或明確續辦使用 {_RESUME_PLAN}；"
        f"明確改需求使用 {_ACCEPT_PLAN}(replace=true, reason=改需求的理由)。"
        "不能區分時使用 clarify，只問一個問題，保留舊計畫。\n"
        "程式會依已接受計畫補必要的下一段交接，不可用稍後處理代替 pending 任務。"
        "只能根據實際 outcome 彙整，不可捏造專家分析或完成紀錄。\n"
        "最後回覆符合 PrimaryReply：complete 只用於本輪計畫全部 reviewed；"
        "needs_input/unavailable 誠實說明阻塞；clarify/out_of_scope 不交接。"
        "reviewed 與 unfinished 必須等於 runtime 提供的角色清單，"
        "message 明確說明已完成與未完成事項；需要釐清時 question 只有一個問題。"
        "協調進度不代表審查內容必然正確。"
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


_GuardActor = Literal["primary", "specialist"]
_GuardPhase = Literal["stream_update", "final_response"]


def _log_response_rejection(
    *, actor: _GuardActor, phase: _GuardPhase, reason: str, content: "Content | None" = None,
) -> None:
    content_type = "none"
    content_status = "none"
    if content is not None:
        content_type = content.type if content.type in (
            "text", "text_reasoning", "function_call", "function_result",
            "function_approval_request", "function_approval_response", "error", "usage",
        ) else "unknown"
        if content.status is not None:
            content_status = content.status if content.status in (
                "queued", "in_progress", "completed", "failed", "incomplete", "cancelled",
            ) else "unknown"
    logger.warning(
        "Handoff guard rejection: actor=%s phase=%s reason=%s content_type=%s content_status=%s "
        "has_exception=%s has_error_code=%s user_input_request=%s",
        actor, phase, reason, content_type, content_status,
        bool(content.exception) if content is not None else False,
        bool(content.error_code) if content is not None else False,
        bool(content.user_input_request) if content is not None else False,
    )


def _check_response_actions(
    response: "ChatResponse | AgentResponse | AgentResponseUpdate", *,
    actor: _GuardActor, phase: _GuardPhase,
) -> None:
    from agent_framework import AgentResponseUpdate

    if response.finish_reason == "cancelled":
        _log_response_rejection(actor=actor, phase=phase, reason="cancelled")
        raise asyncio.CancelledError()
    if (response.additional_properties or {}).get(_DEGRADED_RESULT):
        _log_response_rejection(actor=actor, phase=phase, reason="degraded")
        raise HandoffRoutingError("專家已降級，不能當成正常回交。")
    if response.finish_reason in (
        "length", "content_filter", "error", "incomplete", "failed",
    ):
        _log_response_rejection(actor=actor, phase=phase, reason=f"finish_{response.finish_reason}")
        raise HandoffRoutingError("專家回合未正常完成，不能補上回交。")
    messages = [response] if isinstance(response, AgentResponseUpdate) else response.messages
    for message in messages:
        for content in message.contents:
            if (
                content.user_input_request or content.type == "error"
                or content.exception or content.error_code or content.status in ("failed", "incomplete")
            ):
                if content.user_input_request:
                    reason = "user_input_request"
                elif content.type == "error":
                    reason = "error_content"
                elif content.exception:
                    reason = "content_exception"
                elif content.error_code:
                    reason = "content_error_code"
                elif content.status == "failed":
                    reason = "content_failed"
                else:
                    reason = "content_incomplete"
                _log_response_rejection(actor=actor, phase=phase, reason=reason, content=content)
                raise HandoffRoutingError("專家有錯誤或未完成的使用者動作。")


def _check_response_complete(
    response: "ChatResponse | AgentResponse", *, actor: _GuardActor,
) -> None:
    _check_response_actions(response, actor=actor, phase="final_response")
    if response.continuation_token is not None:
        _log_response_rejection(actor=actor, phase="final_response", reason="pending_continuation")
        raise HandoffRoutingError("專家回合尚未完成，不能補上回交。")
    if response.finish_reason not in ("stop", "tool_calls"):
        _log_response_rejection(
            actor=actor, phase="final_response",
            reason="missing_finish_reason" if response.finish_reason is None else "unsupported_finish_reason",
        )
        raise HandoffRoutingError("專家回合未正常完成，不能補上回交。")


def _require_local_history(session: "AgentSession | None", options: Mapping | None) -> None:
    if session is None:
        raise HandoffRoutingError("Handoff runtime 需要公開 SDK session。")
    if session.service_session_id:
        raise HandoffRoutingError("既有服務端 history 不能安全遷移，請建立新的 session/conversation。")
    if (options or {}).get("store") is not False:
        raise HandoffRoutingError("Handoff runtime 的模型 history 必須使用 store=False。")
    if any((options or {}).get(key) for key in ("previous_response_id", "conversation")):
        raise HandoffRoutingError("Handoff runtime 不能引用服務端 response/conversation。")


def _outcome(response: "ChatResponse | AgentResponse", role: SpecialistKey) -> SpecialistOutcome:
    try:
        outcome = SpecialistOutcome.model_validate_json(response.text, strict=True)
    except ValidationError:
        raise HandoffRoutingError("專家缺少有效 review/needs_input/unavailable outcome。") from None
    if outcome.review is not None and outcome.review.category != _CATEGORIES[role]:
        raise HandoffRoutingError("專家 review 的 category 與實際執行角色不符。")
    return outcome


def _handoff_result(response: "AgentResponse") -> tuple[str, str]:
    results = [
        content for content in response.messages[-1].contents
        if content.type == "function_result"
    ] if response.messages else []
    if len(results) != 1 or not results[0].call_id:
        raise HandoffRoutingError("沒有唯一、有效的 SDK handoff result。")
    payload = results[0].result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            raise HandoffRoutingError("SDK handoff result 格式不符。") from None
    if (
        not isinstance(payload, Mapping) or set(payload) != {"handoff_to"}
        or not isinstance(payload["handoff_to"], str) or not payload["handoff_to"]
    ):
        raise HandoffRoutingError("SDK handoff result 沒有合法目標。")
    return results[0].call_id, payload["handoff_to"]


def _assistant_message(response: "ChatResponse | AgentResponse") -> "Message":
    message = next((
        message for message in reversed(response.messages)
        if message.role == "assistant" and message.text.strip()
    ), None)
    if message is None:
        raise HandoffRoutingError("Handoff 控制需要非空 assistant 訊息。")
    return message


def _read_dispatch(messages: Sequence["Message"], role: SpecialistKey, primary_name: str) -> HandoffDispatch:
    dispatches = []
    for message in messages:
        payload = (message.additional_properties or {}).get(_DISPATCH_KEY)
        if payload is None:
            continue
        if message.role != "assistant" or message.author_name != primary_name:
            raise HandoffRoutingError("Handoff dispatch 來源不是 runtime Primary。")
        try:
            dispatch = HandoffDispatch.model_validate(payload, strict=True)
        except ValidationError:
            raise HandoffRoutingError("Handoff dispatch metadata 不符合契約。") from None
        if dispatch.role == role:
            dispatches.append(dispatch)
    if len(dispatches) != 1:
        raise HandoffRoutingError("專家沒有唯一的當前 plan/step dispatch。")
    return dispatches[0]


def _consume_receipts(ledger: ReviewLedger, messages: Sequence["Message"], roles: dict[str, AgentRole]) -> None:
    for message in messages:
        payload = (message.additional_properties or {}).get(_RECEIPT_KEY)
        if payload is None:
            continue
        try:
            receipt = OutcomeReceipt.model_validate_json(
                json.dumps(payload, allow_nan=False), strict=True,
            )
        except (ValidationError, TypeError, ValueError):
            raise HandoffRoutingError("Handoff outcome receipt 不符合契約。") from None
        dispatch = receipt.dispatch
        expected_source = roles[dispatch.role].agent_name
        if (
            message.role != "assistant" or message.author_name != expected_source
            or receipt.source != expected_source
        ):
            raise HandoffRoutingError("Outcome receipt 不是實際專家產生的 metadata。")
        revision = next((item for item in ledger.revisions if item.id == dispatch.revision_id), None)
        if revision is None:
            raise HandoffRoutingError("Outcome receipt 不屬於已接受 revision。")
        known = revision.receipts.get(receipt.handoff_call_id)
        if known is not None:
            if known != receipt:
                raise HandoffRoutingError("已驗證 receipt 被改寫。")
            continue
        if revision is not ledger.current() or dispatch.step >= len(revision.tasks):
            raise HandoffRoutingError("舊 revision 的未知 outcome 不能更新新需求。")
        task = revision.next_task()
        if task is None or task.status != "running" or task.dispatch != dispatch:
            raise HandoffRoutingError("Outcome receipt 與當前執行步驟不符。")
        task.status = "reviewed" if receipt.outcome.kind == "review" else receipt.outcome.kind
        task.outcome = receipt.outcome
        task.receipt_id = receipt.handoff_call_id
        revision.receipts[receipt.handoff_call_id] = receipt


def build_primary_guard_middleware(roles: dict[str, AgentRole]):
    from agent_framework import AgentResponse, AgentResponseUpdate, ResponseStream, agent_middleware

    @agent_middleware
    async def primary_guard(context, call_next):
        session = context.session
        if session is None or session.service_session_id:
            raise HandoffRoutingError("Primary 需要本機 SDK session，不能清除舊服務端 ID。")
        ledger = load_review_ledger(session)
        user_turn = any(message.role == "user" for message in context.messages)
        if ledger is None:
            if not user_turn:
                raise HandoffRoutingError("內部回交缺少 Primary 需求 ledger。")
            ledger = ReviewLedger(turn=ReviewTurn(number=1, id=uuid.uuid4().hex))
        else:
            if any(
                receipt.source != roles[receipt.dispatch.role].agent_name
                for revision in ledger.revisions for receipt in revision.receipts.values()
            ):
                raise HandoffRoutingError("已保存 outcome receipt 的來源與執行角色不符。")
            _consume_receipts(ledger, context.messages, roles)
            if user_turn:
                revision = ledger.current()
                task = revision.next_task() if revision is not None else None
                if not ledger.turn.closed or task is not None and task.status in ("pending", "running"):
                    raise HandoffRoutingError("未結束的可執行需求不能被新 user turn 默默覆蓋。")
                # 每次真正的 user invocation 都有新 ID；相同本文也不合併成同一 turn。
                ledger.turn = ReviewTurn(number=ledger.turn.number + 1, id=uuid.uuid4().hex)
            elif ledger.turn.closed:
                raise HandoffRoutingError("已結束的 user turn 不能接受額外內部回合。")
        _save_ledger(session, ledger)

        def verify(response: AgentResponse) -> None:
            _check_response_complete(response, actor="primary")
            current = load_review_ledger(session)
            if current is None:
                raise HandoffRoutingError("Primary 執行後 ledger 遺失。")
            revision = current.current()
            task = revision.next_task() if revision is not None else None
            last = response.messages[-1].contents if response.messages else []
            if any(content.type == "function_result" for content in last):
                call_id, target = _handoff_result(response)
                if (
                    task is None or task.status != "running" or task.dispatch is None
                    or target != roles[task.role].agent_name or call_id != task.dispatch.call_id
                ):
                    raise HandoffRoutingError("Primary SDK handoff result 與當前 step 不符。")
                message = _assistant_message(response)
                message.additional_properties = {
                    **(message.additional_properties or {}),
                    _DISPATCH_KEY: task.dispatch.model_dump(mode="json"),
                }
                return
            try:
                answer = PrimaryReply.model_validate_json(_assistant_message(response).text, strict=True)
            except ValidationError:
                raise HandoffRoutingError("Primary 缺少有效 final disposition。") from None
            reviewed, unfinished = current.progress()
            if answer.reviewed != reviewed or answer.unfinished != unfinished:
                raise HandoffRoutingError("Primary 最終完成/未完成清單與已驗證進度不符。")
            if current.turn.decision == "undecided":
                if answer.disposition == "recap":
                    if revision is None or task is not None:
                        raise HandoffRoutingError("未完成計畫不能以 recap 冒充結案。")
                elif answer.disposition not in ("clarify", "out_of_scope"):
                    raise HandoffRoutingError("新的審查要求必須先 planning 或有效 resume。")
                current.turn.decision = answer.disposition
            else:
                expected = task.status if task is not None else "complete"
                if expected not in ("complete", "needs_input", "unavailable") or answer.disposition != expected:
                    raise HandoffRoutingError("Primary 不能以稍後處理結束未完成需求。")
            current.turn.closed = True
            _save_ledger(session, current)
            response.additional_properties["handoff.disposition"] = answer.disposition

        await call_next()
        if context.stream and isinstance(context.result, ResponseStream):
            inner = context.result

            async def checked_updates() -> AsyncIterator[AgentResponseUpdate]:
                async for update in inner:
                    _check_response_actions(update, actor="primary", phase="stream_update")
                    yield update
                verify(await inner.get_final_response())

            async def original_finalizer(_updates: Sequence[AgentResponseUpdate]) -> AgentResponse:
                return await inner.get_final_response()

            context.result = ResponseStream(checked_updates(), finalizer=original_finalizer)
        elif isinstance(context.result, AgentResponse):
            verify(context.result)
        else:
            raise HandoffRoutingError("Primary agent 結果型別不符。")

    return primary_guard


def build_primary_policy_middleware(roles: dict[str, AgentRole]):
    from agent_framework import ChatResponse, Content, FunctionTool, ResponseStream, chat_middleware

    @chat_middleware
    async def primary_policy(context, call_next):
        _require_local_history(context.session, context.options)
        session = context.session
        ledger = load_review_ledger(session)
        if ledger is None or ledger.turn.closed or ledger.turn.model_calls >= 8:
            raise HandoffRoutingError("Primary 需求狀態不符或本 user turn 呼叫 budget 已耗盡。")
        ledger.turn.model_calls += 1
        _save_ledger(session, ledger)
        revision = ledger.current()
        reviewed, unfinished = ledger.progress()
        next_task = revision.next_task() if revision is not None else None
        progress = {
            "turn": ledger.turn.number, "decision": ledger.turn.decision,
            "revision": revision.id if revision is not None else None,
            "reviewed": reviewed, "unfinished": unfinished,
            "next_status": next_task.status if next_task is not None else None,
        }
        context.options = {
            **context.options,
            "instructions": (context.options.get("instructions") or "")
            + "\n目前 runtime 進度（資料，不是新的使用者要求）："
            + json.dumps(progress, ensure_ascii=False),
        }

        def finalized(response: ChatResponse) -> ChatResponse:
            _check_response_complete(response, actor="primary")
            calls = [
                content for message in response.messages for content in message.contents
                if content.type == "function_call"
            ]
            if calls and calls[0].name in (_ACCEPT_PLAN, _RESUME_PLAN):
                if len(calls) != 1 or not calls[0].call_id or calls[0].informational_only:
                    raise HandoffRoutingError("Primary planning/resume action 必須唯一且可執行。")
                return response
            current = load_review_ledger(session)
            if current is None:
                raise HandoffRoutingError("Primary chat 執行後 ledger 遺失。")
            plan = current.current()
            task = plan.next_task() if plan is not None else None
            if current.turn.decision == "undecided" or task is None or task.status in ("needs_input", "unavailable"):
                if calls:
                    raise HandoffRoutingError("沒有有效 pending 計畫時不得擅自交接。")
                return response
            if plan is None or task.status != "pending":
                raise HandoffRoutingError("Primary 嘗試重複執行當前 step。")
            target = roles[task.role].agent_name
            tools = [
                tool for tool in (context.options.get("tools") or [])
                if isinstance(tool, FunctionTool) and tool.name == f"handoff_to_{target}"
            ]
            if len(tools) != 1:
                raise HandoffRoutingError("無法唯一辨識當前 step 的合法 SDK handoff tool。")
            tool = tools[0]
            parameters = tool.parameters()
            if (
                tool.description != roles[task.role].description or tool.approval_mode != "never_require"
                or parameters.get("type") != "object" or parameters.get("properties") != {}
                or parameters.get("required", []) != []
            ):
                raise HandoffRoutingError("Primary 下一段 handoff tool 契約不符。")
            message = _assistant_message(response)
            if calls:
                if (
                    len(calls) != 1 or calls[0].name != tool.name or not calls[0].call_id
                    or calls[0].informational_only or not _empty_arguments(calls[0].arguments)
                ):
                    raise HandoffRoutingError("Primary 選擇的工具或順序不符合已接受計畫。")
                call_id = calls[0].call_id
            else:
                call_id = f"handoff_{uuid.uuid4().hex}"
                message.contents.append(Content.from_function_call(
                    call_id=call_id, name=tool.name, arguments={},
                ))
            task.attempts += 1
            task.status = "running"
            task.dispatch = HandoffDispatch(
                revision_id=plan.id, step=plan.tasks.index(task), role=task.role,
                attempt=task.attempts, dispatch_id=uuid.uuid4().hex, call_id=call_id,
            )
            _save_ledger(session, current)
            return response

        await call_next()
        if context.stream and isinstance(context.result, ResponseStream):
            context.stream_result_hooks.insert(0, finalized)
        elif isinstance(context.result, ChatResponse):
            context.result = finalized(context.result)
        else:
            raise HandoffRoutingError("Primary chat 結果型別不符。")

    return primary_policy


def build_plan_action_middleware():
    from agent_framework import function_middleware

    @function_middleware
    async def plan_action(context, call_next):
        if context.function.name not in (_ACCEPT_PLAN, _RESUME_PLAN):
            await call_next()
            return
        session = context.session
        if session is None:
            raise HandoffRoutingError("Planning/resume action 缺少 Primary session。")
        ledger = load_review_ledger(session)
        if ledger is None or ledger.turn.closed or ledger.turn.decision != "undecided":
            raise HandoffRoutingError("每個新 user turn 只能接受一次 planning/resume 決策。")
        revision = ledger.current()
        task = revision.next_task() if revision is not None else None
        if context.function.name == _ACCEPT_PLAN:
            try:
                arguments = context.arguments
                plan = ReviewPlan.model_validate(
                    arguments.model_dump() if isinstance(arguments, BaseModel) else arguments, strict=True,
                )
            except ValidationError:
                raise HandoffRoutingError("Planning action 的有限需求計畫無效。") from None
            if task is not None and (task.status not in ("needs_input", "unavailable") or not plan.replace):
                raise HandoffRoutingError("未完成需求只能在明確改需求時建立 replacement revision。")
            if task is None and plan.replace:
                raise HandoffRoutingError("沒有未完成需求可供 replacement。")
            if revision is not None:
                revision.retired_turn = ledger.turn.number
            ledger.revisions.append(PlanRevision(
                id=uuid.uuid4().hex, number=len(ledger.revisions) + 1,
                accepted_turn=ledger.turn.number, replacement_reason=plan.reason,
                tasks=[ReviewTask(role=role) for role in plan.roles],
            ))
            ledger.turn.decision = "plan"
        else:
            if task is None or task.status not in ("needs_input", "unavailable") or task.attempts >= 2:
                raise HandoffRoutingError("Resume 沒有可續辦步驟，或步驟 budget 已耗盡。")
            task.status = "pending"
            task.dispatch = None
            task.receipt_id = None
            task.outcome = None
            ledger.turn.decision = "resume"
        await call_next()
        _save_ledger(session, ledger)

    return plan_action


def _planning_tools():
    from agent_framework import FunctionTool

    def accept_plan(roles: list[SpecialistKey], replace: bool = False, reason: str | None = None) -> dict:
        return {"accepted": roles, "replace": replace, "reason": reason}

    def resume_plan() -> dict:
        return {"resumed": True}

    return [
        FunctionTool(
            name=_ACCEPT_PLAN, description="接受本 user turn 的有序審查需求；明確改需求才 replace。",
            input_model=ReviewPlan, func=accept_plan, approval_mode="never_require",
        ),
        FunctionTool(
            name=_RESUME_PLAN, description="使用者已補材料或明確續辦時，續辦目前阻塞的需求。",
            input_model={"type": "object", "properties": {}},
            func=resume_plan, approval_mode="never_require",
        ),
    ]


def build_completion_return_middleware(primary_name: str, role: SpecialistKey):
    """在原 chat stream 的 result hook 補控制 call，保留原 finalizer 與 hooks。"""
    from agent_framework import (
        ChatResponse, Content, FunctionTool, ResponseStream, chat_middleware,
    )

    expected_name = f"handoff_to_{primary_name}"
    expected_description = _return_description(primary_name)

    @chat_middleware
    async def completion_return(context, call_next):
        _require_local_history(context.session, context.options)
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
            _check_response_complete(response, actor="specialist")
            _outcome(response, role)
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
            if not response.text.strip():
                raise HandoffRoutingError("專家沒有正常文字或合法回交。")
            assistants = [message for message in response.messages if message.role == "assistant"]
            if not assistants:
                raise HandoffRoutingError("專家正常回覆缺少 assistant 訊息。")
            assistants[-1].contents.append(Content.from_function_call(
                call_id=f"handoff_{uuid.uuid4().hex}", name=tool.name, arguments={},
            ))
            return response

        await call_next()
        if context.stream and isinstance(context.result, ResponseStream):
            # 沿用原 stream 的 hooks；控制 call 留在同一份訊息中供本機 history 配對。
            context.stream_result_hooks.insert(0, return_control)
        elif isinstance(context.result, ChatResponse):
            context.result = return_control(context.result)
        else:
            raise HandoffRoutingError("專家 chat 結果型別不符合回交契約。")

    return completion_return


def build_return_guard_middleware(primary_name: str, role: SpecialistKey):
    """位於降級處理外層，消費完 stream 後仍核對 SDK 產生的回交結果。"""
    from agent_framework import AgentResponse, AgentResponseUpdate, ResponseStream, agent_middleware

    def verify(response: AgentResponse, dispatch: HandoffDispatch) -> None:
        _check_response_complete(response, actor="specialist")
        call_id, target = _handoff_result(response)
        if target != primary_name:
            raise HandoffRoutingError("SDK 回交結果未指向 Primary。")
        outcome = _outcome(response, role)
        message = _assistant_message(response)
        receipt = OutcomeReceipt(
            dispatch=dispatch, source=message.author_name or "",
            handoff_call_id=call_id, outcome=outcome,
        )
        message.additional_properties = {
            **(message.additional_properties or {}), _RECEIPT_KEY: receipt.model_dump(mode="json"),
        }

    @agent_middleware
    async def return_guard(context, call_next):
        dispatch = _read_dispatch(context.messages, role, primary_name)
        await call_next()
        if context.stream and isinstance(context.result, ResponseStream):
            inner = context.result

            async def checked_updates() -> AsyncIterator[AgentResponseUpdate]:
                async for update in inner:
                    if not isinstance(update, AgentResponseUpdate):
                        raise HandoffRoutingError("專家 stream 更新型別不符。")
                    _check_response_actions(update, actor="specialist", phase="stream_update")
                    yield update
                verify(await inner.get_final_response(), dispatch)

            async def original_finalizer(_updates: Sequence[AgentResponseUpdate]) -> AgentResponse:
                return await inner.get_final_response()

            context.result = ResponseStream(checked_updates(), finalizer=original_finalizer)
        elif isinstance(context.result, AgentResponse):
            verify(context.result, dispatch)
        else:
            raise HandoffRoutingError("專家 agent 結果型別不符合回交契約。")

    return return_guard


def _strict_runtime_schema(schema: dict[str, object]) -> dict[str, object]:
    result = deepcopy(schema)

    def normalize(node: object) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                properties = node.get("properties")
                if not isinstance(properties, dict):
                    raise HandoffRoutingError("Runtime object schema 缺少 properties。")
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                normalize(value)
        elif isinstance(node, list):
            for value in node:
                normalize(value)

    normalize(result)
    return result


def build_participants(
    client, roles: dict[str, AgentRole], *, checkpoint_safe: bool = False
) -> "dict[str, Agent]":
    """依定義檔建構四個 Handoff 執行期參與者。

    `require_per_service_call_history_persistence=True` 是**必要**的：漏掉任何一個，
    `HandoffBuilder.build()` 會直接以 `ValueError` 失敗，訊息會列出缺少設定的 agent。
    這個旗標讓本機保存的對話歷史，與服務端在交接工具呼叫短路後的狀態保持一致。

    Handoff-only options 使用 SpecialistOutcome（內含原 SpecialistReview）與 PrimaryReply。
    四位參與者都使用 store=False，由 SDK 本機 history 保存程式補的控制 call/result。
    共用 client、角色定義及 Lab 1 的 schema/行為不變。

    來源 M04（microsoft/agent-framework 官方 handoff sample 與原始碼）：
    https://github.com/microsoft/agent-framework
    —— 本旗標的強制性與失敗訊息的內容以該 repository 的實際實作為準。
    """
    participants: dict[str, Agent] = {}
    primary_name = roles["primary"].agent_name
    for key, role in roles.items():
        default_options: dict[str, object] = {
            "store": False,
            "response_format": _strict_runtime_schema(
                PrimaryReply.model_json_schema() if key == "primary"
                else SpecialistOutcome.model_json_schema()
            ),
        }
        specialist_key: SpecialistKey | None = next(
            (candidate for candidate in SPECIALIST_KEYS if candidate == key), None
        )
        if key == "primary":
            default_options["tools"] = _planning_tools()
            guard = build_primary_guard_middleware(roles)
            policy = [build_plan_action_middleware(), build_primary_policy_middleware(roles)]
        elif specialist_key is None:
            raise HandoffRoutingError("未知 Handoff 參與者角色。")
        else:
            guard = build_return_guard_middleware(primary_name, specialist_key)
            policy = [build_completion_return_middleware(primary_name, specialist_key)]
        participants[key] = client.as_agent(
            name=role.agent_name,
            description=role.description,
            instructions=(
                role.instructions + (
                    _primary_runtime_instructions() if key == "primary"
                    else _runtime_instructions(primary_name)
                )
            ),
            default_options=default_options or None,
            require_per_service_call_history_persistence=True,
            middleware=[
                guard,
                *([build_checkpoint_safe_middleware()] if checkpoint_safe else []),
                build_degraded_reply_middleware(role.agent_name),
                *policy,
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
