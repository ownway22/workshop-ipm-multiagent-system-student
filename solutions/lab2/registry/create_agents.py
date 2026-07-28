"""建立 workshop 需要的持久化 agent（Lab 2 步驟一）。

執行方式（cwd 必須是 `src`）：

    cd src && uv run python -m registry.create_agents

## 這支腳本做什麼

1. 確認 Lab 1 在 portal 手動建立的 `qvn-coding-agent` **存在**。不存在就報錯並指向 Lab 1，
   **不會**自動補建。
2. 冪等地建立 `qvn-primary-agent`、`qvn-architect-agent`、`qvn-spec-agent`。

## 設計重點

**定義檔是唯一事實來源。** 四個 agent 的 instructions 與 description 都來自 `src/agents/`，
同一份內容餵給兩條路徑：這裡建立**持久化 agent**（portal 可見、方便清理辨識），
`src/workflows/handoff.py` 建立**執行期參與者**。兩邊不可各自維護一份會分歧的內容。

**為什麼不從服務端讀回 Lab 1 的定義**：portal 建立 agent 時**沒有** description 欄位，
讀回來會是空字串；而 description 是 agent discovery 的唯一依據（2026-07-27 實測）。
"""

import asyncio
import json
import sys

from agents import build_agent_roles_list, get_agent_name_prefix
from config import load_settings_or_exit
from models.agent_role import AgentRole

#: 前置檢查失敗（例如 Lab 1 的 agent 不存在）的結束碼。
#: 與 `config.EXIT_CODE_MISSING_ENV`（2）區隔，方便從結束碼判斷卡在哪一關。
EXIT_CODE_PRECHECK_FAILED = 3

#: 建立／更新單一 agent 的逾時（秒）。
_OPERATION_TIMEOUT_SECONDS = 60


class PrecheckError(RuntimeError):
    """前置檢查失敗；訊息中已含完整的修復指引。"""


def _build_credential():
    """建立開發者憑證。不可使用 API key。"""
    from azure.identity.aio import AzureCliCredential

    return AzureCliCredential()


def _build_agent(role: AgentRole, endpoint: str, deployment_name: str, credential):
    """把定義檔的 `AgentRole` 轉成 agent-framework 的 `Agent`。

    `to_prompt_agent()` 只吃 client 為 `FoundryChatClient` 的 `Agent`，且 model、
    instructions、response_format 全部從 `agent.default_options` 取值，因此這裡
    必須把定義檔的內容完整交給 `as_agent()`，不可只傳一部分。
    """
    from agent_framework.foundry import FoundryChatClient

    client = FoundryChatClient(
        project_endpoint=endpoint,
        model=deployment_name,
        credential=credential,
    )

    default_options: dict[str, object] = {}
    if role.response_format is not None:
        default_options["response_format"] = role.response_format

    return client.as_agent(
        name=role.agent_name,
        description=role.description,
        instructions=role.instructions,
        default_options=default_options or None,
    )


def _fingerprint(definition) -> str:
    """把 definition 壓成可比對的字串，用來判斷「已是最新」。"""
    payload = definition.as_dict() if hasattr(definition, "as_dict") else dict(definition)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


async def _get_agent_or_none(project_client, agent_name: str):
    """依名稱查詢 agent；不存在時回傳 `None` 而非拋出例外。"""
    from azure.core.exceptions import ResourceNotFoundError

    try:
        return await project_client.agents.get(agent_name)
    except ResourceNotFoundError:
        return None


async def _assert_lab1_agent_exists(
    project_client, coding_agent_name: str, endpoint: str
) -> None:
    """前置檢查：`qvn-coding-agent` 必須已存在。

    這是「Lab 1 → Lab 2 沿用」的執行點。Lab 2 刻意不自動補建 Lab 1 的 agent，
    否則學員會誤以為兩個 Lab 之間沒有關聯，也就看不出 portal 手動建立與程式碼建立的差別。

    失敗訊息必須同時陳列「查的是哪個專案」與「該專案實際有哪些 agent」。
    2026-07-27 實測踩到的真實情境：portal 上建在 A 專案、`.env` 指向 B 專案，
    光看「找不到 agent」會讓人以為 Lab 1 失敗，而不是想到專案選錯。
    """
    if await _get_agent_or_none(project_client, coding_agent_name) is not None:
        return

    existing = sorted([agent.name async for agent in project_client.agents.list()])
    listing = (
        "\n".join(f"       - {name}" for name in existing)
        if existing
        else "       （這個專案裡一個 agent 都沒有）"
    )

    raise PrecheckError(
        "\n".join(
            [
                f"❌ 找不到 Lab 1 建立的 agent「{coding_agent_name}」，無法繼續。",
                "",
                "Lab 2 刻意**不會**自動補建這個 agent —— 它必須由你在 Lab 1 於 portal 手動建立，",
                "本步驟才看得出「portal 手動建立」與「程式碼建立」的差別。",
                "",
                "目前查詢的專案：",
                f"    {endpoint}",
                "    該專案現有的 agent：",
                listing,
                "",
                "修復（**請先確認是第 1 種還是第 2 種**）：",
                "  1. 若上面的清單裡看得到你在 Lab 1 建立的 agent，只是名稱不同",
                f"     → 把它改名為「{coding_agent_name}」。",
                "  2. 若上面的清單完全不是你在 portal 看到的內容",
                "     → 你在 portal 選到的是**另一個專案**。回到 portal 確認左上角的專案名稱，",
                "       複製該專案的「專案端點」，更新 src/.env 的 FOUNDRY_PROJECT_ENDPOINT，",
                "       再重新執行 `uv run python -m preflight` 確認六項檢查仍全數通過。",
                "  3. 若你根本還沒做 Lab 1",
                "     → 回到 Lab 1 依講義建立，再重新執行本腳本。",
            ]
        )
    )


