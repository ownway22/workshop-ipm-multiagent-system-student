# Lab 4：發布到 Teams

**前置**：[Lab 3](03-lab3-deploy.md) 完成，hosted agent 狀態為 `active`

---

## 目標

把 Lab 3 部署好的 agent 發布到 Microsoft Teams，在 Teams 裡與它對話。

做完你會得到：一個在 Teams「Your agents」裡看得到、可以直接私訊的 agent。

---

## 先講清楚：這一關能做到什麼、不能做到什麼

**能做到**：

- ✅ 在 Teams 裡與 agent **1:1 私訊**
- ✅ 完整的交接對話（和 playground 一樣）

**做不到**（框架目前的限制，不是你設定錯）：

- ❌ **頻道提及**（在團隊頻道 `@` 這個 agent）
- ❌ **群組聊天**

所以待會兒不要試著把它拉進頻道——不會動，而且你會花時間找一個不存在的設定。

**組織範圍發布**（讓整個公司的人都能用）本課程**只作概念說明，不實際操作**。
那需要 Teams 系統管理員核准，流程與時程都不在課程可控範圍內。
今天用的是 **Direct publish + 「Just you」**——只有你自己看得到。

---

## 步驟一：確認有 active version

```bash
cd deploy
azd ai agent show
```

`status` 必須是 `active`。不是的話回 [Lab 3](03-lab3-deploy.md) 步驟五。

---

## 步驟二：開始發布

Foundry portal → 你的專案 → Agents → `qvn-ipm-review` → **Publish**
→ **Teams and Microsoft 365 Copilot**。

---

## 步驟三：填必填中繼資料

| 欄位               | 說明                    |
| ------------------ | ----------------------- |
| Agent name         | 顯示在 Teams 裡的名稱   |
| Short description  | 一句話說明              |
| Long description   | 幾句話說明用途          |
| **Developer name** | ⚠️ **不得超過 32 字元** |
| Website URL        | 你公司的網址即可        |
| Privacy policy URL | 同上                    |
| Terms of use URL   | 同上                    |

> ⚠️ **Developer name 超過 32 字元是最常見的卡關點**，而錯誤訊息不一定指得很清楚。
> 先數一下再填。中文字也算字元。

三個 URL 欄位在「Just you」的發布範圍下不會被實際檢視，
但**格式必須是合法的網址**才過得了驗證。

---

## 步驟四：選 Direct publish

Publish options → **Direct publish**。

（另一個選項會走系統管理員核准流程，今天不用。）

---

## 步驟五：範圍選「Just you」

使用範圍 → **Just you**。

按下發布，等待 **Publish successful**。

---

## 步驟六：在 Teams 裡找到它

開啟 Teams → 左側 **Apps** → **Your agents**（或搜尋你剛才填的 Agent name）。

> ⚠️ 你的 Teams 帳號必須與 Azure 訂閱**在同一個 Entra 租戶**。
> 不同租戶的話，「Just you」發布的 agent 不會出現在你的 Teams 裡。

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

## 一個 1:1 對話串 = 一份記憶

Teams 端的 session 邊界對應 **Teams 原生的對話識別碼**：

- 你和 agent 的 1:1 對話串 → 一個 session
- 你同事和同一個 agent 的對話 → 另一個 session，內容互不可見

所以你昨天在同一個對話串貼的程式碼，今天問「剛剛那段有什麼問題」它記得；
但你同事問一樣的話，它完全不知道你貼過什麼。

---

## 常見失敗

| 現象                                           | 原因                                                    | 處理                                                                |
| ---------------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------- |
| 驗證錯誤，指向 Developer name                  | 超過 32 字元                                            | 縮短                                                                |
| `403 AuthorizationFailed on botServices/write` | 缺 `Azure Bot Service Contributor Role`（資源群組範圍） | 請訂閱管理員在**資源群組**上指派；訂閱 Owner **不包含**這個角色     |
| 發布成功但 Teams 裡找不到                      | Teams 帳號與 Azure 訂閱不同租戶                         | 用同租戶的帳號登入 Teams                                            |
| 第一則訊息等很久                               | 閒置超過 15 分鐘被回收，冷啟動中                        | 等一下就好，不是壞掉                                                |
| 完全沒有回應                                   | 先看 traces 判斷請求有沒有抵達                          | 見 [Lab 3 步驟八](03-lab3-deploy.md#這個技能真正的用處分辨兩種失敗) |
| 回應內容像是舊版本                             | 舊 session 綁在舊 agent 版本                            | `azd ai agent sessions delete <id>`；只開新對話不夠                 |
| 在頻道 `@` 它沒反應                            | **不支援**頻道提及                                      | 改用 1:1 私訊                                                       |

---

## 你完成了什麼

從一個在 portal 手動點出來的單一 agent，到四個 agent 的交接系統，
再到 Azure 上的託管服務，最後到 Teams 裡可以直接對話的助理。

中間每一步的核心程式碼都沒有重寫——變的只是外面那一圈。

---

## 下一步

- [收尾與清理](05-cleanup.md)——離開前把資源刪乾淨
- [如果你卡住了](06-fallback.md)——會後自行重跑的完整步驟
