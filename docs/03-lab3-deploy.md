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

設定四個值（把角括號換成你自己的）：

```bash
azd env set AZURE_RESOURCE_GROUP <你的資源群組名稱>
azd env set AZURE_AI_ACCOUNT_NAME <你的 Foundry 帳戶名稱>
azd env set AZURE_AI_PROJECT_NAME <你的 Foundry 專案名稱>
azd env set MODEL_DEPLOYMENT_NAME <你的模型部署名稱>
```

前三個可從 `.env` 的專案端點推得，或回 Foundry portal 複製；第四個與 `src/.env` 的 `MODEL_DEPLOYMENT_NAME` 相同。

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

本機執行時，`AzureCliCredential` 用的是你 `az login` 的身分；容器內沒有 `az login`，託管後改以**受控識別**（managed identity）向 Foundry 取權杖。此事平台會處理，你只需確認權限給對。

| 角色                               | 範圍     | 用途               |
| ---------------------------------- | -------- | ------------------ |
| Foundry Project Manager            | 專案     | 建立／呼叫 agent   |
| Azure Bot Service Contributor Role | 資源群組 | Lab 4 發布到 Teams |

> ⚠️ 若 `azd provision` 回報權限錯誤，通常是缺 `Foundry Project Manager`（專案範圍）。訂閱的 Owner **不包含**此角色，Lab 0 的 preflight 會檢查它。

---

## 步驟四：部署映像

```bash
azd deploy
```

執行後映像會在 Azure 雲端建置（本機不需安裝 Docker），接著推送、建立 agent 版本並啟動容器，全程需要數分鐘。請保持命令執行，完成後再繼續下一步。

> ⚠️ `azd deploy` 每次執行都會**重寫 `azure.yaml` 並清掉所有註解**。這是 azd 的行為，非你弄壞的。設定值本身不受影響；要找回註解，從版本控制還原該檔即可。

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

送出**與 Lab 2 完全相同**的 12 題。

要取得某一題的完整內容（含要貼上的材料）：

```bash
cd src
uv run python -c "from fixtures.test_items import TEST_ITEMS, render_prompt; print(render_prompt(TEST_ITEMS[0]))"
```

把索引 `[0]` 換成 `[1]`⋯`[11]` 即可取得第 2 到第 12 題。

要一次看完十二題，開 `src/fixtures/materials/test_items.csv`，內容與上述指令相同。

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

**這一步的重點是「一致」，不是「會動」。** 交接路由結果應與你在 DevUI 看到的相同：

| 題號    | 預期路由                                        |
| ------- | ----------------------------------------------- |
| T01–T02 | `primary → coding → primary`                    |
| T03–T04 | `primary → architect → primary`                 |
| T05–T06 | `primary → spec → primary`                      |
| T07–T08 | （無交接）先反問                                |
| T09–T10 | （無交接）說明能力邊界                          |
| T11–T12 | `primary → 專家 A → primary → 專家 B → primary` |

如果結果不一致，**通常是環境變數沒帶上去**（例如 `MODEL_DEPLOYMENT_NAME` 在 azd 環境的值與 `src/.env` 不同）。

另注意：在 Playground 中，你是跟**整個多代理系統**對話，而非單一 agent。這是 `workflow.as_agent()` 的效果——四個 agent 對外看起來就是一個。

---

## 步驟七：用 CLI 呼叫

```bash
azd ai agent invoke --message "請幫我看這段程式碼有沒有問題：result = eval(user_input)"
```

這證明它是**可由程式呼叫的端點**，而非只能在 Foundry portal 操作。

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

> ⚠️ 若在此休息超過十五分鐘，Lab 4 的第一則訊息會明顯較慢——那是冷啟動，不是壞掉，稍候即可。
