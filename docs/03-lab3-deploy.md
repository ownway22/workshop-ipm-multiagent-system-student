# Lab 3：部署到 Azure

**前置條件**：[Lab 0](00-lab0-env-check.md) 的工具、登入與 `src/.env` 已驗證，並完成
[Lab 2](02-lab2-multi-agent.md) 的本機交接。以下命令使用 Bash／WSL，**每個區塊都標示起始目錄**；
不要在 `deploy/` 再執行 `cd src`，也不要在 `src/` 執行專案範圍的 `azd` 命令。

**操作成功條件**：確認實際受測的 source、image、agent version 與 session，
以真實模型完成交接、保留有效專家結果，並在 Playground 的同一對話純文字續接。
`active`、`latest`、readiness HTTP 200 或 Responses `completed` 都不是單獨的驗收證據。

---

本 Lab 的課程目標：

1. 將本機四代理系統部署成可呼叫的 hosted agent。
2. 在 Playground 與 CLI 觀察正確交接及純文字多輪續接。
3. 完成 12 題，與自己在 Lab 2 的結果比較。
4. 確認本次操作的 traces／console logs 可關聯，且不洩漏訊息原文或憑證。

## 部署前先分清五種識別

| 識別 | 代表什麼 | 必須留下的核對資料 |
| --- | --- | --- |
| Source | 本次要部署的程式與 lock | Git SHA、工作區是否有修改、實際 build context |
| Image | 該 source 建出的容器 | 建置紀錄、immutable digest；tag 本身不足以證明內容 |
| Agent version | 某個 agent 的版本定義 | **專案＋agent 名稱＋version**，以及定義中的 image |
| Endpoint routing | 新請求如何選版本 | 該 agent endpoint 的 version selector；不要把 `latest` 當授權 |
| Session／conversation | compute／檔案與對話 lineage | Session ID、實際 `version_indicator`、Conversation ID 或 response chain |

## 步驟一：看懂三個部署檔與目標

| 檔案 | 現行用途 |
| --- | --- |
| [deploy/Dockerfile](../deploy/Dockerfile) | repository 根目錄為 context；鎖定 lock，啟動 `main.py --responses --host 0.0.0.0` |
| [deploy/agent.yaml](../deploy/agent.yaml) | agent 名稱、Responses 2.0.0、模型部署變數及敏感內容記錄設定 |
| [deploy/azure.yaml](../deploy/azure.yaml) | `azure.ai.agents` extension、remote build、`linux/amd64`、CPU／memory |

目前 service 與 agent manifest 的名稱均是 **`qvn-ipm-review`**，必須逐字一致。
不能只改 CLI 的名稱就假設部署已改到獨立測試 agent。`FOUNDRY_*`／`AGENT_*`
是平台保留前綴；端點與 instance identity 由平台注入，模型部署名稱使用
`MODEL_DEPLOYMENT_NAME`，不自行新增保留字變數。

> **共用環境先停在這裡確認授權。** 新增 azd environment 只隔離本機設定，
> 不會自動隔離 Foundry project／agent。相同專案與 agent 名稱仍可能操作既有資產，
> 而建立新版本可能影響使用 `@latest` 的 endpoint。若只獲准測試而未獲准原 agent
> 路由切換，不能照下面預設 `qvn-ipm-review` 直接 deploy；先確認獨立目標與部署資產的安排。
> #6 的獨立測試 agent 不是這三個檔案自動選用的目標。

## 步驟二：初始化 azd 環境

### 首次部署到已獲准的環境

```bash
# 起始目錄：repository 根目錄
cd deploy
azd env new ipm-workshop
uv run --project ../src --locked python scripts/configure_azd_env.py
azd env get-values
```

[configure_azd_env.py](../deploy/scripts/configure_azd_env.py) 從 `src/.env` 讀取
`FOUNDRY_PROJECT_ENDPOINT`／`MODEL_DEPLOYMENT_NAME`，再以**目前 shell 可呼叫的 Azure CLI**
查證登入訂閱、租戶、資源群組、Foundry 帳戶、專案 ARM ID 與模型部署。
全部驗證成功才寫入選取的 azd environment；不是離線匯入。

核對 `AZURE_ENV_NAME`、`AZURE_SUBSCRIPTION_ID`、`AZURE_RESOURCE_GROUP`、
`AZURE_AI_ACCOUNT_NAME`、`AZURE_AI_PROJECT_NAME`、`AZURE_AI_PROJECT_ENDPOINT`、
`MODEL_DEPLOYMENT_NAME` 與你獲准的目標一致。不要把完整 env 輸出貼進公開 issue。

