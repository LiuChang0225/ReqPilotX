import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../shared/api/client";
import { isSaasAccountMode } from "../../shared/api/saasSession";
import type {
  AiProviderSetup,
  ExtractionStatus,
  JsonRecord,
  KnowledgeDocument,
  Project,
  ProjectProcessingProfile,
  RequirementSetsPayload,
  RuntimeReadiness,
  RuntimeCapability,
  SorDocument,
  StructureOptimizationPayload,
  StructurePayload,
  WorkspacePayload,
} from "../../shared/api/contracts";
import { useInterval } from "../../shared/hooks/useInterval";
import { EmptyState, ErrorBlock, LoadingBlock } from "../../shared/ui/Feedback";
import { ProjectManagementPanel } from "../projects/ProjectManagementPanel";
import { RequirementTreePanel } from "../extraction/RequirementTreePanel";
import { RequirementMatrixWorkbench } from "../matrix/RequirementMatrixWorkbench";
import { isAiAnalysisActiveStatus } from "../matrix/requirementMatrixModel";
import { ExportsPanel } from "../exports/ExportsPanel";
import { KnowledgeWorkspace } from "../knowledge/KnowledgeWorkspace";
import {
  bindWorkspaceDataToScope,
  documentScopeKey,
  isExtractionPollingStatus,
  projectScopeKey,
  requirementSetScopeKey,
  WorkspaceProvider,
  type WorkspaceContextValue,
} from "./WorkspaceContext";
import { WorkspaceShell } from "./WorkspaceShell";
import { DocumentWorkSurface } from "./DocumentWorkSurface";
import { SourceItemWorkspace } from "./SourceItemWorkspace";
import { useWorkspaceRoute } from "./route";

type WorkspaceActionScope = "project" | "document" | "requirementSet";
const ReadingWorkspacePage = lazy(async () => ({ default: (await import("../reading/ReadingWorkspacePage")).ReadingWorkspacePage }));

function loadProjectResources(projectId: string, saasMode: boolean) {
  return Promise.all([
    api.sorDocuments(projectId),
    saasMode ? Promise.resolve<KnowledgeDocument[]>([]) : api.knowledgeDocuments(projectId),
    api.projectProcessingProfile(projectId),
  ]);
}

