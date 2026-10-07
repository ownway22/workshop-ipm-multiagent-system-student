"""Responses text conversations with durable Handoff continuation.

The pinned generic WorkflowAgent host expects function results for request_info.
Playground sends user text instead. Use the public host handler and workflow APIs
to return that text to the exact pending Handoff request.
"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import GenericAlias

from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    FileCheckpointStorage,
    Message,
    Workflow,
)
from agent_framework.orchestrations import HandoffAgentUserRequest
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

_CHECKPOINT_TYPES = [
    f"{kind.__module__}:{kind.__qualname__}"
    # request_info also persists its response type, list[Message].
    for kind in (HandoffAgentUserRequest, MessageRole, GenericAlias)
]


class HandoffResponsesHost(ResponsesAgentServerHost):
    """Host text-only Handoff workflows without exposing request_info as a tool."""

    def __init__(
        self,
        workflow_factory: Callable[[], Workflow],
        *,
        checkpoint_root: Path | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._workflow_factory = workflow_factory
        # Foundry persists $HOME, not the container root, across session stops.
        self._checkpoint_root = checkpoint_root or (
            (Path.home() if self.config.is_hosted else Path.cwd()) / ".handoff-checkpoints"
        )
        # A session runs one process. Serialize checkpoint read/run/write so two
        # requests cannot resume and overwrite the same conversation concurrently.
        self._turn_lock = asyncio.Lock()
        self.response_handler(self._respond)

    def _storage(self, user_id: str | None, context_id: str) -> FileCheckpointStorage:
        # Hash each namespace separately: no user-controlled path segments and no
        # cross-user checkpoint access, including for previous_response_id.
        user_key = hashlib.sha256((user_id or "").encode()).hexdigest()
        context_key = hashlib.sha256(context_id.encode()).hexdigest()
        return FileCheckpointStorage(
            self._checkpoint_root / user_key / context_key,
            allowed_checkpoint_types=_CHECKPOINT_TYPES,
        )

    async def _input_messages(self, context: ResponseContext) -> list[Message]:
        messages = []
        for item in await context.get_input_items():
            if not isinstance(item, ItemMessage) or item.role != "user":
                raise ValueError("This Handoff endpoint accepts user text messages only.")
            if isinstance(item.content, str):
                text = item.content
            else:
                if any(
                    not isinstance(part, MessageContentInputTextContent)
                    for part in item.content
                ):
                    raise ValueError("Paste source materials as text; attachments are not supported.")
                text = "\n".join(part.text for part in item.content)
            if text.strip():
                messages.append(Message("user", [text]))
        if not messages:
            raise ValueError("A nonempty user message is required.")
        return messages

    async def _text_chunks(
        self, request: CreateResponse, context: ResponseContext, cancelled: asyncio.Event
    ) -> AsyncIterator[str]:
        if request.previous_response_id and context.conversation_id:
            raise ValueError("Use conversation_id or previous_response_id, not both.")
        if self.config.is_hosted and context.platform_context.call_id is None:
            raise ValueError("This agent requires Responses protocol 2.0.0.")

        messages = await self._input_messages(context)
        user_id = context.platform_context.user_id_key
        restore_id = request.previous_response_id or context.conversation_id
        write_id = context.conversation_id or context.response_id
        async with self._turn_lock:
            workflow = self._workflow_factory()
            checkpoint = None
            if restore_id:
                storage = self._storage(user_id, restore_id)
                # load() fails on corruption; get_latest() silently skips unreadable
                # files and could resume an older turn or reset the conversation.
                checkpoints = [
                    await storage.load(path.stem)
                    for path in storage.storage_path.glob("*.json")
                ]
                if any(item.workflow_name != workflow.name for item in checkpoints):
                    raise ValueError("Conversation checkpoint belongs to a different workflow.")
                checkpoint = max(checkpoints, key=lambda item: item.timestamp, default=None)
                if checkpoint is not None:
                    await workflow.run(
                        checkpoint_id=checkpoint.checkpoint_id, checkpoint_storage=storage
                    )
                elif request.previous_response_id:
                    raise ValueError("The referenced response has no Handoff checkpoint in this session.")
                elif await context.get_history():
                    raise ValueError(
                        "Conversation history exists but its Handoff checkpoint is missing. "
                        "Start a new conversation; this history cannot be safely resumed."
                    )

            responses = None
            if checkpoint is not None and checkpoint.pending_request_info_events:
                pending = checkpoint.pending_request_info_events
                if len(pending) != 1 or not all(
                    isinstance(event.data, HandoffAgentUserRequest)
                    for event in pending.values()
                ):
                    raise ValueError("Cannot route user text to an ambiguous or non-Handoff request.")
                responses = {
                    key: HandoffAgentUserRequest.create_response(messages) for key in pending
                }

            produced_text = False
            pending_request_id = None
            write_storage = self._storage(user_id, write_id)
            async for event in workflow.run(
                message=messages if responses is None else None,
                responses=responses,
                stream=True,
                checkpoint_storage=write_storage,
            ):
                if cancelled.is_set():
                    raise asyncio.CancelledError()
                if event.type in ("output", "intermediate"):
                    data = event.data
                    if isinstance(data, (AgentResponseUpdate, AgentResponse, Message)):
                        if data.text:
                            produced_text = True
                            yield data.text
                    elif isinstance(data, str):
                        produced_text = produced_text or bool(data)
                        yield data
                elif event.type == "error":
                    raise RuntimeError(f"Handoff workflow failed: {event.data}")
                elif event.type == "request_info":
                    if not isinstance(event.data, HandoffAgentUserRequest):
                        raise ValueError("This endpoint only supports Handoff user requests.")
                    pending_request_id = event.request_id
            if not produced_text:
                raise RuntimeError("Handoff workflow produced no user-facing text.")
            if pending_request_id:
                latest = await write_storage.get_latest(workflow_name=workflow.name)
                if latest is None or pending_request_id not in latest.pending_request_info_events:
                    raise RuntimeError("Failed to persist the pending Handoff turn; cannot safely continue.")

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
        except Exception:
            logger.exception("Failed to produce response for Handoff conversation")
            yield events.emit_failed(
                code="server_error",
                message="Handoff conversation failed. Inspect the session logs for details.",
            )
