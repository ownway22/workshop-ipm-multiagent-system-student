targetScope = 'resourceGroup'

param location string
param tags object
param principalId string

@description('部署者的主體型別。互動式部署為 User，CI／服務主體為 ServicePrincipal，以群組授權時為 Group。')
@allowed(['User', 'ServicePrincipal', 'Group'])
param principalType string = 'User'

param projectName string
param modelName string
param modelVersion string
param modelCapacity int

var suffix = uniqueString(subscription().id, resourceGroup().id)
var accountName = 'ai-ipm-${suffix}'
var projectManagerRoleId = 'eadc314b-1a2d-4efa-be10-5d325db5065e'

// 比照 Foundry 基本設定：託管 session 自行管理儲存，不另建 Storage／Cosmos DB／AI Search。
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: modelName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'IPM Workshop'
    description: 'Four-agent IPM review workshop'
  }
  dependsOn: [
    model
  ]
}

resource projectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, principalId, projectManagerRoleId)
  scope: project
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', projectManagerRoleId)
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-ipm-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: json('0.1')
    }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-ipm-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// Foundry 會把此 connection 注入託管 runtime；不要放進原始碼的 .env。
// API version 與其他 CognitiveServices 資源一致使用 GA 的 2025-06-01（已支援 accounts/projects/connections）。
resource insightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: insights.name
  properties: {
    category: 'AppInsights'
    target: insights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: insights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: insights.id
    }
  }
}

output accountName string = account.name
output projectId string = project.id
output projectEndpoint string = project.properties.endpoints['AI Foundry API']
output applicationInsightsId string = insights.id