### 既有環境升級

```bash
# 起始目錄：repository 根目錄
cd deploy
azd env select ipm-workshop
azd env get-values
```

先沿用正確的 environment，不為升級另建一個指向相同 project／agent 的 environment。
`src/.env`、登入訂閱或目標有變動時，重新確認授權後才重跑上面的匯入工具；
只修改 `src/.env` **不會**自動更新 azd environment 或已部署 agent。

本頁 Bash 範例的前提是該 shell 的 `az` 與 `azd` 登入可用且一致。
#6 的 Azure 實測採 Windows 原生 Azure CLI／WAM；私有診斷 wrapper 不是 repository
的通用操作工具，也未證明 `configure_azd_env.py` 可自動使用另一個 shell 的 WAM 登入。
遇到登入環境不一致，先採組織核准的登入方式，不搬移 token／cache、不繞過 TLS 或 script policy。

## 步驟三：建立基礎設施

首次部署，或已確認缺少所需接線且獲准變更時：

```bash
# 起始目錄：deploy/
azd provision
```

[main.bicep](../deploy/infra/main.bicep) 在既有 resource group 建立 ACR，
將 AcrPull 授予既有 Foundry project managed identity、將 Container Registry Tasks Contributor
授予部署者，並建立 project 的 ContainerRegistry 連線。
它**不建立 Foundry project／模型部署**，也不在此建立 hosted agent。

既有升級環境若 ACR、授權與連線已完整，不因 SDK 改版盲目重跑 provision。
新 environment 名稱會參與 ACR／連線命名；新增 environment 可能建立另一份資產，而非隔離原 agent。
權限錯誤先請環境管理者確認資源群組部署／角色指派及 Foundry 專案權限；
訂閱 Owner 不等於具備 Foundry Project Manager。不要把自行擴增 RBAC 當一般排錯步驟。

### 執行時身分不等於部署者

本機使用 `AzureCliCredential`；託管時由 `AgentConfig.from_env().is_hosted` 選擇
`ManagedIdentityCredential` 與平台提供的 `FOUNDRY_AGENT_INSTANCE_CLIENT_ID`。
「你能 deploy」不代表容器身分能呼叫模型；需以本次 session 的身分／模型呼叫記錄核對。
#6 沒有改這項選擇、CPU／memory、Bicep／RBAC 或 Teams 配置。

## 步驟四：部署映像

確定 source、選取 environment、agent 目標及路由影響均已獲准後：

```bash
# 起始目錄：deploy/
azd deploy
```

現有 `remoteBuild: true` 在 Azure 建置 `linux/amd64` image；Dockerfile 使用
`uv sync --locked --no-dev`，再由 extension 建立 agent version。
保存 source SHA、建置 run／digest、agent 名稱與新 version，不只保存「部署成功」訊息。
不要為了強制新版本修改描述文字；先核對實際 code／lock／image 是否變更。
不要重建完全相同的 image 來替代版本或路由查證。

azd 曾重寫 `azure.yaml` 並清除註解；部署後查看差異，分清格式／註解與設定值變動。
不要以 reset 或直接覆蓋整份檔案丟掉其他開發者的修改。

## 步驟五：輪詢到 `active`

```bash
# 起始目錄：deploy/
azd ai agent show --output json
```

`creating`／`updating` 時隔一段時間再查；`failed` 時讀本次 session／system logs，
不要無限 deploy retry。`active` 只代表版本可啟動，還須核對下面資料。
`show` 依 `azure.yaml`／environment 解析目標，不一定等於 endpoint 現在實際送達的版本。

### 以公開 API 核對 agent 定義、routing 與 regular version

以下為 Bash／WSL，需現行 shell 的 `az` 登入可用。端點取自已核對的 environment，
不拼猜 account、registry 或其他專案 URL：

```bash
# 起始目錄：deploy/
PROJECT_ENDPOINT="$(azd env get-value AZURE_AI_PROJECT_ENDPOINT)"
AGENT_NAME="qvn-ipm-review"          # 改為已獲准且確實存在的目標，不是改部署設定
AGENT_VERSION="<本次查得的 regular version>"
az rest --method get --resource https://ai.azure.com \
  --url "${PROJECT_ENDPOINT}/agents/${AGENT_NAME}?api-version=v1" --output json
az rest --method get --resource https://ai.azure.com \
  --url "${PROJECT_ENDPOINT}/agents/${AGENT_NAME}/versions/${AGENT_VERSION}?api-version=v1" --output json
```

