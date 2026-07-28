# Lab 0：環境驗證

**適用對象**：所有參加 workshop 的學員
**檢查點**：前置檢查 6/6 通過

---

## 目標

以一支指令確認你的環境具備完成後續四個 Lab 的所有條件。

做完你會得到：前置檢查**六項全過**的終端機輸出，以及一份只需要填兩個值的 `.env`。

---

## 一、環境需求

下面八項是後續四個 Lab 的必要條件，逐項確認，全部打勾後再往下走。

> ⚠️ 其中**權限**、**專案區域**、**租戶一致性**與**網路可達性**四類
> 如果不具備，**現場沒有辦法補救**——完整的判斷表在第三節。

### 1. Azure 訂閱

- [ ] 有一個可以建立資源的 Azure 訂閱
- [ ] 這個訂閱與你要用的 **Microsoft 365 帳號在同一個租戶**（Lab 4 發布到 Teams 的必要條件）
- [ ] 已安裝 Azure CLI（`az --version`）

### 2. 網路可達性

在你要用來上課的網路環境執行：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://login.microsoftonline.com/common/discovery/instance
```

- [ ] 回應是 `400`（有回應就對了，該端點沒帶參數本來就回 400）

> ⚠️ 如果**逾時沒有回應**，代表你的網路（VPN／資安代理）攔截了 Microsoft 的登入端點。
> 這會讓 `az login` 與部署工具**完全無法運作，而且不會給出有意義的錯誤訊息**。
> 請和你的 IT 部門確認，或改用不受管制的網路。
>
> 自我診斷：`getent hosts login.microsoftonline.com`（Linux／WSL）。
> 如果回傳的 IP 看起來不像 Microsoft 的位址（例如 `6.6.x.x`），就是被攔截了。

### 3. 登入

```bash
az login --use-device-code
az account show --query "{name:name, id:id, tenant:tenantId}" -o table
```

- [ ] 顯示的訂閱就是你預期要用的那一個

> ⚠️ **企業租戶常見狀況**：如果後續步驟出現 `AADSTS530036`，代表你的租戶套用了
> Conditional Access 的 authentication flows 政策。一般的重新登入**沒有用**，
> 必須帶 scope 重新登入：
>
> ```bash
> az logout
> az login --scope "https://ai.azure.com/.default"
> ```

### 4. Foundry 專案

- [ ] 已有一個 **Microsoft Foundry 專案**（不是舊的 hub-based 專案）
- [ ] 專案的**區域**在下方支援清單內
- [ ] 已從 portal 首頁複製「專案端點」（形如 `https://<帳戶>.services.ai.azure.com/api/projects/<專案>`）

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

> 來源：<https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents#region-availability>
> （2026-07-27 查證）
>
> **台灣場次建議** `southeastasia` 或 `japaneast`（延遲較低）。這只是建議，
> 清單內任一區域都可以。

### 5. 模型部署

- [ ] 專案內已有一個支援 **tool calling** 與 **structured outputs** 的模型部署
- [ ] 已複製「部署名稱」（**不是**模型型號）

建議使用 **`gpt-5.4-mini`**，portal 手動部署步驟：

1. 開啟 Foundry portal，進入你的專案。
2. 左側選單點 **Deployments**。
3. 點 **Deploy model** → **Deploy base model**。
4. 在搜尋框輸入 `gpt-5.4-mini`，選取後點 **Confirm**。
5. **Deployment name** 保持預設或自訂；**這個名稱**就是稍後要填進 `.env` 的值。
6. Deployment type 選 **Global Standard**，容量依配額調整。
7. 點 **Deploy**，等待狀態變為 **Succeeded**（數分鐘）。
8. 回到 Deployments 清單，複製剛才的**部署名稱**。

> ℹ️ **`gpt-5.4-mini` 只是建議，不是硬性要求。** 唯一的判定依據是前置檢查第 6 項的
> **實際能力實測**——它會真的呼叫一次 tool calling 與 structured outputs。
> 你手上任何通過該檢查的部署都可以用。

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

- [ ] 兩者都是 `Registered`

