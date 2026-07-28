# Lab 3：部署到 Azure

**前置**：[Lab 2](02-lab2-multi-agent.md) 完成

---

## 目標

把 Lab 2 那個跑在你筆電上的多代理系統，變成 Azure 上一個別人能呼叫的服務。

做完你會得到：

- Foundry 專案的 Agents 清單裡多一個狀態為 `active` 的 hosted agent
- 可在 playground 與**整個多代理系統**對話，結果與本機一致
- 可用 CLI 呼叫的端點
- Application Insights 中看得到本次對話的 traces

---

## 步驟一：看懂三個部署檔

先花三分鐘讀過 `deploy/`，不用改任何內容：

| 檔案                                      | 重點                                                         |
| ----------------------------------------- | ------------------------------------------------------------ |
| [deploy/Dockerfile](../deploy/Dockerfile) | 平台鎖 `linux/amd64`；跑的是**同一支** `main.py --responses` |
| [deploy/agent.yaml](../deploy/agent.yaml) | 協定寫的是 `responses`；環境變數與資源配置                   |
| [deploy/azure.yaml](../deploy/azure.yaml) | azd 服務宣告；服務名稱 = agent 名稱                          |

三個值得留意的地方：

**容器跑的是同一支程式。** `Dockerfile` 的 `CMD` 是
`python main.py --responses --host 0.0.0.0`——和你本機下的指令只差繫結位址。
本機綁 loopback 是為了不把免驗證的端點暴露到區域網；容器內必須綁 `0.0.0.0` 才收得到流量。

**服務名稱必須等於 agent 名稱。** `azure.yaml` 的服務叫 `qvn-ipm-review`，
`agent.yaml` 的 `name` 也是 `qvn-ipm-review`。不一致時部署**會成功**，
但之後 `azd ai agent show` / `invoke` / `monitor` 全都會 404。

**`FOUNDRY_*` 是保留前綴。** 你在 `agent.yaml` 裡看不到 `FOUNDRY_PROJECT_ENDPOINT`，
因為平台會自動注入它。這也是為什麼模型部署名稱叫 `MODEL_DEPLOYMENT_NAME`
而不是 `FOUNDRY_MODEL_DEPLOYMENT_NAME`——後者會撞上保留字讓部署直接失敗。

---

## 步驟二：初始化 azd 環境

```bash
cd deploy
azd env new ipm-workshop
```

設定四個值（把角括號換成你自己的）：

```bash
azd env set AZURE_RESOURCE_GROUP <你的資源群組名稱>
azd env set AZURE_AI_ACCOUNT_NAME <你的 Foundry 帳戶名稱>
azd env set AZURE_AI_PROJECT_NAME <你的 Foundry 專案名稱>
azd env set MODEL_DEPLOYMENT_NAME <你的模型部署名稱>
```

前三個可以從 `.env` 裡的專案端點推出來，或直接回 portal 複製。
第四個與 `src/.env` 的 `MODEL_DEPLOYMENT_NAME` 是同一個值。

確認：

```bash
azd env get-values
```

---

## 步驟三：佈建基礎設施

```bash
azd provision
```

這一步只建立**一個** Azure Container Registry，以及它必要的三項授權與接線。

**不會**建立 Foundry 專案或模型部署——那是你的既有資產，
自動佈建只會覆寫你的環境。

### 身分從「你」變成「容器」

本機執行時，`AzureCliCredential` 用的是你 `az login` 的身分。容器裡沒有 `az login`——
託管後它以**受控識別**（managed identity）向 Foundry 取權杖。這件事平台會處理，
你要做的只有一件：確認權限有給對。

| 角色                               | 範圍     | 用途               |
| ---------------------------------- | -------- | ------------------ |
| Foundry Project Manager            | 專案     | 建立／呼叫 agent   |
| Azure Bot Service Contributor Role | 資源群組 | Lab 4 發布到 Teams |

> ⚠️ 如果 `azd provision` 回報權限錯誤，通常是缺 `Foundry Project Manager`
> （專案範圍）。訂閱的 Owner **不包含**這個角色。Lab 0 的 preflight 會檢查它。

---

## 步驟四：部署，然後去休息

```bash
azd deploy
```

**看到「Building service qvn-ipm-review」之後就可以離開座位。**

