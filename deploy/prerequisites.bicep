targetScope = 'subscription'

@minLength(1)
@maxLength(64)
param environmentName string

@minLength(1)
@maxLength(90)
param resourceGroupName string

param location string

@description('Object ID of the deploying user.')
param principalId string

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
