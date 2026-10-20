# Lab 4：發布到 Teams

**前置條件**：[Lab 3](03-lab3-deploy.md) 在你的發布目標完成實際版本／session 與對話驗收，
不只 hosted agent 狀態 `active`。
**完成條件**：在 Teams 的「Your agents」中找到 agent，並完成一輪交接對話。

---

## 目標

把 Lab 3 部署好的 agent 發布到 Microsoft Teams，在 Teams 裡與它對話。

完成後，你會在 Teams 的「Your agents」中看到可直接私訊的 agent。

---

## 限制與範圍

支援：

- 在 Teams 裡與 agent 1:1 私訊
- 完整的交接對話

目前不支援：

- 頻道提及
- 群組聊天

這是框架限制，不是設定錯誤；請使用 1:1 私訊。

本課程使用 **Direct publish +「Just you」**。組織範圍發布需要 Teams 系統管理員核准，不在本課程內。

Lab 3 的管理員 bootstrap 只處理 ACR／建置授權／project connection，**不保證 Teams 發布權限**。
Bot 資源是 ARM 管理操作，Foundry agent／模型則有 data plane 權限；兩者分開核對。
`Owner`／`Contributor` 一般涵蓋 `Microsoft.BotService/botServices/write`，但 ABAC／deny、
作用範圍與實際被拒絕的 action 仍須查證，不能只憑 preflight 找到或沒找到 Bot 角色 GUID 判定。

---

## 步驟一：確認 agent 狀態

```bash
cd deploy
azd ai agent show
```

`status` 必須是 `active`，還要核對實際 image／版本及 endpoint routing。
若不是預期目標，回到 [Lab 3 步驟五](03-lab3-deploy.md#步驟五輪詢到-active)。
PR #6 的獨立測試 agent regular version 6 沒有把原 `qvn-ipm-review` 路由切換，
也沒有完成 Teams 驗收；不能直接把合併或其 T01 成功當作本 Lab 發布已通過。

---

## 步驟二：開始發布

Foundry portal → 你的專案 → Agents → `qvn-ipm-review` → **Publish**
→ **Teams and Microsoft 365 Copilot**。

---

## 步驟三：填必填中繼資料

| 欄位               | 說明                  |
| ------------------ | --------------------- |
| Agent name         | 顯示在 Teams 裡的名稱 |
| Short description  | 一句話說明            |
| Long description   | 幾句話說明用途        |
| **Developer name** | **不得超過 32 字元**  |
| Website URL        | 你公司的網址即可      |
| Privacy policy URL | 同上                  |
| Terms of use URL   | 同上                  |

> **注意：** Developer name 不得超過 32 字元，中文字也算字元。

三個 URL 欄位在「Just you」的發布範圍下不會被實際檢視，
但**格式必須是合法的網址**才過得了驗證。

---

## 步驟四：選 Direct publish

Publish options → **Direct publish**。

> **提示：** 另一個選項需要系統管理員核准，本課程不使用。

---

## 步驟五：選擇「Just you」範圍

使用範圍 → **Just you**。

按下發布，等待 **Publish successful**。

---

## 步驟六：在 Teams 裡找到它

開啟 Teams → 左側 **Apps** → **Your agents**（或搜尋你剛才填的 Agent name）。

> **注意：** Teams 帳號必須與 Azure 訂閱位於同一個 Entra 租戶，否則 agent 不會出現在「Your agents」。

找到後開啟 1:1 對話。

---

## 步驟七：完成一輪交接對話

送出一題會觸發交接的需求，例如：

```text
請幫我看這段程式碼，順便評估一下這樣的架構有沒有問題：

result = eval(request.args.get("expr"))
```

觀察兩件事：

1. **它有沒有完成交接**——回覆裡應該看得出程式碼與架構是分開分析的
2. **它引用的證據從哪來**——只能來自你貼的內容，不能宣稱它「查過你的 repository」

---

## 驗收

- [ ] 出現 **Publish successful**
- [ ] Teams 的「Your agents」看得到該 agent
- [ ] 完成至少一輪**交接**對話
- [ ] 回應引用的證據**只來自**你的訊息或課程提供的虛構材料
- [ ] 你知道頻道提及與群組聊天**不支援**

---

## 1:1 對話與 session

歷史 [native hosting 可行性驗證未通過](native-hosting-migration.md)，沒有改動 Teams 發布配置。
PR #6 已合併並在獨立 agent 完成有限 Azure／Playground 驗證，但沒有完成 Teams 或全鏈路驗收。
正式 SDK 升級仍保留既有 HandoffResponsesHost；
第一次部署驗收使用新 session／conversation，人工核對純文字多輪、
session 閒置回收後續接、使用者隔離與完整專家輸出，不能只憑本機 HTTP 200 判定通過。

Teams 端的 session 邊界對應 **Teams 原生的對話識別碼**：

- 你和 agent 的 1:1 對話串 → 一個 session
- 你同事和同一個 agent 的對話 → 另一個 session，內容互不可見

這是預期的對話／隔離邊界，仍須在實際 Teams channel 驗證；#6 的 hosted API／GUI 證據
不保證昨天的 Teams 對話一定能續接，也不取代跨使用者的平台授權隔離驗收。

---

## 常見問題排除

| 現象                                           | 原因                                                    | 處理                                                            |
| ---------------------------------------------- | ------------------------------------------------------- | --------------------------------------------------------------- |
| 驗證錯誤，指向 Developer name                  | 超過 32 字元                                            | 縮短                                                            |
| `403 AuthorizationFailed on botServices/write` | 目標 RG 的 Bot ARM action 未獲准，或受 condition／deny 限制 | 請管理員核對實際 action／scope；Owner／Contributor 一般涵蓋該 action，不一律重複加角色或重登 |
| 發布成功但 Teams 裡找不到                      | Teams 帳號與 Azure 訂閱不同租戶                         | 用同租戶的帳號登入 Teams                                        |
| 第一則訊息等很久                               | 可能依該版本閒置設定回收而冷啟動                        | 核對 session／console 狀態，不直接重 deploy                      |
| 完全沒有回應                                   | 核對本次請求與 console，traces 可能尚未接線              | 見 [Lab 3 步驟八](03-lab3-deploy.md#步驟八console-logs-與-traces) |
| 回應內容像是舊版本                             | 先核對 image／版本、路由及實際 session binding          | 新建已確認版本的 session／conversation，保留舊資料，勿以刪除作預設 |
| 在頻道 `@` 它沒反應                            | **不支援**頻道提及                                      | 改用 1:1 私訊                                                   |

---

## 完成內容

你已將單一 agent 擴充為四個 agent 的交接系統，部署至 Azure，並發布到 Teams。核心程式碼不變，只有執行與發布方式不同。

---

## 下一步

- [收尾與清理](05-cleanup.md)——離開前把資源刪乾淨
- [如果你卡住了](06-fallback.md)——會後自行重跑的完整步驟
