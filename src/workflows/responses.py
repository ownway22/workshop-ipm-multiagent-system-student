"""Handoff 工作流的 Responses 主機：使用者以純文字續接，狀態存在 checkpoint。

由 `src/main.py` 的 Responses 模式（以及 Lab 3 部署後的容器）使用。

## 為什麼不直接用 `ResponsesHostServer(workflow.as_agent())`

`WorkflowAgent` 把 Handoff 的 `request_info` 以 function call 的形式對外暴露，期待呼叫端
回傳 function result；但 Foundry Playground 與一般 Responses 用戶端送來的是**純文字**，
第二輪就接不下去。另外，共用同一個 `Workflow` 實例時，兩個請求同時進來會撞上
「Workflow is already running」，狀態也會跨使用者殘留。

本模組改用 `ResponsesAgentServerHost` 的公開 `response_handler` 接口：每一輪重新建立
工作流、從 checkpoint 還原，再把使用者的文字交回正在等待的 `HandoffAgentUserRequest`。

INFO log 只記錄交接 source／target、每輪各來源的首次 output 與等待請求，
不記錄訊息本文或對話識別資料。

## checkpoint 的存放方式

```text
<root>/<sha256(user)>/responses/<sha256(response_id)>/*.json   每一輪各自的 checkpoint
<root>/<sha256(user)>/conversations/<sha256(conversation_id)>   指標：該 conversation 最新一輪的 response_id
```

- **每一輪都寫進自己的 response 目錄**，因此不論前一輪是用 `conversation` 還是
  `previous_response_id` 送出，下一輪都能用 `previous_response_id` 找到它。
- conversation 只保存一個指標檔；續接時先讀指標，再到對應的 response 目錄還原。
- 每輪結束只保留**最後一份** checkpoint，續接時只讀一個小目錄，成本不隨對話長度增加。

## 託管環境的前提

Foundry 會在同一個 session 內保留 `$HOME`（最長 30 天），所以託管時寫在 `$HOME` 底下。
但只帶 `previous_response_id`、沒帶 `agent_session_id` 的呼叫會被分到**新的 sandbox**，
那裡看不到上一輪的 checkpoint；改用 `conversation` 則會自動綁定同一個 session。
來源：https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions
"""

import asyncio
import hashlib
import logging
import os
import shutil
import uuid
import weakref
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import GenericAlias

from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    FileCheckpointStorage,
    Message,
    Workflow,
    WorkflowCheckpoint,
)
from agent_framework.orchestrations import HandoffAgentUserRequest, HandoffSentEvent
from azure.ai.agentserver.responses import ResponseContext, ResponseEventStream
from azure.ai.agentserver.responses.hosting import ResponsesAgentServerHost
from azure.ai.agentserver.responses.models import (
    CreateResponse,
    ItemMessage,
    MessageContentInputTextContent,
    MessageRole,
    ResponseStreamEvent,
)

logger = logging.getLogger(__name__)

#: checkpoint 反序列化的額外允許清單。
#:
#: 不關閉反序列化限制，只多放行 Handoff 需要的三種型別：Handoff 請求本身、
#: `request_info` 一併保存的回應型別 `list[Message]`（`GenericAlias`），以及 Azure role enum。
_CHECKPOINT_TYPES = [
    f"{kind.__module__}:{kind.__qualname__}"
    for kind in (HandoffAgentUserRequest, MessageRole, GenericAlias)
]

#: 使用者輸入錯誤時回傳的 error code。
#:
#: `azure-ai-agentserver-responses==1.0.0b9` 把 handler 拋出的**任何**例外都當成伺服器錯誤
#: （`response.created` 之前是 HTTP 500，之後是 `response.failed` + `server_error`），
#: 與 SDK 文件寫的「`ValueError` 轉成 400」不一致。因此這裡改為明確 `emit_failed`：
#: 呼叫端看得到原因，也能和真正的伺服器錯誤區分。HTTP 狀態碼仍是 200（串流已開始）。
_INVALID_INPUT_CODE = "invalid_prompt"

