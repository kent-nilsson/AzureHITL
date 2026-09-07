param communicationName string
param emailName string
param tags object = {}

@description('Where ACS stores data. "global" resources still require a data location.')
param dataLocation string = 'United States'

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: emailName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

resource domain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource sender 'Microsoft.Communication/emailServices/domains/senderUsernames@2023-04-01' = {
  parent: domain
  name: 'donotreply'
  properties: {
    username: 'donotreply'
    displayName: 'Study Planner'
  }
}

resource communication 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: communicationName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
    linkedDomains: [
      domain.id
    ]
  }
}

output senderAddress string = '${sender.properties.username}@${domain.properties.fromSenderDomain}'
#disable-next-line outputs-should-not-contain-secrets
output connectionString string = communication.listKeys().primaryConnectionString
