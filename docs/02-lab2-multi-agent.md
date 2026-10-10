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

### Handoff-only outcome、需求 ledger 與程式控制回交

`add_handoff` 只設定允許的路由，不保證模型完成審查後自行交回。Handoff runtime
因此對三位專家驗證 `SpecialistOutcome`：`review` 必須包含既有 `SpecialistReview`，
且 category 符合實際角色；`needs_input` 必須說明缺少材料並提出一個問題；
`unavailable` 必須說明原因。後兩者不製造 findings，也不算 reviewed。
只有交接 call、沒有合法 outcome 會明確失敗。有效結果沒有回交 call 時，程式補一個
非文字控制 call；已有合法 call 不重複補，仍由 SDK 真正處理，原串流文字不重播。

空結果、未知工具、未完成的特殊使用者動作或技術降級不能冒充正常審查；
外層 guard 會明確失敗，不默默停在專家等待。取消仍是取消，不注入回交。
guard 允許中間 created/in_progress update 的暫時 continuation token，但只在原 stream
完整消費後接受明確的 stop/tool_calls；pending、缺 terminal 或不完整回合仍失敗。

Primary 的公開 SDK session 是需求 ledger 的唯一 owner。內部 `accept_review_plan` action
登記一至三位、不重複的有序角色；已驗證的 outcome 與真實 SDK receipt 更新進度，
不採信模型自報的 source、done 或 handled_by。若 Primary 只說「稍後處理」而計畫仍有
pending 任務，runtime 會最多補一個當前步驟的合法交接。錯序、重複、損壞狀態及超過
budget 都明確失敗；每個 user turn 最多八次 Primary 模型呼叫，每個 revision 的 step
最多執行兩次，第二次只能由使用者補材料或明確續辦後的 `resume_review_plan` action 觸發。

新 user turn 與內部回交分開：完成計畫後的新審查建立新 revision，不清除材料或歷史；
純回顧用既有結果。needs_input/unavailable 後可有效 resume；明確改需求才建立 replacement
revision，保留舊未完成紀錄；無法判斷時只問一個釐清問題，不默默取消或擅自交接。
相同本文的兩次 user turns 不會被合併成同一要求。

Primary 最後以 Handoff-only `PrimaryReply` 明示 disposition、已 reviewed 與未完成角色。
runtime 對照 ledger 驗證清單；正常釐清與能力邊界仍不交接。
Responses protocol 的 completed 只表示本回合正常結束，**不代表所有業務需求 reviewed**：
needs_input/unavailable 可以誠實結束一輪並保留未完成需求。
這些 runtime 格式使用 JSON Schema mapping，ledger/receipts 保存普通 JSON 資料，
不新增 checkpoint 反序列化允許型別；共用角色與 `SpecialistReview` 不變。
Responses host 在已載入舊 checkpoint、交給 SDK 還原之前驗證 Primary owner
session/state/ledger。缺失、損壞、版本不支援或進度矛盾均直接失敗，不以 SDK 的空字典
缺省值重新初始化；真正全新對話才初始化 ledger。

此政策不啟用 autonomous mode 或模型重試，不增加專家的回交決策呼叫。首次 planning
通常增加一次 Primary 模型呼叫；正常單專家流程為 Primary 三次、專家一次，雙專家為
Primary 四次、每位專家一次。有效 resume action 也需要對應的 Primary 呼叫。
程式保證的是**已接受計畫**的有限、有序執行，不是意圖分類、計畫完整性或審查內容必然正確。

程式補的控制 call 不存在於模型服務端的原始 response，因此 Handoff runtime 的
**Primary 與 specialists 全部設定 `store=False`**。SDK 本機 history 保存配對的 call/result，
並隨既有 checkpoint 持久化；restore 後也保留材料與已驗證結果。後續回合傳送完整本機
上下文，輸入 Token 與快取效果可能不同，本機 history 並非免費。
Hosted session 的持久化仍保留；legacy service-managed ID 不清除或假裝遷移，而是明確失敗。
新部署必須用新 session/conversation 驗證；Lab 1 單獨 Coding 的 instructions/schema、
共用 client 與角色 options 均不變。

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
