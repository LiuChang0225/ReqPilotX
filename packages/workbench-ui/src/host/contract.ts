/** Shared UI/API surface version, not a model capability or result revision. Additive exports keep major stable. */
export const hostUiContract = {
  schema: 'reqpilot-host-ui/v1',
  major: 1,
  minor: 2,
  exports: ['ReqPilotXReadonlyPanel', 'createReadonlyHttpClient', 'createProjectDocumentClient', 'projectDocumentPaths', 'requestWithTransport', 'validWorkSelection', 'ReqPilotXApplication', 'installApplicationTransport', 'createApplicationNavigation'],
  surfaces: ['readonly-plugin-work', 'projects-read-create', 'documents-read-import', 'complete-workbench-application'],
} as const;
