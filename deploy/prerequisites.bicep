targetScope = 'subscription'

@minLength(1)
@maxLength(64)
param environmentName string

@minLength(1)
@maxLength(90)
param resourceGroupName string

param location string

@description('取得 Foundry 專案管理權限的主體 objectId（使用者、服務主體或群組）。')
param principalId string

@description('principalId 的主體型別。互動式部署為 User，CI／服務主體為 ServicePrincipal，以群組授權時為 Group。')
@allowed(['User', 'ServicePrincipal', 'Group'])
param principalType string = 'User'

param projectName string = 'ipm-workshop'
// 與 Lab 0／Lab 1 及 preflight 建議的模型一致。版本依 Microsoft Learn「Foundry Models sold by Azure」
// 與「Model retirement schedule」查證：gpt-5.4-mini 2026-03-17 為 GA。改用其他模型時須一併確認版本與 SKU。
@description('模型名稱，同時作為部署名稱（MODEL_DEPLOYMENT_NAME）。')
param modelName string = 'gpt-5.4-mini'

@description('模型版本。可用 az cognitiveservices model list -l <location> 查詢該區域支援的版本與 SKU。')
param modelVersion string = '2026-03-17'

@minValue(1)
param modelCapacity int = 50

var tags = {
  'azd-env-name': environmentName
  workshop: 'ipm-multiagent'
}

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module prerequisites 'infra/prerequisites-resources.bicep' = {
  name: 'prerequisites-${uniqueString(resourceGroupName)}'
  scope: rg
  params: {
    location: location
    tags: tags
    principalId: principalId
    principalType: principalType
    projectName: projectName
    modelName: modelName
    modelVersion: modelVersion
    modelCapacity: modelCapacity
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_AI_ACCOUNT_NAME string = prerequisites.outputs.accountName
output AZURE_AI_PROJECT_NAME string = projectName
output AZURE_AI_PROJECT_ID string = prerequisites.outputs.projectId
output AZURE_AI_PROJECT_ENDPOINT string = prerequisites.outputs.projectEndpoint
output MODEL_DEPLOYMENT_NAME string = modelName
output APPLICATIONINSIGHTS_RESOURCE_ID string = prerequisites.outputs.applicationInsightsId
