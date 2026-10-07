# Lab 3：部署到 Azure

**前置條件**：[Lab 2](02-lab2-multi-agent.md) 已完成。
**完成條件**：hosted agent 狀態為 `active`，且 Playground 對話結果與本機一致。

---

## 目標

把 Lab 2 那個跑在本機的多代理系統，變成 Azure 上別人能呼叫的服務。

完成後你會得到：

- Foundry 專案的 Agents 清單多一個狀態為 `active` 的 hosted agent
- 可在 Playground 與**整個多代理系統**對話，結果與本機一致
- 一個可用 CLI 呼叫的端點
- Application Insights 中看得到本次對話的 traces

---

## 步驟一：看懂三個部署檔

先讀過 `deploy/`，不需修改內容：

| 檔案                                      | 重點                                                         |
| ----------------------------------------- | ------------------------------------------------------------ |
| [deploy/Dockerfile](../deploy/Dockerfile) | 平台鎖 `linux/amd64`；跑的是**同一支** `main.py --responses` |
| [deploy/agent.yaml](../deploy/agent.yaml) | 協定寫的是 `responses`；環境變數與資源配置                   |
| [deploy/azure.yaml](../deploy/azure.yaml) | azd 服務宣告；服務名稱 = agent 名稱                          |

**服務名稱必須等於 agent 名稱。** `azure.yaml` 的服務與 `agent.yaml` 的 `name` 都是 `qvn-ipm-review`。不一致時部署**會成功**，但之後 `azd ai agent show`／`invoke`／`monitor` 全都會 404。

**`FOUNDRY_*` 是保留前綴。** `agent.yaml` 裡看不到 `FOUNDRY_PROJECT_ENDPOINT`，因為平台會自動注入。這也是模型部署名稱叫 `MODEL_DEPLOYMENT_NAME` 而非 `FOUNDRY_MODEL_DEPLOYMENT_NAME` 的原因——後者會撞上保留字使部署失敗。

---

## 步驟二：初始化 azd 環境

```bash
cd deploy
azd env new ipm-workshop
```

直接沿用 Lab 0 已驗證過的 `src/.env`，不需再回 Azure portal 或 Foundry portal 複製值：

```bash
uv run --project ../src python scripts/configure_azd_env.py
```

這支工具會先確認 `az login` 目前選取的訂閱，再查證專案與模型部署確實存在；任何值不一致時會停止，**不會**寫入部分設定。

工具會依來源自動寫入下列值：

- `az account show`：`AZURE_SUBSCRIPTION_ID`、`AZURE_TENANT_ID`
- 專案端點及 Azure 資源查詢：`AZURE_LOCATION`、`AZURE_RESOURCE_GROUP`、`AZURE_AI_ACCOUNT_NAME`、`AZURE_AI_PROJECT_NAME`、`AZURE_AI_PROJECT_ID`
- `src/.env` 的專案端點：`AZURE_AI_PROJECT_ENDPOINT`、`FOUNDRY_PROJECT_ENDPOINT`
- `src/.env` 的 `MODEL_DEPLOYMENT_NAME`：`AZURE_AI_MODEL_DEPLOYMENT_NAME`、`MODEL_DEPLOYMENT_NAME`

`AZURE_ENV_NAME` 已由 `azd env new` 建立；`AZURE_PRINCIPAL_ID` 與 `AZURE_PRINCIPAL_TYPE` 會由 azd 依目前登入身分自動解析。這些值都不需要手動設定。

確認：

```bash
azd env get-values
```

---

## 步驟三：建立基礎設施

```bash
azd provision
```

這一步只建立**一個** Azure Container Registry 及其必要的三項授權與接線，**不會**建立 Foundry 專案或模型部署——那是你的既有資產，自動建立只會覆寫你的環境。

### 身分從「你」變成「容器」

本機執行時，`AzureCliCredential` 用的是你 `az login` 的身分；容器內沒有 Azure CLI，不能沿用這個 credential。`main.py` 透過 `AgentConfig.from_env().is_hosted` 判斷託管環境，改用 `ManagedIdentityCredential` 與平台注入的 `FOUNDRY_AGENT_INSTANCE_CLIENT_ID` 向 Foundry 取權杖。平台提供身分與端點，但**不會自動替換程式建立的 `AzureCliCredential`**；仍需確認身分具備所需權限。

| 角色                               | 範圍     | 用途               |
| ---------------------------------- | -------- | ------------------ |
| Foundry Project Manager            | 專案     | 建立／呼叫 agent   |
| Azure Bot Service Contributor Role | 資源群組 | Lab 4 發布到 Teams |

> **注意：** `azd provision` 回報權限錯誤時，請確認是否具備專案範圍的 `Foundry Project Manager`。訂閱 Owner 不包含此角色。

---

## 步驟四：部署映像

```bash
azd deploy
```

映像會在 Azure 建置並推送，再建立 agent 版本與啟動容器，需數分鐘。完成前不要中斷命令。

> **注意：** `azd deploy` 會重寫 `azure.yaml` 並移除註解，但不影響設定值。需要註解時可從版本控制還原。