核對回讀定義的 image 與本次 digest，及 agent endpoint 的 version selector。
`source_sha` 若有記錄可比對；它不是所有 azd 部署都保證提供的欄位，沒有時須保留建置 provenance，
不能補寫一個看似已驗證的 SHA。SDK headers 只能輔助確認套件組合，不能代替 image／version 核對。
實際 response 的 `agent_reference.version`／`agent_session_id`（若服務提供）也應一致。

### 可選：供程式測試的新 session 版本 binding

Session ID 代表 compute／檔案；Conversation ID 或 `previous_response_id` 才代表對話 lineage。
只換 conversation、只選 Playground version，或只看 `latest`，都不證明舊 session 已換版。

本地 preview help 支援 `azd ai agent sessions create --version`，但 `invoke` 沒有 `--version`。
#6 實際驗證的公開版本 binding 是 **regular version 的 REST `version_indicator`**：

```bash
# 起始目錄：deploy/；沿用上一區塊已核對的 PROJECT_ENDPOINT／AGENT_NAME／AGENT_VERSION
SESSION_ID="$(uv run --project ../src --no-sync python -c 'import uuid; print("lab3-" + uuid.uuid4().hex)')"
az rest --method post --resource https://ai.azure.com \
  --url "${PROJECT_ENDPOINT}/agents/${AGENT_NAME}/endpoint/sessions?api-version=v1" \
  --body "{\"agent_session_id\":\"${SESSION_ID}\",\"version_indicator\":{\"type\":\"version_ref\",\"agent_version\":\"${AGENT_VERSION}\"}}" \
  --output json
az rest --method get --resource https://ai.azure.com \
  --url "${PROJECT_ENDPOINT}/agents/${AGENT_NAME}/endpoint/sessions/${SESSION_ID}?api-version=v1" \
  --query "{id:agent_session_id,status:status,version:version_indicator}" --output json
```

這是供程式測試的可選步驟，不需要為 GUI 示範先建立一個無法附掛的 session。
它會建立可能計費的 session，先取得執行授權。回讀的 ID 與 `version_indicator.agent_version`
必須符合選定目標，再送模型輸入；若受理不明，先查同 ID，不盲目重建。
`@latest` 不是這個 concrete version 參數；不要把 regular version 的證據推廣成 draft binding 保證。
原 agent 的 draft／routing 曾有不一致紀錄，見[歷史證據](native-hosting-migration.md)。

REST 的版本 binding 不會修改 endpoint selector。CLI `sessions create` 的 help 是語法證據，
不等於此 extension 在所有專案／版本上的雲端 binding 已驗收。
REST 預建 session 主要供程式呼叫；不保證 Playground 能附掛任意現有 Session ID。
GUI 的版本選擇與 session binding 仍須用該次 metadata 核對，不把 CLI 的 session 自動當成 GUI session。
更多公開 session／Responses 路徑見
[Manage hosted agent sessions](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-sessions)；
不能用未知 header 強迫版本或假設只要 active 就一定送達它。

## 步驟六：Playground 的 canonical T01 最小示範

在 Foundry portal 選擇**已獲准的 project＋agent＋版本**，開新 session／conversation；
介面沒有明確的新 session 操作時，依上一節或服務回讀 metadata 確認 binding，不編造 UI 按鈕。
在新 image 首次驗收，不沿用舊版對話。

```bash
# 起始目錄：repository 根目錄；生成材料，不呼叫模型
cd src
uv run --no-sync python -c "from fixtures.test_items import TEST_ITEMS, render_prompt; import sys; p = render_prompt(TEST_ITEMS[0]); assert len(p) == 1596; sys.stdout.write(p)"
```

複製完整輸出，含材料與 code fence。不要從 raw CSV 原文複製 `""`；
若使用 CSV，須複製試算表已解碼的「提示」儲存格。本程式不自動改寫你的程式碼或解開 CSV 引號。

1. 送出完整 T01，等回應完成。
2. **同一對話**送第二輪，以下文字來自已保存的 #6 canonical API 驗收紀錄：

   ```text
   請只根據上一輪 Coding 的健檢結果，用純文字列出原本的嚴重度與最優先兩項建議；不要重新健檢、交接或新增發現。
   ```

