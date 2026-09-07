param planName string
param siteName string
param location string
param tags object = {}

param userAssignedIdentityResourceId string
param userAssignedIdentityClientId string

param appInsightsConnectionString string
param projectEndpoint string
param modelDeploymentName string
param storageTableEndpoint string

@secure()
param acsConnectionString string
param acsSenderAddress string

param defaultApproverEmail string
param approversJson string

@secure()
param approvalSigningKey string

@description('Entra app (client) ID for Easy Auth. Empty = auth disabled.')
param authClientId string = ''

var siteUri = 'https://${siteName}.azurewebsites.net'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  tags: tags
  sku: { name: 'B1', tier: 'Basic' }
  kind: 'linux'
  properties: { reserved: true }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: siteName
  location: location
  tags: tags
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityResourceId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appCommandLine: 'python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir src'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
        { name: 'AZURE_CLIENT_ID', value: userAssignedIdentityClientId }
        { name: 'PROJECT_ENDPOINT', value: projectEndpoint }
        { name: 'MODEL_DEPLOYMENT_NAME', value: modelDeploymentName }
        { name: 'FAKE_AGENT', value: 'false' }
        { name: 'STORAGE_TABLE_ENDPOINT', value: storageTableEndpoint }
        { name: 'ACS_CONNECTION_STRING', value: acsConnectionString }
        { name: 'ACS_SENDER_ADDRESS', value: acsSenderAddress }
        { name: 'DEV_EMAIL_TO_CONSOLE', value: 'false' }
        { name: 'APPROVAL_SIGNING_KEY', value: approvalSigningKey }
        { name: 'PUBLIC_BASE_URL', value: siteUri }
        { name: 'DEFAULT_APPROVER_EMAIL', value: defaultApproverEmail }
        { name: 'APPROVERS_JSON', value: approversJson }
      ]
    }
  }
}

resource authSettings 'Microsoft.Web/sites/config@2023-12-01' = if (!empty(authClientId)) {
  parent: site
  name: 'authsettingsV2'
  properties: {
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: authClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            'api://${authClientId}'
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
      }
    }
  }
}

output uri string = siteUri
output name string = site.name