async def _upsert_agent(project_client, role: AgentRole, definition) -> tuple[str, str]:
    """建立或更新單一持久化 agent，回傳 `(狀態符號, 說明)`。

    冪等性：agent **名稱**是穩定識別碼。已存在時走 `create_version()` 建立
    新版本，agent 本身不會變成第二個，因此重複執行的 agent 總數恆定。

    比對時必須同時涵蓋 definition 與 description——`description` 不在 definition 內，
    它是 `create_version()` 的獨立參數，只比 definition 會漏掉描述變更。
    """
    existing = await _get_agent_or_none(project_client, role.agent_name)

    if existing is not None:
        latest = existing.versions.latest
        if _fingerprint(latest.definition) == _fingerprint(definition) and (
            latest.description == role.description
        ):
            return "⏭️ ", f"已是最新，略過（version {latest.version}）"

    created = await asyncio.wait_for(
        project_client.agents.create_version(
            role.agent_name,
            definition=definition,
            description=role.description,
        ),
        timeout=_OPERATION_TIMEOUT_SECONDS,
    )
    verb = "已更新" if existing is not None else "已建立"
    return "✅", f"{verb}（version {created.version}）"


async def _list_prefixed_agents(project_client, prefix: str) -> list[str]:
    """列出專案中所有以指定前置詞開頭的 agent 名稱（清理也依此判定）。"""
    return sorted(
        [agent.name async for agent in project_client.agents.list() if agent.name.startswith(prefix)]
    )


async def run() -> int:
    """執行完整流程，回傳結束碼。"""
    settings = load_settings_or_exit()
    prefix = get_agent_name_prefix()
    roles = build_agent_roles_list()
    coding_role = next(role for role in roles if role.created_in == "Lab1")
    to_create = [role for role in roles if role.created_in == "Lab2"]

    from agent_framework.foundry import to_prompt_agent
    from azure.ai.projects.aio import AIProjectClient

    width = max(len(role.agent_name) for role in roles) + 2

    async with _build_credential() as credential:
        async with AIProjectClient(
            endpoint=settings.foundry_project_endpoint,
            credential=credential,
        ) as project_client:
            await _assert_lab1_agent_exists(
                project_client,
                coding_role.agent_name,
                settings.foundry_project_endpoint,
            )
            print(
                f"{('[' + coding_role.agent_name + ']'):<{width}} "
                "✅ 已存在（Lab 1 建立）— 其專家角色已依定義檔加入 Handoff"
            )

            for role in to_create:
                agent = _build_agent(
                    role,
                    settings.foundry_project_endpoint,
                    settings.model_deployment_name,
                    credential,
                )
                symbol, detail = await _upsert_agent(
                    project_client, role, to_prompt_agent(agent)
                )
                print(f"{('[' + role.agent_name + ']'):<{width}} {symbol} {detail}")

            names = await _list_prefixed_agents(project_client, prefix)

    print()
    print(f"結果：Foundry 專案中共有 {len(names)} 個 {prefix} agent。")
    print("下一步：啟動 DevUI 觀察交接")
    print("    cd src && uv run python main.py")
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run()))
    except PrecheckError as error:
        # 先把已印出的進度沖出去，避免管線下 stderr 搶在 stdout 前面顯示而讓訊息錯亂。
        sys.stdout.flush()
        print(str(error), file=sys.stderr)
        raise SystemExit(EXIT_CODE_PRECHECK_FAILED) from error


if __name__ == "__main__":
    main()