3. 第二輪完成後，同一對話送第三輪：

   ```text
   同一份結果請保留原嚴重度及專家來源，再用三個條列列出可執行的修正步驟；不要重做分析或交接。
   ```

兩個追問不是新要求的原始碼改寫／再健檢。三輪間不要換 agent、版本、session 或 conversation，
也不要重送第一輪當成續接。失敗先查已受理的 response／session 記錄。

### 看什麼才算操作成功

- 同次 framework 記錄有 `qvn-primary-agent → qvn-coding-agent → qvn-primary-agent`，
  最後為 `Handoff waiting for user input: source=qvn-primary-agent`。
- 原材料完整，Coding 有有效 `SpecialistReview`，不是只有 control call；原專家結果投射一次。
- Primary 逐字保留有效結果、來源與原嚴重度，不採用額外補寫的分析。
- 第二／第三輪能接原結果，不重播整份 review、不發生未要求的再交接；原嚴重度與來源保留。
- Source／image／version／session 證據一致，checkpoint 能保存並恢復。

「來源：Coding」字樣、UI quality 分數或 protocol `completed` 不證明 Coding 真的被呼叫。
路由需對應同筆 response／session 的 framework transition；沒有記錄時列為未證實，不用文字猜測。
驗證文字完整性使用原始回應／checkpoint 或實際 UI，不以 CLI pretty formatter 的中文字形判斷。
Checkpoint 含完整對話，若下載核對只存於私有位置，維持原 allowed types，不關閉反序列化限制。

## 步驟七：CLI 呼叫與續接

先在自己的 shell 查看 `azd ai agent invoke --help`；本頁核對的 preview 支援
`--new-session`、`--new-conversation`、`--session-id`、`--conversation-id`、`--input-file`、`--timeout`。
以下是 **CLI 示範**，不是宣稱 #6 用同一條 azd 指令完成原始驗收：

```bash
# 起始目錄：deploy/；目標／environment／routing 已獲准並核對
PROMPT="$(cd ../src && uv run --no-sync python -c 'from fixtures.test_items import TEST_ITEMS, render_prompt; import sys; sys.stdout.write(render_prompt(TEST_ITEMS[0]))')"
azd ai agent invoke qvn-ipm-review "$PROMPT" --protocol responses \
  --new-session --new-conversation --timeout 180
azd ai agent invoke qvn-ipm-review \
  "請只根據上一輪 Coding 的健檢結果，用純文字列出原本的嚴重度與最優先兩項建議；不要重新健檢、交接或新增發現。" \
  --protocol responses --timeout 180
azd ai agent invoke qvn-ipm-review \
  "同一份結果請保留原嚴重度及專家來源，再用三個條列列出可執行的修正步驟；不要重做分析或交接。" \
  --protocol responses --timeout 180
```

這依 CLI 儲存的同 agent session／conversation 續接。保存第一輪的實際 ID，讀回版本一致才往下；
CLI 預設解析的 environment version／endpoint routing 不符合目標時，不用此示範強行驗收。
第一輪若使用 `--new-session`，會另建 session；後續 monitor／stop 的 `SESSION_ID`
必須改成它實際回傳的 ID，不沿用先前 REST 預建的 ID。預建 session 與 CLI 自建示範擇一使用。
若採上一節預建 session，`invoke --session-id "$SESSION_ID" --new-conversation` 是 help 支援的
指定方式，但須另核對實際回應；不要加不存在的 `invoke --version`，不要假設未測過的組合一定成功。
獨立新題目可用新 conversation；同題追問不得加 `--new-session`／`--new-conversation`。

多行內容可改用 UTF-8 JSON request 檔與 `--input-file`，它是**request body**，不是 CSV 或純文字檔。
直接使用 Responses REST／SDK 時：同一 named conversation 續接，或同時帶
`previous_response_id` **及同一 `agent_session_id`**；不要同時傳 conversation 與 previous response。
原 host 會明確拒絕找不到 checkpoint 的續接，不自動重開成功對話。
CLI 成功仍不能代替 Playground 成功。

## 新版 runtime 行為與代價

三位專家保留原 instructions／strict schema／交接工具，模型先產出 review 或釐清文字，
再由程式補必要的回交控制 call，SDK 實際交回 Primary；control-only 回覆會被拒絕。
同次完全相同的合法 review 可保守合併，不丟掉不同結果。