> 註冊需要數分鐘。**Lab 3 之前**完成即可，不影響 Lab 1、Lab 2。

### 7. 角色指派（最容易卡住的一項）

需要**兩個**角色，範圍不同。訂閱層的 `Owner`／`Contributor` **不包含**它們。

```bash
# 先取得你自己的 objectId
OBJ=$(az ad signed-in-user show --query id -o tsv)

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

> ℹ️ **為什麼用 GUID 而不是角色名稱**：官方近期調整過多個 Foundry RBAC 角色的顯示名稱，
> 用 GUID 才不會因為改名而失效。
>
> | 角色                               | GUID                                   | 範圍         |
> | ---------------------------------- | -------------------------------------- | ------------ |
> | Foundry Project Manager            | `eadc314b-1a2d-4efa-be10-5d325db5065e` | Foundry 專案 |
> | Azure Bot Service Contributor Role | `9fc6112f-f48e-4e27-8b09-72a5c94e4ae9` | 資源群組     |

**如果你不是訂閱擁有者**：把上面兩段指令連同你的 objectId 寄給訂閱管理員。
核准通常要等，**現在就寄出申請**。

### 8. Microsoft Teams

- [ ] 你的 Microsoft 365 帳號可以使用 Teams
- [ ] 該帳號與步驟 1 的 Azure 訂閱**在同一租戶**

> ℹ️ Lab 4 使用「Just you」發布範圍，**不需要** M365 管理員核准。
> 組織範圍的發布需要核准，本 workshop 只作概念說明、不現場實作。

---

## 二、執行驗證

這一關過不了，後面都不用開始。

### 前置狀態

- 上一節的環境需求已全部具備
- 已在 GitHub Codespaces 開啟本 repository（容器建置約 3–5 分鐘）

### 步驟

1. 開啟 Codespace 後，等待右下角的容器建置完成。

2. 在終端機登入 Azure：

   ```bash
   az login --use-device-code
   ```

3. 建立你自己的環境設定檔：

   ```bash
   cd src
   cp .env.template .env
   ```

4. 編輯 `src/.env`，把兩個 `<TODO: ...>` 佔位符換成你的值：

   ```bash
   FOUNDRY_PROJECT_ENDPOINT=<你在環境需求第 4 項複製的專案端點>
   MODEL_DEPLOYMENT_NAME=<你在環境需求第 5 項複製的部署名稱>
   ```

5. 執行前置檢查：

   ```bash
   uv run python -m preflight
   ```

6. 對照輸出，逐項排除問題（見下方「常見錯誤排除表」），直到 **6/6 通過**。

### 驗收標準

- [ ] `uv run python -m preflight` 輸出 **`結果：6/6 通過。可以進入 Lab 1。`**
- [ ] 結束碼為 `0`（`echo $?`）

預期輸出長這樣：

```text
IPM Workshop 環境前置檢查
========================================
[1/6] 登入的訂閱          ✅ <你的訂閱名稱> — 請確認這是你預期使用的訂閱
[2/6] 資源區域            ✅ <區域>（資源群組 <RG>）
[3/6] provider 註冊       ✅ Microsoft.CognitiveServices、Microsoft.BotService
[4/6] 角色指派            ✅ Foundry Project Manager、Azure Bot Service Contributor Role
[5/6] 專案端點            ✅ 可連線
[6/6] 模型能力            ✅ tool calling、structured outputs

