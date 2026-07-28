"""四個 agent 角色的註冊表。

依 FR-061，本模組是 instructions 與 description 的**單一事實來源**，同時供兩條路徑使用：

- `src/registry/`：建立 Foundry 專案中的**持久化 agent**（portal 可見性、清理辨識）
- `src/workflows/`：建構 **Handoff 執行期參與者**

MUST NOT 在其他地方維護第二份會分歧的 instructions。

命名（FR-055）：四個 agent 共用同一組固定前置詞（預設 `qvn-`），MUST NOT 每次執行隨機產生。
完整名稱形式為 `<前置詞><key>-agent`（例如 `qvn-coding-agent`），組成邏輯集中於
`models.agent_role.compose_agent_name()`。前置詞刻意選用無語意組合，降低與學員既有 agent
撞名而在清理時誤刪的風險。
"""

import os

from agents import architect, coding, primary, spec
from models.agent_role import AgentRole

#: 預設前置詞。可用環境變數 `AGENT_NAME_PREFIX` 覆寫，但講義預設不提供此操作。
DEFAULT_AGENT_NAME_PREFIX = "qvn-"

#: 註冊順序即 Handoff 拓撲的 participants 順序：Primary 在最前，三位專家在後。
_ROLE_MODULES = (primary, coding, architect, spec)


def get_agent_name_prefix() -> str:
    """取得目前生效的 agent 名稱前置詞。"""
    prefix = os.getenv("AGENT_NAME_PREFIX", "").strip()
    return prefix or DEFAULT_AGENT_NAME_PREFIX


def build_agent_roles(prefix: str | None = None) -> dict[str, AgentRole]:
    """組出四個角色定義，以 `key` 為索引。

    Args:
        prefix: agent 名稱前置詞；省略時讀取 `AGENT_NAME_PREFIX`，再退回預設值。
    """
    effective_prefix = prefix if prefix is not None else get_agent_name_prefix()
    return {
        module.KEY: module.build_role(effective_prefix) for module in _ROLE_MODULES
    }


def build_agent_roles_list(prefix: str | None = None) -> list[AgentRole]:
    """以註冊順序回傳四個角色定義（Primary 在最前）。"""
    return list(build_agent_roles(prefix).values())


__all__ = [
    "DEFAULT_AGENT_NAME_PREFIX",
    "build_agent_roles",
    "build_agent_roles_list",
    "get_agent_name_prefix",
]