Primary 不指定 JSON response format，**真實模型仍決定路由、完成與釐清**；
本輪有效 `SpecialistReview` 的最終呈現忠實保留來源順序、摘要、發現、證據、建議、限制與嚴重度，
不重貼整份專家 JSON、不使用模型另外補寫的分析。中途待釐清保留已完成結果。

四位 runtime participants 的**內部模型請求**均使用 `store=False`，外層 Responses 保存仍保留。
SDK 本機 history 與既有 checkpoint 仍持久化，下一輪會 replay 完整上下文，
輸入 Token 成本／快取效果可能改變，**不是零額外成本**。
首次升級使用新 session／conversation，不自動遷移、刪除舊 checkpoint 或清掉舊服務端 ID。
資料位置／相容證據見[升級紀錄](native-hosting-migration.md#checkpoint-版本邊界新-session-與-rollback)；
不能把限定合成還原測試推廣成任意舊 state 都相容。

## 步驟八：console logs 與 traces

```bash
# 起始目錄：deploy/；SESSION_ID 必須是本次自己受測的實際 ID
azd ai agent monitor qvn-ipm-review --session-id "$SESSION_ID" --tail 100
azd ai agent monitor qvn-ipm-review --session-id "$SESSION_ID" --type system --tail 100
```

名稱須符合這個 environment 的 service。範例先取有限行數，不無限 `--follow`；
console 中核對 `Handoff transition`、`Handoff output`、`Handoff waiting for user input`，
並與本次 response／時間對應。過晚下載可能只取得較新的 logs，不能用不同時段解釋先前失敗。

Tracing 的**預期操作**是到 Foundry 的 Tracing／Traces，或專案關聯 Application Insights 的 Logs：

```kusto
// 起始介面：本次 project 關聯 Application Insights → Logs
AppTraces
| where TimeGenerated > ago(30m)
| order by TimeGenerated desc
| take 50
```

Table 名稱依 telemetry 接線而異。這不是「一定看得到 traces」的保證：
截至 #6 合併，Application Insights 連線／A365 ImportError／exporter Bad Request 尚未解決，
也未通過完整 tracing 驗收。沒有直接證據可把它們當成 Handoff 問題的根因。
缺 traces 時分開查接線、權限、服務健康與 console 路由，修接線／RBAC 另取授權，不為驗收開啟
敏感內容記錄。`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` 保持不變；
完整課程仍要驗證 traces 搜不到訊息原文、個資或憑證。

## 收尾：停止 compute，不刪資料

Merge、關閉 Playground 視窗或看到 `idle`，都不代表你已主動停止 compute／計費。
只操作自己本次 session；本地 preview 沒有 `sessions stop` 子命令，採已驗證的公開 REST：

```bash
# 起始目錄：deploy/；PROJECT_ENDPOINT／AGENT_NAME／SESSION_ID 為前面核對的同一目標
az rest --method post --resource https://ai.azure.com \
  --url "${PROJECT_ENDPOINT}/agents/${AGENT_NAME}/endpoint/sessions/${SESSION_ID}:stop?api-version=v1" \
  --output none
az rest --method get --resource https://ai.azure.com \
  --url "${PROJECT_ENDPOINT}/agents/${AGENT_NAME}/endpoint/sessions/${SESSION_ID}?api-version=v1" \
  --query "{id:agent_session_id,status:status,stopped_at:stopped_at}" --output json
```

確認同 ID 回讀的 `idle` **及本次有效 `stopped_at`**，資料仍保留；只看 idle 不足。
Stop 會中止正在執行的容器／對話，先完成或明確中止本次操作。
Portal Files／Log stream 或檔案讀取曾恢復 compute；後續讀資料需再核對並停止，
不能宣稱它們永遠只讀、不計費。保留舊 session／checkpoint，不以 delete 作為一般升級預設。

## 目前已驗證範圍

以下是 **2026-10-14 #6 留存證據**，不是讀者環境的新驗收：

| 項目 | 已驗證／仍有限制 |
| --- | --- |
| 新版 source／image | Source `752699b`、54 tests／117 subtests 與 runtime mypy；實際 CI build／smoke 通過，詳見[報告](native-hosting-migration.md) |
| 雲端目標 | 僅獨立 `qvn-ipm-review-sdk6-validation` regular version 6；原 `qvn-ipm-review` 路由未切換 |
| Canonical T01 | `render_prompt(TEST_ITEMS[0])` 的 1,596 字元，API 三輪通過 |
| GUI 指定流程 | 1,646 字元 raw CSV 變體的指定三輪通過；不能混稱同一輸入的 canonical GUI／API 通過 |
| 額外自由詢問 | 曾保存未釐清的短回覆，不保證任意追問全部通過；與 canonical GUI 缺口由 [#7](https://github.com/ownway22/workshop-ipm-multiagent-system-student/issues/7) 追蹤 |
| T03／T10／T12 | 新 image 依使用者指示未重測，不標 PASS；T12 漏 Architect 的歷史限制保留 |
| Traces／完整鏈路 | Telemetry 問題未因 merge 解決；完整 12 題及 Lab 2 → Lab 3 → Lab 4 未全部驗收 |

## 完整 workshop 驗收目標

T01 是最小示範與這次接受限制後的合併門檻，**不是取消以下課程目標**：

| 題號 | 預期控制路由／行為 |
| --- | --- |
| T01–T02 | `primary → coding → primary` |
| T03–T04 | `primary → architect → primary` |
| T05–T06 | `primary → spec → primary` |
| T07–T08 | 無交接；先釐清材料／意圖 |
| T09–T10 | 無交接；說明能力界線 |
| T11 | `primary → coding → primary → architect → primary` |
| T12 | `primary → spec → primary → architect → primary`；歷史漏 Architect 仍列 FAIL／限制 |

其他題目的材料用同一個 `render_prompt` 方法換索引 `[1]` 到 `[11]`。
T11 能檢查逐次跨域交接，但不能代替其他題目或把 T12 限制改成 PASS。

- [ ] 實際 image／agent version／session binding 與預期相同，託管身分能呼叫模型。
- [ ] Playground 與 CLI 在新對話完成交接及第二／第三輪純文字續接，輸出完整且無非預期重播。
- [ ] 12 題逐項檢查實際路由、有效 schema、忠實彙整與業務完成，與 Lab 2 比較。
- [ ] Console／traces 能關聯本次對話，且不含原始訊息、個資或憑證。
- [ ] 已停止自己受測的 compute、保留資料；Lab 4 另行完成，不把 Lab 3 active 當 Teams 通過。

### 短期 hotfix 的歷史驗證與範圍

2026-10-10 version 11 的 T01／續接／T11 PASS、T12 FAIL，以及 2026-10-11 原 agent
最後已知 version 13，都是[既有歷史紀錄](native-hosting-migration.md#review-與合併驗證邊界)。
#6 沒有將原 agent 自動切回該版本；完整 chronology、後續 FAIL／skip 及受測 image 見
[升級報告](native-hosting-migration.md)與[已知限制](../references/known-gaps.md)，不在操作主線重複列長表。

## 常見問題排除

| 現象 | 優先處理 |
| --- | --- |
| 升級後行為仍像舊版 | 先核對 environment、agent 名稱、image、版本與實際 session binding；新建 session／conversation，不刪舊 checkpoint |
| active／readiness 200，但對話失敗 | 查同筆 response 與 session logs、Responses 2.0.0、MI 模型呼叫；不是單憑狀態或 env 推測根因 |
| show／invoke 404 | 核對 service／agent 名稱一致及所選 environment；不要亂換 endpoint |
| 找不到可續接 checkpoint | named conversation 保持同一個；response chain 同帶原 session ID；必要時開新對話，不重開偽裝舊對話成功 |
| 專家只有 control call | 新版會拒絕；查實際 provider terminal／parsed／材料，不把控制 call 當審查 |
| Registry authentication／權限錯誤 | 核對 ACR 接線及既有 MI／部署者權限，環境管理者批准後才補 provision／RBAC |
| `failed to scan dependencies` | 核對原 Dockerfile；`--platform` 在 azure.yaml 的 docker.platform，不放 FROM |
| 模型 429／逾時／受理不明 | 先查本次 response 狀態，不盲目重送；配額／timeout 政策變更另確認 |
| 閒置後冷啟動／不見 traces | 等本次狀態、查 console 與接線；閒置 timeout 依實際版本設定，不以固定 15 分鐘保證或重 deploy |

進入 [Lab 4](04-lab4-publish-teams.md) 前，完成自己目標環境的上述驗收，並另確認發布權限。
SDK 合併或獨立測試 agent 的成功，不代表原 agent 已可直接發布。
