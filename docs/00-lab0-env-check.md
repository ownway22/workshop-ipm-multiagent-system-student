# Lab 0：環境驗證

**前置條件**：可建立資源的 Azure 訂閱，且與 Microsoft 365 帳號位於同一租戶。
**完成條件**：`uv run python -m preflight` 輸出 `6/6 通過`。

---

## 目標

核對八項環境需求，再用一支指令自動驗證其中六項。

---

## 步驟一：核對八項環境需求

preflight 自動檢查六項；網路可達性與 Teams 租戶一致性需自行核對。

| 環境需求（八項）   | preflight 自動檢查（六項）     | 說明                                          |
| ------------------ | ------------------------------ | --------------------------------------------- |
| 1. Azure 訂閱      | [1/6] 登入的訂閱               | 只驗證登入的訂閱，**不驗證租戶一致性**        |
| 2. 網路可達性      | —                              | **無法自動檢查**，請用下方第 2 項的指令核對   |
| 3. 登入            | [1/6] 登入的訂閱               | 與第 1 項合併為同一項檢查                     |
| 4. Foundry 專案    | [2/6] 資源區域、[5/6] 專案端點 | 一項需求拆成區域與連線兩項檢查                |
| 5. 模型部署        | [6/6] 模型能力                 | 實測 tool calling 與 structured outputs       |
| 6. provider 註冊   | [3/6] provider 註冊            | —                                             |
| 7. 角色指派        | [4/6] 角色指派                 | —                                             |
| 8. Microsoft Teams | —                              | **無法自動檢查**，租戶不一致要到 Lab 4 才顯現 |

