# Lab 1：在 Foundry portal 建立第一個 agent

**前置條件**：[Lab 0](00-lab0-env-check.md) 的 preflight 已 **6/6 通過**，且已在瀏覽器登入 Foundry portal 並進入專案。
**完成條件**：在 Foundry portal 的 Playground 與 agent 對話成功。

---

## 目標

在 Foundry portal 手動建立 coding agent，讓它以**結構化格式**回答。

完成後你會學到：

- 一個 agent 最少需要哪些設定（名稱、instructions、模型、輸出格式）
- 如何在 instructions 中限制 agent 只做一件事
- 結構化輸出如何讓回覆成為可被程式處理的資料，而非一段文字

> 這個 agent **Lab 2 會繼續用到**：Lab 2 的建立腳本會先檢查它存在，不存在就報錯。

---

## 步驟一：取得 instructions

點下方連結打開原始檔，複製其中那一段文字（**勿自行打字或從講義複製**）：

[src/agents/coding.py 第 25–58 行：coding agent 的 instructions](../src/agents/coding.py#L25-L58)

按 `Ctrl+C`（macOS 為 `Cmd+C`）複製選取內容。

> **注意：** 不要複製前後的 Python 字串語法（`INSTRUCTIONS = """\` 與結尾的 `"""`）。原始檔是唯一來源，Lab 2 也會讀取它。

## 步驟二：在 Foundry portal 建立 agent

1. 左側選單點 **Agents**。
2. 點 **New agent**（或 **+ Create**）。
3. **Name** 填入：

   ```text
   qvn-coding-agent
   ```

   > **注意：** 名稱必須逐字相同，Lab 2 會依此名稱尋找 agent；清理腳本也會依 `qvn-` 前置詞辨識課程資產，避免誤刪既有 agent。

4. **Model** 選擇 Lab 0 確認過的模型部署（建議 `gpt-5.4-mini`）。
5. **Instructions** 貼上步驟一複製的內容。
6. 點 **Save**。

## 步驟三：設定結構化輸出

先取得要貼上的 JSON：

```bash
cd src && uv run python -c "
import json
from models.specialist_review import build_portal_response_format
print(json.dumps(build_portal_response_format(), ensure_ascii=False, indent=2))
"
```

然後在 Foundry portal：

1. 進入 **Playground** 分頁。
2. 在左欄 Model 名稱右側點**參數圖示**（滑桿圖示）。
3. 在展開的 **Parameters** 面板找到 **Text format**。
4. 將 **Text format** 下拉選單改選 `response_schema`，會自動彈出對話框。
5. 對話框的 **Definition** 欄預設帶一份 `math_response` 範例，**全選刪除**後貼上剛產生的 JSON。
6. 點對話框的 **Save**。
7. 點頁面**右上角的 Save** 儲存 agent 版本（版本號 +1）。

> **提示：** JSON 由 `models/specialist_review.py` 產生，與 Lab 2 共用同一個資料契約，請勿手抄。

## 步驟四：測試

在 Playground 貼上這段程式碼，觀察回覆：

```python
def load_config(path):
    f = open(path)
    return json.load(f)

def run(customer_id):
    q = "SELECT * FROM orders WHERE customer_id = '" + customer_id + "'"
    try:
        return execute(q)
    except Exception:
        pass
```

接著**另開一個對話**，貼上這段**非程式碼**的內容：

```text
這是我們的 Azure 資源清單：一個 App Service（S1）、一個 SQL Database（S0）、
一個儲存體帳戶。請幫我看架構上的風險。
```

---

## 驗收

### 程式碼題目

- [ ] 回覆是**合法的 JSON**，包含 `category`、`summary`、`findings`、`severity`、
      `evidence`、`recommendations`、`limitations` 七個欄位
- [ ] `category` 是 `code`
- [ ] `findings` **至少 1 項**，且指出了 SQL 字串串接、檔案未關閉、例外被吞掉之中的問題
- [ ] `evidence` 的每一項都**可以在你貼上的程式碼中找到對應**——不是泛泛而談
- [ ] `limitations` **至少 1 項**，例如聲明「只看了這段片段，未執行程式碼」
- [ ] 自由文字全部是**繁體中文**（技術名詞維持英文）

### 非程式碼題目

- [ ] agent **拒絕**分析架構，明確表示這超出它的職責
- [ ] **沒有**硬答架構問題

### 誠實性（最重要）

- [ ] 回覆中**沒有**宣稱「已掃描你的 repository」
- [ ] 回覆中**沒有**宣稱「已連線 Azure 查詢」
- [ ] 回覆中**沒有**引用你沒有提供的檔案或系統

> **提示：** `limitations` 會要求模型明確說明未取得的資訊，是本 Lab 的重點。

---

## 常見問題排除

| 現象                                  | 原因                                      | 處理                                                 |
| ------------------------------------- | ----------------------------------------- | ---------------------------------------------------- |
| 找不到 **Text format** 選項           | 不在 Playground 分頁，或參數面板沒展開    | 確認在 Playground，點 Model 右側的滑桿圖示           |
| 貼上 JSON 後對話框顯示格式錯誤        | 複製時漏了開頭或結尾的大括號              | 重跑步驟三的指令，整段重貼                           |
| 回覆不是 JSON，是一般文字             | response format 沒存到，或沒按右上角 Save | 重做步驟三的第 6、7 步（**兩個 Save 都要按**）       |
| 回覆是 JSON 但欄位不對                | 貼到舊的 `math_response` 範例上而沒刪乾淨 | 重做步驟三的第 5 步，**全選刪除**再貼                |
| 回覆是英文                            | instructions 沒貼完整                     | 重做步驟一，確認貼上的內容有「一律使用繁體中文」那段 |
| 非程式碼題目它還是硬答                | instructions 沒貼完整                     | 同上                                                 |
| Lab 2 報錯「找不到 qvn-coding-agent」 | 名稱打錯，或建在**別的專案**              | 見下方說明                                           |

### 關於「建在別的專案」

若訂閱下有多個 Foundry 專案，Foundry portal 會記住上次瀏覽的專案，**很容易在 A 專案建立 agent、而 `.env` 指向 B 專案**。

Lab 2 的腳本遇此情況會同時印出「目前查詢的專案端點」與「該專案實際有哪些 agent」，方便判斷。若清單與 Foundry portal 所見不符，即為專案選錯。

---

## 下一步

進入 [Lab 2：多代理與 Handoff](02-lab2-multi-agent.md)：用程式碼建立三個新 agent，沿用本 Lab 在 Foundry portal 建立的程式碼專家，把四個接成單層星狀拓撲。