結果：6/6 通過。可以進入 Lab 1。
```

> ℹ️ 檢查工具**不會**幫你改任何東西，它只做唯讀檢查並印出可直接複製執行的修復指令。
> 失敗時它會**一次列出所有問題**，不是修一個才發現下一個。

### 常見錯誤排除表

| 檢查項          | 症狀                                 | 原因                                         | 處置                                                               |
| --------------- | ------------------------------------ | -------------------------------------------- | ------------------------------------------------------------------ |
| 啟動前          | `❌ 必要環境變數缺漏或尚未填寫`      | `.env` 沒建立，或 `<TODO: ...>` 佔位符沒換掉 | 依訊息提示的步驟建立並填寫 `src/.env`                              |
| 1 登入的訂閱    | 顯示的訂閱不是你要的                 | 多訂閱環境的預設值                           | `az account set --subscription <訂閱 ID>`                          |
| 2 資源區域      | `不在 hosted agents 支援清單內`      | 專案建在不支援的區域（例如 East Asia）       | **現場無法修復**——需重建專案並重新部署模型，改走觀摩路徑           |
| 3 provider 註冊 | 顯示 `NotRegistered`                 | 未註冊                                       | `az provider register --namespace <ns>`；需數分鐘，可先進 Lab 1    |
| 4 角色指派      | `缺少：Foundry Project Manager…`     | 訂閱層 Owner 不含這兩個角色                  | 依訊息印出的指令指派；**非訂閱擁有者通常無法現場修復**             |
| 5 專案端點      | `AADSTS530036`                       | 租戶的 Conditional Access 政策               | `az logout` 後 `az login --scope "https://ai.azure.com/.default"`  |
| 5 專案端點      | 其他連線失敗                         | 端點填錯，或登入的租戶與專案不同             | 逐字比對 portal 的「專案端點」；`az account show --query tenantId` |
| 6 模型能力      | `部署 <名稱> 不支援：tool calling…`  | 該部署的模型不支援必要能力                   | 依訊息的編號步驟改用其他部署（建議 `gpt-5.4-mini`）                |
| 6 模型能力      | `無法實測：憑證無效，呼叫未抵達模型` | 其實是檢查 5 的憑證問題                      | 先修好檢查 5，**不要**去重新部署模型                               |
| 全部            | 指令卡住且沒有任何輸出               | 網路攔截了 `login.microsoftonline.com`       | 見環境需求第 2 項的網路自檢                                        |

---

## 三、哪些失敗現場救不回來

上面的排除表試過仍然不過時，用這張表判斷你屬於哪一種。標成 ❌ 的項目
**現場沒有辦法補救**——你仍然可以參加，但會改走**觀摩路徑**
（見 [如果你卡住了](06-fallback.md)）：看講師示範，拿帶回材料自己練。

| 條件                                            | 現場可修復？    | 沒有的後果                                 |
| ----------------------------------------------- | --------------- | ------------------------------------------ |
| 可連 `login.microsoftonline.com`                | ❌              | **整場無法進行**（連 `az login` 都不行）   |
| Azure 訂閱與 Microsoft 365 同一租戶             | ❌              | Lab 4 無法發布到 Teams                     |
| Foundry 專案在 hosted agents 支援區域           | ❌              | Lab 3 無法部署（需重建專案並重新部署模型） |
| `Foundry Project Manager`（專案範圍）           | ❌ 通常不行     | Lab 2 無法建立 agent                       |
| `Azure Bot Service Contributor Role`（RG 範圍） | ❌ 通常不行     | Lab 4 發布時 403                           |
| provider 註冊                                   | ⚠️ 可以，但要等 | 註冊需數分鐘，Lab 3 前完成即可             |
| 模型部署                                        | ⚠️ 可以，但要等 | 部署需數分鐘                               |

**權限那兩項最容易被低估**：訂閱層的 `Owner` 或 `Contributor` **不包含**它們。

前兩項 preflight **檢查不到**：網路攔截只會讓指令卡住而不給錯誤，
租戶不一致則要到 Lab 4 發布時才會顯現。所以這兩項 MUST 自己核對，
不能只依賴 6/6 通過。

---

## 四、切換環境只需要改 `.env`

講師示範用的專案和你的專案不同，但**程式碼與講義完全一樣**。
要換到另一個訂閱／專案／模型部署，只需要改 `src/.env` 的兩個值，
**不需要**改動任何程式碼或講義內容。

---

## 下一步

6/6 通過後，進入 [Lab 1](01-lab1-single-agent.md)。

若有任何一項是「現場無法修復」，請告知講師並參考
[觀摩路徑](06-fallback.md)——你仍然可以完成整場學習，只是改為觀摩加帶回練習。
