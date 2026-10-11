# Lab 2：多代理與 Handoff

**前置條件**：[Lab 1](01-lab1-single-agent.md) 已完成，`qvn-coding-agent` 已存在。
**完成條件**：在 DevUI 的 agent 實體完成一題交接，且實體清單同時看得到 agent 與 workflow 兩種類別。

---

## 目標

用程式碼建立另外三個 agent，把四個接成單層星狀的 Handoff 拓撲，在 DevUI 觀察控制權如何在協調者與專家之間移交。

完成後你會得到：

- Foundry 專案中共 4 個 `qvn-` agent（1 個在 Foundry portal 建立、3 個由程式碼建立）
- 一個跑在本機、可對話的多代理系統
- 對「複合需求為何逐次交接、而非同時分派」的實際體感

---

## 步驟一：建立另外三個 agent

```bash
cd src
uv run python -m registry.create_agents
```

輸出重點如下（省略狀態符號）：

```text
[qvn-coding-agent]    已存在（Lab 1 建立）— 其專家角色已依定義檔加入 Handoff
[qvn-primary-agent]   已建立（version 1）
[qvn-architect-agent] 已建立（version 1）
[qvn-spec-agent]      已建立（version 1）

結果：Foundry 專案中共有 4 個 qvn- agent。
下一步：啟動 DevUI 觀察交接
    cd src && uv run python main.py
```

這支腳本**不會**補建 `qvn-coding-agent`，只確認 Lab 1 建立的 coding agent 還在，再把它的專家角色接進 Handoff。

回 Foundry portal 的 Agents 清單確認四個都在。

---

## 步驟二：再執行一次

```bash
uv run python -m registry.create_agents
```

輸出重點如下（省略狀態符號）：

```text
[qvn-coding-agent]    已存在（Lab 1 建立）— 其專家角色已依定義檔加入 Handoff
[qvn-primary-agent]   已是最新，略過（version 1）
[qvn-architect-agent] 已是最新，略過（version 1）
[qvn-spec-agent]      已是最新，略過（version 1）

結果：Foundry 專案中共有 4 個 qvn- agent。
```

agent 名稱是穩定識別碼。重複執行只會更新同名 agent 的版本，不會增加 agent 數量。修改 `src/agents/` 後可重跑，版本號會遞增。

---

## 步驟三：啟動 DevUI

```bash
uv run python main.py
```

用瀏覽器開 `http://localhost:8080`。

在頂端實體選擇器確認有 `qvn-ipm-review`（agent）與 `qvn-ipm-review-workflow`（workflow）。兩者使用同一套拓撲；本 Lab 的 12 題題組一律在 `qvn-ipm-review` 進行。

### 關於 `auth_enabled=False`

本 workshop 為教學方便使用 `auth_enabled=False`，正式環境不應使用。

### 如果瀏覽器顯示 400 Bad Request

表示請求經過連接埠轉發（例如 Codespaces），送來的 `Host` 標頭不在 loopback 允許清單內。改用備援路徑：

```bash
uv run python main.py --forward
```

這會改綁 `0.0.0.0` 並開啟 token 驗證，終端機會印出一組權杖。**先開頁面**（首頁不需要 token），再把權杖貼進 DevUI 設定對話框的 `devui_auth_token` 欄位。

> 誠實說明：備援路徑本身已完整實測可用；「Codespaces 轉發一定送出不合格的 `Host` 標頭」則為**推測**，尚未在真實 Codespace 內確認。兩條路徑皆已備妥，不論哪一種都不會卡住。

---

## 步驟四：送出 12 題題組，觀察交接

這一段全部在 **`qvn-ipm-review`（agent 實體）** 進行；不需在 workflow 實體再跑一次，兩邊是同一套拓撲、路由結果相同。

題組在 `src/fixtures/test_items.py`。要看某一題的完整內容：

```bash
uv run python -c "from fixtures.test_items import TEST_ITEMS, render_prompt; print(render_prompt(TEST_ITEMS[0]))"
```

或直接開 `src/fixtures/materials/test_items.csv`——十二題的完整提示（含已內嵌的材料）都在「提示」欄，選取儲存格複製即可貼進 DevUI。

依序送出，每題觀察三件事：**交給了誰**、**問了什麼**、**有沒有跳過 Primary**。

