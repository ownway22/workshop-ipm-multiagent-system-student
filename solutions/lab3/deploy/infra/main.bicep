// Lab 3：hosted agent 部署所需的最小基礎設施
//
// ## 範圍刻意極小
//
// 只建立**映像儲存**（ACR）與它必要的三項授權／接線。
// 不佈建 Foundry 專案或模型部署——那兩者是學員的既有資產，由前置作業準備好，
// 誤佈建會覆寫學員環境。hosted agent 本身也不在這裡建立：它由 `azd deploy` 經
// `azure.ai.agents` 擴充建立，cpu／memory 來自 `azure.yaml` 的 `config.container.resources`。
//
// ## 為什麼是 resourceGroup 範圍
//
// azd 預設是訂閱範圍並自行建立資源群組。這裡改為資源群組範圍並搭配 `azure.yaml` 的
// `resourceGroup: ${AZURE_RESOURCE_GROUP}`，部署到**既有**資源群組，
// 避免在學員訂閱裡散落新的資源群組。
//
// ## 光有 ACR 不夠：實測踩到的三件事
//
// 只建 ACR 的話，`azd deploy` 會成功、agent 版本卻轉為 `failed`，
// 呼叫端點時才看得到真正原因：
//
//   [ImageError] Container registry authentication failed.
//   Verify the workspace managed identity has AcrPull permissions on the target registry.
//
// 完整的最小組合是下列三項，缺一不可：
//
//   1. **AcrPull** 給 **Foundry 專案的 managed identity** —— 執行期拉取映像。
//   2. **Container Registry Tasks Contributor** 給**部署者** —— `remoteBuild` 走 ACR Tasks 建置。
//   3. Foundry 專案上的 **ContainerRegistry 連線** —— 讓專案知道要用哪個登錄、以何種身分。

targetScope = 'resourceGroup'

@description('資源部署區域。預設沿用資源群組所在區域。')
param location string = resourceGroup().location

@minLength(1)
@maxLength(64)
@description('azd 環境名稱。用於產生具唯一性的資源名稱。')
param environmentName string

@description('執行部署的使用者或服務主體 objectId。由 azd 自動帶入。')
param principalId string

@description('部署者的主體型別。互動式部署為 User，CI 為 ServicePrincipal。')
@allowed(['User', 'ServicePrincipal'])
param principalType string = 'User'

@description('既有 Foundry 帳戶（AI Services account）名稱。不由本檔建立。')
param aiServicesAccountName string

@description('既有 Foundry 專案名稱。不由本檔建立。')
param aiProjectName string

@description('套用到所有資源的標籤。')
param tags object = {
  'azd-env-name': environmentName
  workshop: 'ipm-multiagent'
}

// 內建角色定義 ID（以 GUID 指定，不用顯示名稱——官方近期調整過多個角色的顯示名稱）。
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var acrTasksContributorRoleId = 'fb382eab-e894-4461-af04-94435c366c3f'

// ACR 名稱只允許英數字、5–50 字元，且需全域唯一。
var registryName = 'cr${uniqueString(subscription().id, resourceGroup().id, environmentName)}'

// 參照既有的 Foundry 帳戶與專案，只為取得專案的 managed identity 來授權。
resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiServicesAccountName

  resource aiProject 'projects' existing = {
    name: aiProjectName
  }
}

module registry 'br/public:avm/res/container-registry/registry:0.12.1' = {
  name: 'acr-deployment'
  params: {
    name: registryName
    location: location
    tags: tags
    // Basic 足夠：本 workshop 只推送一個映像，不需要異地複寫或私人端點。
    acrSku: 'Basic'
    // 一律以 Entra ID 身分推送與提取，不啟用管理員帳號。
    acrAdminUserEnabled: false
    roleAssignments: [
      {
        // 執行期：Foundry 專案的 managed identity 需要能**拉取**映像。
        // 少了這一項，agent 版本會轉為 failed 並回報 [ImageError]。
        principalId: aiAccount::aiProject.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: acrPullRoleId
      }
      {
        // 建置期：`docker.remoteBuild: true` 走 ACR Tasks，部署者需要能觸發建置並推送。
        principalId: principalId
        principalType: principalType
        roleDefinitionIdOrName: acrTasksContributorRoleId
      }
    ]
  }
}

// Foundry 專案上的 ACR 連線。沒有這條連線，專案不知道要從哪個登錄、以什麼身分取用映像。
resource acrConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: aiAccount::aiProject
  name: 'acr-${uniqueString(resourceGroup().id, environmentName)}'
  properties: {
    category: 'ContainerRegistry'
    target: registry.outputs.loginServer
    authType: 'ManagedIdentity'
    isSharedToAll: true
    credentials: {
      clientId: aiAccount::aiProject.identity.principalId
      resourceId: registry.outputs.resourceId
    }
    metadata: {
      ResourceId: registry.outputs.resourceId
    }
  }
}

// 下列兩個輸出名稱是 `azure.ai.agents` 擴充讀取的環境變數名稱（自擴充二進位檔萃取確認），
// azd 會把 provision 的輸出寫回 azd 環境，deploy 階段才知道要推到哪個登錄。
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
output AZURE_CONTAINER_REGISTRY_RESOURCE_ID string = registry.outputs.resourceId
output AZURE_ACR_CONNECTION_NAME string = acrConnection.name