export function WorkspacePage() {
  const saasMode = isSaasAccountMode();
  const { route, update } = useWorkspaceRoute();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [documents, setDocuments] = useState<SorDocument[]>([]);
  const [knowledge, setKnowledge] = useState<KnowledgeDocument[]>([]);
  const [workspace, setWorkspace] = useState<WorkspacePayload | null>(null);
  const [requirementSets, setRequirementSets] =
    useState<RequirementSetsPayload | null>(null);
  const [extraction, setExtraction] = useState<ExtractionStatus | null>(null);
  const [structure, setStructure] = useState<StructurePayload | null>(null);
  const [structureOptimization, setStructureOptimization] =
    useState<StructureOptimizationPayload | null>(null);
  const [aiStatus, setAiStatus] = useState<JsonRecord | null>(null);
  const [exports, setExports] = useState<JsonRecord[]>([]);
  const [readiness, setReadiness] = useState<RuntimeReadiness | null>(null);
  const [aiSetup, setAiSetup] = useState<AiProviderSetup | null>(null);
  const [runtimeCapabilities, setRuntimeCapabilities] = useState<
    RuntimeCapability[]
  >([]);
  const [processingProfile, setProcessingProfile] =
    useState<ProjectProcessingProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [notice, setNotice] = useState("");
  const [busyActionState, setBusyActionState] = useState<{
    token: number;
    scope: WorkspaceActionScope;
    identity: string;
    label: string;
  } | null>(null);
  const loadGeneration = useRef(0);
  const projectLoadGeneration = useRef(0);
  const documentLoadGeneration = useRef(0);
  const requirementSetScopeLoadGeneration = useRef(0);
  const extractionLoadGeneration = useRef(0);
  const extractionPollingRequestsRef = useRef(
    new Map<string, Promise<void>>(),
  );
  const structureLoadGeneration = useRef(0);
  const structureOptimizationLoadGeneration = useRef(0);
  const aiLoadGeneration = useRef(0);
  const exportsLoadGeneration = useRef(0);
  const noticeGeneration = useRef(0);
  const actionGeneration = useRef(0);
  const aiWasPolling = useRef(false);
  const extractionPollingStateRef = useRef({
    documentScope: "",
    jobId: "",
    active: false,
    status: "",
  });
  const structureOptimizationWasPolling = useRef(false);
  const routeIdentityRef = useRef("");
  const actionScopesRef = useRef({
    project: projectScopeKey(route.projectId),
    document: documentScopeKey(route.projectId, route.documentId),
    requirementSet: requirementSetScopeKey(
      route.projectId,
      route.documentId,
      route.requirementSetId,
    ),
  });
  const requirementSetIdRef = useRef(route.requirementSetId);
  const requirementSetRouteRef = useRef({
    documentIdentity: documentScopeKey(route.projectId, route.documentId),
    requirementSetId: route.requirementSetId,
  });
  const routeScopeRef = useRef({
    project: projectScopeKey(route.projectId),
    document: documentScopeKey(route.projectId, route.documentId),
    requirementSet: requirementSetScopeKey(
      route.projectId,
      route.documentId,
      route.requirementSetId,
    ),
  });
  const structureIdentityRef = useRef("");
  const documentsScopeRef = useRef("");
  const knowledgeScopeRef = useRef("");
  const processingProfileScopeRef = useRef("");
  const workspaceScopeRef = useRef("");
  const requirementSetsScopeRef = useRef("");
  const extractionScopeRef = useRef("");
  const structureOptimizationScopeRef = useRef("");
  const aiStatusScopeRef = useRef("");
  const exportsScopeRef = useRef("");
  const activeActionRef = useRef<{
    token: number;
    scope: WorkspaceActionScope;
    identity: string;
  } | null>(null);
  routeIdentityRef.current = documentScopeKey(
    route.projectId,
    route.documentId,
  );
  actionScopesRef.current = {
    project: projectScopeKey(route.projectId),
    document: routeIdentityRef.current,
    requirementSet: requirementSetScopeKey(
      route.projectId,
      route.documentId,
      route.requirementSetId,
    ),
  };
  requirementSetIdRef.current = route.requirementSetId;

  useEffect(() => {
    document.title = "ReqPilotX · 需求工作台";
  }, []);

  const currentProjectScope = projectScopeKey(route.projectId);
  const currentDocumentScope = documentScopeKey(
    route.projectId,
    route.documentId,
  );
  const currentRequirementSetScope = requirementSetScopeKey(
    route.projectId,
    route.documentId,
    route.requirementSetId,
  );
  const busyAction =
    busyActionState &&
    busyActionState.identity ===
      actionScopesRef.current[busyActionState.scope]
      ? busyActionState.label
      : "";

  useEffect(() => {
    const previous = routeScopeRef.current;
    const next = {
      project: currentProjectScope,
      document: currentDocumentScope,
      requirementSet: currentRequirementSetScope,
    };
    routeScopeRef.current = next;
    if (
      previous.project === next.project &&
      previous.document === next.document &&
      previous.requirementSet === next.requirementSet
    ) {
      return;
    }

    noticeGeneration.current += 1;
    setNotice("");
    setError(null);
    if (previous.document !== next.document) setRefreshing(false);

    const activeAction = activeActionRef.current;
    if (
      activeAction &&
      activeAction.identity !== actionScopesRef.current[activeAction.scope]
    ) {
      activeActionRef.current = null;
      setBusyActionState(null);
    }
  }, [
    currentDocumentScope,
    currentProjectScope,
    currentRequirementSetScope,
  ]);

  const {
    documents: scopedDocuments,
    knowledge: scopedKnowledge,
    processingProfile: scopedProcessingProfile,
    workspace: scopedWorkspace,
    requirementSets: scopedRequirementSets,
    extraction: scopedExtraction,
    structure: scopedStructure,
    structureOptimization: scopedStructureOptimization,
    aiStatus: scopedAiStatus,
    exports: scopedExports,
  } = bindWorkspaceDataToScope(
    {
      documents,
      knowledge,
      processingProfile,
      workspace,
      requirementSets,
      extraction,
      structure,
      structureOptimization,
      aiStatus,
      exports,
    },
    {
      documents: documentsScopeRef.current,
      knowledge: knowledgeScopeRef.current,
      processingProfile: processingProfileScopeRef.current,
      workspace: workspaceScopeRef.current,
      requirementSets: requirementSetsScopeRef.current,
      extraction: extractionScopeRef.current,
      structure: structureIdentityRef.current,
      structureOptimization: structureOptimizationScopeRef.current,
      aiStatus: aiStatusScopeRef.current,
      exports: exportsScopeRef.current,
    },
    {
      project: currentProjectScope,
      document: currentDocumentScope,
      requirementSet: currentRequirementSetScope,
    },
  );

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === route.projectId) || null,
    [projects, route.projectId],
  );
  const selectedDocument = useMemo(
    () =>
      scopedDocuments.find((document) => document.id === route.documentId) ||
      null,
    [route.documentId, scopedDocuments],
  );

  const refreshExtraction = useCallback(async () => {
    const generation = ++extractionLoadGeneration.current;
    if (!route.projectId || !route.documentId) {
      extractionScopeRef.current = "";
      setExtraction(null);
      return;
    }
    const documentScope = documentScopeKey(route.projectId, route.documentId);
    try {
      const next = await api.extractionStatus(
        route.projectId,
        route.documentId,
      );
      if (generation === extractionLoadGeneration.current) {
        extractionScopeRef.current = documentScope;
        setExtraction(next);
      }
    } catch (cause) {
      if (generation !== extractionLoadGeneration.current) return;
      if ((cause as { status?: number }).status === 404) {
        extractionScopeRef.current = documentScope;
        setExtraction(null);
        return;
      }
      throw cause;
    }
  }, [route.documentId, route.projectId]);

  const refreshStructure = useCallback(async () => {
    const generation = ++structureLoadGeneration.current;
    if (!route.projectId || !route.documentId) {
      structureIdentityRef.current = "";
      setStructure(null);
      return;
    }
    const documentIdentity = documentScopeKey(
      route.projectId,
      route.documentId,
    );
    try {
      const next = await api.structure(route.projectId, route.documentId);
      if (generation === structureLoadGeneration.current) {
        structureIdentityRef.current = documentIdentity;
        setStructure(next);
      }
    } catch (cause) {
      if (generation !== structureLoadGeneration.current) return;
      if ((cause as { status?: number }).status === 404) {
        if (structureIdentityRef.current !== documentIdentity) {
          setStructure(null);
        }
        return;
      }
      throw cause;
    }
  }, [route.documentId, route.projectId]);

  const refreshStructureOptimization = useCallback(async () => {
    const generation = ++structureOptimizationLoadGeneration.current;
    if (!route.projectId || !route.documentId) {
      structureOptimizationScopeRef.current = "";
      setStructureOptimization(null);
      return;
    }
    const documentScope = documentScopeKey(route.projectId, route.documentId);
    try {
      const next = await api.structureOptimizationStatus(
        route.projectId,
        route.documentId,
      );
      if (generation === structureOptimizationLoadGeneration.current) {
        structureOptimizationScopeRef.current = documentScope;
        setStructureOptimization(next);
      }
    } catch (cause) {
      if (generation !== structureOptimizationLoadGeneration.current) return;
      if ((cause as { status?: number }).status === 404) {
        structureOptimizationScopeRef.current = documentScope;
        setStructureOptimization(null);
        return;
      }
      throw cause;
    }
  }, [route.documentId, route.projectId]);

  const refreshAi = useCallback(async () => {
    const generation = ++aiLoadGeneration.current;
    if (saasMode || !route.projectId || !route.documentId) {
      aiStatusScopeRef.current = "";
      setAiStatus(null);
      return;
    }
    const documentScope = documentScopeKey(route.projectId, route.documentId);
    try {
      const next = await api.aiStatus(route.projectId, route.documentId);
      if (generation === aiLoadGeneration.current) {
        aiStatusScopeRef.current = documentScope;
        setAiStatus(next);
      }
    } catch (cause) {
      if (generation !== aiLoadGeneration.current) return;
      if ((cause as { status?: number }).status === 404) {
        aiStatusScopeRef.current = documentScope;
        setAiStatus(null);
        return;
      }
      throw cause;
    }
  }, [route.documentId, route.projectId, saasMode]);

  const refreshExports = useCallback(async () => {
    const generation = ++exportsLoadGeneration.current;
    if (saasMode || !route.projectId) {
      exportsScopeRef.current = "";
      setExports([]);
      return;
    }
    const projectScope = projectScopeKey(route.projectId);
    const next = await api.exports(route.projectId);
    if (generation !== exportsLoadGeneration.current) return;
    exportsScopeRef.current = projectScope;
    setExports(next);
  }, [route.projectId, saasMode]);

  const refreshProject = useCallback(async () => {
    const generation = ++projectLoadGeneration.current;
    if (!route.projectId) {
      documentsScopeRef.current = "";
      knowledgeScopeRef.current = "";
      processingProfileScopeRef.current = "";
      setDocuments([]);
      setKnowledge([]);
      setProcessingProfile(null);
      return;
    }
    const projectId = route.projectId;
    const projectScope = projectScopeKey(projectId);
    const [nextDocuments, nextKnowledge, nextProfile] = await loadProjectResources(projectId, saasMode);
    if (generation !== projectLoadGeneration.current) return;
    documentsScopeRef.current = projectScope;
    knowledgeScopeRef.current = projectScope;
    processingProfileScopeRef.current = projectScope;
    setDocuments(nextDocuments);
    setKnowledge(nextKnowledge);
    setProcessingProfile(nextProfile.profile);
  }, [route.projectId, saasMode]);

  const refreshDocument = useCallback(async () => {
    const generation = ++documentLoadGeneration.current;
    if (!route.projectId || !route.documentId) {
      workspaceScopeRef.current = "";
      requirementSetsScopeRef.current = "";
      extractionScopeRef.current = "";
      structureOptimizationScopeRef.current = "";
      setWorkspace(null);
      setRequirementSets(null);
      setExtraction(null);
      structureIdentityRef.current = "";
      setStructure(null);
      setStructureOptimization(null);
      setRefreshing(false);
      return;
    }
    setRefreshing(true);
    setError(null);
    const projectId = route.projectId;
    const documentId = route.documentId;
    const documentIdentity = documentScopeKey(projectId, documentId);
    const requestedRequirementSetId = requirementSetIdRef.current;
    const workspaceIdentity = requirementSetScopeKey(
      projectId,
      documentId,
      requestedRequirementSetId,
    );
    const results = await Promise.allSettled([
      saasMode ? Promise.resolve(null) : api.workspace(projectId, documentId, true, requestedRequirementSetId),
      saasMode ? Promise.resolve(null) : api.requirementSets(projectId, documentId),
      api.extractionStatus(projectId, documentId),
      api.structure(projectId, documentId),
      api.structureOptimizationStatus(projectId, documentId),
    ]);
    if (generation !== documentLoadGeneration.current) return;
    if (documentIdentity !== routeIdentityRef.current) return;
    const [
      workspaceResult,
      setResult,
      extractionResult,
      structureResult,
      structureOptimizationResult,
    ] = results;
    requirementSetsScopeRef.current = documentIdentity;
    extractionScopeRef.current = documentIdentity;
    structureOptimizationScopeRef.current = documentIdentity;
    if (
      workspaceIdentity ===
      requirementSetScopeKey(projectId, documentId, requirementSetIdRef.current)
    ) {
      workspaceScopeRef.current = workspaceIdentity;
      setWorkspace(
        workspaceResult.status === "fulfilled" ? workspaceResult.value : null,
      );
    }
    setRequirementSets(
      setResult.status === "fulfilled" ? setResult.value : null,
    );
    setExtraction(
      extractionResult.status === "fulfilled" ? extractionResult.value : null,
    );
    if (structureResult.status === "fulfilled") {
      structureIdentityRef.current = documentIdentity;
      setStructure(structureResult.value);
    } else if (structureIdentityRef.current !== documentIdentity) {
      setStructure(null);
    }
    setStructureOptimization(
      structureOptimizationResult.status === "fulfilled"
        ? structureOptimizationResult.value
        : null,
    );
    const unexpected = results.find(
      (result) =>
        result.status === "rejected" &&
        (result.reason as { status?: number }).status !== 404,
    );
    if (unexpected?.status === "rejected") {
      setError(
        unexpected.reason instanceof Error
          ? unexpected.reason
          : new Error(String(unexpected.reason)),
      );
    }
    if (generation === documentLoadGeneration.current) setRefreshing(false);
  }, [route.documentId, route.projectId, saasMode]);

  const refreshRequirementSetScope = useCallback(async () => {
    const generation = ++requirementSetScopeLoadGeneration.current;
    if (saasMode || !route.projectId || !route.documentId) return;
    const projectId = route.projectId;
    const documentId = route.documentId;
    const requirementSetId = route.requirementSetId;
    const documentIdentity = documentScopeKey(projectId, documentId);
    const workspaceIdentity = requirementSetScopeKey(
      projectId,
      documentId,
      requirementSetId,
    );
    const [workspaceResult, setResult] = await Promise.allSettled([
      api.workspace(projectId, documentId, true, requirementSetId),
      api.requirementSets(projectId, documentId),
    ]);
    if (generation !== requirementSetScopeLoadGeneration.current) return;
    if (
      documentIdentity !== routeIdentityRef.current ||
      requirementSetId !== requirementSetIdRef.current
    ) {
      return;
    }
    if (workspaceResult.status === "fulfilled") {
      workspaceScopeRef.current = workspaceIdentity;
      setWorkspace(workspaceResult.value);
    }
    if (setResult.status === "fulfilled") {
      requirementSetsScopeRef.current = documentIdentity;
      setRequirementSets(setResult.value);
    }
    const unexpected = [workspaceResult, setResult].find(
      (result) =>
        result.status === "rejected" &&
        (result.reason as { status?: number }).status !== 404,
    );
    if (unexpected?.status === "rejected") {
      setError(
        unexpected.reason instanceof Error
          ? unexpected.reason
          : new Error(String(unexpected.reason)),
      );
    }
  }, [route.documentId, route.projectId, route.requirementSetId, saasMode]);

  const refreshAll = useCallback(async () => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    setError(null);
    try {
      const [nextProjects, nextReadiness, nextAiSetup, nextCapabilities] =
        await Promise.all([
          api.projects(),
          api.readiness(),
          saasMode ? Promise.resolve(null) : api.aiSetup(),
          api.runtimeCapabilities(),
        ]);
      if (generation !== loadGeneration.current) return;
      setProjects(nextProjects);
      setProjectsLoaded(true);
      setReadiness(nextReadiness);
      setAiSetup(nextAiSetup);
      setRuntimeCapabilities(nextCapabilities.capabilities);
      if (route.projectId) await refreshProject();
      if (route.documentId) await refreshDocument();
    } catch (cause) {
      if (generation !== loadGeneration.current) return;
      setError(cause instanceof Error ? cause : new Error(String(cause)));
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, [refreshDocument, refreshProject, route.documentId, route.projectId, saasMode]);

  useEffect(() => {
    void refreshAll();
    return () => {
      loadGeneration.current += 1;
      projectLoadGeneration.current += 1;
      documentLoadGeneration.current += 1;
      requirementSetScopeLoadGeneration.current += 1;
      extractionLoadGeneration.current += 1;
      structureLoadGeneration.current += 1;
      structureOptimizationLoadGeneration.current += 1;
      aiLoadGeneration.current += 1;
      exportsLoadGeneration.current += 1;
      noticeGeneration.current += 1;
      actionGeneration.current += 1;
      activeActionRef.current = null;
    };
  }, [refreshAll]);

  useEffect(() => {
    if (!projects.length) return;
    if (!route.projectId && !route.pluginSessionId) {
      update(
        {
          projectId: projects[0].id,
          documentId: "",
          requirementSetId: "",
        },
        { replace: true },
      );
    }
  }, [projects, route.projectId, route.pluginSessionId, update]);

  useEffect(() => {
    if (!route.projectId) return;
    const projectId = route.projectId;
    const projectScope = projectScopeKey(projectId);
    let active = true;
    void (async () => {
      try {
        const [nextDocuments, nextKnowledge, nextProfile] = await loadProjectResources(projectId, saasMode);
        if (!active) return;
        documentsScopeRef.current = projectScope;
        knowledgeScopeRef.current = projectScope;
        processingProfileScopeRef.current = projectScope;
        setDocuments(nextDocuments);
        setKnowledge(nextKnowledge);
        setProcessingProfile(nextProfile.profile);
        if (
          nextDocuments.length &&
          !route.documentId && !route.pluginSessionId
        ) {
          update(
            {
              documentId: nextDocuments[0].id,
              requirementSetId: "",
            },
            { replace: true },
          );
        }
      } catch (cause) {
        if (active) {
          setError(cause instanceof Error ? cause : new Error(String(cause)));
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [route.documentId, route.projectId, route.pluginSessionId, update, saasMode]);

  useEffect(() => {
    const documentIdentity = documentScopeKey(
      route.projectId,
      route.documentId,
    );
    if (
      route.projectId &&
      route.documentId &&
      structureIdentityRef.current &&
      structureIdentityRef.current !== documentIdentity
    ) {
      structureIdentityRef.current = "";
      setStructure(null);
    }
  }, [route.documentId, route.projectId]);

  useEffect(() => {
    if (!route.projectId || !route.documentId) {
      workspaceScopeRef.current = "";
      requirementSetsScopeRef.current = "";
      extractionScopeRef.current = "";
      structureOptimizationScopeRef.current = "";
      setWorkspace(null);
      setRequirementSets(null);
      setExtraction(null);
      structureIdentityRef.current = "";
      setStructure(null);
      setStructureOptimization(null);
      return;
    }
    void refreshDocument();
  }, [refreshDocument, route.documentId, route.projectId]);

  useEffect(() => {
    const next = {
      documentIdentity: documentScopeKey(route.projectId, route.documentId),
      requirementSetId: route.requirementSetId,
    };
    const previous = requirementSetRouteRef.current;
    requirementSetRouteRef.current = next;
    if (
      !route.projectId ||
      !route.documentId ||
      previous.documentIdentity !== next.documentIdentity ||
      previous.requirementSetId === next.requirementSetId
    ) {
      return;
    }
    void refreshRequirementSetScope();
  }, [
    refreshRequirementSetScope,
    route.documentId,
    route.projectId,
    route.requirementSetId,
  ]);

  useEffect(() => {
    if (route.task === "export") void refreshExports();
  }, [refreshExports, route.task]);

  useEffect(() => {
    if (route.task === "analysis") void refreshAi();
  }, [refreshAi, route.task]);

  const extractionJobStatus = String(
    scopedExtraction?.job?.status || "",
  ).toUpperCase();
  const extractionJobId = String(scopedExtraction?.job?.job_id || "");
  const extractionActive = isExtractionPollingStatus(extractionJobStatus);
  const extractionPolling = route.task === "extraction" && extractionActive;
  const pollExtraction = useCallback(() => {
    const documentScope = documentScopeKey(route.projectId, route.documentId);
    if (!documentScope) return Promise.resolve();
    const existing = extractionPollingRequestsRef.current.get(documentScope);
    if (existing) return existing;
    const request = refreshExtraction().finally(() => {
      if (extractionPollingRequestsRef.current.get(documentScope) === request) {
        extractionPollingRequestsRef.current.delete(documentScope);
      }
    });
    extractionPollingRequestsRef.current.set(documentScope, request);
    return request;
  }, [refreshExtraction, route.documentId, route.projectId]);
  useInterval(pollExtraction, extractionPolling ? 2500 : null);

  useEffect(() => {
    const previous = extractionPollingStateRef.current;
    extractionPollingStateRef.current = {
      documentScope: currentDocumentScope,
      jobId: extractionJobId,
      active: extractionActive,
      status: extractionJobStatus,
    };
    if (
      route.task === "extraction" &&
      currentDocumentScope &&
      previous.documentScope === currentDocumentScope &&
      previous.jobId === extractionJobId &&
      previous.active &&
      !extractionActive &&
      extractionJobStatus
    ) {
      void refreshStructure();
    }
  }, [
    currentDocumentScope,
    extractionActive,
    extractionJobId,
    extractionJobStatus,
    refreshStructure,
    route.task,
  ]);

  const structureOptimizationPolling =
    route.task === "extraction" &&
    ["PENDING", "RUNNING", "CANCEL_REQUESTED"].includes(
      String(scopedStructureOptimization?.job?.status || "").toUpperCase(),
    );
  useInterval(
    refreshStructureOptimization,
    structureOptimizationPolling ? 2000 : null,
  );

  useEffect(() => {
    if (
      structureOptimizationWasPolling.current &&
      !structureOptimizationPolling
    ) {
      void refreshDocument();
    }
    structureOptimizationWasPolling.current = structureOptimizationPolling;
  }, [refreshDocument, structureOptimizationPolling]);

  const aiPolling =
    route.task === "analysis" &&
    isAiAnalysisActiveStatus(
      scopedAiStatus?.status ??
        scopedAiStatus?.job_status ??
        (scopedAiStatus?.job as JsonRecord | undefined)?.status ??
        "",
    );
  useInterval(refreshAi, aiPolling ? 2500 : null);

  useEffect(() => {
    if (aiWasPolling.current && !aiPolling) {
      void refreshDocument();
    }
    aiWasPolling.current = aiPolling;
  }, [aiPolling, refreshDocument]);

  const runAction = useCallback(
    async <T,>(
      label: string,
      action: () => Promise<T>,
      options: {
        refresh?: "all" | "project" | "document" | "none";
        scope?: "project" | "document" | "requirementSet";
        successNotice?: string;
        failureNotice?: string;
      } = {},
    ): Promise<T | null> => {
      const refresh = options.refresh || "document";
      const actionScope =
        options.scope ||
        (refresh === "project" || refresh === "all"
          ? "project"
          : "document");
      const activeAction = activeActionRef.current;
      if (
        activeAction &&
        activeAction.identity === actionScopesRef.current[activeAction.scope]
      ) {
        return null;
      }
      const actionIdentity = actionScopesRef.current[actionScope];
      const actionToken = ++actionGeneration.current;
      activeActionRef.current = {
        token: actionToken,
        scope: actionScope,
        identity: actionIdentity,
      };
      setBusyActionState({
        token: actionToken,
        scope: actionScope,
        identity: actionIdentity,
        label,
      });
      setNotice("");
      setError(null);
      try {
        const result = await action();
        const contextStillCurrent =
          activeActionRef.current?.token === actionToken &&
          actionIdentity === actionScopesRef.current[actionScope];
        if (!contextStillCurrent) return null;
        const generation = ++noticeGeneration.current;
        setNotice(options.successNotice || `${label}已完成`);
        window.setTimeout(() => {
          if (generation === noticeGeneration.current) setNotice("");
        }, 3200);
        if (refresh === "all") await refreshAll();
        if (refresh === "project") {
          await refreshProject();
        }
        if (refresh === "document") {
          await refreshDocument();
        }
        return result;
      } catch (cause) {
        const normalized =
          cause instanceof Error ? cause : new Error(String(cause));
        if (
          activeActionRef.current?.token === actionToken &&
          actionIdentity === actionScopesRef.current[actionScope]
        ) {
          setError(normalized);
          noticeGeneration.current += 1;
          setNotice(options.failureNotice || `${label}未完成`);
        }
        return null;
      } finally {
        if (activeActionRef.current?.token === actionToken) {
          activeActionRef.current = null;
          setBusyActionState((current) =>
            current?.token === actionToken ? null : current,
          );
        }
      }
    },
    [
      refreshAll,
      refreshDocument,
      refreshProject,
      route.documentId,
      route.projectId,
    ],
  );

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      route,
      updateRoute: update,
      projects,
      documents: scopedDocuments,
      knowledge: scopedKnowledge,
      workspace: scopedWorkspace,
      requirementSets: scopedRequirementSets,
      extraction: scopedExtraction,
      structure: scopedStructure,
      structureOptimization: scopedStructureOptimization,
      aiStatus: scopedAiStatus,
      exports: scopedExports,
      readiness,
      aiSetup,
      runtimeCapabilities,
      processingProfile: scopedProcessingProfile,
      loading,
      refreshing,
      error,
      notice,
      busyAction,
      selectedProject,
      selectedDocument,
      refreshAll,
      refreshProject,
      refreshDocument,
      refreshExtraction,
      refreshStructure,
      refreshStructureOptimization,
      refreshAi,
      refreshExports,
      runAction,
      setNotice,
    }),
    [
      aiSetup,
      runtimeCapabilities,
      busyAction,
      error,
      loading,
      notice,
      projects,
      readiness,
      refreshAi,
      refreshAll,
      refreshDocument,
      refreshExports,
      refreshExtraction,
      refreshProject,
      refreshStructure,
      refreshStructureOptimization,
      refreshing,
      route,
      runAction,
      scopedAiStatus,
      scopedDocuments,
      scopedExports,
      scopedExtraction,
      scopedKnowledge,
      scopedProcessingProfile,
      scopedRequirementSets,
      scopedStructure,
      scopedStructureOptimization,
      scopedWorkspace,
      selectedDocument,
      selectedProject,
      update,
    ],
  );

  const unavailableEntry = route.pluginSessionId && (!route.projectId || !route.documentId)
    ? "插件成果链接缺少项目或文档信息。请从项目管理打开绑定该来源的成果。"
    : projectsLoaded && route.projectId && !selectedProject
    ? "链接中的项目不存在或当前不可访问。请从项目管理选择可用项目。"
    : route.documentId && selectedProject && documentsScopeRef.current === currentProjectScope && !selectedDocument
      ? "链接中的文档不在当前项目中，或已不可访问。请从项目管理重新选择文档。"
      : "";

  return (
    <WorkspaceProvider value={value}>
      <WorkspaceShell>
        {unavailableEntry ? <EmptyState title="当前入口不可用" description={unavailableEntry}
          action={<button type="button" className="button secondary" onClick={() => update({
            task: "projects", projectId: selectedProject?.id || "", documentId: "", requirementSetId: "",
            pluginSessionId: undefined, resultRevision: undefined, reviewCase: undefined,
          })}>返回项目管理</button>} /> : <DocumentWorkSurface>
        {loading && projects.length === 0 ? (
          <LoadingBlock label="正在连接 ReqPilotX 工作区" />
        ) : error && projects.length === 0 ? (
          <ErrorBlock error={error} onRetry={() => void refreshAll()} />
        ) : (
          <>
            {error && (
              <ErrorBlock
                error={error}
                onRetry={() => void refreshDocument()}
              />
            )}
            {route.task === "projects" && <ProjectManagementPanel />}
            {route.task === "reading" && <Suspense fallback={<LoadingBlock label="正在打开原文阅读" />}><ReadingWorkspacePage embedded /></Suspense>}
            {route.task === "extraction" && <RequirementTreePanel />}
            {route.task === "analysis" && (
              <SourceItemWorkspace task="analysis" fallback={<RequirementMatrixWorkbench lens="analysis" />} />
            )}
            {route.task === "knowledge" && <KnowledgeWorkspace />}
            {route.task === "export" && <SourceItemWorkspace task="export" fallback={<ExportsPanel />} />}
          </>
        )}
        </DocumentWorkSurface>}
      </WorkspaceShell>
    </WorkspaceProvider>
  );
}