| 題號    | 你會看到                                                        |
| ------- | --------------------------------------------------------------- |
| T01–T02 | 程式碼題，交給 `qvn-coding-agent`，再交回 Primary               |
| T03–T04 | 架構題，交給 `qvn-architect-agent`，再交回 Primary              |
| T05–T06 | 規格題，交給 `qvn-spec-agent`，再交回 Primary                   |
| T07–T08 | 意圖含糊，Primary **先反問**，不急著交接                        |
| T09–T10 | 完全超出三位專家的職責，Primary 說明能力邊界，**不交接**        |
| T11–T12 | 跨領域，**逐次**交接：專家 A → 回 Primary → 專家 B → 回 Primary |

## 驗收

- [ ] Foundry portal 的 Agents 區段共有 **4 個** `qvn-` agent
- [ ] 重複執行建立腳本**不會**產生重複 agent
- [ ] 建立腳本在 `qvn-coding-agent` 不存在時**明確報錯並指向 Lab 1**
- [ ] DevUI 可開啟（連接埠與驗證都是明確指定的，不依賴套件預設值）
- [ ] 實體清單上**同時看得到 agent 與 workflow 兩種類別**
- [ ] 已在 **agent 類別實體 `qvn-ipm-review` 上完成一題交接**
- [ ] 我能說出**這兩個項目是同一套系統的兩種呈現**
- [ ] 可在介面上**觀察到控制權從 Primary 轉移至專家**
- [ ] 12 題的交接路由符合上表預期
- [ ] 跨領域題是**逐次**交接，過程中**沒有**同時分派給多位專家

---

## 事件流中的工具呼叫

DevUI 的事件流中會出現類似 `handoff_to_qvn-coding-agent` 的工具呼叫。課程一開始說過「所有 agent 都不掛任何工具」，指的是外部業務工具，不包含框架內部交接控制工具。

Handoff 的實作機制**就是**注入交接工具——框架以「呼叫一個工具」表達「把控制權交給誰」。`HandoffBuilder` 的官方說明明確寫出它依賴 cloning、**tool injection** 與 middleware，且參與者必須支援本地工具呼叫。

|                | 業務工具（本 workshop 禁止） | 交接工具（框架內建） |
| -------------- | ---------------------------- | -------------------- |
| 誰定義         | 我們自己                     | 框架自動注入         |
| 做什麼         | 對外部系統做事               | 只表達「換人主導」   |
| 有外部副作用嗎 | 有                           | **沒有**             |

我們禁止的是「讓 agent 能對外部系統做事」——如此它才不會宣稱「我掃描了你的 repository」；禁止的並非「框架以工具機制實作交接」。

### 專家正常回合由程式控制回交

`add_handoff` 只設定允許的路由，不保證模型完成審查後自行交回。Handoff runtime
因此只對三位專家加入 completion-return middleware：模型已有合法回交 call 時不再補；
正常回覆但沒有 call 時，程式補一個非文字的 Primary 回交控制 call，交由 SDK 真正處理。
原串流文字不重播，Primary 仍負責彙整或釐清並等待使用者。這不是模型自發選工具的保證。

空結果、未知工具、未完成的特殊使用者動作或技術降級不能冒充正常審查；
外層 guard 會明確失敗，不默默停在專家等待。取消仍是取消，不注入回交。
缺材料先由 Primary 釐清；專家的補充材料說明不代表業務審查完成。
`SpecialistReview` 原 schema 的限制仍保留，不為了回交捏造結果。

此政策不啟用 autonomous mode，不增加專家用來「決定是否回交」的模型呼叫。
相較於提前停在專家的失敗路徑，會多出原本就應有的 Primary 彙整呼叫與 Token 成本。
Lab 1 單獨 Coding 的共用 instructions、schema 與行為不變，只有 Handoff runtime 有此補強。

程式補的控制 call 不存在於模型服務端的原始 response，因此只有 runtime specialists
設定 `store=False`：模型 history 由 SDK 本機 provider 保存、載入，並隨既有 checkpoint
持久化，包含配對的工具 call/result。Primary 保留原有服務端 history 行為。
這不表示 Hosted session 或 checkpoint 不再持久化；後續專家回合會傳送完整本機上下文，
輸入 Token 成本／快取效果可能不同。已有 service-managed conversation ID 不能安全自動
遷移，程式會明確失敗，不清除 ID 或丟棄 history；新版驗證應建立新 session/conversation。

### 本 hotfix 的驗證邊界與已知限制