映像會在 Azure 上建置（不需要你本機有 Docker），然後推送、建立 agent 版本、
啟動容器。整個過程要好幾分鐘，盯著看沒有任何幫助。

> ⚠️ `azd deploy` 每次執行都會**重寫 `azure.yaml` 並清掉所有註解**。
> 這是 azd 的行為，不是你弄壞的。設定值本身不受影響；想把註解找回來，
> 從版本控制還原該檔即可。

---

## 步驟五：輪詢到 `active`

```bash
azd ai agent show
```

看 `status` 欄位：

| 狀態                    | 意思   | 你該做什麼           |
| ----------------------- | ------ | -------------------- |
| `creating` / `updating` | 還在建 | 等，過一分鐘再查一次 |
| `active`                | 好了   | 進行下一步           |
| `failed`                | 失敗   | 看下方「常見失敗」   |

也可以在 portal 的 Agents 清單看到同一個狀態。

---

## 步驟六：在 playground 送 12 題

到 Foundry portal → 你的專案 → Agents → `qvn-ipm-review` → playground。

送出**與 Lab 2 完全相同**的 12 題。

要取得某一題的完整內容（含要貼上的材料）：

```bash
cd src
uv run python -c "from fixtures.test_items import TEST_ITEMS, render_prompt; print(render_prompt(TEST_ITEMS[0]))"
```

把索引 `[0]` 換成 `[1]`⋯`[11]` 即可取得第 2 到第 12 題。

想一次看完十二題就開 `src/fixtures/materials/test_items.csv`，內容與上面的指令完全相同。

### 至少要送這一題

時間不夠時，**T11（跨領域）是最低限度**——它是唯一能證明「逐次交接」在託管環境
也成立的題目。指令：

```bash
cd src
uv run python -c "from fixtures.test_items import TEST_ITEMS, render_prompt; print(render_prompt(TEST_ITEMS[10]))"
```

它的開頭是：

```text
我同時貼上訂單匯出的程式碼與訂單平台的資源快照，請一起看程式碼品質與架構風險。
```

**成功判準**：回覆中程式碼與架構的發現是**分開**的，且每一項標明來自哪一位專家；
路由應為 `primary → coding → primary → architect → primary`，
與 Lab 2 在 DevUI 看到的完全一致。

### 全部 12 題的預期

**這一步的重點是「一致」，不是「會動」。** 交接路由結果應該與你在 DevUI 看到的相同：

| 題號    | 預期路由                                        |
| ------- | ----------------------------------------------- |
| T01–T02 | `primary → coding → primary`                    |
| T03–T04 | `primary → architect → primary`                 |
| T05–T06 | `primary → spec → primary`                      |
| T07–T08 | （無交接）先反問                                |
| T09–T10 | （無交接）說明能力邊界                          |
| T11–T12 | `primary → 專家 A → primary → 專家 B → primary` |

如果結果不一致，**通常是環境變數沒帶上去**（例如 `MODEL_DEPLOYMENT_NAME`
在 azd 環境裡的值與 `src/.env` 不同）。

另外注意：playground 裡你是跟**整個多代理系統**對話，不是挑其中一個 agent 講話。
這是 `workflow.as_agent()` 的效果——四個 agent 對外看起來就是一個。

---

## 步驟七：用 CLI 呼叫

```bash
azd ai agent invoke --message "請幫我看這段程式碼有沒有問題：result = eval(user_input)"
```

這證明它是一個**真的可以被程式呼叫的端點**，不是只能在 portal 裡點。

---

## 步驟八：看 traces

### 找到 Application Insights

Foundry portal → 你的專案 → **Tracing**（或到 Azure portal 開專案關聯的
Application Insights 資源）。

### 查最近的執行軌跡

在 Application Insights 左側選 **Logs**，執行：

```kusto
AppTraces
| where TimeGenerated > ago(30m)
| order by TimeGenerated desc
| take 50
```

你應該看得到剛才那幾次對話的軌跡。

> ⚠️ 如果你習慣用 `az monitor app-insights query`，對**工作區型**的
> Application Insights 會回 `PathNotFoundError`。改用
> `az monitor log-analytics query --workspace <workspace-guid>`。

### 這個技能真正的用處：分辨兩種失敗

對話出問題時，第一件事**不是**改 instructions，而是先問：
**請求到底有沒有抵達 agent？**