> **注意：** 權限、專案區域、租戶一致性或網路可達性不符時，現場無法補救。請參考[現場無法修復的項目](#現場無法修復的項目)。

### 1. Azure 訂閱

- [ ] 有可建立資源的 Azure 訂閱
- [ ] 該訂閱與要使用的 **Microsoft 365 帳號在同一租戶**（Lab 4 發布到 Teams 的必要條件）
- [ ] 已安裝 Azure CLI（`az --version`）

### 2. 網路可達性

在上課用的網路環境執行：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://login.microsoftonline.com/common/discovery/instance
```

- [ ] 回應 `400`（該端點未帶參數，回 400 即代表可連線）

> **注意：** 逾時且沒有回應，表示 VPN 或資安代理可能攔截 Microsoft 登入端點。請洽 IT 或改用不受管制的網路。
>
> 自我診斷：`getent hosts login.microsoftonline.com`（Linux／WSL）。若回傳 IP 不像 Microsoft 位址（例如 `6.6.x.x`），即為被攔截。

### 3. 登入

```bash
az login --use-device-code
az account show --query "{name:name, id:id, tenant:tenantId}" -o table
```

- [ ] 顯示的訂閱為預期使用的訂閱

> **注意：** 若出現 `AADSTS530036`，請改用帶 scope 的登入指令：
>
> ```bash
> az logout
> az login --scope "https://ai.azure.com/.default"
> ```

### 4. Foundry 專案

- [ ] 已有 **Microsoft Foundry 專案**（非舊的 hub-based 專案）
- [ ] 專案**區域**在下方支援清單內
- [ ] 已從 Foundry portal 首頁複製「專案端點」（形如 `https://<帳戶>.services.ai.azure.com/api/projects/<專案>`）

**hosted agents 支援區域**（共 31 個，**East Asia 不在其中**）：

```text
australiaeast      brazilsouth        canadacentral      canadaeast
centralus          eastus             eastus2            francecentral
germanywestcentral italynorth         japaneast          japanwest
koreacentral       northcentralus     norwayeast         polandcentral
southafricanorth   southcentralus     southeastasia      southindia
spaincentral       swedencentral      switzerlandnorth   switzerlandwest
uaenorth           uksouth            ukwest             westcentralus
westeurope         westus             westus3
```

參考文件：<https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#region-availability>

### 5. 模型部署

- [ ] 專案內已有支援 **tool calling** 與 **structured outputs** 的模型部署
- [ ] 已複製「部署名稱」（**非**模型型號）

建議使用 **`gpt-5.4-mini`**。在 Foundry portal 手動部署的步驟如下：

1. 開啟 Foundry portal，進入專案。
2. 左側選單點 **Deployments**。
3. 點 **Deploy model** → **Deploy base model**。
4. 搜尋 `gpt-5.4-mini`，選取後點 **Confirm**。
5. **Deployment name** 保持預設或自訂；此名稱即稍後填入 `.env` 的值。
6. Deployment type 選 **Global Standard**，容量依配額調整。
7. 點 **Deploy**，等待狀態變為 **Succeeded**（數分鐘）。
8. 回 Deployments 清單，複製**部署名稱**。

### 6. provider 註冊

```bash
az provider register --namespace Microsoft.CognitiveServices
az provider register --namespace Microsoft.BotService
```

確認狀態：

```bash
az provider show --namespace Microsoft.CognitiveServices --query registrationState -o tsv
az provider show --namespace Microsoft.BotService --query registrationState -o tsv
```

- [ ] 兩者皆為 `Registered`

> **提示：** 註冊需數分鐘，在 Lab 3 前完成即可。

### 7. 角色指派

本 workshop 的相容路徑使用下列**兩個指定角色**，範圍不同。
Foundry 的 **data plane** 與 Azure Resource Manager（ARM）管理操作不能混為一談：
`Owner`／`Contributor` 一般涵蓋 `Microsoft.BotService/botServices/write` 等 Bot **ARM**
操作，但不因此取得 Foundry data plane 的 agent／模型權限。角色名稱不同，也可能有等效權限；
preflight 只查下列兩個 GUID，並不展開所有 Actions／DataActions、ABAC condition 或 deny assignment。

以下指令應由在目標 scope 有 `Microsoft.Authorization/roleAssignments/write`、
且 role assignment condition 允許這些 job roles 的管理員核對後執行。
受授權 objectId 必須是**學員**，不是代跑管理員。不要為了課程授予學員
Owner、User Access Administrator 或 Role Based Access Control Administrator，也不要移除 condition。

```bash
# 學員在自己的 shell 取得 objectId，交給管理員；不要在管理員 shell 重查自己的 id
az ad signed-in-user show --query id -o tsv
OBJ="<學員提供的 objectId>"

# 角色 1：Foundry Project Manager（範圍 = Foundry 專案）
az role assignment create \
  --role "eadc314b-1a2d-4efa-be10-5d325db5065e" \
  --assignee-object-id "$OBJ" --assignee-principal-type User \
  --scope "/subscriptions/<訂閱ID>/resourceGroups/<資源群組>/providers/Microsoft.CognitiveServices/accounts/<帳戶>/projects/<專案>"

# 角色 2：Azure Bot Service Contributor Role（範圍 = 資源群組）
az role assignment create \
  --role "9fc6112f-f48e-4e27-8b09-72a5c94e4ae9" \
  --assignee-object-id "$OBJ" --assignee-principal-type User \
  --scope "/subscriptions/<訂閱ID>/resourceGroups/<資源群組>"
```

- [ ] 兩個角色都指派完成

> **提示：** 使用 GUID 可避免角色顯示名稱調整後失效。
>
> | 角色                               | GUID                                   | 範圍         |
> | ---------------------------------- | -------------------------------------- | ------------ |
> | Foundry Project Manager            | `eadc314b-1a2d-4efa-be10-5d325db5065e` | Foundry 專案 |
> | Azure Bot Service Contributor Role | `9fc6112f-f48e-4e27-8b09-72a5c94e4ae9` | 資源群組     |

**Lab 3 是另一層授權**：自助 `azd provision` 會建立 ACR、在 ACR scope 指派
AcrPull 給 project managed identity、Container Registry Tasks Contributor 給學員，並寫入
project ContainerRegistry connection。前述兩個角色不證明學員能寫入 ACR role assignments。
沒有該權限時，走 [Lab 3 管理員 bootstrap](03-lab3-deploy.md#路徑-b管理員-bootstrap)，
沿用學員原 environment 與既有 project／model；不必授予學員 RBAC Administrator。

管理員的 condition 若只禁止 Owner／User Access Administrator／Role Based Access Control
Administrator，並不代表必然禁止上述 job roles；仍須以目標 scope、deny 與實際 ARM 回應判斷。
若 403 明示 ABAC condition，不要當作 token 或網路問題反覆重試。核准需時間，請儘早提出申請。

### 8. Microsoft Teams

- [ ] Microsoft 365 帳號可使用 Teams
- [ ] 該帳號與第 1 項的 Azure 訂閱**在同一租戶**

> **提示：** Lab 4 使用「Just you」發布，不需 M365 管理員核准。組織範圍發布不在本課程內。

---

## 步驟二：執行前置檢查

確認步驟一已完成，並已開啟 Codespace（建置約 3–5 分鐘）。

1. 開啟 Codespace，等待容器建置完成。

2. 登入 Azure：

   ```bash
   az login --use-device-code
   ```

3. 建立環境設定檔：

   ```bash
   cd src
   cp .env.template .env
   ```

4. 編輯 `src/.env`，將兩個 `<TODO: ...>` 佔位符換成實際值：

   ```bash
   FOUNDRY_PROJECT_ENDPOINT=<環境需求第 4 項複製的專案端點>
   MODEL_DEPLOYMENT_NAME=<環境需求第 5 項複製的部署名稱>
   ```

5. 執行前置檢查：

   ```bash
   uv run python -m preflight
   ```

6. 對照輸出逐項排除問題（見[常見問題排除](#常見問題排除)），直到 **6/6 通過**。

---

## 驗收

- [ ] `uv run python -m preflight` 輸出 **`結果：6/6 通過。可以進入 Lab 1。`**
- [ ] 結束碼為 `0`（`echo $?`）

輸出重點如下（省略狀態符號）：

```text
IPM Workshop 環境前置檢查
========================================
[1/6] 登入的訂閱          通過 <你的訂閱名稱> — 請確認這是你預期使用的訂閱
[2/6] 資源區域            通過 <區域>（資源群組 <RG>）
[3/6] provider 註冊       通過 Microsoft.CognitiveServices、Microsoft.BotService
[4/6] 角色指派            通過 已找到指定角色；僅確認角色存在，不代表 effective permissions、ABAC／deny 或 Lab 3 ACR bootstrap 通過
[5/6] 專案端點            通過 可連線
[6/6] 模型能力            通過 tool calling、structured outputs

結果：6/6 通過。可以進入 Lab 1。
```

> **提示：** preflight 不變更 Azure 資源；模型能力檢查會呼叫模型，可能計費。
> `6/6` 不是完整授權驗收，尤其不證明 Lab 3 bootstrap 或 Lab 4 發布能成功。

---

## 常見問題排除

| 檢查項          | 現象                                 | 原因                                         | 處理                                                                       |
| --------------- | ------------------------------------ | -------------------------------------------- | -------------------------------------------------------------------------- |
| 啟動前          | `必要環境變數缺漏或尚未填寫`         | `.env` 沒建立，或 `<TODO: ...>` 佔位符沒換掉 | 依訊息提示的步驟建立並填寫 `src/.env`                                      |
| 1 登入的訂閱    | 顯示的訂閱不是你要的                 | 多訂閱環境的預設值                           | `az account set --subscription <訂閱 ID>`                                  |
| 2 資源區域      | `不在 hosted agents 支援清單內`      | 專案建在不支援的區域（例如 East Asia）       | **現場無法修復**——需重建專案並重新部署模型，改走觀摩路徑                   |
| 3 provider 註冊 | 顯示 `NotRegistered`                 | 未註冊                                       | `az provider register --namespace <ns>`；需數分鐘，可先進 Lab 1            |
| 4 角色指派      | `未找到指定 workshop 角色或查詢失敗` | 未查到指定 GUID、讀取受限，或使用其他等效角色 | 請管理員分別核對 Foundry data plane 與 Bot ARM 權限；不要只看角色名稱 |
| Lab 3 provision | `roleAssignments/write`／ABAC 403 | ACR scope 的 IAM 權限或 condition 不允許本次指派 | 使用 [管理員 bootstrap](03-lab3-deploy.md#路徑-b管理員-bootstrap)，不移除 condition |
| 5 專案端點      | `AADSTS530036`                       | 租戶的 Conditional Access 政策               | `az logout` 後 `az login --scope "https://ai.azure.com/.default"`          |
| 5 專案端點      | 其他連線失敗                         | 端點填錯，或登入的租戶與專案不同             | 逐字比對 Foundry portal 的「專案端點」；`az account show --query tenantId` |
| 6 模型能力      | `部署 <名稱> 不支援：tool calling…`  | 該部署的模型不支援必要能力                   | 依訊息的編號步驟改用其他部署（建議 `gpt-5.4-mini`）                        |
| 6 模型能力      | `無法實測：憑證無效，呼叫未抵達模型` | 其實是檢查 5 的憑證問題                      | 先修好檢查 5，**不要**去重新部署模型                                       |
| 全部            | 指令卡住且沒有任何輸出               | 網路攔截了 `login.microsoftonline.com`       | 見步驟一第 2 項的網路自檢                                                  |

---

## 現場無法修復的項目

排除後仍未通過時，用下表判斷是否能現場修復。無法修復時，改走[觀摩路徑](06-fallback.md)。

| 條件                                            | 現場可修復？ | 沒有的後果                                 |
| ----------------------------------------------- | ------------ | ------------------------------------------ |
| 可連 `login.microsoftonline.com`                | 不可         | **整場無法進行**（連 `az login` 都不行）   |
| Azure 訂閱與 Microsoft 365 同一租戶             | 不可         | Lab 4 無法發布到 Teams                     |
| Foundry 專案在 hosted agents 支援區域           | 不可         | Lab 3 無法部署（需重建專案並重新部署模型） |
| `Foundry Project Manager`（專案範圍）           | 通常不可     | Lab 2 無法建立 agent                       |
| `Azure Bot Service Contributor Role`（RG 範圍） | 通常不可     | Lab 4 發布時 403                           |
| provider 註冊                                   | 可，但需等待 | 註冊需數分鐘，Lab 3 前完成即可             |
| 模型部署                                        | 可，但需等待 | 部署需數分鐘                               |

**權限要分層看**：Foundry data plane、Bot ARM 操作、ACR 建置與 ACR role assignment
是不同需求。`Owner`／`Contributor` 一般涵蓋 Bot ARM 操作；preflight 沒找到 Bot 角色 GUID
不等於沒有該操作權限，也不能因為找到兩個 GUID 就宣稱所有 ABAC 條件通過。

前兩項 preflight **無法自動檢查**：網路攔截只會讓指令卡住而不報錯，租戶不一致要到 Lab 4 發布時才顯現。因此必須自行核對，不能只依賴 6/6 通過。

---

## 補充：切換環境只需改 `.env`

切換訂閱、專案或模型部署時，只需修改 `src/.env` 的兩個值。

---

## 下一步

6/6 通過後，進入 [Lab 1：在 Foundry portal 建立第一個 agent](01-lab1-single-agent.md)。

若有項目無法現場修復，請告知講師並改走[觀摩路徑](06-fallback.md)。
