@description('Name of the deployment environment. Used to compose Azure resource names and tags.')
param environmentName string

@description('Primary Azure region for the App Service deployment.')
param location string

@description('App Service plan SKU for the web app.')
param appServiceSkuName string

@description('Number of App Service plan workers. Keep this at 1 while SQLite and in-process jobs remain in use.')
@minValue(1)
param appServiceWorkerCount int

@description('Python runtime version for the Linux web app.')
param pythonVersion string

@description('Persistent storage root used by ReqPilotX inside App Service.')
param reqpilotStorageRoot string

@description('Directory for AI trace files on App Service.')
param reqpilotTraceDir string

@description('Health endpoint used by App Service health checks.')
param healthCheckPath string

@description('Startup command used by App Service for the FastAPI application.')
param startupCommand string

@description('Existing Azure OpenAI endpoint. Leave empty until the environment is ready to wire to Azure OpenAI.')
param azureOpenAIEndpoint string

@description('Azure OpenAI API version used by the deployed app.')
param azureOpenAIApiVersion string

@description('Existing Azure OpenAI chat deployment name.')
param azureOpenAIChatDeployment string

@description('Existing Azure OpenAI embedding deployment name.')
param azureOpenAIEmbeddingDeployment string

@description('Embedding dimension expected by the Azure AI Search index.')
@minValue(1)
param azureOpenAIEmbeddingDimensions int

@description('Existing Azure AI Search endpoint.')
param azureAISearchEndpoint string

@description('Existing Azure AI Search index name.')
param azureAISearchIndexName string

@description('Existing Azure AI Search semantic configuration name.')
param azureAISearchSemanticConfiguration string

@description('Tags applied to all Azure resources created by this template.')
param tags object = {}

var safeEnvironmentName = toLower(replace(replace(environmentName, '_', '-'), ' ', '-'))
var uniqueSuffix = toLower(uniqueString(subscription().subscriptionId, resourceGroup().id, environmentName))
var resourceToken = take('${safeEnvironmentName}-${uniqueSuffix}', 28)

var appServicePlanName = take('asp-${resourceToken}-reqpilotx', 40)
var appName = take('app-${resourceToken}-reqpilotx', 60)
var applicationInsightsName = take('appi-${resourceToken}-reqpilotx', 60)
var logAnalyticsWorkspaceName = take('log-${resourceToken}-reqpilotx', 63)

var defaultTags = union({
  app: 'reqpilotx'
  'azd-env-name': environmentName
  host: 'appservice'
}, tags)

var appSettings = union(
  {
    AZURE_OPENAI_API_VERSION: azureOpenAIApiVersion
    ENABLE_ORYX_BUILD: 'true'
    PYTHONUNBUFFERED: '1'
    REQPILOT_AI_ASSESSMENT_MODE: 'llm_shadow'
    REQPILOT_AI_TIMEOUT_SECONDS: '45'
    REQPILOT_AI_TOP_K: '5'
    REQPILOT_AI_TRACE_DIR: reqpilotTraceDir
    REQPILOT_AZURE_ALLOW_INTERACTIVE_LOGIN: '0'
    REQPILOT_AZURE_AUTH_MODE: 'managed_identity'
    REQPILOT_RETRIEVAL_BACKEND: 'azure_sdk'
    REQPILOT_STORAGE_DIR: reqpilotStorageRoot
    SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
  },
  empty(azureOpenAIEndpoint) ? {} : {
    AZURE_OPENAI_ENDPOINT: azureOpenAIEndpoint
  },
  empty(azureOpenAIChatDeployment) ? {} : {
    AZURE_OPENAI_CHAT_DEPLOYMENT: azureOpenAIChatDeployment
  },
  empty(azureOpenAIEmbeddingDeployment) ? {} : {
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: azureOpenAIEmbeddingDeployment
    AZURE_OPENAI_EMBEDDING_DIMENSIONS: string(azureOpenAIEmbeddingDimensions)
  },
  empty(azureAISearchEndpoint) ? {} : {
    AZURE_AI_SEARCH_ENDPOINT: azureAISearchEndpoint
  },
  empty(azureAISearchIndexName) ? {} : {
    AZURE_AI_SEARCH_INDEX_NAME: azureAISearchIndexName
  },
  empty(azureAISearchSemanticConfiguration) ? {} : {
    AZURE_AI_SEARCH_SEMANTIC_CONFIGURATION: azureAISearchSemanticConfiguration
  }
)

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  properties: {
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
  }
  tags: defaultTags
}

module applicationInsights 'br/public:avm/res/insights/component:0.7.1' = {
  name: 'applicationInsights'
  params: {
    name: applicationInsightsName
    applicationType: 'web'
    location: location
    retentionInDays: 30
    tags: defaultTags
    workspaceResourceId: logAnalytics.id
  }
}

module appServicePlan 'br/public:avm/res/web/serverfarm:0.7.0' = {
  name: 'appServicePlan'
  params: {
    name: appServicePlanName
    kind: 'linux'
    location: location
    reserved: true
    skuCapacity: appServiceWorkerCount
    skuName: appServiceSkuName
    tags: defaultTags
  }
}

module webApp 'br/public:avm/res/web/site:0.22.0' = {
  name: 'webApp'
  params: {
    name: appName
    configs: [
      {
        name: 'appsettings'
        applicationInsightResourceId: applicationInsights.outputs.resourceId
        properties: appSettings
      }
    ]
    httpsOnly: true
    kind: 'app,linux'
    location: location
    managedIdentities: {
      systemAssigned: true
    }
    publicNetworkAccess: 'Enabled'
    serverFarmResourceId: appServicePlan.outputs.resourceId
    siteConfig: {
      alwaysOn: true
      appCommandLine: startupCommand
      ftpsState: 'Disabled'
      healthCheckPath: healthCheckPath
      http20Enabled: true
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
    }
    tags: defaultTags
  }
}

output appName string = appName
output appServicePlanName string = appServicePlanName
output applicationInsightsConnectionString string = applicationInsights.outputs.connectionString
output applicationInsightsName string = applicationInsightsName
output logAnalyticsWorkspaceId string = logAnalytics.id
output logAnalyticsWorkspaceName string = logAnalyticsWorkspaceName
output managedIdentityPrincipalId string = webApp.outputs.?systemAssignedMIPrincipalId ?? ''
output storageRoot string = reqpilotStorageRoot
output webAppUrl string = 'https://${webApp.outputs.defaultHostname}'