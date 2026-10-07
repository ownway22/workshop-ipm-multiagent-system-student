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
param modelName string = 'gpt-4.1-mini'
param modelVersion string = '2025-04-14'

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
