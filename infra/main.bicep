targetScope = 'resourceGroup'

@description('Name of the deployment environment. Used to compose Azure resource names and tags.')
param environmentName string

@description('Primary Azure region for the App Service deployment.')
param location string = resourceGroup().location

@description('App Service plan SKU for the web app.')
param appServiceSkuName string = 'S1'

@description('Number of App Service plan workers. Keep this at 1 while SQLite and in-process jobs remain in use.')
@minValue(1)
param appServiceWorkerCount int = 1

@description('Python runtime version for the Linux web app.')
param pythonVersion string = '3.11'

@description('Persistent storage root used by ReqPilotX inside App Service.')
param reqpilotStorageRoot string = '/home/site/data/reqpilotx/storage'

@description('Directory for AI trace files on App Service.')
param reqpilotTraceDir string = '/home/LogFiles/reqpilotx-ai'

@description('Health endpoint used by App Service health checks.')
param healthCheckPath string = '/api/health'

@description('Startup command used by App Service for the FastAPI application.')
param startupCommand string = 'gunicorn --bind=0.0.0.0:8000 --workers 1 --worker-class uvicorn.workers.UvicornWorker --timeout 600 --access-logfile - --error-logfile - app.main:app'

@description('Existing Azure OpenAI endpoint. Leave empty until the environment is ready to wire to Azure OpenAI.')
param azureOpenAIEndpoint string = ''

@description('Azure OpenAI API version used by the deployed app.')
param azureOpenAIApiVersion string = '2024-10-21'

@description('Existing Azure OpenAI chat deployment name.')
param azureOpenAIChatDeployment string = ''

@description('Existing Azure OpenAI embedding deployment name.')
param azureOpenAIEmbeddingDeployment string = ''

@description('Embedding dimension expected by the Azure AI Search index.')
@minValue(1)
param azureOpenAIEmbeddingDimensions int = 1536

@description('Existing Azure AI Search endpoint.')
param azureAISearchEndpoint string = ''

@description('Existing Azure AI Search index name.')
param azureAISearchIndexName string = 'reqpilot-knowledge'

@description('Existing Azure AI Search semantic configuration name.')
param azureAISearchSemanticConfiguration string = 'reqpilot-semantic'

@description('Tags applied to all Azure resources created by this template.')
param tags object = {}

module resources 'resources.bicep' = {
  name: 'reqpilotx-resources'
  params: {
    environmentName: environmentName
    location: location
    appServiceSkuName: appServiceSkuName
    appServiceWorkerCount: appServiceWorkerCount
    pythonVersion: pythonVersion
    reqpilotStorageRoot: reqpilotStorageRoot
    reqpilotTraceDir: reqpilotTraceDir
    healthCheckPath: healthCheckPath
    startupCommand: startupCommand
    azureOpenAIEndpoint: azureOpenAIEndpoint
    azureOpenAIApiVersion: azureOpenAIApiVersion
    azureOpenAIChatDeployment: azureOpenAIChatDeployment
    azureOpenAIEmbeddingDeployment: azureOpenAIEmbeddingDeployment
    azureOpenAIEmbeddingDimensions: azureOpenAIEmbeddingDimensions
    azureAISearchEndpoint: azureAISearchEndpoint
    azureAISearchIndexName: azureAISearchIndexName
    azureAISearchSemanticConfiguration: azureAISearchSemanticConfiguration
    tags: tags
  }
}

output appName string = resources.outputs.appName
output appServicePlanName string = resources.outputs.appServicePlanName
output applicationInsightsConnectionString string = resources.outputs.applicationInsightsConnectionString
output applicationInsightsName string = resources.outputs.applicationInsightsName
output logAnalyticsWorkspaceId string = resources.outputs.logAnalyticsWorkspaceId
output logAnalyticsWorkspaceName string = resources.outputs.logAnalyticsWorkspaceName
output managedIdentityPrincipalId string = resources.outputs.managedIdentityPrincipalId
output storageRoot string = resources.outputs.storageRoot
output webAppUrl string = resources.outputs.webAppUrl