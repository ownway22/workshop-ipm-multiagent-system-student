"""Agent 角色的宣告式定義結構。

依 FR-061，`AgentRole` 是四個 agent 的 instructions 與 description 的**單一事實來源**，
同時餵給兩條路徑：

1. `src/registry/`：以 `to_prompt_agent()` + `agents.create_version()` 建立**持久化 agent**
   （portal 可見性、教學展示、清理辨識）。
2. `src/workflows/`：以 `FoundryChatClient.as_agent(...)` 建構 **Handoff 執行期參與者**。

兩條路徑 MUST NOT 各自維護一份會分歧的 instructions。

實測背景（research.md 決策 R03）：持久化的 `FoundryAgent` 不是 `agent_framework.Agent`
的子類，直接放入 `HandoffBuilder.participants()` 會得到
`TypeError: Participants must be Agent instances.`。這正是必須拆開兩種角色的原因。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CreatedIn = Literal["Lab1", "Lab2"]

#: agent 名稱的後綴。完整形式為 `<前置詞><key><後綴>`，例如 `qvn-coding-agent`。
#: FR-055 只約束**前置詞**必須是固定的 `qvn-`；後綴是為了在 portal 的 agent 清單中
#: 一眼辨識用途。四個角色 MUST 共用同一組命名形式，混用會讓學員誤以為是教材錯誤。
AGENT_NAME_SUFFIX = "-agent"


def compose_agent_name(prefix: str, key: str) -> str:
    """組出 agent 在 Foundry 專案中的名稱。

    這是四個角色**唯一**的名稱組成處；要調整命名規則只需改這裡。
    清理指引（FR-009）依前置詞過濾，不受後綴影響。
    """
    return f"{prefix}{key}{AGENT_NAME_SUFFIX}"


class AgentRole(BaseModel):
    """單一 agent 角色的完整定義。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Literal["primary", "coding", "architect", "spec"] = Field(
        description="角色鍵；agent_name 由前置詞加上本值組成",
    )
    agent_name: str = Field(
        min_length=1,
        description=(
            "Foundry 專案中的 agent 名稱，形式為 `<前置詞><key>-agent`（FR-055）。"
            "MUST 以 `compose_agent_name()` 組出，MUST NOT 在各模組各自拼接"
        ),
    )
    description: str = Field(
        min_length=1,
        description=(
            "agent discovery 的唯一依據（FR-012）。四個角色的描述 MUST 明顯互斥，"
            "並以「輸入材料的型態」而非動詞區分，否則會直接損害路由正確率"
        ),
    )
    instructions: str = Field(
        min_length=1,
        description="繁體中文的完整 instructions（FR-043、FR-057）",
    )
    created_in: CreatedIn = Field(
        description="建立時機：Coding 於 Lab 1 由學員在 portal 手動建立，其餘於 Lab 2 由程式碼建立",
    )
    is_orchestrator: bool = Field(
        description="是否為協調者；僅 primary 為 True（FR-012）",
    )
    response_format: type[BaseModel] | None = Field(
        default=None,
        description="結構化輸出的資料契約；三個專家為 SpecialistReview，Primary 為 PrimarySummary",
    )

    @field_validator("description", "instructions")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("欄位不得為空白字串")
        return value
