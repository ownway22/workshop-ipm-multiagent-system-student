# Lab 1：在 portal 建立第一個 Agent

**檢查點**：檢查點 1（portal playground）

---

## 目標

在 Foundry portal 手動建立一個**程式碼健檢專家** agent，並讓它以**結構化格式**回答。

做完你會知道：

- 一個 agent 最少需要哪些設定（名稱、instructions、模型、輸出格式）
- 「限制 agent 只做一件事」是怎麼寫進 instructions 的
- 結構化輸出如何讓回覆變成可被程式處理的資料，而不只是一段文字

> 這個 agent **Lab 2 會繼續用到**。Lab 2 的建立腳本會先檢查它存在，
> 不存在就直接報錯——刻意如此，讓你看得出「portal 手動建立」與「程式碼建立」的差別。

## 前置狀態

- [ ] Lab 0 的 `uv run python -m preflight` 已 **6/6 通過**
- [ ] 已在瀏覽器登入 Foundry portal 並進入你的專案

---

## 步驟

### 1. 取得要貼上的 instructions

**不要自己打字，也不要從講義複製。** 在 Codespace 的終端機執行：

```bash
cd src && uv run python -c "from agents.coding import INSTRUCTIONS; print(INSTRUCTIONS)"
```

把輸出**完整**複製起來（約 820 字元）。

> ℹ️ **為什麼用指令而不是把內容印在講義裡**：`src/agents/coding.py` 是這份 instructions
> 的**唯一來源**。Lab 2 的程式碼也讀同一個檔案。如果講義另外抄一份，兩邊遲早會不一致，
> 而你會很難發現為什麼 Lab 1 和 Lab 2 的 agent 行為不同。

### 2. 在 portal 建立 agent

1. 左側選單點 **Agents**。
2. 點 **New agent**（或 **+ Create**）。
3. **Name** 填入：

   ```text
   qvn-coding-agent
   ```

   > ⚠️ 名稱必須**逐字相同**。Lab 2 的腳本依這個名稱尋找它。
   > `qvn-` 前置詞是刻意選的無語意組合，收尾的清理指引依它辨識本次 workshop 建立的
   > agent，才不會誤刪你原有的資產。

4. **Model** 選擇你在 Lab 0 確認過的模型部署（建議 `gpt-5.4-mini`）。
5. **Instructions** 貼上步驟 1 複製的內容。
6. 點 **Save**。

### 3. 設定結構化輸出（response format）

先取得要貼上的 JSON：

```bash
cd src && uv run python -c "
import json
from models.specialist_review import build_portal_response_format
print(json.dumps(build_portal_response_format(), ensure_ascii=False, indent=2))
"
```

然後在 portal：

1. 進入 **Playground** 分頁。
2. 在左欄 Model 名稱右側點**參數圖示**（滑桿圖示）。
3. 展開的 **Parameters** 面板中找到 **Text format**。
4. 把 **Text format** 這個**下拉選單**改選 `response_schema`——選完會自動彈出對話框。
5. 對話框的 **Definition** 欄預設帶一份 `math_response` 範例。**全選刪除**，貼上剛才產生的 JSON。
6. 點對話框的 **Save**。
7. 點頁面**右上角的 Save** 儲存 agent 版本（版本號會 +1）。

> ℹ️ 這份 JSON 由 `models/specialist_review.py` 的 `SpecialistReview` 產生，
> 與 Lab 2 程式碼用的是**同一個資料契約**。同樣**不要手抄**。

### 4. 測試

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

## 驗收標準（檢查點 1）

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

> ℹ️ 最後這三項是這個 Lab 真正的重點。`limitations` 欄位是**結構性的保險**：
> 即使模型傾向宣稱自己看得很完整，它也必須在同一份輸出裡說明自己沒看到什麼。
> 這比在 instructions 裡寫「請不要誇大」有效得多。

---

## 常見錯誤排除

| 症狀                                  | 原因                                      | 處置                                                        |
| ------------------------------------- | ----------------------------------------- | ----------------------------------------------------------- |
| 找不到 **Text format** 選項           | 不在 Playground 分頁，或參數面板沒展開    | 確認在 Playground，點 Model 右側的滑桿圖示                  |
| 貼上 JSON 後對話框顯示格式錯誤        | 複製時漏了開頭或結尾的大括號              | 重跑步驟 3 的指令，整段重貼                                 |
| 回覆不是 JSON，是一般文字             | response format 沒存到，或沒按右上角 Save | 重做步驟 3 的第 6、7 步（**兩個 Save 都要按**）             |
| 回覆是 JSON 但欄位不對                | 貼到舊的 `math_response` 範例上而沒刪乾淨 | 重做步驟 3 的第 5 步，**全選刪除**再貼                      |
| 回覆是英文                            | instructions 沒貼完整                     | 重跑步驟 1 的指令，確認貼上的內容有「一律使用繁體中文」那段 |
| 非程式碼題目它還是硬答                | instructions 沒貼完整                     | 同上                                                        |
| Lab 2 報錯「找不到 qvn-coding-agent」 | 名稱打錯，或建在**別的專案**              | 見下方說明                                                  |

### 關於「建在別的專案」

如果你的訂閱底下有多個 Foundry 專案，portal 會記住你上次瀏覽的專案。
**很容易在 A 專案建 agent，而 `.env` 指向 B 專案。**

Lab 2 的腳本遇到這種情況時，會同時印出「目前查詢的是哪個專案端點」與
「該專案實際有哪些 agent」，方便你一眼判斷。若清單完全不是你在 portal 看到的內容，
就是專案選錯了。

---

## 下一步

進入 [Lab 2：多代理與 Handoff](02-lab2-multi-agent.md)。

你會用程式碼建立三個新 agent，沿用這一關在 portal 建立的程式碼專家，
把四個接成單層星狀拓撲。