#: 伺服器錯誤一律回傳這段通用訊息，細節只寫進 log，不外洩給呼叫端。
_SERVER_ERROR_MESSAGE = "Handoff 對話失敗，請查看 session log。"


class HandoffRequestError(ValueError):
    """使用者可以自行修正的請求錯誤；訊息會原文回傳給呼叫端，請勿放入內部細節。"""


class HandoffResponsesHost(ResponsesAgentServerHost):
    """以純文字續接 Handoff 工作流，不把 `request_info` 暴露成工具呼叫。"""

    def __init__(
        self,
        workflow_factory: Callable[[], Workflow],
        *,
        checkpoint_root: Path | None = None,
        checkpoint_validator: Callable[[WorkflowCheckpoint], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._workflow_factory = workflow_factory
        self._checkpoint_validator = checkpoint_validator
        # 託管時容器根目錄不是持久儲存，只有 $HOME 會在 session 停止後保留。
        self._checkpoint_root = checkpoint_root or (
            (Path.home() if self.config.is_hosted else Path.cwd()) / ".handoff-checkpoints"
        )
        # 同一個 conversation 的「讀 checkpoint → 執行 → 寫回」必須序列化，否則兩個請求
        # 會從同一份狀態出發、互相覆寫。鎖以 conversation 為單位，其他對話不必排隊；
        # 用 WeakValueDictionary 讓沒人使用的鎖自動回收。
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self.response_handler(self._respond)

    # ── 路徑與鎖 ───────────────────────────────────────────

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def _user_root(self, user_id: str | None) -> Path:
        # 一律雜湊：路徑不含使用者可控的字串，不同使用者也無法互讀 checkpoint。
        return self._checkpoint_root / self._hash(user_id or "")

    def _response_dir(self, user_id: str | None, response_id: str) -> Path:
        return self._user_root(user_id) / "responses" / self._hash(response_id)

    def _conversation_pointer(self, user_id: str | None, conversation_id: str) -> Path:
        return self._user_root(user_id) / "conversations" / self._hash(conversation_id)

    @staticmethod
    def _storage(directory: Path) -> FileCheckpointStorage:
        return FileCheckpointStorage(directory, allowed_checkpoint_types=_CHECKPOINT_TYPES)

    def _lock_key(self, user_id: str | None, context_id: str) -> str:
        return f"{self._hash(user_id or '')}:{context_id}"

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # ── checkpoint 讀寫 ─────────────────────────────────────

    @staticmethod
    async def _latest_checkpoint(directory: Path) -> WorkflowCheckpoint | None:
        """嚴格讀取目錄內最新的 checkpoint。

        逐一用 `load()` 讀：檔案損毀就直接失敗。`get_latest()` 會默默略過讀不到的檔案，
        可能還原到較舊的一輪，或讓對話被當成全新開始。每個目錄只屬於單一輪，
        且該輪結束時只留一份，所以全部讀取的成本是固定的。
        """
        if not directory.is_dir():
            return None
        storage = HandoffResponsesHost._storage(directory)
        checkpoints = [await storage.load(path.stem) for path in directory.glob("*.json")]
        return max(checkpoints, key=lambda item: item.timestamp, default=None)

    @staticmethod
    def _keep_only(directory: Path, checkpoint_id: str) -> None:
        """刪掉這一輪的中間 checkpoint，只留續接需要的最後一份。"""
        for path in directory.glob("*.json"):
            if path.stem != checkpoint_id:
                path.unlink(missing_ok=True)

    @staticmethod
    def _write_pointer(pointer: Path, response_id: str) -> None:
        # 先寫暫存檔再取代，避免行程中斷時留下半截的指標。
        pointer.parent.mkdir(parents=True, exist_ok=True)
        temp = pointer.with_name(f"{pointer.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(response_id, encoding="utf-8")
        os.replace(temp, pointer)

    async def _restore_dir(
        self, request: CreateResponse, context: ResponseContext, user_id: str | None
    ) -> Path | None:
        """決定要從哪一輪還原；全新對話回傳 `None`。"""
        if request.previous_response_id:
            directory = self._response_dir(user_id, request.previous_response_id)
            if not directory.is_dir():
                raise HandoffRequestError(
                    "找不到 previous_response_id 對應的 Handoff checkpoint。"
                    "託管環境請一併帶上同一個 agent_session_id，或改用 conversation 續接。"
                )
            return directory
        if context.conversation_id:
            pointer = self._conversation_pointer(user_id, context.conversation_id)
            if pointer.is_file():
                return self._response_dir(user_id, pointer.read_text(encoding="utf-8").strip())
            if await context.get_history():
                raise HandoffRequestError(
                    "這個 conversation 已有歷史紀錄，但找不到對應的 Handoff checkpoint，"
                    "無法安全續接。請開新的 conversation。"
                )
        return None

    # ── 請求處理 ───────────────────────────────────────────

    @staticmethod
    def _event_source(data: object) -> str:
        if isinstance(data, AgentResponseUpdate):
            return data.author_name or data.agent_id or "unknown"
        if isinstance(data, AgentResponse):
            return data.agent_id or next(
                (message.author_name for message in data.messages if message.author_name),
                "unknown",
            )
        if isinstance(data, Message):
            return data.author_name or "unknown"
        return "unknown"

    async def _input_messages(self, context: ResponseContext) -> list[Message]:
        messages = []
        for item in await context.get_input_items():
            if not isinstance(item, ItemMessage) or item.role != "user":
                raise HandoffRequestError("這個 Handoff 端點只接受使用者的文字訊息。")
            if isinstance(item.content, str):
                text = item.content
            else:
                if any(
                    not isinstance(part, MessageContentInputTextContent)
                    for part in item.content
                ):
                    raise HandoffRequestError("不支援附件，請把程式碼或文件內容直接貼成文字。")
                text = "\n".join(
                    part.text for part in item.content
                    if isinstance(part, MessageContentInputTextContent)
                )
            if text.strip():
                messages.append(Message("user", [text]))
        if not messages:
            raise HandoffRequestError("請輸入非空白的訊息。")
        return messages

    async def _text_chunks(
        self, request: CreateResponse, context: ResponseContext, cancelled: asyncio.Event
    ) -> AsyncIterator[str]:
        if request.previous_response_id and context.conversation_id:
            raise HandoffRequestError("conversation 與 previous_response_id 只能擇一使用。")
        if self.config.is_hosted and context.platform_context.call_id is None:
            raise HandoffRequestError("此 agent 需要 Responses protocol 2.0.0。")

        messages = await self._input_messages(context)
        user_id = context.platform_context.user_id_key
        # previous_response_id 的每一輪都寫進新的 response 目錄，不會互相覆寫，
        # 所以只有 conversation 需要真正排隊；response_id 每次都不同，等於不鎖。
        lock_key = self._lock_key(user_id, context.conversation_id or context.response_id)
        async with self._lock(lock_key):
            workflow = self._workflow_factory()
            checkpoint = None
            write_dir = self._response_dir(user_id, context.response_id)
            restore_dir = await self._restore_dir(request, context, user_id)
            if restore_dir is not None:
                checkpoint = await self._latest_checkpoint(restore_dir)
                if checkpoint is None:
                    raise HandoffRequestError(
                        "找不到可續接的 Handoff checkpoint。請開新的 conversation。"
                    )
                if checkpoint.workflow_name != workflow.name:
                    raise HandoffRequestError(
                        "這段對話的 checkpoint 屬於其他工作流，無法續接。請開新的 conversation。"
                    )
                if self._checkpoint_validator is not None:
                    self._checkpoint_validator(checkpoint)
                # 還原本身也會寫出一份 checkpoint。先把前一輪的 checkpoint 複製到這一輪的目錄
                # 再從這裡還原，前一輪的目錄因此維持唯讀：從同一個 response 分岔的請求
                # 互不干擾，也不會改寫歷史。
                write_dir.mkdir(parents=True, exist_ok=True)
                file_name = f"{checkpoint.checkpoint_id}.json"
                shutil.copyfile(restore_dir / file_name, write_dir / file_name)
                await workflow.run(
                    checkpoint_id=checkpoint.checkpoint_id,
                    checkpoint_storage=self._storage(write_dir),
                )

            responses = None
            if checkpoint is not None and checkpoint.pending_request_info_events:
                pending = checkpoint.pending_request_info_events
                if len(pending) != 1 or not all(
                    isinstance(event.data, HandoffAgentUserRequest)
                    for event in pending.values()
                ):
                    raise RuntimeError("無法把使用者文字對應到唯一的 Handoff 請求。")
                responses = {
                    key: HandoffAgentUserRequest.create_response(messages) for key in pending
                }

            produced_text = False
            pending_request_id = None
            logged_sources: set[str] = set()
            async for event in workflow.run(
                message=messages if responses is None else None,
                responses=responses,
                stream=True,
                checkpoint_storage=self._storage(write_dir),
            ):
                if cancelled.is_set():
                    raise asyncio.CancelledError()
                if event.type in ("output", "intermediate"):
                    data = event.data
                    if isinstance(data, (AgentResponseUpdate, AgentResponse, Message)):
                        source = self._event_source(data)
                        if source not in logged_sources:
                            logger.info("Handoff output: source=%s", source)
                            logged_sources.add(source)
                        if data.text:
                            produced_text = True
                            yield data.text
                    elif isinstance(data, str):
                        produced_text = produced_text or bool(data)
                        yield data
                elif event.type == "handoff_sent" and isinstance(event.data, HandoffSentEvent):
                    logger.info(
                        "Handoff transition: source=%s target=%s",
                        event.data.source,
                        event.data.target,
                    )
                elif event.type == "error":
                    raise RuntimeError(f"Handoff workflow failed: {event.data}")
                elif event.type == "request_info":
                    if not isinstance(event.data, HandoffAgentUserRequest):
                        raise RuntimeError("此端點只支援 Handoff 的使用者請求。")
                    logger.info(
                        "Handoff waiting for user input: source=%s request_id=%s",
                        self._event_source(event.data.agent_response),
                        event.request_id,
                    )
                    pending_request_id = event.request_id
            if not produced_text:
                raise RuntimeError("Handoff workflow 沒有產生任何回覆文字。")

            # 確認最後一份 checkpoint 確實帶著等待中的請求，才讓呼叫端以為可以續接。
            latest = await self._latest_checkpoint(write_dir)
            if pending_request_id and (
                latest is None or pending_request_id not in latest.pending_request_info_events
            ):
                raise RuntimeError("無法保存等待中的 Handoff 回合，不能安全續接。")
            if latest is not None:
                self._keep_only(write_dir, latest.checkpoint_id)
                if context.conversation_id:
                    self._write_pointer(
                        self._conversation_pointer(user_id, context.conversation_id),
                        context.response_id,
                    )

    async def _respond(
        self, request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event
    ) -> AsyncIterator[ResponseStreamEvent]:
        events = ResponseEventStream(response_id=context.response_id, model=request.model)
        yield events.emit_created()
        yield events.emit_in_progress()
        try:
            message = events.add_output_item_message()
            yield message.emit_added()
            async for event in message.atext_content(
                self._text_chunks(request, context, cancellation_signal)
            ):
                yield event
            yield message.emit_done()
            yield events.emit_completed()
        except HandoffRequestError as error:
            logger.info("拒絕 Handoff 請求：%s", error)
            yield events.emit_failed(code=_INVALID_INPUT_CODE, message=str(error))
        except Exception:
            logger.exception("Handoff 對話處理失敗")
            yield events.emit_failed(code="server_error", message=_SERVER_ERROR_MESSAGE)
