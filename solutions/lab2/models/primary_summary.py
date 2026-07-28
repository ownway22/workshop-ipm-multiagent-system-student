"""Primary Agent 的整合輸出資料契約。

僅在複合需求（`cross_domain`）完成逐次交接後產生。Primary 只負責彙整與標示來源，
MUST NOT 補寫專家未提供的分析內容，也 MUST NOT 自行調整嚴重度（FR-020）。

⚠️ **本型別不作為 `response_format`**（2026-07-27 spike S3／T037 實測後改定）。
實測發現對 Primary 指定 `response_format` 會讓**每一句**回覆都被套進彙整形狀，
壓掉 FR-014 的釐清提問與 FR-015 的能力範圍說明，且模型會**捏造** `handled_by`。
詳見 `src/agents/primary.py` 的 `build_role` docstring。

保留本型別的用途是**彙整內容的文件契約**：它定義彙整該有哪些欄位與各欄的
硬性規則，供 `INSTRUCTIONS` 對照、以及 T048a 比對四個檢查點的彙整品質時當檢核清單。
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.specialist_review import Severity


class AttributedFinding(BaseModel):
    """單一發現，並標示其來源專家 agent。"""

    model_config = ConfigDict(extra="forbid")

    source_agent: str = Field(
        min_length=1,
        description="產出此發現的專家 agent 名稱；MUST 出現在 PrimarySummary.handled_by 之中",
    )
    finding: str = Field(
        min_length=1,
        description=(
            "繁體中文的發現內容；MUST 逐字或語意等價地取自該專家的 findings，"
            "MUST NOT 補寫專家未提供的內容"
        ),
    )
    severity: Severity = Field(
        description="MUST 與來源專家給的值相同，Primary MUST NOT 自行調整",
    )

    @field_validator("source_agent", "finding")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("欄位不得為空白字串")
        return value


class PrimarySummary(BaseModel):
    """Primary Agent 對複合需求的整合輸出。"""

    model_config = ConfigDict(extra="forbid")

    handled_by: list[str] = Field(
        min_length=1,
        description="依實際交接順序記錄接手過的 agent 名稱（含 qvn- 前置詞）",
    )
    consolidated_findings: list[AttributedFinding] = Field(
        min_length=1,
        description="彙整後的發現；每一項 MUST 標示來源 agent",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="尚未釐清的事項；可為空",
    )

    @field_validator("handled_by")
    @classmethod
    def _handled_by_not_blank(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name.strip():
                raise ValueError("handled_by 的項目不得為空白字串")
        return value

    @model_validator(mode="after")
    def _source_agent_must_be_in_handled_by(self) -> "PrimarySummary":
        """確保每項發現的來源都真的參與過本次對話。

        這是 FR-020「MUST NOT 補寫專家未提供的內容」在結構層的第一道防線：
        若 Primary 憑空捏造一個沒接手過的 agent 當來源，這裡會直接失敗。
        """
        participants = set(self.handled_by)
        unknown = sorted(
            {
                item.source_agent
                for item in self.consolidated_findings
                if item.source_agent not in participants
            }
        )
        if unknown:
            raise ValueError(
                f"source_agent 必須出現在 handled_by 之中；未知的來源：{'、'.join(unknown)}"
            )
        return self