---

## 步驟五：輪詢到 `active`

```bash
azd ai agent show
```

看 `status` 欄位：

| 狀態                    | 意思   | 你該做什麼             |
| ----------------------- | ------ | ---------------------- |
| `creating` / `updating` | 還在建 | 等約一分鐘後再查一次   |
| `active`                | 好了   | 進行下一步             |
| `failed`                | 失敗   | 看下方「常見問題排除」 |

也可在 Foundry portal 的 Agents 清單看到同一狀態。

---

## 步驟六：在 Playground 送 12 題

到 Foundry portal → 你的專案 → Agents → `qvn-ipm-review` → **Playground**。

送出與 Lab 2 相同的 12 題。

要取得某一題的完整內容（含要貼上的材料）：

```bash
cd src
uv run python -c "from fixtures.test_items import TEST_ITEMS, render_prompt; print(render_prompt(TEST_ITEMS[0]))"
```

把索引 `[0]` 換成 `[1]`⋯`[11]` 即可取得第 2 到第 12 題。

也可開啟 `src/fixtures/materials/test_items.csv` 一次查看 12 題。

### 至少要送這一題

時間不夠時，**T11（跨領域）是最低限度**——它是唯一能證明「逐次交接」在託管環境也成立的題目。指令：

```bash
cd src
uv run python -c "from fixtures.test_items import TEST_ITEMS, render_prompt; print(render_prompt(TEST_ITEMS[10]))"
```

它的開頭是：

```text
我同時貼上訂單匯出的程式碼與訂單平台的資源快照，請一起看程式碼品質與架構風險。
```

**成功判準**：回覆中程式碼與架構的發現是**分開**的，且每一項標明來自哪一位專家；路由應為 `primary → coding → primary → architect → primary`，與 Lab 2 在 DevUI 看到的完全一致。

### 全部 12 題的預期

交接路由應與 DevUI 的結果一致：

| 題號    | 預期路由                                        |
| ------- | ----------------------------------------------- |
| T01–T02 | `primary → coding → primary`                    |
| T03–T04 | `primary → architect → primary`                 |
| T05–T06 | `primary → spec → primary`                      |
| T07–T08 | （無交接）先反問                                |
| T09–T10 | （無交接）說明能力邊界                          |
| T11–T12 | `primary → 專家 A → primary → 專家 B → primary` |

如果結果不一致，**通常是環境變數沒帶上去**（例如 `MODEL_DEPLOYMENT_NAME` 在 azd 環境的值與 `src/.env` 不同）。

Playground 對話的對象是整個多代理系統。`HandoffResponsesHost` 將四代理工作流包成單一
Responses 端點，使用公開的 `response_handler` 與 `Workflow.run(responses=...)` 接口，
把「繼續」等文字交回正在等待的 Handoff 請求，而不是要求 Playground 傳送工具回傳值。

每輪重新建立工作流，並依使用者與 conversation 還原 checkpoint；不共用上一輪的 executor
記憶體狀態。checkpoint 僅額外允許 Handoff 請求、`list[Message]` 型別描述與 Azure role enum，
不關閉反序列化限制。本機 checkpoint 位於忽略版控的 `.handoff-checkpoints/`。
託管時寫入 `$HOME/.handoff-checkpoints/`，讓 session 停止後仍能還原；容器根目錄不是
持久儲存。保存前移除 SDK 的 `raw_representation` 除錯物件，保留文字、工具資料與結構化
結果；若最後一輪 checkpoint 無法保存，回傳失敗而非假裝可以繼續對話。
這個端點接受文字訊息；程式碼與架構材料請直接貼成文字，不使用附件或外部工具回傳值。

---

## 步驟七：用 CLI 呼叫

```bash
azd ai agent invoke "請幫我看這段程式碼有沒有問題：result = eval(user_input)"
```

確認端點可由程式呼叫。

多行材料建議寫成 UTF-8 JSON 檔（`{"input":"完整提示與材料","stream":true}`），
再用 `azd ai agent invoke --input-file request.json`，避免 Windows 命令列只送出第一行。
同一題的追問保留原 session 與 conversation；獨立題目可加上 `--new-conversation`。
如果從舊版 generic WorkflowAgent host 升級，第一次須加上 `--new-session --new-conversation`，
因為舊 session 綁定舊版本，且舊 checkpoint 不會自動遷移。

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

---

## 驗收

- [ ] Agents 區段看得到該 hosted agent，狀態為 `active`
- [ ] Playground 可與**整個多代理系統**對話（不是只能與單一 agent 對話）
- [ ] 12 題的交接路由結果**與 Lab 2 一致**
- [ ] `azd ai agent invoke` 呼叫成功
- [ ] Application Insights 中看得到本次對話的 traces
- [ ] traces 中**搜不到**你貼上的程式碼原文、個人資料或憑證

---

## 常見問題排除

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

進入 [Lab 4：發布到 Teams](04-lab4-publish-teams.md)。

> **提示：** 閒置超過 15 分鐘後，Lab 4 的第一則訊息可能因冷啟動而較慢。
