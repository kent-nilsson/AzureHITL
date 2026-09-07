targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('Name of the environment; used to derive resource names.')
param environmentName string

@description('Primary location for all resources.')
param location string

@description('Model deployment name to create in Azure AI Foundry.')
param modelDeploymentName string = 'gpt-4o'

@description('Model to deploy (name/version).')
param modelName string = 'gpt-4o'
param modelVersion string = '2024-11-20'

@description('Fallback approver e-mail (the "boss") when a learner is not in approversJson.')
param defaultApproverEmail string

@description('JSON string mapping learner e-mail -> approver e-mail. Example: {"a@x.com":"b@x.com"}')
param approversJson string = '{}'

@description('Optional Entra app (client) ID to enable App Service Easy Auth. Leave empty to run open with DEV identity.')
param authClientId string = ''

@description('Random secret used to sign Approve/Reject links.')
@secure()
param approvalSigningKey string

var abbrs = loadJsonContent('abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

module identity 'modules/identity.bicep' = {
  scope: rg
  name: 'identity'
  params: {
    name: '${abbrs.managedIdentityUserAssignedIdentities}${resourceToken}'
    location: location
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  scope: rg
  name: 'monitoring'
  params: {
    logAnalyticsName: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    appInsightsName: '${abbrs.insightsComponents}${resourceToken}'
    location: location
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storage'
  params: {
    name: '${abbrs.storageStorageAccounts}${resourceToken}'
    location: location
    tags: tags
    principalId: identity.outputs.principalId
  }
}

module aiFoundry 'modules/ai-foundry.bicep' = {
  scope: rg
  name: 'ai-foundry'
  params: {
    accountName: '${abbrs.cognitiveServicesAccounts}${resourceToken}'
    projectName: 'proj-${resourceToken}'
    location: location
    tags: tags
    principalId: identity.outputs.principalId
    modelDeploymentName: modelDeploymentName
    modelName: modelName
    modelVersion: modelVersion
  }
}

module communication 'modules/communication.bicep' = {
  scope: rg
  name: 'communication'
  params: {
    communicationName: '${abbrs.communicationServices}${resourceToken}'
    emailName: '${abbrs.communicationEmailServices}${resourceToken}'
    tags: tags
  }
}

module appService 'modules/appservice.bicep' = {
  scope: rg
  name: 'appservice'
  params: {
    planName: '${abbrs.webServerFarms}${resourceToken}'
    siteName: '${abbrs.webSitesAppService}${resourceToken}'
    location: location
    tags: union(tags, { 'azd-service-name': 'web' })
    userAssignedIdentityResourceId: identity.outputs.resourceId
    userAssignedIdentityClientId: identity.outputs.clientId
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    projectEndpoint: aiFoundry.outputs.projectEndpoint
    modelDeploymentName: modelDeploymentName
    storageTableEndpoint: storage.outputs.tableEndpoint
    acsConnectionString: communication.outputs.connectionString
    acsSenderAddress: communication.outputs.senderAddress
    defaultApproverEmail: defaultApproverEmail
    approversJson: approversJson
    approvalSigningKey: approvalSigningKey
    authClientId: authClientId
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output SERVICE_WEB_ENDPOINT string = appService.outputs.uri
output PROJECT_ENDPOINT string = aiFoundry.outputs.projectEndpoint
output MODEL_DEPLOYMENT_NAME string = modelDeploymentName
output STORAGE_TABLE_ENDPOINT string = storage.outputs.tableEndpoint
output ACS_SENDER_ADDRESS string = communication.outputs.senderAddress
output PUBLIC_BASE_URL string = appService.outputs.uri