本次短期 hotfix 的程式與測試收斂至歷史遠端驗證的 version 11／commit `6dea353`：
保留 credential、Responses 純文字續接、checkpoint／隔離及專家回交、stream finalization 修正。
後續 outcome／需求 ledger、Primary planning／storage 改變、ledger restore validator 與大型
診斷以新的 forward revert 撤回，不改寫原作者或實驗 commit 歷史。
Primary 仍負責彙整與下一段路由；本 hotfix 不保證所有跨領域子需求完成。

已保存的 SDK runtime regression 中，T01、同題續接與 T11 為 PASS，最後 waiting source
都是 Primary。T12 僅有 `primary → spec → primary`，未執行 Architect、缺實質彙整，
仍為 **FAIL／已知限制**。Responses 的 `completed` 不等於全部業務需求完成；
完整結果與部署狀態見 [Lab 3 的 hotfix 驗證紀錄](03-lab3-deploy.md#短期-hotfix-的歷史驗證與範圍)。

原 12 題與完整 workshop 驗收標準不變，這不是全部 Playground 驗收通過。
A365 ImportError／exporter Bad Request 尚未解決，Tracing 不宣稱通過；
沒有直接因果證據，不拿 telemetry 錯誤解釋 T12。
後續另開 SDK／hosting 升級工作並保留 T12 regression；
目前沒有證據證明舊 SDK 無法克服此限制，也不承諾升級必然修好。

---

## 常見問題排除

| 現象                                    | 原因                                     | 處理                                                                         |
| --------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------- |
| 建立腳本說找不到 `qvn-coding-agent`     | `.env` 指向的專案與 Foundry portal 不同  | 照錯誤訊息比對「該專案現有的 agent」清單；不符就是專案選錯                   |
| DevUI 開不起來，顯示連接埠被占用        | 8080 已被其他程式使用                    | `uv run python main.py --port 8090`                                          |
| 瀏覽器 400 Bad Request                  | 經過連接埠轉發                           | 改用 `uv run python main.py --forward`                                       |
| 實體清單只看到一個項目                  | 服務是舊版程式碼啟動的                   | 回終端機確認啟動訊息有列出兩個名稱；沒有就 `Ctrl+C` 重跑                     |
| workflow 類別實體送出後沒反應           | 前一輪還在跑，或模型配額壓力             | 等一分鐘；持續發生就改用 agent 實體，兩者是同一套拓撲                        |
| 對話跑到一半沒反應                      | 模型部署配額用盡或服務端壅塞             | 等一分鐘重試；持續發生就調高部署的 TPM 配額                                  |
| 出現「觸發流量限制（HTTP 429）」        | 短時間送太多題，或全班同時操作同一個部署 | 等一分鐘再送；這是降級回覆而非崩潰                                           |
| 連續快速送出多則，agent 只回答第一段    | 都落在同一 session，但處理順序不保證     | 貼材料**一次貼完**，不要拆成多則連發                                         |
| 出現「我原本要把這一段交給…」           | 該專家目前不可用                         | 這是正常的降級行為；依訊息中的步驟修復                                       |
| 改了 `src/agents/` 但行為沒變           | 沒重跑建立腳本，或 DevUI 沒重啟          | 重跑 `uv run python -m registry.create_agents`，再重啟 DevUI                 |
| 專家回覆不是 JSON                       | 該 agent 的 `response_format` 沒生效     | 重跑建立腳本；若仍如此，回 Foundry portal 確認 Lab 1 的 response format 還在 |
| 回覆旁出現「Unable to process request」 | 套件的已知缺陷（見下一節）               | 重新啟動服務再送下一題；這不是你的環境問題                                   |

---

## DevUI 已知限制

目前版本無法將 Handoff 狀態完整隔離在單一對話。2026-07-28 實測確認：

1. **workflow 實體**：按「新對話」後，仍讀得到上一段對話的內容。
2. **agent 實體**：第一輪交接結束後，繼續追問或開新對話都可能回 `Unexpected content type while awaiting request info responses.`

唯一可靠的重置方式是按 `Ctrl+C`，再重跑 `uv run python main.py`。這是 `agent-framework-devui` prerelease 版本的已知行為，不是操作錯誤。

---

## 下一步

進入 [Lab 3：部署到 Azure](03-lab3-deploy.md)：同一套拓撲，如何變成別人也能用的服務。