| 你看到的                        | 判讀                   | 該查什麼                                         |
| ------------------------------- | ---------------------- | ------------------------------------------------ |
| traces 裡**沒有**對應時間的請求 | **請求沒抵達**         | 發布設定、Teams 通道、網路、session 是否已被回收 |
| traces 裡**有**請求但回應不對   | **agent 回應不如預期** | instructions、路由拓撲、模型能力                 |

這兩種狀況的處理方向**完全相反**。沒有 traces 就只能猜——
猜錯的典型後果是花二十分鐘調 instructions，而問題其實出在 Teams 通道沒設好。

Lab 4 會用到這個判斷。

### 然後做一件事：搜尋你貼進去的程式碼原文

```kusto
AppTraces
| where TimeGenerated > ago(30m)
| where Message contains "eval("
```

**搜不到才是對的。**

`agent.yaml` 裡有一行 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: "false"`。
託管環境的**預設值是 `true`**——不設它的話，使用者訊息與 agent 回覆會逐字進入
Application Insights。而且本機 DevUI 走的是另一套預設值（不記錄），
所以**只看本機永遠不會發現這件事**。

這是本課程唯一一個「本機與託管行為不同」的地方，值得記住。

---

## 驗收

- [ ] Agents 區段看得到該 hosted agent，狀態為 `active`
- [ ] playground 可與**整個多代理系統**對話（不是只能與單一 agent 對話）
- [ ] 12 題的交接路由結果**與 Lab 2 一致**
- [ ] `azd ai agent invoke` 呼叫成功
- [ ] Application Insights 中看得到本次對話的 traces
- [ ] traces 中**搜不到**你貼上的程式碼原文、個人資料或憑證

---

## 常見失敗

| 現象                                                    | 原因                                                  | 處理                                                                                        |
| ------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `failed to scan dependencies`                           | Dockerfile 的 `FROM` 加了 `--platform=`               | 移除；平台只在 `azure.yaml` 的 `docker.platform` 指定                                       |
| `[ImageError] Container registry authentication failed` | 缺 AcrPull 或 ACR 連線                                | 重跑 `azd provision`；權限傳播需要一點時間                                                  |
| `Environment variable 'FOUNDRY_…' is reserved`          | 自行宣告了保留前綴的變數                              | 移除；平台會自動注入                                                                        |
| 部署成功但 `azd ai agent show` 回 404                   | 服務名稱 ≠ agent 名稱                                 | 對齊 `azure.yaml` 的服務名稱與 `agent.yaml` 的 `name`                                       |
| `/readiness` 回 200 但送訊息就錯                        | `agent.yaml` 的 protocol 版本不對                     | 值由 `agent-framework-foundry-hosting` 版本決定，目前是 `2.0.0`                             |
| 容器啟動後隨即 FAILED                                   | 環境變數沒進去                                        | 檢查是否誤用 `environmentVariables`（camelCase）；正確是 `environment_variables` 且值為清單 |
| 部署「成功」但行為沒變                                  | 參數完全沒變，未產生新版本                            | 確認有實際變更；必要時改個描述文字強制產生新版本                                            |
| 改版後對話行為還是舊的                                  | 舊 session 綁在舊版本                                 | `azd ai agent sessions delete <id>`；只開新對話不夠                                         |
| 區域相關錯誤                                            | 選到不支援 hosted agents 的區域（例如 **East Asia**） | 改用官方支援清單內的區域；本課程建議 Southeast Asia 或 Japan East（preflight 會檢查）       |
| 呼叫時回 HTTP 429                                       | 觸發模型部署的流量限制                                | 等一分鐘重試；持續發生就調高 TPM 配額                                                       |
| 連續快速送出多則訊息，回覆像只看到第一段                | 多則落在同一 session，但處理順序不保證                | 貼材料**一次貼完**                                                                          |
| 休息後第一則訊息很慢                                    | 閒置超過 15 分鐘被回收，冷啟動中                      | 等一下就好，**不要**重跑部署                                                                |

---

## 下一步

[Lab 4：發布到 Teams](04-lab4-publish-teams.md)

> ⚠️ 如果你在這裡休息超過十五分鐘，Lab 4 的第一則訊息會明顯比較慢——
> 那是冷啟動，不是壞掉。多等一下就好。
