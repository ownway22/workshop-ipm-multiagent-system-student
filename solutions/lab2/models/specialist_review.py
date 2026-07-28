"""專家 Agent 的健檢結果資料契約。

這是整個多代理系統唯一橫跨四個檢查點的資料契約。因為所有 agent 都不掛工具，
協作品質完全取決於這份契約是否穩定。

語言分工：欄位名與列舉值維持英文（機器契約，跨層解析用），
自由文字欄位一律繁體中文（給人閱讀），其中的技術名詞、API、型別與變數名維持原始英文。
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReviewCategory(str, Enum):
    """健檢類別。必須與產出結果的專家 agent 對應，不得跨界。"""

    CODE = "code"
    ARCHITECTURE = "architecture"
    SPECIFICATION = "specification"


class Severity(str, Enum):
    """嚴重度。整份結果取最嚴重的單一等級，不是平均值。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def _reject_blank_items(items: list[str]) -> list[str]:
    """拒絕僅含空白的陣列項目。

    模型偶爾會為了湊滿必填欄位而回傳空字串或空白，這在 JSON schema 的 minItems
    層面看起來合規，實際上卻是無內容的輸出，因此在模型層再擋一次。
    """
    for item in items:
        if not item.strip():
            raise ValueError("陣列項目不得為空白字串")
    return items


class SpecialistReview(BaseModel):
    """專家 Agent 的結構化健檢結果。

    掛載方式：以 `default_options={"response_format": SpecialistReview}` 傳入
    `FoundryChatClient.as_agent(...)`。這是資料契約而非工具，因此不違反「不掛工具」的設計。
    """

    # 三個專家 agent 的輸出必須嚴格符合此形狀，多餘欄位視為契約違反。
    model_config = ConfigDict(extra="forbid")

    category: ReviewCategory = Field(
        description="健檢類別；必須與產出的專家 agent 對應",
    )
    summary: str = Field(
        min_length=1,
        description="繁體中文的整體摘要",
    )
    findings: list[str] = Field(
        min_length=1,
        description="繁體中文的發現清單；至少 1 項",
    )
    severity: Severity = Field(
        description="整份結果中最嚴重的單一等級",
    )
    evidence: list[str] = Field(
        min_length=1,
        description=(
            "繁體中文的證據清單；每一項必須可追溯到使用者訊息或課程提供的虛構材料。"
            "不可引用未提供的檔案、repository、Azure 即時狀態或外部文件"
        ),
    )
    recommendations: list[str] = Field(
        min_length=1,
        description="繁體中文的建議清單；至少 1 項",
    )
    limitations: list[str] = Field(
        min_length=1,
        description=(
            "繁體中文的限制清單；必須至少說明一項未涵蓋範圍。"
            "此欄位是防止 agent 誇大的結構性保險——即使模型傾向宣稱完整覆蓋，"
            "也必須在同一份輸出中宣告自己的限制"
        ),
    )

    @field_validator("summary")
    @classmethod
    def _summary_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary 不得為空白字串")
        return value

    @field_validator("findings", "evidence", "recommendations", "limitations")
    @classmethod
    def _items_not_blank(cls, value: list[str]) -> list[str]:
        return _reject_blank_items(value)


#: JSON Schema 中屬於「驗證細節」而非「結構」的關鍵字。
#: 這些會在產生 portal 貼上版時被移除，對應的約束改由 `SpecialistReview` 在解析階段把關，
#: 並在欄位 `description` 中以文字告知模型（例如「至少 1 項」）。
#:
#: 這是**保守做法，不是服務端的硬性限制**：2026-07-27 實測，SDK 路徑經
#: `to_prompt_agent()` 送出的 schema 保留這些關鍵字且 `strict: true`，`create_version()`
#: 一樣接受。移除它們是為了讓 portal 對話框的貼上版本盡量落在 OpenAI 相容 structured
#: outputs 的受限子集內，降低學員在 Lab 1 卡關的機率。
_STRICT_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "default",
        "title",
    }
)


def build_portal_response_format(name: str = "specialist_review") -> dict:
    """產生可直接貼進 Foundry portal「Add response format」對話框的 JSON。

    portal 需要的是 raw JSON schema（含 `name`／`strict`／`schema` 三個鍵），與 SDK 直接
    傳 Pydantic 型別的路徑不同。這個函式**從同一個 `SpecialistReview` 產生**，避免講義裡
    的 JSON 與程式碼日後分歧——這與 instructions 要求單一事實來源是同一種問題。

    產生時做三件事：

    1. 內嵌 `$defs`／`$ref`：讓貼上的內容自成一份，學員不必理解 JSON Schema 的引用語法。
    2. 移除 `strict` 模式不支援的驗證關鍵字（見 `_STRICT_UNSUPPORTED_KEYWORDS`）。
    3. 對每一個 object 補上 `additionalProperties: false`，這是 `strict` 模式的硬性要求。

    Args:
        name: portal 上這份 response format 的識別名稱，只允許英數與 `_`、`-`。
    """
    raw = SpecialistReview.model_json_schema()
    defs = raw.get("$defs", {})
    schema = _to_strict_schema(raw, defs)
    schema.pop("description", None)
    return {"name": name, "strict": True, "schema": schema}


def _to_strict_schema(node: object, defs: dict) -> dict:
    """遞迴內嵌 `$ref`、移除不支援的關鍵字，並補上 `additionalProperties: false`。"""
    if not isinstance(node, dict):
        return node  # type: ignore[return-value]

    if "$ref" in node:
        ref_name = node["$ref"].rsplit("/", 1)[-1]
        resolved = _to_strict_schema(defs[ref_name], defs)
        # 保留呼叫端寫的 description，它比 enum 自身的類別說明更貼近欄位語意。
        for key, value in node.items():
            if key != "$ref":
                resolved[key] = value
        resolved.pop("$defs", None)
        return resolved

    result: dict = {}
    for key, value in node.items():
        if key in _STRICT_UNSUPPORTED_KEYWORDS or key == "$defs":
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {k: _to_strict_schema(v, defs) for k, v in value.items()}
        elif key == "items":
            result[key] = _to_strict_schema(value, defs)
        else:
            result[key] = value

    if result.get("type") == "object":
        result["additionalProperties"] = False
    return result

