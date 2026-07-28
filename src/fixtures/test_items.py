"""12 題代表性測試題組。

四個檢查點**共用**同一組題目，這樣「本機能跑通」與「Teams 也一致」
才是可比對的結論，而不是各自用不同題目各說各話。

題目分布：六種意圖類型各 2 題，共 12 題。
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

#: 虛構材料的所在目錄。
MATERIALS_DIR = Path(__file__).resolve().parent / "materials"


class IntentType(str, Enum):
    """使用者意圖類型。"""

    CODE_REVIEW = "code_review"
    ARCHITECTURE_REVIEW = "architecture_review"
    SPEC_REVIEW = "spec_review"
    AMBIGUOUS = "ambiguous"
    OUT_OF_SCOPE = "out_of_scope"
    CROSS_DOMAIN = "cross_domain"


class ExpectedOutcome(str, Enum):
    """預期結果標註。"""

    HANDOFF_CODING = "handoff_coding"
    HANDOFF_ARCHITECT = "handoff_architect"
    HANDOFF_SPEC = "handoff_spec"
    CLARIFYING_QUESTION = "clarifying_question"
    CAPABILITY_REPLY = "capability_reply"
    SEQUENTIAL_HANDOFF = "sequential_handoff"


@dataclass(frozen=True)
class TestItem:
    """單一測試題。"""

    id: str
    intent_type: IntentType
    prompt: str
    expected_outcome: ExpectedOutcome
    checkpoint_scope: tuple[int, ...]
    material_refs: tuple[str, ...] = ()
    #: 僅 `cross_domain` 使用：預期的交接順序（以角色 `key` 表示）。
    expected_handoff_order: tuple[str, ...] = field(default=())


#: 檢查點 1 只有 Coding Agent 存在，因此僅 `code_review` 兩題與「非程式碼題」適用。
_ALL_CHECKPOINTS = (1, 2, 3, 4)
_MULTI_AGENT_CHECKPOINTS = (2, 3, 4)

TEST_ITEMS: tuple[TestItem, ...] = (
    # ── code_review ───────────────────────────────────────────
    TestItem(
        id="T01",
        intent_type=IntentType.CODE_REVIEW,
        prompt=(
            "以下是我們訂單匯出工具的程式碼，請幫我做程式碼健檢，指出品質問題。\n\n"
            "（請貼上 src/fixtures/materials/python_snippet_01.py 的完整內容）"
        ),
        expected_outcome=ExpectedOutcome.HANDOFF_CODING,
        checkpoint_scope=_ALL_CHECKPOINTS,
        material_refs=("python_snippet_01.py",),
    ),
    TestItem(
        id="T02",
        intent_type=IntentType.CODE_REVIEW,
        prompt=(
            "這段庫存同步的 Python 程式碼有沒有需要改進的地方？\n\n"
            "（請貼上 src/fixtures/materials/python_snippet_02.py 的完整內容）"
        ),
        expected_outcome=ExpectedOutcome.HANDOFF_CODING,
        checkpoint_scope=_ALL_CHECKPOINTS,
        material_refs=("python_snippet_02.py",),
    ),
    # ── architecture_review ───────────────────────────────────
    TestItem(
        id="T03",
        intent_type=IntentType.ARCHITECTURE_REVIEW,
        prompt=(
            "這是我們訂單平台的 Azure 資源快照，請幫我看架構上的風險。\n\n"
            "（請貼上 src/fixtures/materials/azure_snapshot_01.md 的完整內容）"
        ),
        expected_outcome=ExpectedOutcome.HANDOFF_ARCHITECT,
        # 檢查點 1 只有 Coding Agent，本題在該檢查點的用途是確認它會回覆「超出自身職責」
        # 而不是硬答。
        checkpoint_scope=_ALL_CHECKPOINTS,
        material_refs=("azure_snapshot_01.md",),
    ),
    TestItem(
        id="T04",
        intent_type=IntentType.ARCHITECTURE_REVIEW,
        prompt=(
            "庫存同步平台的資源配置如下，環境隔離與容量規劃有沒有問題？\n\n"
            "（請貼上 src/fixtures/materials/azure_snapshot_02.md 的完整內容）"
        ),
        expected_outcome=ExpectedOutcome.HANDOFF_ARCHITECT,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
        material_refs=("azure_snapshot_02.md",),
    ),
    # ── spec_review ───────────────────────────────────────────
    TestItem(
        id="T05",
        intent_type=IntentType.SPEC_REVIEW,
        prompt=(
            "這份訂單匯出功能的規格節錄，哪些地方描述得不夠清楚？\n\n"
            "（請貼上 src/fixtures/materials/spec_excerpt_01.md 的完整內容）"
        ),
        expected_outcome=ExpectedOutcome.HANDOFF_SPEC,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
        material_refs=("spec_excerpt_01.md",),
    ),
    TestItem(
        id="T06",
        intent_type=IntentType.SPEC_REVIEW,
        prompt=(
            "請檢視這份交付清單節錄，看看驗收標準與時程有沒有問題。\n\n"
            "（請貼上 src/fixtures/materials/spec_excerpt_02.md 的完整內容）"
        ),
        expected_outcome=ExpectedOutcome.HANDOFF_SPEC,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
        material_refs=("spec_excerpt_02.md",),
    ),
    # ── ambiguous：預期先收到一個簡短釐清問題，不得逕行交接 ────
    TestItem(
        id="T07",
        intent_type=IntentType.AMBIGUOUS,
        prompt="幫我健檢一下這個專案。",
        expected_outcome=ExpectedOutcome.CLARIFYING_QUESTION,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
    ),
    TestItem(
        id="T08",
        intent_type=IntentType.AMBIGUOUS,
        prompt="我覺得我們的東西品質好像有點問題，你可以看看嗎？",
        expected_outcome=ExpectedOutcome.CLARIFYING_QUESTION,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
    ),
    # ── out_of_scope：預期收到能力範圍說明，不得勉強交接 ───────
    TestItem(
        id="T09",
        intent_type=IntentType.OUT_OF_SCOPE,
        prompt="請幫我預估這個專案下一季需要多少人力，並排出招募時程。",
        expected_outcome=ExpectedOutcome.CAPABILITY_REPLY,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
    ),
    TestItem(
        id="T10",
        intent_type=IntentType.OUT_OF_SCOPE,
        prompt="幫我把這份交付清單翻成日文，然後寄給客戶窗口。",
        expected_outcome=ExpectedOutcome.CAPABILITY_REPLY,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
    ),
    # ── cross_domain：預期逐次交接，且控制權每次都回到 Primary ─
    TestItem(
        id="T11",
        intent_type=IntentType.CROSS_DOMAIN,
        prompt=(
            "我同時貼上訂單匯出的程式碼與訂單平台的資源快照，"
            "請一起看程式碼品質與架構風險。\n\n"
            "（請依序貼上 src/fixtures/materials/python_snippet_01.py 與 "
            "src/fixtures/materials/azure_snapshot_01.md 的完整內容）"
        ),
        expected_outcome=ExpectedOutcome.SEQUENTIAL_HANDOFF,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
        material_refs=("python_snippet_01.py", "azure_snapshot_01.md"),
        expected_handoff_order=("coding", "architect"),
    ),
    TestItem(
        id="T12",
        intent_type=IntentType.CROSS_DOMAIN,
        prompt=(
            "這是庫存同步專案的交付清單節錄與資源快照，"
            "請幫我看規格描述與架構配置是否一致。\n\n"
            "（請依序貼上 src/fixtures/materials/spec_excerpt_02.md 與 "
            "src/fixtures/materials/azure_snapshot_02.md 的完整內容）"
        ),
        expected_outcome=ExpectedOutcome.SEQUENTIAL_HANDOFF,
        checkpoint_scope=_MULTI_AGENT_CHECKPOINTS,
        material_refs=("spec_excerpt_02.md", "azure_snapshot_02.md"),
        expected_handoff_order=("spec", "architect"),
    ),
)


def items_for_checkpoint(checkpoint: int) -> tuple[TestItem, ...]:
    """取得指定檢查點適用的題目。

    檢查點 1（portal playground）此時只有 Coding Agent，因此僅涵蓋 `code_review` 兩題
    與用來確認職責邊界的非程式碼題。
    """
    return tuple(item for item in TEST_ITEMS if checkpoint in item.checkpoint_scope)


def load_material(filename: str) -> str:
    """讀取虛構材料的內容，供學員直接複製貼上。"""
    return (MATERIALS_DIR / filename).read_text(encoding="utf-8")


#: 題目文字中「請貼上 …」這類給人看的操作指示。自動化執行時要換成材料的實際內容。
_PASTE_INSTRUCTION = re.compile(r"\n*（請(?:依序)?貼上.*?）", re.DOTALL)

#: 包住材料內容的圍欄。材料本身最長只出現單一反引號，用三個以上即可避免衝突；
#: 這裡用四個，讓材料內部若日後加入一般的 ``` 區塊也不會提前結束。
_FENCE = "````"


def render_prompt(item: TestItem) -> str:
    """把題目中的「請貼上 …」指示換成材料的實際內容。

    `TestItem.prompt` 保留給**人手操作**的檢查點（1、3、4 要學員自己貼材料）；
    自動化執行的檢查點 2 則需要完整內容。兩者必須出自同一個 `TestItem`，
    否則四個檢查點就不是在比同一件事。
    """
    head = _PASTE_INSTRUCTION.sub("", item.prompt).rstrip()
    if not item.material_refs:
        return head

    blocks = [
        f"{name}：\n\n{_FENCE}\n{load_material(name).rstrip()}\n{_FENCE}"
        for name in item.material_refs
    ]
    return "\n\n".join([head, *blocks])
