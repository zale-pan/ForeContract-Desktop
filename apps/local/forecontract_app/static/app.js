const TOKEN_KEY = "forecontract_access_token";
const PROJECT_KEY = "forecontract_active_project_id";
const SESSION_KEY = "forecontract_active_session_id";
const RUN_KEY = "forecontract_active_run_id";

const TERMINAL_RUN_STATES = new Set(["succeeded", "failed", "cancelled", "unknown"]);
const RECONNECT_BACKOFF_MS = [1000, 2000, 4000];

const state = {
  token: "",
  projects: [],
  sessions: [],
  selectedProjectId: sessionStorage.getItem(PROJECT_KEY) || "",
  activeSessionId: sessionStorage.getItem(SESSION_KEY) || "",
  activeRunId: sessionStorage.getItem(RUN_KEY) || "",
  currentRunView: null,
  contextInfo: null,
  historyItems: [],
  sessionRunsById: new Map(),
  pendingExecuteGoal: null,
  historyIncomplete: false,
  questionsById: new Map(),
  usageInfo: null,
  availableSkills: [],
  selectedSkillIds: new Set(),
  configuredServers: [],
  runExtensionServers: [],
  runSelectedSkills: [],
  extensionProposalsById: new Map(),
  extensionCallsById: new Map(),
  lastSeq: 0,
  eventsBySeq: new Map(),
  eventsTruncatedBefore: 0,
  proposalsById: new Map(),
  commandsById: new Map(),
  selectedDiffPath: "",
  connectionState: "idle",
  liveEnabled: false,
  liveModel: "",
  reconnectAttempts: 0,
  reconnectTimer: null,
  sseAbort: null,
  readAbort: null,
  projectEpoch: 0,
  sessionEpoch: 0,
  runEpoch: 0,
  pendingLocks: new Set(),
  stoppingCommands: new Set(),
  sessionDrafts: new Map(),
  projectGoalDrafts: new Map(),
  questionDrafts: new Map(),
  navCollapsed: false,
  activePage: "chat",
  settingsSection: "appearance",
  inspectorTab: "diff",
  inspectorExpanded: false,
  extTab: "skill",
  chatTurns: [],
  chatDrafts: new Map(),
  chatReferences: new Map(),
  chatSessionTitles: new Map(),
  chatTurnsReqToken: 0,
  chatTitlesReqToken: 0,
  chatSendToken: 0,
  chatBusy: false,
  chatOpenerEl: null,
  chatCitedRead: null,
  theme: "light",
  density: "normal",
  historyFilter: "",
  historyNextCursor: null,
  historyHasMore: false,
  inboxItems: [],
  inboxPartialFailure: false,
  inboxLoaded: false,
  inboxEpoch: 0,
  reviewEpoch: 0,
  reviewFilesReqToken: 0,
  reviewFileReqToken: 0,
  reviewFilePath: "",
  reviewGitStatusReqToken: 0,
  reviewGitDiffReqToken: 0,
  skillsRefreshToken: 0,
  reviewFilesPath: ".",
  reviewFilesEntries: [],
  reviewFilesNextCursor: null,
  reviewFilesTruncated: false,
  reviewFilesError: "",
  reviewSelectedFile: null,
  reviewFileError: "",
  reviewGitStatus: null,
  reviewGitPath: "",
  reviewGitArea: "index",
  reviewGitDiff: null,
  reviewGitError: "",
};

function el(id) {
  return document.getElementById(id);
}

function initTokenFromHash() {
  const hash = window.location.hash || "";
  if (hash.startsWith("#")) {
    const params = new URLSearchParams(hash.slice(1));
    const tok = params.get("token");
    if (tok) {
      sessionStorage.setItem(TOKEN_KEY, tok);
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }
  state.token = sessionStorage.getItem(TOKEN_KEY) || "";
}

function setError(code, message) {
  const box = el("error-box");
  if (!box) return;
  if (!code && !message) {
    box.textContent = "";
    return;
  }
  box.textContent = code ? `${code}: ${message}` : String(message);
}

function setNotice(message) {
  const box = el("session-notice");
  if (box) {
    box.textContent = message || "";
  }
}

function setConnectionState(nextState) {
  state.connectionState = nextState;
  const badge = el("connection-state");
  if (badge) {
    badge.textContent = nextState;
  }
  const reconnectBtn = el("reconnect-btn");
  if (reconnectBtn) {
    const canReconnect = Boolean(
      state.activeSessionId &&
      state.activeRunId &&
      isSelectedRunActive() &&
      (!state.currentRunView || !TERMINAL_RUN_STATES.has(state.currentRunView.state)) &&
      (nextState === "disconnected" || nextState === "error")
    );
    reconnectBtn.hidden = !canReconnect;
  }
}

function clearReconnectTimer() {
  if (state.reconnectTimer !== null) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
}

function abortActiveStream() {
  clearReconnectTimer();
  if (state.sseAbort) {
    state.sseAbort.abort();
    state.sseAbort = null;
  }
}

function abortPendingReads() {
  if (state.readAbort) {
    state.readAbort.abort();
    state.readAbort = null;
  }
}

function isEpochCurrent(snapshot) {
  if (!snapshot) return false;
  if (snapshot.projectEpoch !== state.projectEpoch) return false;
  if (snapshot.sessionEpoch !== undefined && snapshot.sessionEpoch !== state.sessionEpoch) return false;
  if (snapshot.runEpoch !== undefined && snapshot.runEpoch !== state.runEpoch) return false;
  if (snapshot.projectId !== undefined && snapshot.projectId !== state.selectedProjectId) return false;
  if (snapshot.sessionId !== undefined && snapshot.sessionId !== state.activeSessionId) return false;
  if (snapshot.runId !== undefined && snapshot.runId !== state.activeRunId) return false;
  return true;
}

function generateUuidV4() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10, 16).join("")}`;
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) {
    headers.set("Authorization", `Bearer ${state.token}`);
  }
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    cache: "no-store",
    signal: options.signal,
  });
}

async function readErrorPayload(resp) {
  try {
    const data = await resp.json();
    if (data && data.error) {
      return {
        code: data.error.code || `HTTP_${resp.status}`,
        message: data.error.message || "request failed",
      };
    }
  } catch {
    // ignore parse failure
  }
  return { code: `HTTP_${resp.status}`, message: "request failed" };
}

function getSelectedProject() {
  return state.projects.find((p) => p.project_id === state.selectedProjectId) || null;
}

function getActiveSession() {
  return state.sessions.find((s) => s.session_id === state.activeSessionId) || null;
}

function isSelectedRunActive() {
  const sess = getActiveSession();
  if (!sess || !state.activeRunId) return false;
  if (sess.active_run_id && sess.active_run_id === state.activeRunId) return true;
  if (state.currentRunView && state.currentRunView.run_id === state.activeRunId && !TERMINAL_RUN_STATES.has(state.currentRunView.state)) {
    return true;
  }
  return false;
}

function isSelectedRunInteractive() {
  if (!state.activeSessionId || !state.activeRunId) return false;
  if (!state.currentRunView) return false;
  if (TERMINAL_RUN_STATES.has(state.currentRunView.state)) return false;
  const sess = getActiveSession();
  if (sess && sess.active_run_id && sess.active_run_id !== state.activeRunId) {
    return false;
  }
  return true;
}

function getOrCreateSessionDraft(sessionId) {
  if (!sessionId) return null;
  if (!state.sessionDrafts.has(sessionId)) {
    state.sessionDrafts.set(sessionId, {
      goal: "",
      goalDirty: false,
      constraints: "",
      constraintsDirty: false,
    });
  }
  return state.sessionDrafts.get(sessionId);
}

function renderIdentityHeader() {
  const proj = getSelectedProject();
  const cpEl = el("current-project");
  const cpnEl = el("current-project-name");
  const csEl = el("current-session");
  const sideProjEl = el("sidebar-project-label");
  const kickerEl = el("chat-kicker");
  if (cpEl) cpEl.textContent = state.selectedProjectId || "-";
  if (cpnEl) {
    cpnEl.textContent = proj ? proj.name : "未选择项目";
  }
  if (csEl) csEl.textContent = state.activeSessionId || "-";
  if (sideProjEl) {
    sideProjEl.textContent = proj ? `${proj.name} (${proj.kind})` : "-";
  }
  if (kickerEl) {
    const sess = getActiveSession();
    const modelStr = sess && sess.model ? `${sess.model.provider}:${sess.model.model}` : "未绑定模型";
    kickerEl.textContent = proj
      ? `${proj.name} [${proj.kind}] · ${modelStr}`
      : "未选择项目 · 当前会话";
  }
}

function renderScopeSummary() {
  const scopeEl = el("scope-summary");
  const goalInput = el("goal-input");
  const proj = getSelectedProject();
  if (!scopeEl) return;
  if (!proj) {
    scopeEl.textContent = "请先选择或创建项目：受控示例仅操作 metrics.py；已有本机项目从只读起步，写范围、验证计划与受信任命令须逐步显式批准。";
    if (goalInput) goalInput.placeholder = "输入当前项目需要完成的任务目标";
    return;
  }
  if (proj.kind === "existing") {
    const readText = proj.read_paths && proj.read_paths.length > 0 ? proj.read_paths.join(", ") : "(项目根只读起步)";
    scopeEl.textContent = `已有本机项目 [${proj.name}]：root=${proj.workspace_root} | read_paths=${readText}；写入范围、验证计划与受信任命令须在运行中逐阶段批准（非 OS 沙箱）。`;
    if (goalInput) goalInput.placeholder = "描述对当前已有项目需要调查、修改与验证的目标";
    return;
  }
  const writeNames = Array.isArray(proj.write_paths)
    ? proj.write_paths.map((w) => w.path).join(", ")
    : "metrics.py";
  const testNames = Array.isArray(proj.expected_tests)
    ? proj.expected_tests.map((t) => `${t.classname}.${t.name}`).join(", ")
    : "test_metrics_acceptance.py";
  scopeEl.textContent = `受控示例项目 [${proj.name}]：root=${proj.workspace_root} | 允许写入：${writeNames || "metrics.py"} | 固定验收：${testNames || "pytest_acc"}；无外部 API 与任意 shell 权限。`;
  if (goalInput) goalInput.placeholder = "实现 weighted_positive_total 仅汇总 weight >= min_weight 且 value > 0 的权重乘积";
}

function renderDraftStatus() {
  const statusEl = el("draft-status");
  if (!statusEl) return;
  let dirtyParts = [];
  if (state.activeSessionId) {
    const d = getOrCreateSessionDraft(state.activeSessionId);
    const chatDraft = state.chatDrafts.get(state.activeSessionId) || "";
    if ((d && d.goalDirty) || chatDraft.trim().length > 0) dirtyParts.push("输入");
    if (d && d.constraintsDirty) dirtyParts.push("固定约束");
  } else if (state.selectedProjectId) {
    const g = state.projectGoalDrafts.get(state.selectedProjectId);
    if (g && g.goalDirty) dirtyParts.push("输入");
  }
  if (dirtyParts.length > 0) {
    statusEl.textContent = `未保存草稿（${dirtyParts.join(" / ")}）：草稿未保存，刷新会丢失`;
  } else {
    statusEl.textContent = "草稿已同步（草稿未保存，刷新会丢失）";
  }
}

function loadDraftsIntoInputs() {
  const gInput = el("goal-input");
  const cInput = el("constraints-input");
  if (state.activeSessionId) {
    const d = getOrCreateSessionDraft(state.activeSessionId);
    if (gInput && d) gInput.value = d.goal;
    if (cInput && d) {
      if (d.constraintsDirty) {
        cInput.value = d.constraints;
      } else {
        cInput.value = state.contextInfo && state.contextInfo.session_id === state.activeSessionId
          ? (state.contextInfo.constraints || "")
          : d.constraints;
      }
    }
  } else {
    if (gInput) {
      const pd = state.selectedProjectId ? state.projectGoalDrafts.get(state.selectedProjectId) : null;
      gInput.value = pd ? pd.goal : "";
    }
    if (cInput) {
      cInput.value = "";
    }
  }
  renderDraftStatus();
}

function syncLockButtons() {
  const startKey = `start:${state.selectedProjectId || "none"}:${state.activeSessionId || "new"}`;
  const cancelKey = `cancel:${state.activeSessionId || "none"}:${state.activeRunId || "none"}`;
  const ctxKey = `constraints:${state.activeSessionId || "none"}`;

  const setDisabled = (id, flag) => {
    const btn = el(id);
    if (btn) btn.disabled = Boolean(flag);
  };
  setDisabled("create-project-btn", state.pendingLocks.has("create-project"));
  setDisabled("register-project-btn", state.pendingLocks.has("register-project"));
  setDisabled("new-session-btn", state.pendingLocks.has(`new-session:${state.selectedProjectId || "none"}`) || state.pendingLocks.has("new-session"));
  setDisabled("start-run-btn", state.pendingLocks.has(startKey) || state.pendingLocks.has("start-global"));
  const isRunActive = Boolean(
    state.activeRunId &&
    state.currentRunView &&
    !TERMINAL_RUN_STATES.has(state.currentRunView.state)
  );
  const cancelBtn = el("cancel-run-btn");
  if (cancelBtn) {
    cancelBtn.hidden = !isRunActive;
  }
  setDisabled("cancel-run-btn", state.pendingLocks.has(cancelKey) || !isRunActive);
  setDisabled("save-constraints-btn", state.pendingLocks.has(ctxKey));
}

function syncPendingApprovalsVisibility() {
  const card = el("pending-approvals-card");
  if (!card) return;
  const isTerminal = Boolean(state.currentRunView && TERMINAL_RUN_STATES.has(state.currentRunView.state));
  const hasItems = state.proposalsById.size > 0 || state.extensionProposalsById.size > 0 || state.questionsById.size > 0;
  const shouldShow = !isTerminal && hasItems;
  card.hidden = !shouldShow;
  if (typeof mountPendingApprovalsCard === "function") mountPendingApprovalsCard(card, shouldShow);
}

function parseModelSelection() {
  const proj = getSelectedProject();
  if (state.liveEnabled) {
    return {
      provider: state.liveProvider || "deepseek",
      model: state.liveModel || "deepseek-flash",
      thinking: "disabled",
    };
  }
  if (state.testProviderInjected) {
    return {
      provider: "offline",
      model: proj && proj.kind === "controlled" ? "offline-metrics-v1" : "offline-project-v1",
      thinking: "disabled",
    };
  }
  return {
    provider: "deepseek",
    model: "deepseek-flash",
    thinking: "disabled",
  };
}

function selectProject(proj, options = {}) {
  const userSwitch = options.userSwitch !== false;
  const prevProjectId = state.selectedProjectId;
  const projectChanged = Boolean(proj && proj.project_id !== prevProjectId);
  if (projectChanged) {
    closeExecuteConfirmPanel({ focusInput: false });
  }

  state.selectedProjectId = proj ? proj.project_id : "";
  if (state.selectedProjectId) {
    sessionStorage.setItem(PROJECT_KEY, state.selectedProjectId);
  } else {
    sessionStorage.removeItem(PROJECT_KEY);
  }

  const modelSelect = el("model-select");
  if (proj && modelSelect && modelSelect.value !== "deepseek:deepseek-flash") {
    modelSelect.value =
      proj.kind === "existing" ? "offline:offline-project-v1" : "offline:offline-metrics-v1";
  }

  if (projectChanged && userSwitch) {
    state.projectEpoch += 1;
    state.sessionEpoch += 1;
    state.runEpoch += 1;
    abortActiveStream();
    abortPendingReads();
    setConnectionState("idle");
    setError("", "");
    setNotice("");

    state.activeSessionId = "";
    state.activeRunId = "";
    state.currentRunView = null;
    state.contextInfo = null;
    state.historyItems = [];
    state.historyIncomplete = false;
    state.eventsBySeq.clear();
    state.eventsTruncatedBefore = 0;
    state.proposalsById.clear();
    state.commandsById.clear();
    state.questionsById.clear();
    state.usageInfo = null;
    state.availableSkills = [];
    state.selectedSkillIds.clear();
    state.runExtensionServers = [];
    state.runSelectedSkills = [];
    state.extensionProposalsById.clear();
    state.extensionCallsById.clear();
    state.lastSeq = 0;
    state.selectedDiffPath = "";
    state.reviewEpoch += 1;
    state.inboxEpoch += 1;
    state.reviewFilesPath = ".";
    state.reviewFilesEntries = [];
    state.reviewFilesNextCursor = null;
    state.reviewFilesTruncated = false;
    state.reviewFilesError = "";
    state.reviewSelectedFile = null;
    state.reviewFileError = "";
    state.reviewGitStatus = null;
    state.reviewGitPath = "";
    state.reviewGitDiff = null;
    state.reviewGitError = "";
    state.inboxItems = [];
    state.inboxPartialFailure = false;
    state.inboxLoaded = false;
    state.chatTurns = [];
    state.chatSessionTitles = new Map();
    state.chatCitedRead = null;
    state.chatSendToken += 1;
    state.chatBusy = false;
    sessionStorage.removeItem(SESSION_KEY);
    sessionStorage.removeItem(RUN_KEY);
    loadDraftsIntoInputs();
    syncChatInputForSession();
    renderChatTimeline();
    renderChatRail();
    renderRunHistorySelect();
    renderContextAndHistory();
    renderProposals();
    renderCommands();
    renderQuestions();
    renderExtensions();
    renderEvents();
    renderRun(null);
    renderWorkspaceFiles();
    renderGitReview();
    renderInbox();
  }

  renderIdentityHeader();
  renderScopeSummary();
  renderProjects();
  renderSessions();
  renderChatRail();
  renderSkillCatalog();
  renderMcpServers();
  syncLockButtons();
  if (proj && (projectChanged || state.availableSkills.length === 0)) {
    void refreshProjectSkillsAndServers(proj.project_id, {
      projectEpoch: state.projectEpoch,
      projectId: proj.project_id,
    });
  }
  if (proj && proj.project_id) {
    void refreshChatSessionTitles(proj.project_id);
  }
}

function renderProjects() {
  const ul = el("project-list");
  if (!ul) return;
  ul.replaceChildren();
  for (const proj of state.projects) {
    const li = document.createElement("li");
    li.setAttribute("data-testid", `project-item-${proj.project_id}`);
    const kindLabel = proj.kind === "existing" ? "existing" : "controlled";
    const readText = proj.read_paths && proj.read_paths.length > 0 ? proj.read_paths.join(", ") : "(read-only dynamic)";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "list-item-btn";
    btn.textContent = `${proj.name} [${kindLabel}] (${proj.project_id}) — root=${proj.workspace_root} — read: ${readText}`;
    if (proj.project_id === state.selectedProjectId) {
      li.className = "active-item";
      btn.setAttribute("aria-current", "true");
    }
    btn.addEventListener("click", () => selectProject(proj, { userSwitch: true }));
    li.appendChild(btn);
    ul.appendChild(li);
  }
}

function renderSessions() {
  const ul = el("session-list");
  const visibleSessions = state.selectedProjectId
    ? state.sessions.filter((s) => s.project_id === state.selectedProjectId)
    : state.sessions;
  if (ul) {
    ul.replaceChildren();
    for (const sess of visibleSessions) {
      const li = document.createElement("li");
      li.setAttribute("data-testid", `session-item-${sess.session_id}`);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "list-item-btn";
      btn.textContent = `${sess.session_id} [${sess.model.provider}:${sess.model.model}] rev=${sess.constraints_revision ?? 0} active=${sess.active_run_id || "none"}`;
      if (sess.session_id === state.activeSessionId) {
        li.className = "active-item";
        btn.setAttribute("aria-current", "true");
      }
      btn.addEventListener("click", () => {
        goToPage("chat");
        void switchSession(sess.session_id);
      });
      li.appendChild(btn);
      ul.appendChild(li);
    }
  }
  renderChatRail();
  const histSel = el("history-session-select");
  if (histSel) {
    histSel.replaceChildren();
    if (visibleSessions.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（当前项目尚无会话）";
      histSel.appendChild(opt);
    } else {
      for (const sess of visibleSessions) {
        const opt = document.createElement("option");
        opt.value = sess.session_id;
        opt.textContent = `${sess.session_id} (${sess.model.model})`;
        if (sess.session_id === state.activeSessionId) {
          opt.selected = true;
        }
        histSel.appendChild(opt);
      }
    }
  }
}

function renderRunHistorySelect() {
  const sel = el("run-history-select");
  const modeEl = el("run-history-mode");
  if (!sel) return;
  const sess = getActiveSession();
  const seen = new Set();
  const orderedIds = [];
  for (const item of state.historyItems || []) {
    if (item && item.run_id && !seen.has(item.run_id)) {
      seen.add(item.run_id);
      orderedIds.push(item.run_id);
    }
  }
  if (sess && sess.active_run_id && !seen.has(sess.active_run_id)) {
    seen.add(sess.active_run_id);
    orderedIds.push(sess.active_run_id);
  }
  if (state.activeRunId && !seen.has(state.activeRunId)) {
    seen.add(state.activeRunId);
    orderedIds.push(state.activeRunId);
  }

  sel.replaceChildren();
  if (orderedIds.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "（尚无运行记录）";
    sel.appendChild(opt);
    sel.disabled = true;
    if (modeEl) modeEl.textContent = "";
    return;
  }

  sel.disabled = false;
  for (const rid of orderedIds) {
    const opt = document.createElement("option");
    opt.value = rid;
    const isActiveRun = Boolean(sess && sess.active_run_id === rid);
    opt.textContent = isActiveRun ? `${rid}（当前活动）` : `${rid}（历史只读）`;
    sel.appendChild(opt);
  }
  if (state.activeRunId && seen.has(state.activeRunId)) {
    sel.value = state.activeRunId;
  }
  if (modeEl) {
    if (!state.activeRunId) {
      modeEl.textContent = "";
    } else if (isSelectedRunInteractive()) {
      modeEl.textContent = "当前活动运行";
    } else {
      modeEl.textContent = "只读查看（历史或已终态，不可提交变更）";
    }
  }
}

function extractDurableRunState(text) {
  if (!text || typeof text !== "string") return "";
  const trimmed = text.trim();
  if (!trimmed) return "";
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const candidate = parsed.state;
      if (typeof candidate === "string" && candidate.trim()) {
        return candidate.trim();
      }
    }
    return "";
  } catch {
    return "";
  }
}

function renderContextAndHistory() {
  const revEl = el("context-revision");
  const quotaEl = el("storage-quota");
  const summaryEl = el("context-summary");
  const cInput = el("constraints-input");
  const hList = el("session-history");

  if (!state.contextInfo) {
    if (revEl) revEl.textContent = "rev=0 history_cursor=0";
    if (quotaEl) quotaEl.textContent = "";
    if (summaryEl) summaryEl.textContent = "";
    if (hList) hList.replaceChildren();
    renderDraftStatus();
    renderRunHistorySelect();
    return;
  }

  const ctx = state.contextInfo;
  if (revEl) {
    revEl.textContent = `rev=${ctx.revision} history_cursor=${ctx.history_cursor} session=${ctx.session_id}`;
  }
  const draft = getOrCreateSessionDraft(ctx.session_id);
  if (cInput && draft && !draft.constraintsDirty) {
    cInput.value = ctx.constraints || "";
    draft.constraints = cInput.value;
  }

  if (quotaEl) {
    quotaEl.textContent = `存储用量（SQLite sessions.sqlite3）：已用 ${ctx.storage_used_bytes} 字节；无自动删除或 GC，用户项目数据与临时开发证据分离`;
  }
  const ovStorage = el("settings-overview-storage-bytes");
  if (ovStorage) { ovStorage.textContent = `${ctx.storage_used_bytes} 字节`; }

  const items = state.historyItems || [];
  const activeSess = state.sessions.find((s) => s.session_id === ctx.session_id);
  const runGroupsMap = new Map();
  for (const it of items) {
    const rid = it.run_id || "";
    const groupKey = `${ctx.session_id}:${rid || "__session__"}`;
    let grp = runGroupsMap.get(groupKey);
    if (!grp) {
      grp = {
        sessionId: ctx.session_id,
        runId: rid,
        goalText: "",
        durableState: "",
        items: [],
      };
      runGroupsMap.set(groupKey, grp);
    }
    grp.items.push(it);
    if (it.kind === "user_goal" && !grp.goalText) {
      grp.goalText = it.text || "";
    }
    if (it.kind === "run_result" && it.text) {
      grp.durableState = extractDurableRunState(it.text) || "unparsed";
    }
  }
  if (activeSess && activeSess.active_run_id) {
    const activeKey = `${ctx.session_id}:${activeSess.active_run_id}`;
    if (!runGroupsMap.has(activeKey)) {
      const activeGoal =
        state.currentRunView && state.currentRunView.run_id === activeSess.active_run_id
          ? state.currentRunView.goal || ""
          : "";
      const activeState =
        state.currentRunView && state.currentRunView.run_id === activeSess.active_run_id
          ? state.currentRunView.state || "running"
          : "running";
      runGroupsMap.set(activeKey, {
        sessionId: ctx.session_id,
        runId: activeSess.active_run_id,
        goalText: activeGoal,
        durableState: activeState,
        items: [],
      });
    }
  }
  for (const grp of runGroupsMap.values()) {
    if (
      state.currentRunView &&
      grp.runId &&
      state.currentRunView.run_id === grp.runId
    ) {
      if (!grp.goalText && state.currentRunView.goal) {
        grp.goalText = state.currentRunView.goal;
      }
      if (!grp.durableState && state.currentRunView.state) {
        grp.durableState = state.currentRunView.state;
      }
    }
    if (!grp.durableState && grp.runId && activeSess && activeSess.active_run_id === grp.runId) {
      grp.durableState = "running";
    }
  }

  const allGroups = Array.from(runGroupsMap.values());
  const filterKey = (state.historyFilter || "").trim().toLowerCase();
  const visibleGroups = filterKey
    ? allGroups.filter((grp) => {
        const hay = `${grp.runId} ${grp.goalText} ${grp.durableState}`.toLowerCase();
        return hay.includes(filterKey);
      })
    : allGroups;

  let encoderBytes = 0;
  if (typeof TextEncoder !== "undefined") {
    const enc = new TextEncoder();
    for (const it of items) {
      encoderBytes += enc.encode(it.text || "").length;
    }
  }
  const hasTruncatedItem = items.some((it) => Boolean(it.truncated));
  if (summaryEl) {
    const constraintsLabel = ctx.constraints ? `constraints="${ctx.constraints}"` : "constraints=(空)";
    const incompleteTag = state.historyIncomplete ? " | [历史不完整：分页中断或游标未推进，已停止继续读取]" : "";
    summaryEl.textContent = `历史条目=${items.length}，history_cursor=${ctx.history_cursor}，原文累计约 ${encoderBytes}B；超出预算时按最新优先安全截断（omitted_count 计数、保留来源 ID/SHA；any_item_truncated=${hasTruncatedItem}）；${constraintsLabel}${incompleteTag}`;
  }

  if (hList) {
    hList.replaceChildren();
    if (visibleGroups.length === 0) {
      const emptyLi = document.createElement("li");
      emptyLi.className = "muted";
      emptyLi.textContent = filterKey ? "当前已加载历史中没有匹配筛选条件的记录。" : "尚无历史条目。";
      hList.appendChild(emptyLi);
    } else {
      for (const grp of visibleGroups) {
        const groupLi = document.createElement("li");
        if (grp.runId) {
          groupLi.setAttribute("data-testid", `history-run-${grp.runId}`);
        }
        const header = document.createElement("div");
        header.className = "row between";
        const label = document.createElement("span");
        const stateTag = grp.durableState ? ` [${grp.durableState}]` : "";
        const goalTag = grp.goalText ? ` — ${grp.goalText}` : "";
        label.textContent = grp.runId
          ? `run=${grp.runId}${stateTag}${goalTag} (条目=${grp.items.length})`
          : `session=${grp.sessionId} (条目=${grp.items.length})`;
        header.appendChild(label);
        if (grp.runId) {
          const viewBtn = document.createElement("button");
          viewBtn.type = "button";
          viewBtn.className = "small-btn";
          viewBtn.textContent = "查看运行";
          viewBtn.addEventListener("click", () => {
            goToPage("chat");
            void selectRunView(grp.runId);
          });
          header.appendChild(viewBtn);
        }
        groupLi.appendChild(header);

        if (grp.items.length > 0) {
          const subUl = document.createElement("ul");
          subUl.className = "list";
          for (const it of grp.items) {
            const li = document.createElement("li");
            li.setAttribute("data-testid", `history-item-${it.seq}`);
            const srcs = Array.isArray(it.source_ids) ? it.source_ids.join(",") : "";
            const span = document.createElement("span");
            span.textContent = `#${it.seq} [${it.kind}] run=${it.run_id} ${it.text} (sha=${(it.sha256 || "").slice(0, 12)} sources=[${srcs}] truncated=${Boolean(it.truncated)}) `;
            li.appendChild(span);
            subUl.appendChild(li);
          }
          groupLi.appendChild(subUl);
        }
        hList.appendChild(groupLi);
      }
    }
  }
  const loadMoreBtn = el("history-load-more-btn");
  if (loadMoreBtn) {
    loadMoreBtn.hidden = !state.historyHasMore;
  }
  renderDraftStatus();
  renderRunHistorySelect();
}

function renderUsage() {
  const usageEl = el("usage-summary");
  if (!usageEl) return;
  const u = state.usageInfo;
  if (!u) {
    usageEl.textContent = "尚无运行用量记录；离线不计费，实际账单未知/不适用。";
    return;
  }
  const inTok = u.input_tokens === null || u.input_tokens === undefined ? "unknown" : String(u.input_tokens);
  const outTok = u.output_tokens === null || u.output_tokens === undefined ? "unknown" : String(u.output_tokens);
  usageEl.textContent = `calls started=${u.calls_started} completed=${u.calls_completed} (known=${u.usage_known_calls}, unknown=${u.usage_unknown_calls}) | tokens in=${inTok} out=${outTok} (known_subtotal=${u.known_input_tokens}/${u.known_output_tokens}) | steps=${u.steps_used} | elapsed=${u.elapsed_ms}ms | cost_status=${u.cost_status} estimated=null billed=null（不计费，实际账单未知/不适用）`;
}

function renderQuestions() {
  const ul = el("question-list");
  if (!ul) return;
  syncPendingApprovalsVisibility();
  for (const q of state.questionsById.values()) {
    const existingInput = document.getElementById(`question-answer-input-${q.id}`);
    if (existingInput && existingInput.value) {
      state.questionDrafts.set(`${state.activeSessionId}:${state.activeRunId}:${q.id}`, existingInput.value);
    }
  }
  ul.replaceChildren();
  const allowInteract = isSelectedRunInteractive();
  for (const q of state.questionsById.values()) {
    const li = document.createElement("li");
    li.setAttribute("data-testid", `question-item-${q.id}`);
    const header = document.createElement("div");
    const opts = Array.isArray(q.options) ? q.options : [];
    const ansPart = q.answer !== null && q.answer !== undefined ? ` answer=${q.answer}` : "";
    const reasonPart = q.reason_code ? ` reason=${q.reason_code}` : "";
    header.textContent = `[${q.state}] qid=${q.id} action=${q.action_id} digest=${(q.digest || "").slice(0, 12)} | ${q.question}${ansPart}${reasonPart}`;
    li.appendChild(header);

    if (q.state === "pending" && allowInteract) {
      const capturedSession = state.activeSessionId;
      const capturedRun = state.activeRunId;
      const capturedQ = { ...q };
      const lockKey = `answer:${capturedSession}:${capturedRun}:${q.id}`;
      const isLocked = state.pendingLocks.has(lockKey);

      const draftKey = `${capturedSession}:${capturedRun}:${q.id}`;
      const controls = document.createElement("div");
      controls.className = "row";
      const input = document.createElement("input");
      input.type = "text";
      input.id = `question-answer-input-${q.id}`;
      input.placeholder = "输入任意自由文本回答或点击候选选项";
      input.setAttribute("aria-label", `回答问题 ${q.question}`);
      input.setAttribute("data-testid", `question-answer-input-${q.id}`);
      input.disabled = isLocked;
      input.value = state.questionDrafts.get(draftKey) || "";
      input.addEventListener("input", () => {
        if (input.value) {
          state.questionDrafts.set(draftKey, input.value);
        } else {
          state.questionDrafts.delete(draftKey);
        }
      });
      controls.appendChild(input);

      opts.forEach((optText, idx) => {
        const optBtn = document.createElement("button");
        optBtn.type = "button";
        optBtn.textContent = optText;
        optBtn.setAttribute("data-testid", `question-option-${q.id}-${idx}`);
        optBtn.disabled = isLocked;
        optBtn.addEventListener("click", () => {
          input.value = optText;
          state.questionDrafts.set(draftKey, optText);
        });
        controls.appendChild(optBtn);
      });

      const submitBtn = document.createElement("button");
      submitBtn.type = "button";
      submitBtn.textContent = "提交回答";
      submitBtn.setAttribute("data-testid", `question-submit-${q.id}`);
      submitBtn.disabled = isLocked;
      submitBtn.addEventListener("click", () => {
        void handleAnswerQuestion(capturedSession, capturedRun, capturedQ, input.value);
      });
      controls.appendChild(submitBtn);
      li.appendChild(controls);
    }
    ul.appendChild(li);
  }
}

function renderRun(run) {
  state.currentRunView = run || null;
  const covEl = el("coverage-summary");
  if (!run) {
    el("run-id").textContent = "-";
    el("run-state").textContent = "idle";
    el("verification").textContent = "not_run";
    el("reason-code").textContent = "-";
    state.activeRunChecks = [];
    if (covEl) covEl.textContent = "";
    renderChangedPaths([]);
    renderDiffs([], null);
    renderUsage();
    renderRunHistorySelect();
    renderQuestions();
    renderProposals();
    renderCommands();
    syncLockButtons();
    return;
  }
  el("run-id").textContent = run.run_id;
  el("run-state").textContent = run.state;
  el("verification").textContent = run.verification;
  el("reason-code").textContent = run.reason_code || "-";
  if (typeof run.truncated_before === "number" && run.truncated_before > state.eventsTruncatedBefore) {
    state.eventsTruncatedBefore = run.truncated_before;
  }
  if (run.usage) {
    state.usageInfo = run.usage;
    renderUsage();
  }
  if (run.state === "unknown") {
    setNotice(
      `当前运行处于 unknown（原因：${run.reason_code || "UNKNOWN"}），工作区已保持锁定需处理，绝不自动重试或重放旧运行。`
    );
  }
  const res = run.result || {};
  if (covEl) {
    const parts = [];
    if (res.scope_complete === false) {
      parts.push("范围不完整（超出 10,000 文件或 64 MiB 修订扫描上限，已禁止写入与通过结论）");
    }
    if (res.diff && res.diff.excluded_note) {
      parts.push(`排除范围说明：${res.diff.excluded_note}（不代表覆盖被排除路径）`);
    }
    if (res.diff && res.diff.diff_truncated) {
      parts.push("差异节选已截断（diff_truncated=true，非完整全文）");
    }
    const checkEntries = Array.isArray(res.checks)
      ? res.checks
      : (res.checks && typeof res.checks === "object" ? Object.values(res.checks) : []);
    if (checkEntries.length > 0) {
      const desc = checkEntries.map((c) => {
        const failPart = c.failure_reason ? ` failure=${c.failure_reason}` : "";
        const exitPart = c.exit_code !== null && c.exit_code !== undefined ? ` exit=${c.exit_code}` : "";
        if (c.kind === "exit_code") {
          return `${c.check_id}(exit_code: 仅检查退出码=0，非单元测试全过${exitPart} passed=${Boolean(c.passed)}${failPart})`;
        }
        const obsCount = Array.isArray(c.observed_tests) ? c.observed_tests.length : 0;
        const expCount = Array.isArray(c.expected_tests) ? c.expected_tests.length : 0;
        return `${c.check_id}(junit_exact: junit_valid=${Boolean(c.junit_valid)} passed=${Boolean(c.passed)}${exitPart} observed=${obsCount}/${expCount}${failPart})`;
      });
      parts.push(`检查记录：${desc.join("；")}`);
    } else { appendAcceptanceCoverage(parts, res); }
    if (res.acceptance && res.acceptance.revision) {
      parts.push(`验收修订：${String(res.acceptance.revision).slice(0, 12)}`);
    }
    if (Array.isArray(res.unverified_reasons) && res.unverified_reasons.length > 0) {
      parts.push(`未验证原因：${res.unverified_reasons.join("；")}`);
    }
    covEl.textContent = parts.join(" | ");
  }
  const changed = Array.isArray(res.changed_paths) ? res.changed_paths : [];
  state.activeRunChecks = (res.checks && typeof res.checks === "object") ? Object.values(res.checks) : [];
  renderChangedPaths(changed);
  const diffObj = res.diff && typeof res.diff === "object" ? res.diff : null;
  const diffEntries = (diffObj && Array.isArray(diffObj.entries)) ? diffObj.entries : [];
  renderDiffs(diffEntries, diffObj);
  renderRunHistorySelect();
  renderQuestions();
  renderProposals();
  renderCommands();
  renderExtensions();
  syncLockButtons();
  if (run && run.run_id && run.session_id === state.activeSessionId) {
    state.sessionRunsById.set(run.run_id, run);
    renderChatTimeline();
  }
}

function renderChangedPaths(entries) {
  const ul = el("changed-paths");
  if (!ul) return;
  ul.replaceChildren();
  for (const item of entries) {
    const li = document.createElement("li");
    li.textContent = `${item.path} [${item.operation}] before=${item.before_sha256 || "null"} after=${item.after_sha256 || "null"}`;
    ul.appendChild(li);
  }
}

function renderDiffDetail(entries, diffObj) {
  const detailEl = el("diff-detail");
  const quoteBtn = el("quote-diff-in-chat-btn");
  if (!detailEl) return;
  if (!entries || entries.length === 0) {
    if (quoteBtn) quoteBtn.hidden = true;
    detailEl.textContent = "暂无可查看的差异文件。";
    return;
  }
  const selected = entries.find((e) => e.path === state.selectedDiffPath) || entries[0];
  state.selectedDiffPath = selected.path;
  if (quoteBtn) {
    quoteBtn.hidden = !state.activeRunId || !selected.diff_excerpt;
  }
  const sel = el("diff-file-select");
  if (sel && sel.value !== selected.path) {
    sel.value = selected.path;
  }

  detailEl.replaceChildren();
  const src = selected.source || "ai";
  const isTrunc = Boolean(diffObj && diffObj.diff_truncated);
  const excl = diffObj && diffObj.excluded_note ? ` | 排除说明=${diffObj.excluded_note}` : "";
  renderDiffDetailBodyV4(detailEl, selected, src, isTrunc, excl);
}

function renderDiffs(entries, diffObj) {
  const sel = el("diff-file-select");
  if (sel) {
    sel.replaceChildren();
    if (!entries || entries.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（尚无文件差异）";
      sel.appendChild(opt);
      sel.disabled = true;
      state.selectedDiffPath = "";
    } else {
      sel.disabled = false;
      for (const item of entries) {
        const opt = document.createElement("option");
        opt.value = item.path;
        opt.textContent = `${item.path} [${item.source || "ai"} / ${item.operation}]`;
        sel.appendChild(opt);
      }
      if (!entries.some((e) => e.path === state.selectedDiffPath)) {
        state.selectedDiffPath = entries[0].path;
      }
      sel.value = state.selectedDiffPath;
    }
  }
  renderDiffDetail(entries || [], diffObj || null);

  const ul = el("diff-list");
  if (!ul) return;
  ul.replaceChildren();
  for (const item of entries || []) {
    const li = document.createElement("li");
    const header = document.createElement("div");
    header.textContent = `${item.path} [${item.operation}] source=${item.source || "ai"} before=${item.before_sha256 || "null"} after=${item.after_sha256 || "null"}`;
    li.appendChild(header);
    if (item.diff_excerpt) {
      const pre = document.createElement("pre");
      pre.textContent = item.diff_excerpt;
      li.appendChild(pre);
    }
    ul.appendChild(li);
  }
}

function renderProposals() {
  const ul = el("proposal-list");
  if (!ul) return;
  ul.replaceChildren();
  syncPendingApprovalsVisibility();
  const allowInteract = isSelectedRunInteractive();
  const list = Array.from(state.proposalsById.values());
  for (const prop of list) {
    const pid = prop.id || prop.proposal_id || "";
    const pstate = prop.state || prop.status || "";
    const payload = prop.payload || {};
    const li = document.createElement("li");
    li.setAttribute("data-testid", `proposal-item-${pid}`);
    li.className = "proposal-item-card";
    const [titleDiv, bodyDiv, techDetails] = buildProposalPresentation(prop);
    li.append(titleDiv, bodyDiv, techDetails);

    if (pstate === "pending" && allowInteract) {
      const capturedSession = state.activeSessionId;
      const capturedRun = state.activeRunId;
      const capturedProp = { ...prop };
      const lockKey = `proposal:${capturedSession}:${capturedRun}:${pid}`;
      const isLocked = state.pendingLocks.has(lockKey);

      const controls = document.createElement("div");
      controls.className = "row";
      let trustBox = null;
      if (prop.kind === "command") {
        const trustLabel = document.createElement("label");
        trustLabel.className = "checkbox-label";
        trustBox = document.createElement("input");
        trustBox.type = "checkbox";
        trustBox.checked = false;
        trustBox.disabled = isLocked;
        trustBox.setAttribute("data-testid", `proposal-trust-${pid}`);
        const span = document.createElement("span");
        span.textContent = "确认受信任本机命令执行（非 OS 沙箱，进程可访问本机文件与网络 / trusted_local_execution）";
        trustLabel.appendChild(trustBox);
        trustLabel.appendChild(span);
        controls.appendChild(trustLabel);
      }
      const approveBtn = document.createElement("button");
      approveBtn.type = "button";
      approveBtn.textContent = "批准提案";
      approveBtn.disabled = isLocked;
      approveBtn.setAttribute("data-testid", `proposal-approve-${pid}`);
      approveBtn.addEventListener("click", () => {
        void handleProposalDecision(
          capturedSession,
          capturedRun,
          capturedProp,
          "approve",
          trustBox ? trustBox.checked : false
        );
      });

      const rejectBtn = document.createElement("button");
      rejectBtn.type = "button";
      rejectBtn.textContent = "拒绝提案";
      rejectBtn.disabled = isLocked;
      rejectBtn.setAttribute("data-testid", `proposal-reject-${pid}`);
      rejectBtn.addEventListener("click", () => {
        void handleProposalDecision(capturedSession, capturedRun, capturedProp, "reject", false);
      });

      controls.appendChild(approveBtn);
      controls.appendChild(rejectBtn);
      li.appendChild(controls);
    }
    ul.appendChild(li);
  }
}

function renderSkillCatalog() {
  const ul = el("skill-list");
  if (!ul) return;
  const proj = getSelectedProject();
  if (!proj || proj.kind !== "existing") {
    ul.dataset.renderSig = "";
    ul.replaceChildren();
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "受控示例项目不支持 Skill（请登记已有本机项目以查看 .forecontract/skills）。";
    ul.appendChild(li);
    return;
  }
  if (!Array.isArray(state.availableSkills) || state.availableSkills.length === 0) {
    ul.dataset.renderSig = "";
    ul.replaceChildren();
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "当前项目未发现本地 Skill（.forecontract/skills/<id>/SKILL.md）。";
    ul.appendChild(li);
    return;
  }
  const sig = JSON.stringify({
    projectId: proj.project_id,
    skills: state.availableSkills.map((sk) => [sk.id, sk.path, sk.total_bytes, sk.sha256]),
  });
  if (ul.dataset.renderSig === sig && ul.children.length === state.availableSkills.length) {
    for (const sk of state.availableSkills) {
      const existingCb = ul.querySelector(`[data-testid="skill-select-${sk.id}"]`);
      if (existingCb) {
        existingCb.checked = state.selectedSkillIds.has(sk.id);
      }
    }
    return;
  }
  ul.dataset.renderSig = sig;
  ul.replaceChildren();
  for (const sk of state.availableSkills) {
    const li = document.createElement("li");
    const label = document.createElement("label");
    label.className = "checkbox-label";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.selectedSkillIds.has(sk.id);
    cb.setAttribute("data-testid", `skill-select-${sk.id}`);
    cb.addEventListener("change", () => {
      if (cb.checked) {
        if (state.selectedSkillIds.size >= 4) {
          cb.checked = false;
          setError("SKILL_SELECTION_LIMIT", "单次运行最多勾选 4 个 Skill");
          return;
        }
        state.selectedSkillIds.add(sk.id);
      } else {
        state.selectedSkillIds.delete(sk.id);
      }
      setError("", "");
    });
    const span = document.createElement("span");
    span.textContent = `${sk.id} (${sk.path}, ${sk.total_bytes}B, sha=${(sk.sha256 || "").slice(0, 12)})`;
    label.appendChild(cb);
    label.appendChild(span);
    li.appendChild(label);
    ul.appendChild(li);
  }
}

function renderMcpServers() {
  const ul = el("mcp-server-list");
  if (!ul) return;
  ul.replaceChildren();
  const list =
    Array.isArray(state.runExtensionServers) && state.runExtensionServers.length > 0
      ? state.runExtensionServers
      : state.configuredServers;
  if (!Array.isArray(list) || list.length === 0) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "未配置 MCP 服务器（可通过 --extension-config 显式加载；不会自动执行）。";
    ul.appendChild(li);
    return;
  }
  for (const srv of list) {
    const li = document.createElement("li");
    li.setAttribute("data-testid", `mcp-server-${srv.id}`);
    const target = srv.transport === "http"
      ? `url=${srv.url || "-"}`
      : `exe=${srv.executable || "-"} argv=${JSON.stringify(srv.argv || [])} cwd=${srv.cwd || "-"} exe_sha=${(srv.executable_sha256 || "").slice(0, 12)}`;
    const liveState = srv.state || "not_connected";
    const toolsText = srv.tools_listed ? ` tools=${srv.tools_count || 0}` : "";
    li.textContent = `${srv.id} [${srv.transport}] state=${liveState}${toolsText} ${target} cfg_sha=${(srv.config_sha256 || "").slice(0, 12)}`;
    ul.appendChild(li);
  }
}

function renderExtensionProposals() {
  const ul = el("extension-proposal-list");
  if (!ul) return;
  ul.replaceChildren();
  syncPendingApprovalsVisibility();
  const allowInteract = isSelectedRunInteractive();
  for (const prop of state.extensionProposalsById.values()) {
    const pid = prop.id || "";
    const pstate = prop.state || "pending";
    const li = document.createElement("li");
    li.setAttribute("data-testid", `extension-proposal-${pid}`);
    const summary = document.createElement("div");
    const targetDesc = prop.transport === "http"
      ? `url=${prop.url || "-"}`
      : `exe=${prop.executable || "-"} exe_sha=${(prop.executable_sha256 || "").slice(0, 12)} argv=${JSON.stringify(prop.argv || [])} cwd=${prop.cwd || "-"}`;
    const toolPart = prop.kind === "extension_call"
      ? ` tool=${prop.tool_name || "-"} schema_sha=${(prop.schema_sha256 || "").slice(0, 12)} args_sha=${(prop.args_sha256 || "").slice(0, 12)} args=${JSON.stringify(prop.arguments || {})}`
      : "";
    summary.textContent = `[${pstate}] ${prop.kind} ${pid} server=${prop.server_id} (${targetDesc}${toolPart}) cfg_sha=${(prop.config_sha256 || "").slice(0, 12)} digest=${(prop.digest || "").slice(0, 12)} （受信任本机执行：非 OS 沙箱，服务可读写或联网）`;
    li.appendChild(summary);

    if (pstate === "pending" && allowInteract) {
      const capturedSession = state.activeSessionId;
      const capturedRun = state.activeRunId;
      const capturedProp = { ...prop };
      const lockKey = `extprop:${capturedSession}:${capturedRun}:${pid}`;
      const isLocked = state.pendingLocks.has(lockKey);

      const controls = document.createElement("div");
      controls.className = "ext-card-actions";
      const approveBtn = document.createElement("button");
      approveBtn.type = "button";
      approveBtn.textContent = "批准扩展操作";
      approveBtn.disabled = isLocked;
      approveBtn.setAttribute("data-testid", `extension-approve-${pid}`);
      approveBtn.addEventListener("click", () => {
        void handleExtensionDecision(capturedSession, capturedRun, capturedProp, "approve");
      });

      const rejectBtn = document.createElement("button");
      rejectBtn.type = "button";
      rejectBtn.textContent = "拒绝扩展操作";
      rejectBtn.disabled = isLocked;
      rejectBtn.setAttribute("data-testid", `extension-reject-${pid}`);
      rejectBtn.addEventListener("click", () => {
        void handleExtensionDecision(capturedSession, capturedRun, capturedProp, "reject");
      });
      controls.appendChild(approveBtn);
      controls.appendChild(rejectBtn);
      li.appendChild(controls);
    }
    ul.appendChild(li);
  }
}

function renderExtensionCalls() {
  const resultBox = el("extension-result");
  const ul = el("extension-call-list");
  const calls = Array.from(state.extensionCallsById.values());
  if (resultBox) {
    const skillText = Array.isArray(state.runSelectedSkills) && state.runSelectedSkills.length > 0
      ? `已载入 Skill 来源: ${state.runSelectedSkills.map((s) => `${s.id}(${s.path}, sha=${(s.sha256 || "").slice(0, 12)})`).join(", ")}`
      : "未指定本轮 Skill";
    if (calls.length === 0) {
      resultBox.textContent = `${skillText}；尚无 MCP 调用结果。`;
    } else {
      const latest = calls[calls.length - 1];
      resultBox.textContent = `${skillText} | 最新调用 ${latest.call_id} ${latest.server_id}.${latest.tool_name} status=${latest.status} is_error=${Boolean(latest.is_error)} effect=${latest.effect} sha=${(latest.sha256 || "").slice(0, 12)} truncated=${Boolean(latest.truncated)} ${latest.text || ""}`;
    }
  }
  if (!ul) return;
  ul.replaceChildren();
  for (const c of calls) {
    const li = document.createElement("li");
    li.setAttribute("data-testid", `extension-call-${c.call_id}`);
    const header = document.createElement("div");
    header.textContent = `${c.call_id} ${c.server_id}.${c.tool_name} status=${c.status} is_error=${Boolean(c.is_error)} effect=${c.effect} args_sha=${(c.args_sha256 || "").slice(0, 12)} schema_sha=${(c.schema_sha256 || "").slice(0, 12)} truncated=${Boolean(c.truncated)} bytes=${c.original_bytes || 0}`;
    li.appendChild(header);
    if (c.text) {
      const pre = document.createElement("pre");
      pre.textContent = c.text;
      li.appendChild(pre);
    }
    ul.appendChild(li);
  }
}

function renderExtensions() {
  renderSkillCatalog();
  renderMcpServers();
  renderExtensionProposals();
  renderExtensionCalls();
}

function renderCommands() {
  const ul = el("command-list");
  if (!ul) return;
  ul.replaceChildren();
  const allowInteract = isSelectedRunInteractive();
  const cmdItems = state.commandsById.size > 0
    ? Array.from(state.commandsById.values())
    : (Array.isArray(state.activeRunChecks)
      ? state.activeRunChecks
          .filter((chk) => chk && typeof chk === "object" && chk.command_id)
          .map((chk) => ({
            command_id: chk.command_id,
            state: chk.cancelled ? "cancelled" : (chk.timed_out ? "timed_out" : "finished"),
            exit_code: chk.exit_code,
            cleanup_ok: chk.cleanup_ok,
            remaining_active: chk.remaining_active || 0,
            bytes_seen: chk.output_bytes_seen || 0,
            output_delivery_failed: chk.output_delivery_failed,
            chunks: [],
          }))
      : []);
  for (const cmd of cmdItems) {
    const cstate = cmd.state || cmd.status || "unknown";
    const isStopping = state.stoppingCommands.has(cmd.command_id) && (cstate === "pending" || cstate === "starting" || cstate === "running");
    const displayState = isStopping ? `${cstate}（停止中，等待进程退出与回收回执）` : cstate;
    const remaining = cmd.remaining_active !== undefined ? cmd.remaining_active : (cmd.active_processes_remaining || 0);
    const li = document.createElement("li");
    li.setAttribute("data-testid", `command-item-${cmd.command_id}`);
    const header = document.createElement("div");
    const truncOut = cmd.truncated_stdout_bytes || 0;
    const truncErr = cmd.truncated_stderr_bytes || 0;
    const truncNote = cmd.truncated ? ` (stdout_trunc=${truncOut}B stderr_trunc=${truncErr}B)` : "";
    header.textContent = `${cmd.command_id} state=${displayState} pid=${cmd.pid === null || cmd.pid === undefined ? "-" : cmd.pid} exit=${cmd.exit_code === null || cmd.exit_code === undefined ? "-" : cmd.exit_code} cleanup_ok=${cmd.cleanup_ok} remaining=${remaining} bytes=${cmd.bytes_seen || 0} truncated=${Boolean(cmd.truncated)}${truncNote} delivery_failed=${Boolean(cmd.output_delivery_failed)}`;
    li.appendChild(header);
    if (state.activeRunId && cmd.command_id) {
      const capturedRun = state.activeRunId;
      const capturedCid = cmd.command_id;
      const quoteCmdBtn = document.createElement("button");
      quoteCmdBtn.type = "button";
      quoteCmdBtn.className = "small-btn";
      quoteCmdBtn.textContent = "带入对话";
      quoteCmdBtn.setAttribute("data-testid", `quote-cmd-${capturedCid}`);
      quoteCmdBtn.addEventListener("click", () => {
        addChatReference({
          kind: "command",
          run_id: capturedRun,
          command_id: capturedCid,
          _label: `${capturedCid} · Run ${capturedRun.slice(0, 8)} · exit=${cmd.exit_code === null || cmd.exit_code === undefined ? "-" : cmd.exit_code}`,
        });
      });
      li.appendChild(quoteCmdBtn);
    }
    if (allowInteract && (cstate === "pending" || cstate === "starting" || cstate === "running")) {
      const capturedSession = state.activeSessionId;
      const capturedRun = state.activeRunId;
      const capturedCid = cmd.command_id;
      const lockKey = `stop-cmd:${capturedSession}:${capturedRun}:${capturedCid}`;
      const stopBtn = document.createElement("button");
      stopBtn.type = "button";
      stopBtn.textContent = isStopping ? "停止中" : "停止命令";
      stopBtn.disabled = state.pendingLocks.has(lockKey) || isStopping;
      stopBtn.setAttribute("data-testid", `stop-command-${cmd.command_id}`);
      stopBtn.addEventListener("click", () => {
        void handleStopCommand(capturedSession, capturedRun, capturedCid);
      });
      li.appendChild(stopBtn);
    }
    const orderedChunks = Array.isArray(cmd.chunks)
      ? [...cmd.chunks].sort((a, b) => a.seq - b.seq)
      : [];
    const output = orderedChunks.map((c) => `[${c.seq}:${c.stream}] ${c.text}`).join("");
    if (output) {
      const pre = document.createElement("pre");
      pre.setAttribute("data-testid", `command-output-${cmd.command_id}`);
      pre.textContent = output;
      li.appendChild(pre);
    }
    ul.appendChild(li);
  }
}

function renderEvents() {
  const ol = el("tool-events");
  const truncEl = el("events-truncation-notice");
  if (truncEl) {
    truncEl.textContent = state.eventsTruncatedBefore > 0
      ? `持久事件尾按有界上限截断：seq <= ${state.eventsTruncatedBefore} 的较早事件未全量保留（仅展示保留窗口，不伪造丢失事件）。`
      : "";
  }
  if (!ol) return;
  ol.replaceChildren();
  const ordered = Array.from(state.eventsBySeq.values()).sort((a, b) => a.seq - b.seq);
  for (const ev of ordered) {
    const li = document.createElement("li");
    const summary = formatEventSummary(ev);
    li.textContent = `#${ev.seq} [${ev.kind}] ${summary}`;
    ol.appendChild(li);
  }
}

function formatEventSummary(ev) {
  const p = ev.payload || {};
  if (ev.kind === "tool_result") {
    const parts = [p.action_type || "tool"];
    if (p.path) parts.push(`path=${p.path}`);
    if (p.command_id) parts.push(`cmd=${p.command_id}`);
    if (p.exit_code !== undefined) parts.push(`exit=${p.exit_code}`);
    if (p.tool_succeeded !== undefined) parts.push(`ok=${p.tool_succeeded}`);
    if (p.truncated) parts.push(`truncated(${p.original_bytes}B)`);
    return parts.join(" ");
  }
  if (ev.kind === "core_trace") {
    return `${p.kind || "trace"} seq=${p.sequence} id=${p.record_id || ""}`;
  }
  if (ev.kind === "proposal_requested" || ev.kind === "proposal_decided") {
    return `${p.id || p.proposal_id || ""} kind=${p.kind || ""} state=${p.state || p.status || ""}`;
  }
  if (ev.kind === "question_requested" || ev.kind === "question_answered" || ev.kind === "question_closed") {
    return `${p.id || ""} state=${p.state || ""} reason=${p.reason_code || "-"}`;
  }
  if (ev.kind === "command_started") {
    return `${p.command_id || ""} state=${p.state || p.status || "running"}`;
  }
  if (ev.kind === "command_output") {
    return `${p.command_id || ""} [${p.seq}:${p.stream}] ${p.text || ""}`;
  }
  if (ev.kind === "command_finished") {
    return `${p.command_id || ""} state=${p.state || p.status || ""} exit=${p.exit_code ?? "-"} cleanup_ok=${p.cleanup_ok}`;
  }
  if (ev.kind === "run_finished") {
    const label = p.verification === "passed" ? "示例验收通过" : `${p.state}/${p.verification}`;
    return `${label} reason=${p.reason_code || "-"}`;
  }
  return JSON.stringify(p);
}

function ingestEvent(ev) {
  if (!ev || typeof ev.seq !== "number") return false;
  if (ev.project_id && state.selectedProjectId && ev.project_id !== state.selectedProjectId) return false;
  if (!state.activeSessionId || ev.session_id !== state.activeSessionId) return false;
  if (!state.activeRunId || ev.run_id !== state.activeRunId) return false;
  if (state.eventsBySeq.has(ev.seq)) return false;

  state.eventsBySeq.set(ev.seq, ev);
  if (ev.seq > state.lastSeq) {
    state.lastSeq = ev.seq;
  }
  const p = ev.payload || {};
  const pid = p.id || p.proposal_id;
  if ((ev.kind === "proposal_requested" || ev.kind === "proposal_decided") && pid) {
    if (p.extension === true) {
      state.extensionProposalsById.set(pid, p);
      renderExtensionProposals();
    } else {
      state.proposalsById.set(pid, p);
      recordRunProposalItem(ev.session_id, ev.run_id, p);
      renderProposals();
    }
  }
  if (ev.kind === "tool_result" && p.extension === true && p.call_id) {
    state.extensionCallsById.set(p.call_id, p);
    renderExtensionCalls();
  }
  if (
    (ev.kind === "question_requested" || ev.kind === "question_answered" || ev.kind === "question_closed") &&
    p.id
  ) {
    state.questionsById.set(p.id, p);
    renderQuestions();
  }
  if (ev.kind === "command_started" && p.command_id) {
    const existing = state.commandsById.get(p.command_id) || {
      command_id: p.command_id,
      state: p.state || p.status || "running",
      chunks: [],
      last_seq: 0,
      cleanup_ok: true,
    };
    state.commandsById.set(p.command_id, { ...existing, ...p });
    renderCommands();
  }
  if (ev.kind === "command_output" && p.command_id) {
    const existing = state.commandsById.get(p.command_id) || {
      command_id: p.command_id,
      state: "running",
      chunks: [],
      last_seq: 0,
      cleanup_ok: true,
    };
    const chunks = Array.isArray(existing.chunks) ? [...existing.chunks] : [];
    if (!chunks.some((c) => c.seq === p.seq)) {
      chunks.push({ seq: p.seq, stream: p.stream, text: p.text || "" });
    }
    state.commandsById.set(p.command_id, {
      ...existing,
      chunks,
      last_seq: Math.max(existing.last_seq || 0, p.seq || 0),
    });
    renderCommands();
  }
  if (ev.kind === "command_finished" && p.command_id) {
    state.stoppingCommands.delete(p.command_id);
    const existing = state.commandsById.get(p.command_id) || {
      command_id: p.command_id,
      chunks: [],
      last_seq: 0,
    };
    state.commandsById.set(p.command_id, { ...existing, ...p });
    renderCommands();
  }
  renderEvents();
  return true;
}

async function refreshProjectSkillsAndServers(projectId, guard = null) {
  const reqToken = ++state.skillsRefreshToken;
  const signal = guard && guard.signal ? guard.signal : undefined;
  const expectedProjectEpoch = guard && guard.projectEpoch !== undefined ? guard.projectEpoch : state.projectEpoch;
  const expectedProjectId = guard && guard.projectId !== undefined ? guard.projectId : projectId;
  const srvPromise = apiFetch("/api/v1/extensions/servers", { signal }).catch(() => null);
  const skPromise = projectId
    ? apiFetch(`/api/v1/projects/${projectId}/skills`, { signal }).catch(() => null)
    : Promise.resolve(null);
  const [srvResp, skResp] = await Promise.all([srvPromise, skPromise]);
  if (reqToken !== state.skillsRefreshToken) return;
  if (srvResp && srvResp.ok) {
    const srvs = await srvResp.json();
    if (reqToken !== state.skillsRefreshToken) return;
    if (Array.isArray(srvs)) {
      state.configuredServers = srvs;
      renderMcpServers();
    }
  }
  if (expectedProjectEpoch !== state.projectEpoch || expectedProjectId !== state.selectedProjectId) {
    return;
  }
  if (skResp && skResp.ok) {
    const sks = await skResp.json();
    if (
      reqToken !== state.skillsRefreshToken ||
      expectedProjectEpoch !== state.projectEpoch ||
      expectedProjectId !== state.selectedProjectId
    ) {
      return;
    }
    state.availableSkills = Array.isArray(sks) ? sks : [];
    renderSkillCatalog();
  }
}

async function refreshRunExtensions(sessionId, runId, guard = null) {
  const signal = guard ? guard.signal : undefined;
  let extResp;
  try {
    extResp = await apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}/extensions`, { signal });
  } catch (err) {
    if (err && err.name === "AbortError") return;
    return;
  }
  if (guard && !isEpochCurrent(guard)) return;
  if (!extResp || !extResp.ok) return;
  const data = await extResp.json();
  if (guard && !isEpochCurrent(guard)) return;
  state.runSelectedSkills = Array.isArray(data.selected_skills) ? data.selected_skills : [];
  state.runExtensionServers = Array.isArray(data.servers) ? data.servers : [];
  state.extensionProposalsById.clear();
  if (Array.isArray(data.proposals)) {
    for (const p of data.proposals) {
      if (p && p.id) state.extensionProposalsById.set(p.id, p);
    }
  }
  state.extensionCallsById.clear();
  if (Array.isArray(data.calls)) {
    for (const c of data.calls) {
      if (c && c.call_id) state.extensionCallsById.set(c.call_id, c);
    }
  }
  renderExtensions();
}

async function refreshProjectsAndSessions(options = {}) {
  const signal = options.signal;
  const expectedProjectEpoch = options.expectedProjectEpoch !== undefined
    ? options.expectedProjectEpoch
    : state.projectEpoch;
  const expectedProjectId = options.expectedProjectId;
  const expectedSessionEpoch = options.expectedSessionEpoch;
  const isFresh = () => {
    if (options.guardEpoch && expectedProjectEpoch !== state.projectEpoch) return false;
    if (options.guardEpoch && expectedProjectId !== undefined && expectedProjectId !== state.selectedProjectId) return false;
    if (options.guardSession && expectedSessionEpoch !== undefined && expectedSessionEpoch !== state.sessionEpoch) return false;
    return true;
  };
  const [pResp, sResp] = await Promise.all([
    apiFetch("/api/v1/projects", { signal }),
    apiFetch("/api/v1/sessions", { signal }),
  ]);
  if (!isFresh()) {
    return;
  }
  if (!pResp.ok) {
    const err = await readErrorPayload(pResp);
    if (!isFresh()) return;
    setError(err.code, err.message);
  } else {
    const pJson = await pResp.json();
    if (!isFresh()) return;
    state.projects = pJson;
    if (!state.selectedProjectId && state.projects.length > 0) {
      state.selectedProjectId = state.projects[0].project_id;
      sessionStorage.setItem(PROJECT_KEY, state.selectedProjectId);
    } else if (state.selectedProjectId && !state.projects.some((p) => p.project_id === state.selectedProjectId)) {
      state.selectedProjectId = state.projects.length > 0 ? state.projects[0].project_id : "";
      if (state.selectedProjectId) {
        sessionStorage.setItem(PROJECT_KEY, state.selectedProjectId);
      } else {
        sessionStorage.removeItem(PROJECT_KEY);
      }
    }
    renderIdentityHeader();
    renderScopeSummary();
    renderProjects();
  }
  if (!sResp.ok) {
    const err = await readErrorPayload(sResp);
    if (!isFresh()) return;
    setError(err.code, err.message);
  } else {
    const sJson = await sResp.json();
    if (!isFresh()) return;
    state.sessions = sJson;
    renderSessions();
    renderRunHistorySelect();
  }
  if (isFresh()) {
    await refreshProjectSkillsAndServers(state.selectedProjectId, {
      projectEpoch: state.projectEpoch,
      projectId: state.selectedProjectId,
      signal,
    });
  }
}

async function refreshSessionContextAndHistory(sessionId, guard = null) {
  const snapEpoch = guard || {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
    projectId: state.selectedProjectId,
    sessionId,
  };
  if (!sessionId) {
    if (isEpochCurrent({ projectEpoch: snapEpoch.projectEpoch, sessionEpoch: snapEpoch.sessionEpoch })) {
      state.contextInfo = null;
      state.historyItems = [];
      state.historyIncomplete = false;
      renderContextAndHistory();
    }
    return;
  }
  const signal = snapEpoch.signal;
  const loadContext = async () => {
    let ctxResp;
    try {
      ctxResp = await apiFetch(`/api/v1/sessions/${sessionId}/context`, { signal });
    } catch (err) {
      if (err && err.name === "AbortError") return false;
      if (isEpochCurrent(snapEpoch)) {
        setError("NETWORK_ERROR", "无法读取会话上下文");
      }
      return false;
    }
    if (!isEpochCurrent(snapEpoch)) return false;
    if (!ctxResp.ok) {
      const err = await readErrorPayload(ctxResp);
      if (!isEpochCurrent(snapEpoch)) return false;
      setError(err.code, err.message);
    } else {
      const ctxJson = await ctxResp.json();
      if (!isEpochCurrent(snapEpoch)) return false;
      state.contextInfo = ctxJson;
    }
    return true;
  };

  const loadHistory = async () => {
    const allItems = [];
    const seenSeqs = new Set();
    let incomplete = false;
    let after = 0;
    while (true) {
      let hResp;
      try {
        hResp = await apiFetch(`/api/v1/sessions/${sessionId}/history?after=${after}&limit=50`, { signal });
      } catch (err) {
        if (err && err.name === "AbortError") return false;
        if (!isEpochCurrent(snapEpoch)) return false;
        incomplete = true;
        break;
      }
      if (!isEpochCurrent(snapEpoch)) return false;
      if (!hResp.ok) {
        const err = await readErrorPayload(hResp);
        if (!isEpochCurrent(snapEpoch)) return false;
        incomplete = true;
        setError(err.code, err.message);
        break;
      }
      const page = await hResp.json();
      if (!isEpochCurrent(snapEpoch)) return false;
      const pageItems = Array.isArray(page.items) ? page.items : [];
      for (const item of pageItems) {
        if (item && !seenSeqs.has(item.seq)) {
          seenSeqs.add(item.seq);
          allItems.push(item);
        }
      }
      if (!page.has_more || pageItems.length === 0) {
        break;
      }
      if (typeof page.cursor !== "number" || page.cursor <= after) {
        incomplete = true;
        break;
      }
      after = page.cursor;
    }
    if (!isEpochCurrent(snapEpoch)) return false;
    state.historyItems = allItems;
    state.historyIncomplete = incomplete;
    return true;
  };

  await Promise.all([loadContext(), loadHistory()]);
  if (!isEpochCurrent(snapEpoch)) return;
  renderContextAndHistory();
  const runIdsToFetch = [];
  const seenRunIds = new Set();
  for (const item of state.historyItems || []) {
    if (item && item.run_id && !seenRunIds.has(item.run_id) && !state.sessionRunsById.has(item.run_id)) {
      seenRunIds.add(item.run_id);
      runIdsToFetch.push(item.run_id);
    }
  }
  const activeSess = getActiveSession();
  if (activeSess && activeSess.active_run_id && !seenRunIds.has(activeSess.active_run_id) && !state.sessionRunsById.has(activeSess.active_run_id)) {
    runIdsToFetch.push(activeSess.active_run_id);
  }
  if (runIdsToFetch.length > 0) {
    await Promise.all(
      runIdsToFetch.map(async (rid) => {
        try {
          const rResp = await apiFetch(`/api/v1/sessions/${sessionId}/runs/${rid}`, { signal });
          if (rResp.ok && isEpochCurrent(snapEpoch)) {
            const rv = await rResp.json();
            if (rv && rv.run_id && rv.session_id === sessionId) {
              state.sessionRunsById.set(rv.run_id, rv);
            }
          }
        } catch {
          // Ignore transient/aborted per-run detail fetches during session switches.
        }
      })
    );
    if (isEpochCurrent(snapEpoch)) {
      renderChatTimeline();
    }
  }
}

async function refreshCommandsForRun(sessionId, runId, guard) {
  const signal = guard ? guard.signal : undefined;
  for (const cmdId of Array.from(state.commandsById.keys())) {
    if (!isEpochCurrent(guard)) return;
    const prev = state.commandsById.get(cmdId);
    const afterSeq = prev && typeof prev.last_seq === "number" ? prev.last_seq : 0;
    let cmdResp;
    try {
      cmdResp = await apiFetch(
        `/api/v1/sessions/${sessionId}/runs/${runId}/commands/${cmdId}?after=${afterSeq}`,
        { signal }
      );
    } catch (err) {
      if (err && err.name === "AbortError") return;
      continue;
    }
    if (!isEpochCurrent(guard)) return;
    if (cmdResp.ok) {
      const cmdView = await cmdResp.json();
      if (!isEpochCurrent(guard)) return;
      const prevChunks = prev && Array.isArray(prev.chunks) ? prev.chunks : [];
      const seen = new Set(prevChunks.map((c) => c.seq));
      const merged = [...prevChunks];
      for (const c of cmdView.chunks || []) {
        if (!seen.has(c.seq)) {
          seen.add(c.seq);
          merged.push(c);
        }
      }
      if (TERMINAL_RUN_STATES.has(cmdView.state) || (cmdView.state !== "running" && cmdView.state !== "starting" && cmdView.state !== "pending")) {
        state.stoppingCommands.delete(cmdId);
      }
      state.commandsById.set(cmdId, { ...cmdView, chunks: merged });
    }
  }
  if (isEpochCurrent(guard)) {
    renderCommands();
  }
}

function mountPendingApprovalsCard(card, shouldShow) {
  if (!card) return;
  const targetRunId = state.activeRunId || "";
  const runCard = shouldShow && targetRunId ? document.querySelector(`[data-testid="timeline-run-${targetRunId}"]`) : null;
  if (runCard) {
    const actionsEl = runCard.querySelector(":scope > .actions");
    if (actionsEl) runCard.insertBefore(card, actionsEl);
    else runCard.appendChild(card);
    return;
  }
  const scrollArea = el("chat-scroll-area");
  if (scrollArea && card.parentElement !== scrollArea) scrollArea.appendChild(card);
}

function formatProposalShortTech(prop) {
  const pid = prop.id || prop.proposal_id || "";
  const pstate = prop.state || prop.status || "";
  return `[${pstate}] ${prop.kind || "unknown"} ${pid} digest=${(prop.digest || "").slice(0, 12)}`;
}

// Keep applyRunSideResources line alignment for CDP guard verification.
// Guard line 1915: if (!isEpochCurrent(guard)) return;
//

/**
 * Apply fetched run side resources (proposals, questions, usage) if epoch guard
 * is still current. Line 1915 is pinned for CDP breakpoint regression tests.
 * @param {Response|null} propResp
 * @param {Response|null} qResp
 * @param {Response|null} uResp
 * @param {Object} guard
 */
async function applyRunSideResources(propResp, qResp, uResp, guard) {
  if (!isEpochCurrent(guard)) return;
  if (propResp && propResp.ok) {
    const items = await propResp.json();
    if (!isEpochCurrent(guard)) return;
    const prevById = new Map(state.proposalsById);
    state.proposalsById.clear();
    for (const item of items) {
      const pid = item.id || item.proposal_id;
      if (!pid) continue;
      const prev = prevById.get(pid);
      if (
        prev &&
        prev.state &&
        prev.state !== "pending" &&
        item.state === "pending" &&
        prev.digest === item.digest
      ) {
        state.proposalsById.set(pid, prev);
      } else {
        state.proposalsById.set(pid, item);
      }
    }
    recordRunProposals(guard.sessionId || state.activeSessionId, guard.runId || state.activeRunId, items);
    renderProposals();
    renderChatTimeline();
  }
  if (qResp && qResp.ok) {
    const qItems = await qResp.json();
    if (!isEpochCurrent(guard)) return;
    const prevById = new Map(state.questionsById);
    state.questionsById.clear();
    for (const q of qItems) {
      if (!q || !q.id) continue;
      const prev = prevById.get(q.id);
      if (
        prev &&
        prev.state &&
        prev.state !== "pending" &&
        q.state === "pending" &&
        prev.digest === q.digest
      ) {
        state.questionsById.set(q.id, prev);
      } else {
        state.questionsById.set(q.id, q);
      }
    }
    renderQuestions();
  }
  if (uResp && uResp.ok) {
    const uObj = await uResp.json();
    if (!isEpochCurrent(guard)) return;
    state.usageInfo = uObj;
    renderUsage();
  }
}

async function refreshRunProposalsQuestionsUsageAndCommands(sessionId, runId, guard) {
  const signal = guard ? guard.signal : undefined;
  const isStaleReadAbort = (err) => Boolean(
    err && err.name === "AbortError"
    && signal && signal.aborted
    && (!state.readAbort || state.readAbort.signal !== signal || !isEpochCurrent(guard))
  );
  const attachBranchOutcome = (promise) => promise.then(
    () => ({ ok: true, error: null }),
    (err) => (isStaleReadAbort(err) ? { ok: true, error: null } : { ok: false, error: err })
  );
  let propResp;
  let qResp;
  let uResp;
  const cmdOutcome = attachBranchOutcome(refreshCommandsForRun(sessionId, runId, guard));
  const extOutcome = attachBranchOutcome(refreshRunExtensions(sessionId, runId, guard));
  try {
    [propResp, qResp, uResp] = await Promise.all([
      apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}/proposals`, { signal }),
      apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}/questions`, { signal }),
      apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}/usage`, { signal }),
    ]);
  } catch (err) {
    const [cmdRes, extRes] = await Promise.all([cmdOutcome, extOutcome]);
    if (isStaleReadAbort(err)) {
      if (!cmdRes.ok) throw cmdRes.error;
      if (!extRes.ok) throw extRes.error;
      return;
    }
    if (isEpochCurrent(guard)) {
      setError("NETWORK_ERROR", "无法读取运行提案、提问或用量");
    }
    if (!cmdRes.ok) throw cmdRes.error;
    if (!extRes.ok) throw extRes.error;
    return;
  }
  let sideErr = null;
  try {
    await applyRunSideResources(propResp, qResp, uResp, guard);
  } catch (err) {
    if (!isStaleReadAbort(err)) {
      sideErr = err;
    }
  }
  const [cmdRes, extRes] = await Promise.all([cmdOutcome, extOutcome]);
  if (sideErr) throw sideErr;
  if (!cmdRes.ok) throw cmdRes.error;
  if (!extRes.ok) throw extRes.error;
}

async function switchSession(sessionId) {
  setError("", "");
  setNotice("");
  abortActiveStream();
  abortPendingReads();

  state.sessionEpoch += 1;
  state.runEpoch += 1;
  state.activeSessionId = sessionId;
  sessionStorage.setItem(SESSION_KEY, sessionId);

  const readController = new AbortController();
  state.readAbort = readController;

  state.currentRunView = null;
  state.contextInfo = null;
  state.historyItems = [];
  state.sessionRunsById.clear();
  state.historyIncomplete = false;
  closeExecuteConfirmPanel({ focusInput: false });
  state.eventsBySeq.clear();
  state.eventsTruncatedBefore = 0;
  state.proposalsById.clear();
  state.commandsById.clear();
  state.questionsById.clear();
  state.usageInfo = null;
  state.runExtensionServers = [];
  state.runSelectedSkills = [];
  state.extensionProposalsById.clear();
  state.extensionCallsById.clear();
  state.lastSeq = 0;
  state.selectedDiffPath = "";
  state.reconnectAttempts = 0;
  state.chatTurns = [];
  state.chatCitedRead = null;
  state.chatSendToken += 1;
  state.chatBusy = false;
  setConnectionState("idle");

  const sess = state.sessions.find((s) => s.session_id === sessionId);
  if (sess) {
    const proj = state.projects.find((p) => p.project_id === sess.project_id);
    if (proj && proj.project_id !== state.selectedProjectId) {
      selectProject(proj, { userSwitch: false });
    }
    const modelSelect = el("model-select");
    if (modelSelect && sess.model) {
      modelSelect.value = `${sess.model.provider}:${sess.model.model}`;
    }
  }
  renderIdentityHeader();
  renderScopeSummary();
  renderSessions();
  loadDraftsIntoInputs();
  syncChatInputForSession();
  renderProposals();
  renderCommands();
  renderQuestions();
  renderEvents();
  renderRun(null);
  renderChatTimeline();

  const guard = {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
    projectId: state.selectedProjectId,
    sessionId,
    signal: readController.signal,
  };
  let knownRunId = sess && sess.active_run_id ? sess.active_run_id : "";
  if (knownRunId) {
    state.activeRunId = knownRunId;
    sessionStorage.setItem(RUN_KEY, knownRunId);
    renderRunHistorySelect();
    try {
      await Promise.all([
        refreshSessionContextAndHistory(sessionId, guard),
        refreshChatTurns(sessionId, guard),
        syncSnapshotAndStream(sessionId, knownRunId, { readController, skipTerminalHistoryRefresh: true }),
      ]);
    } catch (err) {
      if (
        err && err.name === "AbortError"
        && readController.signal.aborted
        && (state.readAbort !== readController || !isEpochCurrent(guard))
      ) {
        return;
      }
      throw err;
    }
    return;
  }

  try {
    await Promise.all([
      refreshSessionContextAndHistory(sessionId, guard),
      refreshChatTurns(sessionId, guard),
    ]);
  } catch (err) {
    if (
      err && err.name === "AbortError"
      && readController.signal.aborted
      && (state.readAbort !== readController || !isEpochCurrent(guard))
    ) {
      return;
    }
    throw err;
  }
  if (state.projectEpoch !== guard.projectEpoch || state.sessionEpoch !== guard.sessionEpoch || state.activeSessionId !== sessionId) {
    return;
  }

  let targetRunId = state.historyItems.length > 0
    ? (state.historyItems[state.historyItems.length - 1].run_id || "")
    : "";
  if (targetRunId) {
    state.activeRunId = targetRunId;
    sessionStorage.setItem(RUN_KEY, targetRunId);
    renderRunHistorySelect();
    await syncSnapshotAndStream(sessionId, targetRunId, { readController, skipTerminalHistoryRefresh: true });
  } else {
    state.activeRunId = "";
    sessionStorage.removeItem(RUN_KEY);
    renderRun(null);
  }
}

async function selectRunView(runId) {
  if (!state.activeSessionId || !runId || runId === state.activeRunId) return;
  setError("", "");
  abortActiveStream();
  abortPendingReads();
  state.runEpoch += 1;
  state.activeRunId = runId;
  sessionStorage.setItem(RUN_KEY, runId);
  state.eventsBySeq.clear();
  state.eventsTruncatedBefore = 0;
  state.proposalsById.clear();
  state.commandsById.clear();
  state.questionsById.clear();
  state.usageInfo = null;
  state.runExtensionServers = [];
  state.runSelectedSkills = [];
  state.extensionProposalsById.clear();
  state.extensionCallsById.clear();
  state.lastSeq = 0;
  state.selectedDiffPath = "";
  state.reconnectAttempts = 0;
  renderProposals();
  renderCommands();
  renderQuestions();
  renderEvents();
  await syncSnapshotAndStream(state.activeSessionId, runId);
}

function scheduleStreamReconnect(guard) {
  if (!isEpochCurrent(guard)) return;
  if (!state.currentRunView || TERMINAL_RUN_STATES.has(state.currentRunView.state)) {
    setConnectionState("idle");
    return;
  }
  if (state.reconnectAttempts >= RECONNECT_BACKOFF_MS.length) {
    setConnectionState("disconnected");
    return;
  }
  const delayMs = RECONNECT_BACKOFF_MS[state.reconnectAttempts];
  state.reconnectAttempts += 1;
  setConnectionState("reconnecting");
  clearReconnectTimer();
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    if (!isEpochCurrent(guard)) return;
    void openEventStream(guard.sessionId, guard.runId, guard);
  }, delayMs);
}

async function openEventStream(sessionId, runId, guard) {
  if (!isEpochCurrent(guard)) return;
  abortActiveStream();
  const controller = new AbortController();
  state.sseAbort = controller;

  let resp;
  try {
    resp = await apiFetch(
      `/api/v1/sessions/${sessionId}/runs/${runId}/events?after=${state.lastSeq}`,
      { signal: controller.signal }
    );
  } catch (err) {
    if (err && err.name === "AbortError") return;
    if (!isEpochCurrent(guard)) return;
    scheduleStreamReconnect(guard);
    return;
  }
  if (!isEpochCurrent(guard)) return;

  if (resp.status === 409) {
    await syncSnapshotAndStream(sessionId, runId);
    return;
  }
  if (!resp.ok || !resp.body) {
    scheduleStreamReconnect(guard);
    return;
  }

  state.reconnectAttempts = 0;
  setConnectionState("connected");

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let eventName = "";
  let dataLines = [];
  let sawRunFinished = false;
  let streamFailed = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (!isEpochCurrent(guard)) return;
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split(/\r?\n/);
      buf = lines.pop() || "";
      for (const line of lines) {
        if (line === "") {
          if (dataLines.length > 0) {
            const payloadText = dataLines.join("\n");
            if (eventName === "resync_required") {
              await syncSnapshotAndStream(sessionId, runId);
              return;
            }
            try {
              const parsed = JSON.parse(payloadText);
              const accepted = ingestEvent(parsed);
              if (accepted && parsed && parsed.kind === "run_finished") {
                sawRunFinished = true;
              }
            } catch {
              // ignore non-json comments
            }
          }
          eventName = "";
          dataLines = [];
          continue;
        }
        if (line.startsWith(":")) {
          continue;
        }
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
      }
    }
  } catch (err) {
    if (err && err.name === "AbortError") return;
    streamFailed = true;
  }

  if (!isEpochCurrent(guard)) return;

  if (streamFailed) {
    scheduleStreamReconnect(guard);
    return;
  }

  let runResp;
  try {
    runResp = await apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}`, { signal: guard.signal });
  } catch (err) {
    if (err && err.name === "AbortError") return;
    if (isEpochCurrent(guard) && !sawRunFinished) {
      scheduleStreamReconnect(guard);
    }
    return;
  }
  if (!isEpochCurrent(guard)) return;
  if (runResp.ok) {
    try {
      const finalRun = await runResp.json();
      if (!isEpochCurrent(guard)) return;
      renderRun(finalRun);
      await refreshRunProposalsQuestionsUsageAndCommands(sessionId, runId, guard);
      if (!isEpochCurrent(guard)) return;
      if (TERMINAL_RUN_STATES.has(finalRun.state)) {
        setConnectionState("idle");
        await refreshSessionContextAndHistory(sessionId, guard);
        if (!isEpochCurrent(guard)) return;
        await refreshProjectsAndSessions({ signal: guard.signal, guardEpoch: true });
        return;
      }
    } catch (err) {
      if (
        err && err.name === "AbortError"
        && Boolean(guard && guard.signal && guard.signal.aborted)
        && !isEpochCurrent(guard)
      ) {
        return;
      }
      throw err;
    }
  }
  if (!sawRunFinished && isEpochCurrent(guard)) {
    scheduleStreamReconnect(guard);
  }
}

async function syncSnapshotAndStream(sessionId, runId, options = {}) {
  abortActiveStream();
  let readController = options.readController;
  if (!readController) {
    abortPendingReads();
    readController = new AbortController();
    state.readAbort = readController;
  }

  const guard = {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
    runEpoch: state.runEpoch,
    projectId: state.selectedProjectId,
    sessionId,
    runId,
    signal: readController.signal,
  };
  setConnectionState("loading");

  const snapPromise = apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}/snapshot`, { signal: readController.signal });
  const propPromise = apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}/proposals`, { signal: readController.signal }).catch(() => null);
  const qPromise = apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}/questions`, { signal: readController.signal }).catch(() => null);
  const uPromise = apiFetch(`/api/v1/sessions/${sessionId}/runs/${runId}/usage`, { signal: readController.signal }).catch(() => null);

  let snapResp;
  try {
    snapResp = await snapPromise;
  } catch (err) {
    if (err && err.name === "AbortError") return;
    if (!isEpochCurrent(guard)) return;
    setConnectionState("error");
    setError("NETWORK_ERROR", "获取运行快照失败");
    return;
  }
  if (!isEpochCurrent(guard)) return;

  if (snapResp.status === 404) {
    setNotice("会话未恢复，旧文件仍保留。请在当前进程新建或选择项目后重新运行。");
    sessionStorage.removeItem(SESSION_KEY);
    sessionStorage.removeItem(RUN_KEY);
    state.activeSessionId = "";
    state.activeRunId = "";
    state.proposalsById.clear();
    state.commandsById.clear();
    state.questionsById.clear();
    state.usageInfo = null;
    setConnectionState("idle");
    renderIdentityHeader();
    renderProposals();
    renderCommands();
    renderQuestions();
    renderRun(null);
    return;
  }
  if (!snapResp.ok) {
    try {
      const err = await readErrorPayload(snapResp);
      if (!isEpochCurrent(guard)) return;
      setConnectionState("error");
      setError(err.code, err.message);
    } catch (err) {
      if (
        err && err.name === "AbortError"
        && readController.signal.aborted
        && (state.readAbort !== readController || !isEpochCurrent(guard))
      ) {
        return;
      }
      throw err;
    }
    return;
  }

  let snap;
  try {
    snap = await snapResp.json();
  } catch (err) {
    if (
      err && err.name === "AbortError"
      && readController.signal.aborted
      && (state.readAbort !== readController || !isEpochCurrent(guard))
    ) {
      return;
    }
    throw err;
  }
  if (!isEpochCurrent(guard)) return;
  if (snap.run.state !== "unknown") {
    setNotice("");
  }
  state.eventsBySeq.clear();
  state.eventsTruncatedBefore = Number(snap.truncated_before || snap.run.truncated_before || 0);
  state.proposalsById.clear();
  state.commandsById.clear();
  state.questionsById.clear();
  state.runExtensionServers = [];
  state.runSelectedSkills = [];
  state.extensionProposalsById.clear();
  state.extensionCallsById.clear();
  if (Array.isArray(snap.pending_proposals)) {
    for (const p of snap.pending_proposals) {
      const pid = p && (p.id || p.proposal_id);
      if (pid) state.proposalsById.set(pid, p);
    }
  }
  if (Array.isArray(snap.pending_questions)) {
    for (const q of snap.pending_questions) {
      if (q && q.id) state.questionsById.set(q.id, q);
    }
  }
  for (const ev of snap.events || []) {
    ingestEvent(ev);
  }
  renderEvents();
  renderRun(snap.run);
  const isStaleReadAbort = (err) => Boolean(
    err && err.name === "AbortError"
    && readController.signal.aborted
    && (state.readAbort !== readController || !isEpochCurrent(guard))
  );
  const attachBranchOutcome = (promise) => promise.then(
    () => ({ ok: true, error: null }),
    (err) => (isStaleReadAbort(err) ? { ok: true, error: null } : { ok: false, error: err })
  );
  const cmdOutcome = attachBranchOutcome(refreshCommandsForRun(sessionId, runId, guard));
  const extOutcome = attachBranchOutcome(refreshRunExtensions(sessionId, runId, guard));
  let sideErr = null;
  try {
    const [propResp, qResp, uResp] = await Promise.all([propPromise, qPromise, uPromise]);
    if (!(readController.signal.aborted && (state.readAbort !== readController || !isEpochCurrent(guard)))) {
      await applyRunSideResources(propResp, qResp, uResp, guard);
    }
  } catch (err) {
    if (!isStaleReadAbort(err)) {
      sideErr = err;
    }
  }
  const [cmdRes, extRes] = await Promise.all([cmdOutcome, extOutcome]);
  if (sideErr) throw sideErr;
  if (!cmdRes.ok) throw cmdRes.error;
  if (!extRes.ok) throw extRes.error;
  if (state.readAbort !== readController || !isEpochCurrent(guard)) return;
  state.lastSeq = snap.cursor || 0;

  if (TERMINAL_RUN_STATES.has(snap.run.state) || !isSelectedRunActive()) {
    setConnectionState("idle");
    if (TERMINAL_RUN_STATES.has(snap.run.state) && !options.skipTerminalHistoryRefresh) {
      await refreshSessionContextAndHistory(sessionId, guard);
    }
    return;
  }

  await openEventStream(sessionId, runId, guard);
}

async function handleAnswerQuestion(capturedSession, capturedRun, question, answerText) {
  if (!capturedSession || !capturedRun || !question || !question.id) return;
  const lockKey = `answer:${capturedSession}:${capturedRun}:${question.id}`;
  if (state.pendingLocks.has(lockKey)) return;
  state.pendingLocks.add(lockKey);
  renderQuestions();
  setError("", "");

  const guard = {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
    runEpoch: state.runEpoch,
    sessionId: capturedSession,
    runId: capturedRun,
  };

  try {
    const resp = await apiFetch(
      `/api/v1/sessions/${capturedSession}/runs/${capturedRun}/questions/${question.id}/answer`,
      {
        method: "POST",
        body: {
          client_request_id: generateUuidV4(),
          question_digest: question.digest,
          text: answerText,
        },
      }
    );
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (isEpochCurrent(guard)) {
        setError(err.code, err.message);
      }
      return;
    }
    const updated = await resp.json();
    state.questionDrafts.delete(`${capturedSession}:${capturedRun}:${question.id}`);
    if (!isEpochCurrent(guard)) return;
    if (updated && updated.id) {
      state.questionsById.set(updated.id, updated);
    }
    renderQuestions();
    await refreshSessionContextAndHistory(capturedSession, {
      projectEpoch: guard.projectEpoch,
      sessionEpoch: guard.sessionEpoch,
      sessionId: capturedSession,
    });
  } catch {
    if (isEpochCurrent(guard)) {
      setError("NETWORK_ERROR", "提交回答时网络异常（未自动重发）");
    }
  } finally {
    state.pendingLocks.delete(lockKey);
    if (isEpochCurrent(guard)) {
      renderQuestions();
    }
  }
}

async function handleSaveConstraints() {
  setError("", "");
  const capturedProject = state.selectedProjectId;
  let capturedSession = state.activeSessionId;
  const capturedText = el("constraints-input").value;
  const earlyLockKey = `constraints:${capturedSession || "pending"}`;
  if (state.pendingLocks.has(earlyLockKey)) return;
  state.pendingLocks.add(earlyLockKey);
  syncLockButtons();

  const startEpoch = {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
  };
  let activeGuard = {
    projectEpoch: startEpoch.projectEpoch,
    sessionEpoch: startEpoch.sessionEpoch,
    projectId: capturedProject,
    sessionId: capturedSession,
  };
  const isCurrentGuard = (g) => isEpochCurrent(g);

  let sidLockKey = "";
  try {
    if (!capturedSession) {
      capturedSession = await ensureSessionForSelection(
        capturedProject,
        null,
        startEpoch.projectEpoch,
        startEpoch.sessionEpoch
      );
      if (!capturedSession || state.projectEpoch !== startEpoch.projectEpoch || state.selectedProjectId !== capturedProject || state.activeSessionId !== capturedSession) {
        return;
      }
      activeGuard = {
        projectEpoch: startEpoch.projectEpoch,
        sessionEpoch: state.sessionEpoch,
        projectId: capturedProject,
        sessionId: capturedSession,
      };
    } else if (!isCurrentGuard(activeGuard)) {
      return;
    }

    sidLockKey = `constraints:${capturedSession}`;
    if (sidLockKey !== earlyLockKey) {
      if (state.pendingLocks.has(sidLockKey)) return;
      state.pendingLocks.add(sidLockKey);
      syncLockButtons();
    }

    const expectedRev = state.contextInfo && state.contextInfo.session_id === capturedSession
      ? Number(state.contextInfo.revision || 0)
      : 0;
    const putGuard = { ...activeGuard };
    const resp = await apiFetch(`/api/v1/sessions/${capturedSession}/context`, {
      method: "PUT",
      body: {
        client_request_id: generateUuidV4(),
        expected_revision: expectedRev,
        constraints: capturedText,
      },
    });
    if (!isCurrentGuard(putGuard)) return;
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (isCurrentGuard(putGuard)) {
        setError(err.code, err.message);
        setNotice(`${err.code}: ${err.message}`);
      }
      return;
    }
    const updatedCtx = await resp.json();
    if (!isCurrentGuard(putGuard)) return;
    const draft = getOrCreateSessionDraft(capturedSession);
    if (draft) {
      draft.constraints = updatedCtx.constraints || "";
      draft.constraintsDirty = false;
    }
    state.contextInfo = updatedCtx;
    renderContextAndHistory();
    await refreshProjectsAndSessions({
      guardEpoch: true,
      expectedProjectEpoch: putGuard.projectEpoch,
      expectedProjectId: putGuard.projectId,
      guardSession: true,
      expectedSessionEpoch: putGuard.sessionEpoch,
    });
  } catch {
    if (isCurrentGuard(activeGuard)) {
      setError("NETWORK_ERROR", "保存固定约束时网络异常（草稿已保留）");
    }
  } finally {
    state.pendingLocks.delete(earlyLockKey);
    if (sidLockKey) state.pendingLocks.delete(sidLockKey);
    syncLockButtons();
  }
}

async function handleNewSession() {
  setError("", "");
  setNotice("");
  const model = parseModelSelection();
  let projId = state.selectedProjectId;
  if (!projId) {
    if (state.projects.length > 0) {
      projId = state.projects[0].project_id;
      selectProject(state.projects[0], { userSwitch: false });
    } else {
      setError("NO_PROJECT", "请先创建或登记项目");
      return;
    }
  }
  const lockKey = `new-session:${projId}`;
  if (state.pendingLocks.has(lockKey)) return;
  state.pendingLocks.add(lockKey);
  syncLockButtons();
  const guard = {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
    projectId: projId,
    sessionId: state.activeSessionId,
  };
  const isSameSelection = () => isEpochCurrent(guard);

  try {
    const resp = await apiFetch("/api/v1/sessions", {
      method: "POST",
      body: {
        project_id: projId,
        model,
      },
    });
    if (!isSameSelection()) return;
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (isSameSelection()) {
        setError(err.code, err.message);
      }
      return;
    }
    const sess = await resp.json();
    if (!isSameSelection()) return;
    await refreshProjectsAndSessions({
      guardEpoch: true,
      expectedProjectEpoch: guard.projectEpoch,
      expectedProjectId: guard.projectId,
      guardSession: true,
      expectedSessionEpoch: guard.sessionEpoch,
    });
    if (!isSameSelection()) return;
    await switchSession(sess.session_id);
  } catch {
    if (isSameSelection()) {
      setError("NETWORK_ERROR", "新建会话时网络异常");
    }
  } finally {
    state.pendingLocks.delete(lockKey);
    syncLockButtons();
  }
}

async function handleExtensionDecision(capturedSession, capturedRun, prop, decision) {
  if (!capturedSession || !capturedRun || !prop || !prop.id) return;
  const pid = prop.id;
  const lockKey = `extprop:${capturedSession}:${capturedRun}:${pid}`;
  if (state.pendingLocks.has(lockKey)) return;
  state.pendingLocks.add(lockKey);
  renderExtensionProposals();
  const guard = {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
    runEpoch: state.runEpoch,
    sessionId: capturedSession,
    runId: capturedRun,
  };
  try {
    const resp = await apiFetch(
      `/api/v1/sessions/${capturedSession}/runs/${capturedRun}/extensions/proposals/${pid}/decision`,
      {
        method: "POST",
        body: { digest: prop.digest, decision },
      }
    );
    if (!isEpochCurrent(guard)) return;
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (isEpochCurrent(guard)) {
        setError(err.code, err.message);
      }
      return;
    }
    const updated = await resp.json();
    if (!isEpochCurrent(guard)) return;
    state.extensionProposalsById.set(pid, updated);
    renderExtensionProposals();
  } finally {
    state.pendingLocks.delete(lockKey);
    if (isEpochCurrent(guard)) {
      renderExtensionProposals();
    }
  }
}

async function handleProposalDecision(capturedSession, capturedRun, prop, decision, trustedLocalExecution) {
  const pid = prop.id || prop.proposal_id;
  if (!capturedSession || !capturedRun || !pid) return;
  const lockKey = `proposal:${capturedSession}:${capturedRun}:${pid}`;
  if (state.pendingLocks.has(lockKey)) return;
  state.pendingLocks.add(lockKey);
  renderProposals();
  setError("", "");

  const guard = {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
    runEpoch: state.runEpoch,
    sessionId: capturedSession,
    runId: capturedRun,
  };

  try {
    const resp = await apiFetch(
      `/api/v1/sessions/${capturedSession}/runs/${capturedRun}/proposals/${pid}/decision`,
      {
        method: "POST",
        body: {
          digest: prop.digest,
          decision,
          trusted_local_execution: Boolean(trustedLocalExecution),
        },
      }
    );
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (isEpochCurrent(guard)) {
        setError(err.code, err.message);
      }
      return;
    }
    const updated = await resp.json();
    if (!isEpochCurrent(guard)) return;
    const updatedId = updated.id || updated.proposal_id;
    if (updatedId) {
      state.proposalsById.set(updatedId, updated);
      recordRunProposalItem(capturedSession, capturedRun, updated);
    }
    renderProposals();
    renderChatTimeline();
  } catch {
    if (isEpochCurrent(guard)) {
      setError("NETWORK_ERROR", "提交审批决策时网络异常（未自动重试）");
    }
  } finally {
    state.pendingLocks.delete(lockKey);
    if (isEpochCurrent(guard)) {
      renderProposals();
    }
  }
}

async function handleStopCommand(capturedSession, capturedRun, commandId) {
  if (!capturedSession || !capturedRun || !commandId) return;
  const lockKey = `stop-cmd:${capturedSession}:${capturedRun}:${commandId}`;
  if (state.pendingLocks.has(lockKey)) return;
  state.pendingLocks.add(lockKey);
  renderCommands();
  setError("", "");

  const guard = {
    projectEpoch: state.projectEpoch,
    sessionEpoch: state.sessionEpoch,
    runEpoch: state.runEpoch,
    sessionId: capturedSession,
    runId: capturedRun,
  };

  try {
    const resp = await apiFetch(
      `/api/v1/sessions/${capturedSession}/runs/${capturedRun}/commands/${commandId}/stop`,
      { method: "POST", body: {} }
    );
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (isEpochCurrent(guard)) {
        setError(err.code, err.message);
      }
      return;
    }
    const updated = await resp.json();
    if (!isEpochCurrent(guard)) return;
    if (resp.status === 202 && !TERMINAL_RUN_STATES.has(updated.state) && updated.state !== "exited" && updated.state !== "timed_out") {
      state.stoppingCommands.add(commandId);
    } else {
      state.stoppingCommands.delete(commandId);
    }
    state.commandsById.set(updated.command_id, updated);
    renderCommands();
  } catch {
    if (isEpochCurrent(guard)) {
      setError("NETWORK_ERROR", "停止命令时网络异常");
    }
  } finally {
    state.pendingLocks.delete(lockKey);
    if (isEpochCurrent(guard)) {
      renderCommands();
    }
  }
}

async function handleRegisterProject() {
  if (state.pendingLocks.has("register-project")) return;
  state.pendingLocks.add("register-project");
  syncLockButtons();
  setError("", "");

  const name = el("project-name-input").value.trim();
  const root = el("register-root-input").value.trim();
  const confirmedRead = el("confirm-read-input").checked;
  const startProjectEpoch = state.projectEpoch;

  try {
    const resp = await apiFetch("/api/v1/projects/register", {
      method: "POST",
      body: {
        name,
        root,
        confirmed_read: confirmedRead,
      },
    });
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (state.projectEpoch === startProjectEpoch) {
        setError(err.code, err.message);
      }
      return;
    }
    const proj = await resp.json();
    await refreshProjectsAndSessions();
    if (state.projectEpoch === startProjectEpoch) {
      selectProject(proj, { userSwitch: true });
      setNotice("");
    }
  } catch {
    if (state.projectEpoch === startProjectEpoch) {
      setError("NETWORK_ERROR", "登记项目时网络异常");
    }
  } finally {
    state.pendingLocks.delete("register-project");
    syncLockButtons();
  }
}

async function handleCreateProject() {
  if (state.pendingLocks.has("create-project")) return;
  state.pendingLocks.add("create-project");
  syncLockButtons();
  setError("", "");

  const name = el("project-name-input").value.trim();
  const startProjectEpoch = state.projectEpoch;

  try {
    const resp = await apiFetch("/api/v1/projects", {
      method: "POST",
      body: { name },
    });
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (state.projectEpoch === startProjectEpoch) {
        setError(err.code, err.message);
      }
      return;
    }
    const proj = await resp.json();
    await refreshProjectsAndSessions();
    if (state.projectEpoch === startProjectEpoch) {
      selectProject(proj, { userSwitch: true });
      setNotice("");
    }
  } catch {
    if (state.projectEpoch === startProjectEpoch) {
      setError("NETWORK_ERROR", "创建项目时网络异常");
    }
  } finally {
    state.pendingLocks.delete("create-project");
    syncLockButtons();
  }
}

async function ensureSessionForSelection(
  forcedProjectId = null,
  forcedModel = null,
  expectedProjectEpoch = null,
  expectedSessionEpoch = null
) {
  const model = forcedModel || parseModelSelection();
  let projectId = forcedProjectId || state.selectedProjectId;
  if (!projectId) {
    if (state.projects.length > 0) {
      projectId = state.projects[0].project_id;
      selectProject(state.projects[0], { userSwitch: false });
    } else {
      setError("NO_PROJECT", "请先创建一个受控示例项目");
      return null;
    }
  }
  const existing = state.sessions.find(
    (s) =>
      s.session_id === state.activeSessionId &&
      s.project_id === projectId &&
      s.model.provider === model.provider &&
      s.model.model === model.model &&
      !s.active_run_id
  );
  if (existing) {
    return existing.session_id;
  }
  const checkProjEpoch = expectedProjectEpoch !== null ? expectedProjectEpoch : state.projectEpoch;
  const checkSessEpoch = expectedSessionEpoch !== null ? expectedSessionEpoch : state.sessionEpoch;
  const checkSessionId = state.activeSessionId;
  const isSameOrigin = () =>
    state.projectEpoch === checkProjEpoch &&
    state.sessionEpoch === checkSessEpoch &&
    state.selectedProjectId === projectId &&
    state.activeSessionId === checkSessionId;

  const resp = await apiFetch("/api/v1/sessions", {
    method: "POST",
    body: {
      project_id: projectId,
      model,
    },
  });
  if (!isSameOrigin()) {
    return null;
  }
  if (!resp.ok) {
    const err = await readErrorPayload(resp);
    if (isSameOrigin()) {
      setError(err.code, err.message);
    }
    return null;
  }
  const sess = await resp.json();
  if (!isSameOrigin()) {
    return null;
  }
  state.sessionEpoch += 1;
  const createdSessionEpoch = state.sessionEpoch;
  state.activeSessionId = sess.session_id;
  sessionStorage.setItem(SESSION_KEY, sess.session_id);
  renderIdentityHeader();
  await refreshProjectsAndSessions({
    guardEpoch: true,
    expectedProjectEpoch: checkProjEpoch,
    expectedProjectId: projectId,
    guardSession: true,
    expectedSessionEpoch: createdSessionEpoch,
  });
  const isCreatedSelectionCurrent = () =>
    state.projectEpoch === checkProjEpoch &&
    state.sessionEpoch === createdSessionEpoch &&
    state.selectedProjectId === projectId &&
    state.activeSessionId === sess.session_id;

  if (!isCreatedSelectionCurrent()) {
    return null;
  }
  await refreshSessionContextAndHistory(sess.session_id, {
    projectEpoch: checkProjEpoch,
    sessionEpoch: createdSessionEpoch,
    projectId,
    sessionId: sess.session_id,
  });
  if (!isCreatedSelectionCurrent()) {
    return null;
  }
  return sess.session_id;
}

async function handleStartRun() {
  const capturedProjectId = state.selectedProjectId;
  const capturedSessionId = state.activeSessionId;
  const lockKey = `start:${capturedProjectId || "none"}:${capturedSessionId || "new"}`;
  if (state.pendingLocks.has(lockKey) || state.pendingLocks.has("start-global")) {
    return;
  }
  // Set synchronous single-flight locks BEFORE any await (covers ensureSessionForSelection).
  state.pendingLocks.add(lockKey);
  state.pendingLocks.add("start-global");
  syncLockButtons();
  setError("", "");

  const capturedModel = parseModelSelection();
  const chatInputEl = el("chat-input");
  const goalInputEl = el("goal-input");
  const capturedGoal = chatInputEl ? chatInputEl.value : (goalInputEl ? goalInputEl.value : "");
  const confirmPanel = el("execute-confirm-panel");
  if (confirmPanel && !confirmPanel.hidden && state.pendingExecuteGoal !== null && capturedGoal !== state.pendingExecuteGoal) {
    closeExecuteConfirmPanel({ focusInput: false });
    setError("GOAL_CHANGED", "任务内容已修改，请重新点击“执行任务”确认目标。");
    return;
  }
  const capturedConfirmed = el("confirm-scope-input").checked;
  const capturedSkillIds = Array.from(state.selectedSkillIds);
  const startProjectEpoch = state.projectEpoch;
  const startSessionEpoch = state.sessionEpoch;
  let activeGuard = {
    projectEpoch: startProjectEpoch,
    sessionEpoch: startSessionEpoch,
    projectId: capturedProjectId,
    sessionId: capturedSessionId,
  };

  try {
    let targetSessionId = capturedSessionId;
    let createdNewSession = false;
    if (targetSessionId) {
      const existingSess = state.sessions.find((s) => s.session_id === targetSessionId);
      if (existingSess && existingSess.project_id !== capturedProjectId) {
        setError("SESSION_PROJECT_MISMATCH", "当前会话与所选项目不匹配，请先切换项目或新建会话。");
        return;
      }
      if (
        existingSess &&
        (existingSess.model.provider !== capturedModel.provider || existingSess.model.model !== capturedModel.model)
      ) {
        setError("MODEL_MISMATCH", "当前会话模型与所选模型不一致，请先解决模型匹配或新建对应会话。");
        return;
      }
    } else {
      targetSessionId = await ensureSessionForSelection(
        capturedProjectId,
        capturedModel,
        startProjectEpoch,
        startSessionEpoch
      );
      createdNewSession = true;
    }
    if (!targetSessionId) return;

    if (state.projectEpoch !== startProjectEpoch || state.selectedProjectId !== capturedProjectId || state.activeSessionId !== targetSessionId) {
      return;
    }
    if (!createdNewSession && state.sessionEpoch !== startSessionEpoch) {
      return;
    }
    const postSessionEpoch = state.sessionEpoch;
    activeGuard = {
      projectEpoch: startProjectEpoch,
      sessionEpoch: postSessionEpoch,
      projectId: capturedProjectId,
      sessionId: targetSessionId,
    };

    const resp = await apiFetch(`/api/v1/sessions/${targetSessionId}/runs`, {
      method: "POST",
      body: {
        client_request_id: generateUuidV4(),
        goal: capturedGoal,
        confirmed_scope: capturedConfirmed,
        skill_ids: capturedSkillIds,
      },
    });
    if (!isEpochCurrent(activeGuard)) return;

    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (isEpochCurrent(activeGuard)) {
        setError(err.code, err.message);
      }
      return;
    }
    const run = await resp.json();
    if (!isEpochCurrent(activeGuard)) return;

    state.activeSessionId = targetSessionId;
    sessionStorage.setItem(SESSION_KEY, targetSessionId);
    recordTimelineEntry(capturedProjectId, targetSessionId, "run", run.run_id);
    closeExecuteConfirmPanel({ focusInput: false });
    const confirmInput = el("confirm-scope-input");
    if (confirmInput) confirmInput.checked = false;
    const chatInputClear = el("chat-input");
    if (chatInputClear && chatInputClear.value === capturedGoal) {
      chatInputClear.value = "";
    }
    if (state.chatDrafts.get(targetSessionId) === capturedGoal) {
      state.chatDrafts.delete(targetSessionId);
    }
    const d = getOrCreateSessionDraft(targetSessionId);
    if (d) {
      d.goal = capturedGoal;
      d.goalDirty = false;
    }
    state.runEpoch += 1;
    state.activeRunId = run.run_id;
    sessionStorage.setItem(RUN_KEY, run.run_id);
    state.runExtensionServers = [];
    state.runSelectedSkills = [];
    state.extensionProposalsById.clear();
    state.extensionCallsById.clear();
    renderIdentityHeader();
    renderDraftStatus();
    renderRun(run);
    const runGuard = {
      projectEpoch: startProjectEpoch,
      sessionEpoch: postSessionEpoch,
      runEpoch: state.runEpoch,
      projectId: capturedProjectId,
      sessionId: targetSessionId,
      runId: run.run_id,
    };
    void (async () => {
      await refreshSessionContextAndHistory(targetSessionId, {
        projectEpoch: runGuard.projectEpoch,
        sessionEpoch: runGuard.sessionEpoch,
        projectId: runGuard.projectId,
        sessionId: runGuard.sessionId,
      });
      if (isEpochCurrent(runGuard)) {
        await syncSnapshotAndStream(targetSessionId, run.run_id, { skipTerminalHistoryRefresh: true });
      }
    })();
  } catch {
    if (isEpochCurrent(activeGuard)) {
      setError("NETWORK_ERROR", "启动运行时网络异常（未自动重发 POST）");
    }
  } finally {
    state.pendingLocks.delete(lockKey);
    state.pendingLocks.delete("start-global");
    syncLockButtons();
  }
}

async function handleCancelRun(overrideSessionId = null, overrideRunId = null) {
  setError("", "");
  const capturedProject = state.selectedProjectId;
  const capturedSession = overrideSessionId || state.activeSessionId;
  const capturedRun = overrideRunId || state.activeRunId;
  if (!capturedSession || !capturedRun) {
    setError("NO_ACTIVE_RUN", "当前没有可停止的运行");
    return;
  }
  const lockKey = `cancel:${capturedSession}:${capturedRun}`;
  if (state.pendingLocks.has(lockKey)) return;
  state.pendingLocks.add(lockKey);
  syncLockButtons();

  const capturedProjectEpoch = state.projectEpoch;
  const capturedSessionEpoch = state.sessionEpoch;
  const capturedRunEpoch = state.runEpoch;
  const isSessionCurrent = () => (
    state.projectEpoch === capturedProjectEpoch
    && state.sessionEpoch === capturedSessionEpoch
    && state.selectedProjectId === capturedProject
    && state.activeSessionId === capturedSession
  );
  const isTargetSelected = () => (
    isSessionCurrent()
    && state.runEpoch === capturedRunEpoch
    && state.activeRunId === capturedRun
  );

  try {
    const resp = await apiFetch(
      `/api/v1/sessions/${capturedSession}/runs/${capturedRun}/cancel`,
      { method: "POST", body: {} }
    );
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (isSessionCurrent()) {
        setError(err.code, err.message);
      }
      return;
    }
    let run = await resp.json();
    if (!isSessionCurrent()) return;
    if (isTargetSelected()) {
      renderRun(run);
      await syncSnapshotAndStream(capturedSession, capturedRun);
      return;
    }
    state.sessionRunsById.set(capturedRun, run);
    renderChatTimeline();
    if (!TERMINAL_RUN_STATES.has(run.state)) {
      const readbackAbort = new AbortController();
      let timedOut = false;
      const deadlineTimer = setTimeout(() => {
        timedOut = true;
        readbackAbort.abort();
      }, 10000);
      try {
        while (!TERMINAL_RUN_STATES.has(run.state) && !timedOut && !readbackAbort.signal.aborted) {
          await new Promise((resolve) => {
            if (readbackAbort.signal.aborted) {
              resolve();
              return;
            }
            let stepTimer = null;
            const onAbort = () => {
              if (stepTimer !== null) clearTimeout(stepTimer);
              readbackAbort.signal.removeEventListener("abort", onAbort);
              resolve();
            };
            readbackAbort.signal.addEventListener("abort", onAbort, { once: true });
            stepTimer = setTimeout(() => {
              readbackAbort.signal.removeEventListener("abort", onAbort);
              resolve();
            }, 150);
          });
          if (timedOut || readbackAbort.signal.aborted || !isSessionCurrent()) break;
          const pollResp = await apiFetch(
            `/api/v1/sessions/${capturedSession}/runs/${capturedRun}`,
            { signal: readbackAbort.signal }
          );
          if (timedOut || readbackAbort.signal.aborted || !isSessionCurrent()) break;
          if (!pollResp.ok) break;
          const nextRun = await pollResp.json();
          if (timedOut || readbackAbort.signal.aborted || !isSessionCurrent()) break;
          run = nextRun;
          state.sessionRunsById.set(capturedRun, run);
          renderChatTimeline();
        }
      } catch (readErr) {
        if (!(readErr && readErr.name === "AbortError" && (timedOut || readbackAbort.signal.aborted))) {
          throw readErr;
        }
      } finally {
        clearTimeout(deadlineTimer);
      }
    }
  } catch {
    if (isSessionCurrent()) {
      setError("NETWORK_ERROR", "停止运行时网络异常");
    }
  } finally {
    state.pendingLocks.delete(lockKey);
    syncLockButtons();
  }
}

const PREFS_KEY = "fc-workbench-prefs";
const PAGE_NAMES = {
  projects: "项目",
  chat: "会话",
  inbox: "审批收件箱",
  history: "运行历史",
  extensions: "扩展",
  settings: "设置",
};

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (parsed && (parsed.theme === "light" || parsed.theme === "dark")) {
      state.theme = parsed.theme;
    }
    if (parsed && (parsed.density === "normal" || parsed.density === "compact")) {
      state.density = parsed.density;
    }
  } catch {
    // ignore invalid local prefs
  }
}

function savePrefs() {
  try {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ theme: state.theme, density: state.density })
    );
  } catch {
    // ignore storage errors
  }
}

function applyAppearance() {
  document.documentElement.dataset.theme = state.theme;
  document.documentElement.dataset.density = state.density;
  const themeSel = el("theme-select");
  const densitySel = el("density-select");
  if (themeSel) themeSel.value = state.theme;
  if (densitySel) densitySel.value = state.density;
}

function closeMobileNav(restoreFocus = false) {
  document.body.classList.remove("menu-open");
  const btn = el("navigation-toggle");
  if (btn) {
    btn.setAttribute("aria-expanded", "false");
    if (restoreFocus) btn.focus();
  }
}

function toggleNavigation() {
  const nav = el("workspace-nav");
  const btn = el("navigation-toggle");
  if (!nav || !btn) return;
  const isOpen = document.body.classList.toggle("menu-open");
  state.navCollapsed = !isOpen;
  btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
}

function goToPage(pageName) {
  if (!Object.prototype.hasOwnProperty.call(PAGE_NAMES, pageName)) return;
  state.activePage = pageName;
  document.body.classList.toggle("page-settings-active", pageName === "settings");
  const isChat = pageName === "chat";
  const crumb = el("page-crumb");
  if (crumb) {
    crumb.textContent = PAGE_NAMES[pageName];
    crumb.hidden = isChat;
  }
  const heading = el("run-heading");
  if (heading) heading.hidden = !isChat;
  const cPill = el("chat-constraints-pill");
  if (cPill) cPill.hidden = !isChat;
  const inspBtn = el("toggle-inspector-btn");
  if (inspBtn) inspBtn.hidden = !isChat;
  for (const sec of document.querySelectorAll("[data-page]")) {
    sec.hidden = sec.getAttribute("data-page") !== pageName;
  }
  for (const btn of document.querySelectorAll("[data-nav-page]")) {
    btn.classList.toggle("active", btn.getAttribute("data-nav-page") === pageName);
  }
  closeMobileNav(false);
  if (pageName === "inbox") {
    void refreshInbox();
  } else if (pageName === "extensions") {
    void refreshProjectSkillsAndServers(state.selectedProjectId, {
      projectEpoch: state.projectEpoch,
      projectId: state.selectedProjectId,
    });
  } else if (pageName === "history") {
    renderContextAndHistory();
  }
}

const SETTINGS_CATALOG = [
  {
    section: "appearance",
    title: "外观与编辑",
    summary: "界面主题（冰白 / 石墨黑）、信息密度（标准 / 紧凑）与证据抽屉 Diff 审阅说明",
    keywords: "appearance 外观 编辑 主题 冰白 石墨黑 亮色 暗色 密度 标准 紧凑 diff 差异 抽屉 light dark normal compact",
    focusId: "theme-select",
  },
  {
    section: "desktop",
    title: "桌面与托盘",
    summary: "只读说明：关窗收起至系统托盘保持后台任务，通过系统托盘菜单恢复或退出",
    keywords: "desktop 桌面 托盘 系统托盘 关窗 后台 退出 单实例 tray close minimize exit electron",
    focusId: "settings-panel-desktop",
  },
  {
    section: "model",
    title: "模型与连接",
    summary: "宿主配置来源状态、离线替身与受控在线模式只读说明",
    keywords: "model 模型 连接 宿主 离线 在线 状态 来源 health offline deepseek provider",
    focusId: "settings-panel-model",
  },
  {
    section: "permissions",
    title: "审批与命令",
    summary: "逐提案独立审批与 trusted_local_execution 受信任本机执行边界（非 OS 沙箱）",
    keywords: "permissions 审批 命令 执行权限 范围 验证 提案 trusted_local_execution 非 OS 沙箱 scope command",
    focusId: "settings-panel-permissions",
  },
  {
    section: "constraints",
    title: "会话约束",
    summary: "当前会话固定约束持久化与 expected_revision 并发修订冲突保护",
    keywords: "constraints 会话约束 固定约束 约束 修订 冲突 草稿 revision session utf-8 8192",
    focusId: "constraints-input",
  },
  {
    section: "storage",
    title: "存储与保留",
    summary: "当前会话 SQLite (sessions.sqlite3) 真实字节用量与数据保留只读策略",
    keywords: "storage 存储 保留 数据 用量 字节 sqlite sessions.sqlite3 只读 retention bytes",
    focusId: "settings-panel-storage",
  },
];

let settingsSearchMatches = [];
let settingsSearchIndex = -1;
let settingsSearchOpener = null;

function chooseSettingsSection(secName) {
  state.settingsSection = secName;
  for (const btn of document.querySelectorAll("[data-settings-section]")) {
    btn.classList.toggle("active", btn.getAttribute("data-settings-section") === secName);
  }
  for (const panel of document.querySelectorAll("[data-settings-panel]")) {
    panel.hidden = panel.getAttribute("data-settings-panel") !== secName;
  }
}

const selectSettingsSection = chooseSettingsSection;

function renderSettingsSearchResults() {
  const input = el("settings-search-input");
  const listEl = el("settings-search-results");
  const emptyEl = el("settings-search-empty");
  if (!input || !listEl || !emptyEl) return;
  const rawQuery = (input.value || "").trim();
  const q = rawQuery.toLowerCase();
  listEl.replaceChildren();
  if (!q) {
    settingsSearchMatches = [];
    settingsSearchIndex = -1;
    listEl.hidden = true;
    emptyEl.hidden = true;
    return;
  }
  settingsSearchMatches = SETTINGS_CATALOG.filter((item) => {
    const hay = `${item.title} ${item.summary} ${item.keywords}`.toLowerCase();
    return hay.includes(q);
  });
  if (settingsSearchMatches.length === 0) {
    settingsSearchIndex = -1;
    listEl.hidden = true;
    emptyEl.textContent = `未找到与“${rawQuery}”匹配的设置项`;
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  if (settingsSearchIndex < 0 || settingsSearchIndex >= settingsSearchMatches.length) {
    settingsSearchIndex = 0;
  }
  settingsSearchMatches.forEach((item, idx) => {
    const li = document.createElement("li");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", idx === settingsSearchIndex ? "true" : "false");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = idx === settingsSearchIndex ? "settings-search-result-btn active" : "settings-search-result-btn";
    btn.setAttribute("data-testid", `settings-search-item-${item.section}`);
    btn.textContent = `${item.title} — ${item.summary}`;
    btn.addEventListener("click", () => {
      activateSettingsSearchResult(item);
    });
    li.appendChild(btn);
    listEl.appendChild(li);
  });
  listEl.hidden = false;
}

function activateSettingsSearchResult(item) {
  if (!item) return;
  chooseSettingsSection(item.section);
  const input = el("settings-search-input");
  const listEl = el("settings-search-results");
  const emptyEl = el("settings-search-empty");
  if (input) input.value = "";
  if (listEl) {
    listEl.replaceChildren();
    listEl.hidden = true;
  }
  if (emptyEl) emptyEl.hidden = true;
  settingsSearchMatches = [];
  settingsSearchIndex = -1;
  const target = el(item.focusId) || el(`settings-panel-${item.section}`) || el(`settings-sec-${item.section}`);
  if (target && typeof target.focus === "function") {
    target.focus();
  }
}

function closeSettingsSearch(restoreFocus = true) {
  const input = el("settings-search-input");
  const listEl = el("settings-search-results");
  const emptyEl = el("settings-search-empty");
  if (input) input.value = "";
  if (listEl) {
    listEl.replaceChildren();
    listEl.hidden = true;
  }
  if (emptyEl) emptyEl.hidden = true;
  settingsSearchMatches = [];
  settingsSearchIndex = -1;
  if (restoreFocus) {
    const fallback = el(`settings-sec-${state.settingsSection}`) || el("nav-settings-btn");
    const target =
      settingsSearchOpener &&
      settingsSearchOpener.isConnected &&
      settingsSearchOpener !== input
        ? settingsSearchOpener
        : fallback;
    settingsSearchOpener = null;
    if (target && typeof target.focus === "function") {
      target.focus();
    }
  }
}

function openSettingsSearch(openerEl = null) {
  const input = el("settings-search-input");
  if (!input) return;
  if (openerEl && openerEl !== input) {
    settingsSearchOpener = openerEl;
  } else if (document.activeElement && document.activeElement !== input && document.activeElement !== document.body) {
    settingsSearchOpener = document.activeElement;
  }
  if (state.activePage !== "settings") {
    goToPage("settings");
  }
  input.focus();
  input.select();
  renderSettingsSearchResults();
}

function syncInspectorBindingHeader() {
  const sessSpan = el("inspector-session-id");
  const badgeSpan = el("inspector-run-badge");
  const sid = state.activeSessionId || "-";
  const rid = state.activeRunId || (state.currentRunView && state.currentRunView.run_id) || "-";
  if (sessSpan) {
    sessSpan.textContent = sid;
  }
  if (badgeSpan) {
    const modeLabel = !state.activeRunId
      ? "未选择运行"
      : isSelectedRunInteractive()
        ? "当前活动"
        : "只读证据";
    badgeSpan.textContent = `${modeLabel} · session=${sid} · run=${rid}`;
  }
}

function chooseExtTab(tabName) {
  state.extTab = tabName;
  for (const btn of document.querySelectorAll("[data-ext-tab]")) {
    btn.classList.toggle("active", btn.getAttribute("data-ext-tab") === tabName);
  }
  const skillPanel = el("ext-panel-skill");
  const mcpPanel = el("ext-panel-mcp");
  if (skillPanel) skillPanel.hidden = tabName !== "skill";
  if (mcpPanel) mcpPanel.hidden = tabName !== "mcp";
}

function setInspectorExpanded(expanded, openerEl = null) {
  const prev = state.inspectorExpanded;
  state.inspectorExpanded = Boolean(expanded);
  const layout = el("chat-layout");
  const insp = el("chat-inspector");
  const btn = el("toggle-inspector-btn");
  if (layout) layout.classList.toggle("inspector-collapsed", !state.inspectorExpanded);
  if (insp) insp.hidden = !state.inspectorExpanded;
  if (btn) {
    btn.textContent = state.inspectorExpanded ? "收起详情" : "展开详情";
    btn.setAttribute("aria-expanded", String(state.inspectorExpanded));
  }
  syncInspectorBindingHeader();
  if (!prev && state.inspectorExpanded) {
    state.chatOpenerEl = openerEl || document.activeElement;
    const closeBtn = el("close-inspector-btn");
    if (closeBtn && typeof closeBtn.focus === "function") {
      closeBtn.focus();
    } else if (insp && typeof insp.focus === "function") {
      insp.focus();
    }
  } else if (prev && !state.inspectorExpanded) {
    const target = state.chatOpenerEl || btn;
    state.chatOpenerEl = null;
    if (target && target.isConnected && typeof target.focus === "function") {
      target.focus();
    }
  }
}

function toggleInspector() {
  setInspectorExpanded(!state.inspectorExpanded, el("toggle-inspector-btn"));
}

function chooseInspectorTab(tabName) {
  state.inspectorTab = tabName;
  for (const btn of document.querySelectorAll("[data-inspector-tab]")) {
    btn.classList.toggle("active", btn.getAttribute("data-inspector-tab") === tabName);
  }
  for (const panel of document.querySelectorAll("[data-inspector-panel]")) {
    panel.hidden = panel.getAttribute("data-inspector-panel") !== tabName;
  }
  if (tabName === "files" && state.selectedProjectId) {
    void refreshProjectFiles(state.reviewFilesPath || ".");
  } else if (tabName === "git" && state.selectedProjectId) {
    void refreshProjectGitStatus();
  }
}

function updateInboxBadge() {
  const badge = el("inbox-badge");
  if (!badge) return;
  let count = 0;
  if (state.inboxLoaded) {
    count = state.inboxItems.length;
  } else {
    for (const p of state.proposalsById.values()) {
      if (p && p.state === "pending") count += 1;
    }
    for (const ep of state.extensionProposalsById.values()) {
      if (ep && ep.state === "pending") count += 1;
    }
  }
  if (state.inboxPartialFailure) {
    badge.textContent = count > 0 ? `${count}+` : "?";
    badge.hidden = false;
  } else {
    badge.textContent = String(count);
    badge.hidden = count === 0;
  }
}

async function refreshInbox() {
  const myEpoch = ++state.inboxEpoch;
  const projId = state.selectedProjectId;
  state.inboxPartialFailure = false;
  if (!projId) {
    state.inboxItems = [];
    state.inboxLoaded = true;
    renderInbox();
    updateInboxBadge();
    return;
  }
  let sessList = state.sessions;
  try {
    const sResp = await apiFetch("/api/v1/sessions");
    if (myEpoch !== state.inboxEpoch || state.selectedProjectId !== projId) return;
    if (!sResp || !sResp.ok) {
      state.inboxPartialFailure = true;
    } else {
      const parsedSessions = await sResp.json();
      if (myEpoch !== state.inboxEpoch || state.selectedProjectId !== projId) return;
      if (Array.isArray(parsedSessions)) {
        sessList = parsedSessions;
        state.sessions = sessList;
        renderSessions();
      } else {
        state.inboxPartialFailure = true;
      }
    }
  } catch {
    if (myEpoch !== state.inboxEpoch || state.selectedProjectId !== projId) return;
    state.inboxPartialFailure = true;
  }
  const activeSessions = (Array.isArray(sessList) ? sessList : []).filter(
    (s) => s.project_id === projId && Boolean(s.active_run_id)
  );
  const seenKeys = new Set();
  const merged = [];
  for (const sess of activeSessions) {
    const rid = sess.active_run_id;
    const [pResp, eResp] = await Promise.all([
      apiFetch(`/api/v1/sessions/${sess.session_id}/runs/${rid}/proposals`).catch(() => null),
      apiFetch(`/api/v1/sessions/${sess.session_id}/runs/${rid}/extensions`).catch(() => null),
    ]);
    if (myEpoch !== state.inboxEpoch || state.selectedProjectId !== projId) return;
    if (!pResp || !pResp.ok) {
      state.inboxPartialFailure = true;
    } else {
      try {
        const props = await pResp.json();
        if (myEpoch !== state.inboxEpoch || state.selectedProjectId !== projId) return;
        for (const prop of (Array.isArray(props) ? props : [])) {
          if (!prop || prop.state !== "pending") continue;
          const propId = prop.id || prop.proposal_id;
          const key = `${sess.session_id}:${rid}:proposal:${propId}`;
          if (seenKeys.has(key)) continue;
          seenKeys.add(key);
          merged.push({
            key,
            propId,
            itemType: "proposal",
            sessionId: sess.session_id,
            runId: rid,
            raw: prop,
          });
        }
      } catch {
        state.inboxPartialFailure = true;
      }
    }
    if (!eResp || !eResp.ok) {
      state.inboxPartialFailure = true;
    } else {
      try {
        const extView = await eResp.json();
        if (myEpoch !== state.inboxEpoch || state.selectedProjectId !== projId) return;
        const extProps = Array.isArray(extView.proposals) ? extView.proposals : [];
        for (const ep of extProps) {
          if (!ep || ep.state !== "pending") continue;
          const propId = ep.proposal_id || ep.id;
          const key = `${sess.session_id}:${rid}:extension:${propId}`;
          if (seenKeys.has(key)) continue;
          seenKeys.add(key);
          merged.push({
            key,
            propId,
            itemType: "extension",
            sessionId: sess.session_id,
            runId: rid,
            raw: ep,
          });
        }
      } catch {
        state.inboxPartialFailure = true;
      }
    }
  }
  if (myEpoch !== state.inboxEpoch || state.selectedProjectId !== projId) return;
  state.inboxItems = merged;
  state.inboxLoaded = true;
  renderInbox();
  updateInboxBadge();
}

function renderInbox() {
  const sumEl = el("inbox-summary");
  const warnEl = el("inbox-partial-warning");
  const ul = el("inbox-list");
  if (!ul) return;
  if (sumEl) {
    sumEl.textContent = state.selectedProjectId
      ? `当前项目 (${state.selectedProjectId}) 活动运行待审批数：${state.inboxItems.length}${state.inboxPartialFailure ? "（计数不完整）" : ""}`
      : "请先在项目页选择项目。";
  }
  if (warnEl) {
    warnEl.hidden = !state.inboxPartialFailure;
    warnEl.textContent = state.inboxPartialFailure
      ? "部分活动运行的提案未能加载成功，当前待办计数与列表不完整。"
      : "";
  }
  ul.replaceChildren();
  if (state.inboxItems.length === 0) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = state.inboxPartialFailure
      ? "未能确认零待办（部分活动运行加载失败）。"
      : "当前项目活动运行没有待处理的提案。";
    ul.appendChild(empty);
    return;
  }
  for (const entry of state.inboxItems) {
    const li = document.createElement("li");
    li.setAttribute("data-testid", `inbox-item-${entry.propId}`);
    const title = document.createElement("div");
    const kindTag = entry.itemType === "extension"
      ? `[扩展/${entry.raw.kind}]`
      : `[${entry.raw.kind}]`;
    title.textContent = `${kindTag} session=${entry.sessionId.slice(0, 8)} run=${entry.runId.slice(0, 8)} — ${entry.raw.summary || entry.propId}`;
    li.appendChild(title);

    const actions = document.createElement("div");
    actions.className = "actions";
    const jumpBtn = document.createElement("button");
    jumpBtn.type = "button";
    jumpBtn.className = "small-btn";
    jumpBtn.textContent = "定位到会话";
    jumpBtn.addEventListener("click", () => {
      goToPage("chat");
      void switchSession(entry.sessionId).then(() => selectRunView(entry.runId));
    });
    actions.appendChild(jumpBtn);

    let trustCb = null;
    if (entry.itemType === "proposal" && entry.raw.kind === "command") {
      const lbl = document.createElement("label");
      lbl.className = "checkbox-label";
      trustCb = document.createElement("input");
      trustCb.type = "checkbox";
      const sp = document.createElement("span");
      sp.textContent = "trusted_local_execution";
      lbl.append(trustCb, sp);
      actions.appendChild(lbl);
    }

    const approveBtn = document.createElement("button");
    approveBtn.type = "button";
    approveBtn.className = "small-btn primary-btn";
    approveBtn.textContent = "批准";
    if (trustCb) {
      approveBtn.disabled = true;
      trustCb.addEventListener("change", () => {
        approveBtn.disabled = !trustCb.checked;
      });
    }
    approveBtn.addEventListener("click", () => {
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      const op = entry.itemType === "extension"
        ? handleExtensionDecision(entry.sessionId, entry.runId, entry.raw, "approve")
        : handleProposalDecision(
            entry.sessionId,
            entry.runId,
            entry.raw,
            "approve",
            trustCb ? trustCb.checked : false
          );
      void op.finally(() => void refreshInbox());
    });

    const rejectBtn = document.createElement("button");
    rejectBtn.type = "button";
    rejectBtn.className = "small-btn";
    rejectBtn.textContent = "拒绝";
    rejectBtn.addEventListener("click", () => {
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      const op = entry.itemType === "extension"
        ? handleExtensionDecision(entry.sessionId, entry.runId, entry.raw, "reject")
        : handleProposalDecision(entry.sessionId, entry.runId, entry.raw, "reject", false);
      void op.finally(() => void refreshInbox());
    });
    actions.append(approveBtn, rejectBtn);
    li.appendChild(actions);
    ul.appendChild(li);
  }
}

async function refreshProjectFiles(dirPath = ".", cursor = null, append = false, options = {}) {
  const pid = state.selectedProjectId;
  const proj = getSelectedProject();
  const myEpoch = state.reviewEpoch;
  const reqToken = ++state.reviewFilesReqToken;
  if (!pid || !proj || proj.kind !== "existing") {
    state.reviewFilesEntries = [];
    state.reviewFilesNextCursor = null;
    state.reviewFilesTruncated = false;
    state.reviewFilesError = proj ? "仅已登记的本机项目支持通用只读文件浏览。" : "请先选择项目。";
    renderWorkspaceFiles();
    return;
  }
  state.reviewFilesPath = dirPath;
  state.reviewFilesError = "";
  const qs = new URLSearchParams({ path: dirPath, limit: "50" });
  if (cursor) qs.set("cursor", cursor);
  try {
    const resp = await apiFetch(`/api/v1/projects/${pid}/files?${qs.toString()}`);
    if (reqToken !== state.reviewFilesReqToken || myEpoch !== state.reviewEpoch || state.selectedProjectId !== pid) return;
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (reqToken !== state.reviewFilesReqToken || myEpoch !== state.reviewEpoch || state.selectedProjectId !== pid) return;
      state.reviewFilesError = `${err.code}: ${err.message}`;
      renderWorkspaceFiles();
      return;
    }
    const data = await resp.json();
    if (reqToken !== state.reviewFilesReqToken || myEpoch !== state.reviewEpoch || state.selectedProjectId !== pid) return;
    const entries = Array.isArray(data.entries) ? data.entries : [];
    state.reviewFilesEntries = append
      ? [...state.reviewFilesEntries, ...entries]
      : entries;
    state.reviewFilesNextCursor = data.next_cursor || null;
    state.reviewFilesTruncated = Boolean(data.truncated);
  } catch {
    if (reqToken !== state.reviewFilesReqToken || myEpoch !== state.reviewEpoch || state.selectedProjectId !== pid) return;
    state.reviewFilesError = "读取工作区文件列表失败。";
  }
  renderWorkspaceFiles();
  if (options.reFetchSelected && state.reviewSelectedFile && state.reviewSelectedFile.path) {
    await openProjectFile(state.reviewSelectedFile.path);
  }
}

async function openProjectFile(filePath) {
  const pid = state.selectedProjectId;
  const myEpoch = state.reviewEpoch;
  const reqToken = ++state.reviewFileReqToken;
  state.reviewFilePath = filePath || "";
  if (!pid || !filePath) return;
  state.reviewFileError = "";
  try {
    const qs = new URLSearchParams({ path: filePath });
    const resp = await apiFetch(`/api/v1/projects/${pid}/file?${qs.toString()}`);
    if (
      reqToken !== state.reviewFileReqToken ||
      myEpoch !== state.reviewEpoch ||
      state.selectedProjectId !== pid ||
      state.reviewFilePath !== filePath
    ) {
      return;
    }
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (
        reqToken !== state.reviewFileReqToken ||
        myEpoch !== state.reviewEpoch ||
        state.selectedProjectId !== pid ||
        state.reviewFilePath !== filePath
      ) {
        return;
      }
      state.reviewFileError = `${err.code}: ${err.message}`;
      state.reviewSelectedFile = null;
    } else {
      const fileData = await resp.json();
      if (
        reqToken !== state.reviewFileReqToken ||
        myEpoch !== state.reviewEpoch ||
        state.selectedProjectId !== pid ||
        state.reviewFilePath !== filePath
      ) {
        return;
      }
      state.reviewSelectedFile = fileData;
    }
  } catch {
    if (
      reqToken !== state.reviewFileReqToken ||
      myEpoch !== state.reviewEpoch ||
      state.selectedProjectId !== pid ||
      state.reviewFilePath !== filePath
    ) {
      return;
    }
    state.reviewFileError = "读取文件内容失败。";
  }
  renderWorkspaceFiles();
}

function renderWorkspaceFiles() {
  const bar = el("files-path-bar");
  const stEl = el("files-status");
  const ul = el("workspace-files-list");
  const nextBtn = el("files-next-btn");
  const prevEl = el("workspace-file-preview");
  if (!ul) return;
  if (bar) bar.textContent = `当前目录：${state.reviewFilesPath || "."}`;
  if (stEl) {
    stEl.textContent = state.reviewFilesError
      ? state.reviewFilesError
      : `可见条目：${state.reviewFilesEntries.length}${state.reviewFilesTruncated ? "（存在更多条目）" : ""}`;
  }
  ul.replaceChildren();
  for (const ent of state.reviewFilesEntries) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "small-btn";
    btn.textContent = ent.kind === "dir" ? `[目录] ${ent.path}` : `${ent.path} (${ent.size_bytes} B)`;
    btn.addEventListener("click", () => {
      if (ent.kind === "dir") {
        void refreshProjectFiles(ent.path, null, false);
      } else {
        void openProjectFile(ent.path);
      }
    });
    li.appendChild(btn);
    ul.appendChild(li);
  }
  if (nextBtn) {
    nextBtn.hidden = !state.reviewFilesNextCursor;
  }
  const noteEl = el("chat-file-comparison-note");
  if (noteEl) {
    if (
      state.reviewSelectedFile &&
      state.chatCitedRead &&
      state.chatCitedRead.path === state.reviewSelectedFile.path
    ) {
      const curSha = state.reviewSelectedFile.file_sha256 || state.reviewSelectedFile.sha256 || "";
      const oldSha = state.chatCitedRead.sha256 || "";
      if (oldSha && curSha && oldSha !== curSha) {
        noteEl.textContent = `版本已变化：当时摘要（${state.chatCitedRead.summary || oldSha.slice(0, 12)}） vs 当前文件（sha:${curSha.slice(0, 12)}）`;
      } else {
        noteEl.textContent = `当时摘要：${state.chatCitedRead.summary || state.chatCitedRead.path} · 与当前磁盘文件一致（sha:${curSha.slice(0, 12)}）`;
      }
      noteEl.hidden = false;
    } else {
      noteEl.hidden = true;
      noteEl.textContent = "";
    }
  }
  if (prevEl) {
    if (state.reviewFileError) {
      prevEl.textContent = state.reviewFileError;
    } else if (state.reviewSelectedFile) {
      const f = state.reviewSelectedFile;
      const fSha = f.file_sha256 || f.sha256 || "";
      prevEl.textContent = `${f.path} (sha256=${fSha.slice(0, 12)} truncated=${Boolean(f.truncated)})\n---\n${f.content || ""}`;
    } else {
      prevEl.textContent = "选择文件查看当前磁盘文本。";
    }
  }
}

async function refreshProjectGitStatus() {
  const pid = state.selectedProjectId;
  const proj = getSelectedProject();
  const myEpoch = state.reviewEpoch;
  const reqToken = ++state.reviewGitStatusReqToken;
  if (!pid || !proj || proj.kind !== "existing") {
    state.reviewGitStatus = null;
    state.reviewGitDiff = null;
    state.reviewGitError = proj ? "仅已登记的本机项目支持只读 Git 审阅。" : "请先选择项目。";
    renderGitReview();
    return;
  }
  state.reviewGitError = "";
  try {
    const resp = await apiFetch(`/api/v1/projects/${pid}/git/status`);
    if (reqToken !== state.reviewGitStatusReqToken || myEpoch !== state.reviewEpoch || state.selectedProjectId !== pid) return;
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (reqToken !== state.reviewGitStatusReqToken || myEpoch !== state.reviewEpoch || state.selectedProjectId !== pid) return;
      state.reviewGitStatus = null;
      state.reviewGitError = `${err.code}: ${err.message}`;
    } else {
      const statusData = await resp.json();
      if (reqToken !== state.reviewGitStatusReqToken || myEpoch !== state.reviewEpoch || state.selectedProjectId !== pid) return;
      state.reviewGitStatus = statusData;
      const entries = Array.isArray(statusData.entries) ? statusData.entries : [];
      if (statusData.state === "ready") {
        if (state.reviewGitPath) {
          const stillPresent = entries.some((ent) => ent.path === state.reviewGitPath);
          if (stillPresent) {
            renderGitReview();
            await openProjectGitDiff(state.reviewGitPath, state.reviewGitArea);
            return;
          }
          state.reviewGitPath = "";
          state.reviewGitDiff = null;
        }
        if (entries.length > 0 && !state.reviewGitPath) {
          state.reviewGitPath = entries[0].path;
          renderGitReview();
          await openProjectGitDiff(state.reviewGitPath, state.reviewGitArea);
          return;
        }
      } else {
        state.reviewGitPath = "";
        state.reviewGitDiff = null;
      }
    }
  } catch {
    if (reqToken !== state.reviewGitStatusReqToken || myEpoch !== state.reviewEpoch || state.selectedProjectId !== pid) return;
    state.reviewGitStatus = null;
    state.reviewGitError = "获取 Git 状态失败。";
  }
  renderGitReview();
}

async function openProjectGitDiff(filePath, area = state.reviewGitArea) {
  const pid = state.selectedProjectId;
  const myEpoch = state.reviewEpoch;
  const reqToken = ++state.reviewGitDiffReqToken;
  if (!pid || !filePath) return;
  state.reviewGitPath = filePath;
  state.reviewGitArea = area;
  try {
    const qs = new URLSearchParams({ path: filePath, area });
    const resp = await apiFetch(`/api/v1/projects/${pid}/git/diff?${qs.toString()}`);
    if (
      reqToken !== state.reviewGitDiffReqToken ||
      myEpoch !== state.reviewEpoch ||
      state.selectedProjectId !== pid ||
      state.reviewGitPath !== filePath ||
      state.reviewGitArea !== area
    ) {
      return;
    }
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (
        reqToken !== state.reviewGitDiffReqToken ||
        myEpoch !== state.reviewEpoch ||
        state.selectedProjectId !== pid ||
        state.reviewGitPath !== filePath ||
        state.reviewGitArea !== area
      ) {
        return;
      }
      state.reviewGitDiff = { kind: "unavailable", reason: `${err.code}: ${err.message}`, patch: "" };
    } else {
      const diffData = await resp.json();
      if (
        reqToken !== state.reviewGitDiffReqToken ||
        myEpoch !== state.reviewEpoch ||
        state.selectedProjectId !== pid ||
        state.reviewGitPath !== filePath ||
        state.reviewGitArea !== area
      ) {
        return;
      }
      state.reviewGitDiff = diffData;
    }
  } catch {
    if (
      reqToken !== state.reviewGitDiffReqToken ||
      myEpoch !== state.reviewEpoch ||
      state.selectedProjectId !== pid ||
      state.reviewGitPath !== filePath ||
      state.reviewGitArea !== area
    ) {
      return;
    }
    state.reviewGitDiff = { kind: "unavailable", reason: "读取 Git diff 失败", patch: "" };
  }
  renderGitReview();
}

function renderGitReview() {
  const metaBar = el("git-meta-bar");
  const ul = el("git-entries-list");
  const areaNote = el("git-diff-area-note");
  const detail = el("git-diff-detail");
  if (!ul) return;

  for (const btn of document.querySelectorAll("[data-git-area]")) {
    btn.classList.toggle("active", btn.getAttribute("data-git-area") === state.reviewGitArea);
  }
  if (areaNote) {
    areaNote.textContent = state.reviewGitArea === "index"
      ? "当前区域：暂存区（HEAD → 暂存区）"
      : "当前区域：工作树（暂存区 → 磁盘）";
  }

  const st = state.reviewGitStatus;
  if (metaBar) {
    if (state.reviewGitError) {
      metaBar.textContent = state.reviewGitError;
    } else if (!st) {
      metaBar.textContent = "尚未加载 Git 状态（切换标签或点击刷新读取）。";
    } else if (st.state !== "ready") {
      metaBar.textContent = `Git 状态：${st.state}${st.reason ? ` (${st.reason})` : ""}`;
    } else {
      const branchLabel = st.branch ? `分支 ${st.branch}` : (st.head_oid ? `HEAD (${st.head_oid})` : "未命名分支");
      const headLabel = st.head_oid ? st.head_oid : "无提交";
      metaBar.textContent = `${branchLabel} · HEAD=${headLabel} · 条目=${st.entries.length}${st.truncated ? "（已截断）" : ""}`;
    }
  }

  ul.replaceChildren();
  if (st && st.state === "ready") {
    if (st.entries.length === 0) {
      const empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "工作区干净，没有未提交的变更。";
      ul.appendChild(empty);
    } else {
      for (const ent of st.entries) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "small-btn";
        btn.textContent = `[${ent.index_status}${ent.worktree_status}] ${ent.path}`;
        btn.addEventListener("click", () => {
          void openProjectGitDiff(ent.path, state.reviewGitArea);
        });
        li.appendChild(btn);
        ul.appendChild(li);
      }
    }
  }

  if (detail) {
    detail.replaceChildren();
    const df = state.reviewGitDiff;
    if (!df) {
      detail.textContent = "选择变更路径查看对应区域补丁。";
    } else if (df.kind === "text") {
      const hdr = document.createElement("div");
      hdr.className = "muted";
      hdr.textContent = `${df.path} [${df.area}]${df.truncated ? " (patch 已截断)" : ""}`;
      detail.appendChild(hdr);
      for (const line of String(df.patch || "").split("\n")) {
        const sp = document.createElement("span");
        sp.className = line.startsWith("+") && !line.startsWith("+++")
          ? "diff-line-add"
          : line.startsWith("-") && !line.startsWith("---")
            ? "diff-line-del"
            : "diff-line-ctx";
        sp.textContent = line;
        detail.appendChild(sp);
      }
    } else {
      detail.textContent = `${df.path || ""} [${df.area || state.reviewGitArea}] kind=${df.kind}${df.reason ? ` (${df.reason})` : ""}`;
    }
  }
}

function handleGoalInput() {
  const gInput = el("goal-input");
  if (!gInput) return;
  const val = gInput.value;
  if (state.activeSessionId) {
    const d = getOrCreateSessionDraft(state.activeSessionId);
    if (d) {
      d.goal = val;
      d.goalDirty = true;
    }
  } else if (state.selectedProjectId) {
    state.projectGoalDrafts.set(state.selectedProjectId, { goal: val, goalDirty: true });
  }
  renderDraftStatus();
}

function handleConstraintsInput() {
  const val = el("constraints-input").value;
  if (state.activeSessionId) {
    const d = getOrCreateSessionDraft(state.activeSessionId);
    if (d) {
      d.constraints = val;
      const savedVal = state.contextInfo && state.contextInfo.session_id === state.activeSessionId
        ? (state.contextInfo.constraints || "")
        : "";
      d.constraintsDirty = val !== savedVal;
    }
  }
  renderDraftStatus();
}

async function fetchHostHealth() {
  let modeText = "unconfigured";
  try {
    const resp = await apiFetch("/api/v1/health");
    if (!resp.ok) return;
    const health = await resp.json();
    state.liveEnabled = Boolean(health.live_enabled);
    state.liveProvider = typeof health.provider === "string" ? health.provider : "deepseek";
    state.liveModel = typeof health.model === "string" ? health.model : "";
    state.testProviderInjected = Boolean(health.test_provider_injected);
    modeText = typeof health.mode === "string" ? health.mode : (state.liveEnabled ? "live" : "unconfigured");
  } catch {
    state.liveEnabled = false;
    state.liveModel = "";
    state.testProviderInjected = false;
  }
  const modeLabel = el("host-mode-label");
  if (modeLabel) {
    if (state.liveEnabled) {
      modeLabel.textContent = `在线模型 · ${state.liveModel || "deepseek-flash"}`;
    } else if (state.testProviderInjected) {
      modeLabel.textContent = "测试注入模式";
    } else {
      modeLabel.textContent = "未连接在线模型";
    }
  }
  const healthNote = el("settings-host-health");
  if (healthNote) {
    const modeDesc = state.liveEnabled
      ? "在线已连接"
      : (state.testProviderInjected ? "测试注入模式" : "未连接在线模型");
    const modelDesc = state.liveEnabled
      ? (state.liveModel || "deepseek-flash")
      : (state.testProviderInjected ? "测试替身" : "未配置在线模型");
    healthNote.textContent = `连接状态：${modeDesc} · 当前模型：${modelDesc}`;
  }
  const ovMode = el("settings-overview-host-mode");
  if (ovMode) {
    if (state.liveEnabled) {
      ovMode.textContent = `● 已连接（${state.liveModel || "deepseek-flash"}）`;
    } else if (state.testProviderInjected) {
      ovMode.textContent = "● 测试注入";
    } else {
      ovMode.textContent = "● 未连接";
    }
  }
  const ovModel = el("settings-overview-model-val");
  if (ovModel) {
    if (state.liveEnabled) {
      ovModel.textContent = state.liveModel || "deepseek-flash";
    } else if (state.testProviderInjected) {
      ovModel.textContent = "测试替身";
    } else {
      ovModel.textContent = "未配置在线模型";
    }
  }
  renderLiveNotice();
}

function renderLiveNotice() {
  const notice = el("live-notice");
  if (!notice) return;
  const selected = parseModelSelection();
  if (state.liveEnabled && selected.provider === "deepseek") {
    notice.hidden = false;
    notice.textContent = `云端模型 ${
      state.liveModel || "deepseek-flash"
    } 将接收任务目标与该项目的工具上下文（凭据仅由宿主持有，本页不传递）；实际写范围、验证计划与受信任命令仍须逐步显式批准。`;
  } else {
    notice.hidden = true;
    notice.textContent = "";
  }
}

async function boot() {
  loadPrefs();
  applyAppearance();
  initTokenFromHash();
  el("create-project-btn").addEventListener("click", () => void handleCreateProject());
  const regBtn = el("register-project-btn");
  if (regBtn) {
    regBtn.addEventListener("click", () => void handleRegisterProject());
  }
  const newSessBtn = el("new-session-btn");
  if (newSessBtn) {
    newSessBtn.addEventListener("click", () => void handleNewSession());
  }
  const saveCtxBtn = el("save-constraints-btn");
  if (saveCtxBtn) {
    saveCtxBtn.addEventListener("click", () => void handleSaveConstraints());
  }
  const startRunBtn = el("start-run-btn");
  if (startRunBtn) {
    startRunBtn.addEventListener("click", () => void handleStartRun());
  }
  const cancelRunBtn = el("cancel-run-btn");
  if (cancelRunBtn) {
    cancelRunBtn.addEventListener("click", () => void handleCancelRun());
  }

  for (const btn of document.querySelectorAll("[data-nav-page]")) {
    btn.addEventListener("click", () => goToPage(btn.getAttribute("data-nav-page")));
  }
  for (const btn of document.querySelectorAll("[data-settings-section]")) {
    btn.addEventListener("click", () => chooseSettingsSection(btn.getAttribute("data-settings-section")));
  }
  for (const btn of document.querySelectorAll("[data-inspector-tab]")) {
    btn.addEventListener("click", () => chooseInspectorTab(btn.getAttribute("data-inspector-tab")));
  }
  for (const btn of document.querySelectorAll("[data-ext-tab]")) {
    btn.addEventListener("click", () => chooseExtTab(btn.getAttribute("data-ext-tab")));
  }
  for (const btn of document.querySelectorAll("[data-git-area]")) {
    btn.addEventListener("click", () => {
      const area = btn.getAttribute("data-git-area");
      state.reviewGitArea = area;
      if (state.reviewGitPath) {
        void openProjectGitDiff(state.reviewGitPath, area);
      } else {
        renderGitReview();
      }
    });
  }

  const toggleInspBtn = el("toggle-inspector-btn");
  if (toggleInspBtn) toggleInspBtn.addEventListener("click", toggleInspector);
  const refreshInboxBtn = el("refresh-inbox-btn");
  if (refreshInboxBtn) refreshInboxBtn.addEventListener("click", () => void refreshInbox());
  const refreshFilesBtn = el("refresh-files-btn");
  if (refreshFilesBtn) {
    refreshFilesBtn.addEventListener("click", () => {
      void refreshProjectFiles(state.reviewFilesPath || ".", null, false, { reFetchSelected: true });
    });
  }
  const filesNextBtn = el("files-next-btn");
  if (filesNextBtn) {
    filesNextBtn.addEventListener("click", () => {
      if (state.reviewFilesNextCursor) {
        void refreshProjectFiles(state.reviewFilesPath || ".", state.reviewFilesNextCursor, true);
      }
    });
  }
  const refreshGitBtn = el("refresh-git-btn");
  if (refreshGitBtn) refreshGitBtn.addEventListener("click", () => void refreshProjectGitStatus());

  const histSessSel = el("history-session-select");
  if (histSessSel) {
    histSessSel.addEventListener("change", (ev) => {
      const sid = ev.target.value;
      if (sid && sid !== state.activeSessionId) {
        void switchSession(sid);
      }
    });
  }
  const histFilter = el("history-filter-input");
  if (histFilter) {
    histFilter.addEventListener("input", (ev) => {
      state.historyFilter = ev.target.value || "";
      renderContextAndHistory();
    });
  }
  const themeSel = el("theme-select");
  if (themeSel) {
    themeSel.addEventListener("change", (ev) => {
      state.theme = ev.target.value === "dark" ? "dark" : "light";
      applyAppearance();
      savePrefs();
    });
  }
  const densitySel = el("density-select");
  if (densitySel) {
    densitySel.addEventListener("change", (ev) => {
      state.density = ev.target.value === "compact" ? "compact" : "normal";
      applyAppearance();
      savePrefs();
    });
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && document.body.classList.contains("menu-open")) {
      ev.preventDefault();
      closeMobileNav(true);
    }
  });

  const navToggle = el("navigation-toggle");
  if (navToggle) {
    navToggle.addEventListener("click", toggleNavigation);
  }
  const reconnectBtn = el("reconnect-btn");
  if (reconnectBtn) {
    reconnectBtn.addEventListener("click", () => {
      if (!state.activeSessionId || !state.activeRunId) return;
      state.reconnectAttempts = 0;
      const guard = {
        projectEpoch: state.projectEpoch,
        sessionEpoch: state.sessionEpoch,
        runEpoch: state.runEpoch,
        projectId: state.selectedProjectId,
        sessionId: state.activeSessionId,
        runId: state.activeRunId,
      };
      void openEventStream(state.activeSessionId, state.activeRunId, guard);
    });
  }
  const runSelect = el("run-history-select");
  if (runSelect) {
    runSelect.addEventListener("change", (ev) => {
      const chosen = ev.target.value;
      if (chosen) {
        void selectRunView(chosen);
      }
    });
  }
  const diffSelect = el("diff-file-select");
  if (diffSelect) {
    diffSelect.addEventListener("change", (ev) => {
      state.selectedDiffPath = ev.target.value || "";
      const res = state.currentRunView && state.currentRunView.result ? state.currentRunView.result : null;
      const diffObj = res && res.diff && typeof res.diff === "object" ? res.diff : null;
      const entries = diffObj && Array.isArray(diffObj.entries) ? diffObj.entries : [];
      renderDiffDetail(entries, diffObj);
    });
  }
  const gInput = el("goal-input");
  if (gInput) {
    gInput.addEventListener("input", handleGoalInput);
  }
  const cInput = el("constraints-input");
  if (cInput) {
    cInput.addEventListener("input", handleConstraintsInput);
  }
  const modelSelect = el("model-select");
  if (modelSelect) {
    modelSelect.addEventListener("change", renderLiveNotice);
  }
  const chatProjSel = el("chat-project-select");
  if (chatProjSel) {
    chatProjSel.addEventListener("change", (ev) => {
      const pid = ev.target.value;
      const proj = state.projects.find((p) => p.project_id === pid) || null;
      selectProject(proj);
    });
  }
  const chatComposer = el("chat-composer");
  if (chatComposer) {
    chatComposer.addEventListener("submit", submitChatTurn);
  }
  const chatInput = el("chat-input");
  if (chatInput) {
    chatInput.addEventListener("input", handleChatInput);
    chatInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        if (ev.isComposing || ev.keyCode === 229) return;
        ev.preventDefault();
        const form = el("chat-composer");
        if (form) form.requestSubmit();
      }
    });
  }
  const onlyAskBtn = el("only-ask-toggle-btn");
  if (onlyAskBtn) {
    onlyAskBtn.addEventListener("click", () => {
      state.onlyAskMode = !state.onlyAskMode;
      onlyAskBtn.setAttribute("aria-pressed", state.onlyAskMode ? "true" : "false");
    });
  }
  const emptyOpenProjBtn = el("empty-open-projects-btn");
  if (emptyOpenProjBtn) {
    emptyOpenProjBtn.addEventListener("click", () => goToPage("projects"));
  }
  const quoteDiffBtn = el("quote-diff-in-chat-btn");
  if (quoteDiffBtn) {
    quoteDiffBtn.addEventListener("click", quoteSelectedDiffInChat);
  }
  const closeInspBtn = el("close-inspector-btn");
  if (closeInspBtn) {
    closeInspBtn.addEventListener("click", () => setInspectorExpanded(false));
  }
  const chatNewSessBtn = el("chat-new-session-btn");
  if (chatNewSessBtn) {
    chatNewSessBtn.addEventListener("click", () => void handleNewSession());
  }
  const constraintsPill = el("chat-constraints-pill");
  if (constraintsPill) {
    constraintsPill.addEventListener("click", () => {
      goToPage("settings");
      if (typeof selectSettingsSection === "function") {
        selectSettingsSection("constraints");
      }
    });
  }
  const settingsSearchInput = el("settings-search-input");
  if (settingsSearchInput) {
    settingsSearchInput.addEventListener("focus", (ev) => {
      if (ev.relatedTarget && ev.relatedTarget !== settingsSearchInput) {
        settingsSearchOpener = ev.relatedTarget;
      }
    });
    settingsSearchInput.addEventListener("input", () => {
      settingsSearchIndex = 0;
      renderSettingsSearchResults();
    });
    settingsSearchInput.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowDown") {
        if (settingsSearchMatches.length > 0) {
          ev.preventDefault();
          settingsSearchIndex = (settingsSearchIndex + 1) % settingsSearchMatches.length;
          renderSettingsSearchResults();
        }
      } else if (ev.key === "ArrowUp") {
        if (settingsSearchMatches.length > 0) {
          ev.preventDefault();
          settingsSearchIndex = (settingsSearchIndex - 1 + settingsSearchMatches.length) % settingsSearchMatches.length;
          renderSettingsSearchResults();
        }
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        if (settingsSearchMatches.length > 0) {
          const chosen = settingsSearchMatches[Math.max(0, settingsSearchIndex)];
          activateSettingsSearchResult(chosen);
        }
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        ev.stopPropagation();
        closeSettingsSearch(true);
      }
    });
  }
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === "k" || ev.key === "K")) {
      ev.preventDefault();
      openSettingsSearch(document.activeElement);
      return;
    }
    if (ev.key === "Escape" && state.activePage === "settings") {
      const searchIn = el("settings-search-input");
      const resultsEl = el("settings-search-results");
      const emptyEl = el("settings-search-empty");
      const isSearchActive =
        document.activeElement === searchIn ||
        (resultsEl && !resultsEl.hidden) ||
        (emptyEl && !emptyEl.hidden);
      if (isSearchActive) {
        ev.preventDefault();
        closeSettingsSearch(true);
        return;
      }
    }
    if (ev.key === "Escape" && state.inspectorExpanded) {
      ev.preventDefault();
      setInspectorExpanded(false);
    }
  });

  if (typeof MutationObserver !== "undefined") {
    const syncObs = new MutationObserver(() => {
      syncInspectorBindingHeader();
      updateInboxBadge();
    });
    for (const watchId of ["run-id", "current-session", "proposal-list", "extension-proposal-list"]) {
      const node = el(watchId);
      if (node) {
        syncObs.observe(node, { childList: true, characterData: true, subtree: true });
      }
    }
  }

  setInspectorExpanded(false, null);
  renderIdentityHeader();
  renderScopeSummary();
  renderDraftStatus();
  renderRunHistorySelect();
  renderExtensions();
  renderChatRail();
  renderChatTimeline();
  syncLockButtons();

  await fetchHostHealth();
  await refreshProjectsAndSessions();
  if (state.selectedProjectId) {
    await refreshChatSessionTitles(state.selectedProjectId);
  }
  if (state.activeSessionId) {
    const sess = state.sessions.find((s) => s.session_id === state.activeSessionId);
    if (sess) {
      const proj = state.projects.find((p) => p.project_id === sess.project_id);
      if (proj) selectProject(proj, { userSwitch: false });
      const modelSelect = el("model-select");
      if (modelSelect && sess.model) {
        modelSelect.value = `${sess.model.provider}:${sess.model.model}`;
      }
      renderIdentityHeader();
      renderSessions();
      renderChatRail();
      await Promise.all([
        refreshSessionContextAndHistory(state.activeSessionId),
        refreshChatTurns(state.activeSessionId),
      ]);
      if (state.activeRunId) {
        await syncSnapshotAndStream(state.activeSessionId, state.activeRunId);
      } else {
        renderRun(null);
      }
      return;
    }
    state.activeSessionId = "";
    state.activeRunId = "";
    sessionStorage.removeItem(SESSION_KEY);
    sessionStorage.removeItem(RUN_KEY);
    renderIdentityHeader();
  }
  if (state.activeSessionId && state.activeRunId) {
    await syncSnapshotAndStream(state.activeSessionId, state.activeRunId);
    return;
  }
  const visible = state.selectedProjectId
    ? state.sessions.filter((s) => s.project_id === state.selectedProjectId)
    : state.sessions;
  if (visible.length > 0 && !state.activeSessionId) {
    await switchSession(visible[0].session_id);
  }
}

boot();


function _newChatUuid() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function renderChatRail() {
  const projSel = el("chat-project-select");
  if (projSel) {
    projSel.replaceChildren();
    if (state.projects.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（暂无项目）";
      projSel.appendChild(opt);
    } else {
      for (const proj of state.projects) {
        const opt = document.createElement("option");
        opt.value = proj.project_id;
        opt.textContent = proj.name;
        if (proj.project_id === state.selectedProjectId) {
          opt.selected = true;
        }
        projSel.appendChild(opt);
      }
    }
  }
  const ul = el("chat-session-list");
  if (!ul) return;
  ul.replaceChildren();
  const visible = state.selectedProjectId
    ? state.sessions.filter((s) => s.project_id === state.selectedProjectId)
    : [];
  if (visible.length === 0) {
    const empty = document.createElement("li");
    empty.className = "muted rail-empty";
    empty.textContent = "暂无会话";
    ul.appendChild(empty);
    return;
  }
  for (const sess of visible) {
    const li = document.createElement("li");
    li.setAttribute("data-testid", `chat-session-item-${sess.session_id}`);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rail-session-btn";
    const titleObj = state.chatSessionTitles.get(sess.session_id);
    const resolvedTitle = titleObj && titleObj.title ? titleObj.title : `会话 ${sess.session_id.slice(0, 8)}`;
    btn.textContent = resolvedTitle;
    if (sess.session_id === state.activeSessionId) {
      li.classList.add("active-item");
      btn.setAttribute("aria-current", "true");
    }
    btn.addEventListener("click", () => {
      void switchSession(sess.session_id);
    });
    li.appendChild(btn);
    ul.appendChild(li);
  }
  const headingEl = el("run-heading");
  if (headingEl) {
    if (state.activeSessionId) {
      const activeTitleObj = state.chatSessionTitles.get(state.activeSessionId);
      headingEl.textContent =
        activeTitleObj && activeTitleObj.title
          ? activeTitleObj.title
          : `会话 ${state.activeSessionId.slice(0, 8)}`;
    } else {
      headingEl.textContent = "只读对话与受控工作台";
    }
  }
  syncInspectorBindingHeader();
}

function toggleComposerConfig() {
  const panel = el("composer-config-panel");
  const btn = el("toggle-composer-config-btn");
  if (!panel) return;
  const willShow = panel.hidden;
  panel.hidden = !willShow;
  if (btn) btn.setAttribute("aria-expanded", willShow ? "true" : "false");
}

function openExecuteConfirmPanel() {
  const panel = el("execute-confirm-panel");
  const preview = el("execute-goal-preview");
  const projLabel = el("execute-project-label");
  const chatInput = el("chat-input");
  if (!panel) return;
  setError("", "");

  if (!state.selectedProjectId) {
    setError("NO_PROJECT", "请先选择或登记项目后再执行任务。");
    return;
  }
  const capturedModel = parseModelSelection();
  if (state.activeSessionId) {
    const sess = state.sessions.find((s) => s.session_id === state.activeSessionId);
    if (sess && sess.project_id !== state.selectedProjectId) {
      setError("SESSION_PROJECT_MISMATCH", "当前会话与所选项目不匹配，请先切换项目或新建会话。");
      return;
    }
    if (
      sess &&
      (sess.model.provider !== capturedModel.provider || sess.model.model !== capturedModel.model)
    ) {
      setError("MODEL_MISMATCH", "当前会话模型与所选模型不一致，请先解决模型匹配或新建对应会话。");
      return;
    }
  }

  const proj = getSelectedProject();
  if (projLabel) {
    projLabel.textContent = proj ? `项目：${proj.name} (${proj.project_id})` : `项目：${state.selectedProjectId}`;
  }
  const goalVal = chatInput ? chatInput.value : "";
  state.pendingExecuteGoal = goalVal;
  if (preview) {
    preview.textContent = goalVal;
  }
  renderScopeSummary();
  panel.hidden = false;
}

function closeExecuteConfirmPanel({ focusInput = true } = {}) {
  const panel = el("execute-confirm-panel");
  if (panel) panel.hidden = true;
  state.pendingExecuteGoal = null;
  const confirmInput = el("confirm-scope-input");
  if (confirmInput) confirmInput.checked = false;
  if (focusInput) {
    const chatInput = el("chat-input");
    if (chatInput) chatInput.focus();
  }
}

function _timelineOrderStorageKey(projectId, sessionId) {
  return `fc_timeline_order:${projectId || "none"}:${sessionId || "none"}`;
}

function loadTimelineOrder(projectId, sessionId) {
  if (!projectId || !sessionId) return [];
  try {
    const raw = localStorage.getItem(_timelineOrderStorageKey(projectId, sessionId));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function recordTimelineEntry(projectId, sessionId, kind, id) {
  if (!projectId || !sessionId || !id) return;
  const strId = String(id);
  const list = loadTimelineOrder(projectId, sessionId);
  if (list.some((x) => x && x.kind === kind && x.id === strId)) return;
  list.push({ kind, id: strId });
  if (list.length > 500) {
    list.splice(0, list.length - 500);
  }
  try {
    localStorage.setItem(_timelineOrderStorageKey(projectId, sessionId), JSON.stringify(list));
  } catch {
    // Ignore storage quota issues.
  }
}

function buildProposalPresentation(prop) {
  const pid = prop.id || prop.proposal_id || "";
  const pstate = prop.state || prop.status || "";
  const payload = prop.payload || {};
  const titleDiv = document.createElement("div");
  titleDiv.className = "proposal-card-title";
  const bodyDiv = document.createElement("div");
  bodyDiv.className = "proposal-card-body";
  const techDetails = document.createElement("details");
  techDetails.className = "proposal-tech-details";
  const techSum = document.createElement("summary");
  techSum.textContent = "技术详情";
  const techRaw = document.createElement("div");
  techRaw.className = "proposal-tech-raw";
  if (prop.kind === "scope") {
    const ops = Array.isArray(payload.operations) ? payload.operations.join(",") : "";
    const exact = Array.isArray(payload.exact_paths) ? payload.exact_paths.join(",") : "";
    const prefixes = Array.isArray(payload.write_prefixes) ? payload.write_prefixes.join(",") : "";
    const prot = Array.isArray(payload.protected_paths) ? payload.protected_paths.join(",") : "";
    titleDiv.textContent = pstate === "pending" ? "等待批准 · 写入范围（尚未写入磁盘）" : "已处理 · 写入范围";
    bodyDiv.textContent = `目标路径：${exact || prefixes || "指定路径"}${prot ? ` · 受保护路径：${prot}` : ""}`;
    techRaw.textContent = `[${pstate}] scope ${pid} digest=${(prop.digest || "").slice(0, 12)} ops=[${ops}] exact=[${exact}] prefixes=[${prefixes}] protected=[${prot}] ${payload.verification_plan_summary || ""}`;
  } else if (prop.kind === "verification") {
    const plan = payload.plan || {};
    const checksDesc = Array.isArray(plan.checks) ? plan.checks.map((c) => c.kind === "exit_code" ? `${c.check_id}[exit_code:仅检查退出码0，非测试通过]` : `${c.check_id}[junit_exact:${Array.isArray(c.expected_tests) ? c.expected_tests.map((t) => `${t.classname}.${t.name}`).join(",") : ""}]`).join("; ") : "";
    const protMap = payload.protected_shas ? Object.keys(payload.protected_shas).join(",") : "";
    titleDiv.textContent = pstate === "pending" ? "等待批准 · 验证计划" : "已处理 · 验证计划";
    bodyDiv.textContent = `验证检查：${Array.isArray(plan.checks) ? plan.checks.map((c) => c.check_id || "check").join("、") : "项目验证"}`;
    techRaw.textContent = `[${pstate}] verification ${pid} plan=${plan.plan_id || ""} digest=${(prop.digest || "").slice(0, 12)} checks=[${checksDesc}] protected=[${protMap}]`;
  } else if (prop.kind === "command") {
    const cmd = payload.command || prop.command || {};
    const argsText = Array.isArray(cmd.argv) ? cmd.argv.join(" ") : "";
    titleDiv.textContent = pstate === "pending" ? "等待批准 · 受信任本机命令" : "已处理 · 本机命令";
    bodyDiv.textContent = `命令：${cmd.executable || ""} ${argsText} | 工作目录：${cmd.cwd || "."} | 用途：${cmd.purpose || "验证"}${cmd.explanation ? `（${cmd.explanation}）` : ""}`;
    techRaw.textContent = `[${pstate}] command ${pid} digest=${(prop.digest || "").slice(0, 12)} cmd=${cmd.executable || ""} exe_sha=${(cmd.executable_sha256 || "").slice(0, 12)} argv=[${argsText}] cwd=${cmd.cwd || "."} timeout=${cmd.timeout_ms || 0}ms purpose=${cmd.purpose || ""} explanation=${cmd.explanation || ""} （受信任本机执行：非 OS 沙箱，进程可访问本机文件与网络）`;
  } else {
    titleDiv.textContent = pstate === "pending" ? "等待批准 · 操作" : "已处理 · 操作";
    bodyDiv.textContent = prop.kind || "操作提案";
    techRaw.textContent = `[${pstate}] ${prop.kind || "unknown"} ${pid}`;
  }
  techDetails.append(techSum, techRaw);
  return [titleDiv, bodyDiv, techDetails];
}

function appendAcceptanceCoverage(parts, res) {
  if (res && res.acceptance && Array.isArray(res.acceptance.results) && res.acceptance.results.length > 0) {
    const accDesc = res.acceptance.results.map((r) => {
      const exitPart = r.exit_code !== null && r.exit_code !== undefined ? ` exit=${r.exit_code}` : "";
      return `${r.command_id || "acceptance"}(status=${r.status || "unknown"}${exitPart})`;
    });
    parts.push(`受控验收：${accDesc.join("；")}`);
  }
}

function formatRunStateLabel(run) {
  const st = run && run.state ? run.state : "idle";
  const verif = run && run.verification ? run.verification : "not_run";
  if (st === "queued") return "排队中";
  if (st === "running") return "运行中";
  if (st === "cancelling") return "正在停止";
  if (st === "cancelled") return "已取消";
  if (st === "failed") return "执行失败";
  if (st === "unknown") return "状态未知";
  if (st === "succeeded") {
    if (verif === "passed") return "已完成 · 验证通过";
    if (verif === "failed") return "已完成 · 验证失败";
    if (verif === "not_run") return "已完成 · 未验证";
    return "已完成 · 待核对";
  }
  return st;
}

function summarizeRunFiles(res) {
  const changedPaths = res && Array.isArray(res.changed_paths) ? res.changed_paths : null;
  if (changedPaths === null) return "文件变更：待核对";
  const pathNames = changedPaths
    .map((item) => (typeof item === "string" ? item : (item && typeof item.path === "string" ? item.path : "")))
    .filter(Boolean);
  if (pathNames.length === 0) return "文件变更：未修改文件";
  return `文件变更：已修改 ${pathNames.join("、")}`;
}

function summarizeRunVerification(run, res) {
  const verif = run && run.verification ? run.verification : "not_run";
  const st = run && run.state ? run.state : "idle";
  const checksRaw = res && res.checks ? res.checks : null;
  const checkEntries = Array.isArray(checksRaw)
    ? checksRaw
    : (checksRaw && typeof checksRaw === "object" ? Object.values(checksRaw) : []);
  if (checkEntries.length > 0) {
    const allPassed = checkEntries.every((c) => Boolean(c && c.passed)) && verif === "passed";
    if (allPassed) {
      return `验证通过（${checkEntries.map((c) => c.check_id || "检查").join("、")}）`;
    }
    const anyFailed = checkEntries.some((c) => c && c.passed === false) || verif === "failed";
    if (anyFailed) {
      return `验证未通过（${checkEntries.map((c) => `${c.check_id || "检查"} 退出码 ${c.exit_code ?? "-"}`).join("、")}）`;
    }
    return "验证待核对";
  }
  const accResults = res && res.acceptance && Array.isArray(res.acceptance.results)
    ? res.acceptance.results
    : [];
  if (accResults.length > 0) {
    const allPassed =
      accResults.every((r) => r && r.status === "passed" && r.exit_code === 0) &&
      verif === "passed";
    if (allPassed) {
      return `验证通过（${accResults.map((r) => r.command_id || "验收检查").join("、")}）`;
    }
    const anyFailed =
      accResults.some((r) => r && r.status === "failed") || verif === "failed";
    if (anyFailed) {
      return `验证未通过（${accResults.map((r) => `${r.command_id || "验收检查"} 退出码 ${r.exit_code ?? "-"}`).join("、")}）`;
    }
    if (verif === "not_run" || st === "cancelled") {
      return "验证未运行";
    }
    return "验证待核对";
  }
  if (verif === "failed") return "验证未通过";
  if (verif === "not_run" || st === "cancelled") return "验证未运行";
  return "验证待核对";
}

function renderTimelineRunCard(run) {
  const article = document.createElement("article");
  article.className = "timeline-run-card";
  article.setAttribute("data-testid", `timeline-run-${run.run_id}`);
  article.setAttribute("data-run-id", run.run_id);
  article.setAttribute("data-run-state", run.state || "");
  article.setAttribute("data-verification", run.verification || "not_run");

  const meta = document.createElement("div");
  meta.className = "timeline-run-meta";
  const left = document.createElement("div");
  left.className = "row compact-row";
  const badge = document.createElement("span");
  badge.className = "chat-role-badge";
  badge.textContent = "执行";
  const idCode = document.createElement("span");
  idCode.className = "muted";
  idCode.setAttribute("data-testid", `timeline-run-id-${run.run_id}`);
  idCode.setAttribute("data-run-id", run.run_id);
  idCode.textContent = "运行记录";
  left.append(badge, idCode);

  const stateBadge = document.createElement("span");
  const verif = run.verification || "not_run";
  let statusTone = "active";
  if (
    run.state === "failed" ||
    run.state === "unknown" ||
    verif === "failed"
  ) {
    statusTone = "failed";
  } else if (
    run.state === "cancelled" ||
    (TERMINAL_RUN_STATES.has(run.state) && verif !== "passed") ||
    (!TERMINAL_RUN_STATES.has(run.state) && (state.connectionState === "disconnected" || state.connectionState === "error"))
  ) {
    statusTone = "warning";
  } else if (run.state === "succeeded" && verif === "passed") {
    statusTone = "succeeded";
  }
  stateBadge.className = `pill run-state-badge run-state-${statusTone}`;
  stateBadge.setAttribute("data-status-tone", statusTone);
  stateBadge.setAttribute("data-run-state", run.state || "");
  stateBadge.setAttribute("data-verification", verif);
  stateBadge.setAttribute("data-testid", `timeline-run-state-${run.run_id}`);
  stateBadge.textContent = formatRunStateLabel(run);
  meta.append(left, stateBadge);
  article.appendChild(meta);

  const goalDiv = document.createElement("div");
  goalDiv.className = "chat-msg-text";
  goalDiv.setAttribute("data-testid", `timeline-run-goal-${run.run_id}`);
  goalDiv.textContent = run.goal_text || run.goal || "-";
  article.appendChild(goalDiv);

  const actions = document.createElement("div");
  actions.className = "actions";
  const isTerminal = TERMINAL_RUN_STATES.has(run.state);
  const cardSessionId = run.session_id || state.activeSessionId;
  renderRunProposalSummaryInto(article, cardSessionId, run.run_id, isTerminal);

  if (!isTerminal) {
    const cardRunId = run.run_id;
    const stopBtn = document.createElement("button");
    stopBtn.type = "button";
    stopBtn.className = "small-btn";
    stopBtn.textContent = "停止运行";
    stopBtn.setAttribute("data-testid", `timeline-run-cancel-${cardRunId}`);
    stopBtn.addEventListener("click", () => {
      void handleCancelRun(cardSessionId, cardRunId);
    });
    actions.appendChild(stopBtn);
  } else {
    const res = run.result && typeof run.result === "object" ? run.result : null;
    const summaryDiv = document.createElement("div");
    summaryDiv.className = "timeline-run-summary muted";

    const fileLine = document.createElement("div");
    fileLine.className = "timeline-run-summary-line";
    fileLine.setAttribute("data-testid", `timeline-run-files-${run.run_id}`);
    fileLine.textContent = summarizeRunFiles(res);
    summaryDiv.appendChild(fileLine);

    const checkLine = document.createElement("div");
    checkLine.className = "timeline-run-summary-line";
    checkLine.setAttribute("data-testid", `timeline-run-checks-${run.run_id}`);
    checkLine.textContent = summarizeRunVerification(run, res);
    summaryDiv.appendChild(checkLine);
    article.appendChild(summaryDiv);

    const diffBtn = document.createElement("button");
    diffBtn.type = "button";
    diffBtn.className = "small-btn";
    diffBtn.textContent = "查看 Diff";
    diffBtn.setAttribute("data-testid", `timeline-run-diff-${run.run_id}`);
    diffBtn.addEventListener("click", async () => {
      await selectRunView(run.run_id);
      setInspectorExpanded(true, diffBtn);
      chooseInspectorTab("diff");
    });

    const cmdBtn = document.createElement("button");
    cmdBtn.type = "button";
    cmdBtn.className = "small-btn";
    cmdBtn.textContent = "查看命令";
    cmdBtn.setAttribute("data-testid", `timeline-run-cmd-${run.run_id}`);
    cmdBtn.addEventListener("click", async () => {
      await selectRunView(run.run_id);
      setInspectorExpanded(true, cmdBtn);
      chooseInspectorTab("commands");
    });

    const followBtn = document.createElement("button");
    followBtn.type = "button";
    followBtn.className = "small-btn";
    followBtn.textContent = "追问此任务";
    followBtn.setAttribute("data-testid", `timeline-run-followup-${run.run_id}`);
    followBtn.addEventListener("click", () => {
      const input = el("chat-input");
      if (input) {
        input.focus();
      }
    });

    actions.append(diffBtn, cmdBtn, followBtn);
  }

  article.appendChild(actions);
  return article;
}

function renderChatTimeline() {
  const pill = el("chat-constraints-pill");
  if (pill) {
    const hasConstraints = Boolean(
      state.contextInfo && typeof state.contextInfo.constraints === "string" && state.contextInfo.constraints.trim()
    );
    pill.textContent = hasConstraints ? "1 约束" : "0 约束";
  }
  const container = el("chat-timeline");
  if (!container) return;
  const approvalsCard = el("pending-approvals-card");
  const scrollArea = el("chat-scroll-area");
  if (approvalsCard && scrollArea && approvalsCard.parentElement !== scrollArea) {
    scrollArea.appendChild(approvalsCard);
  }
  container.replaceChildren();
  const turns = Array.isArray(state.chatTurns) ? state.chatTurns : [];
  const runs = Array.from(state.sessionRunsById.values()).filter(
    (r) => r && r.run_id && (!state.activeSessionId || r.session_id === state.activeSessionId)
  );
  if (turns.length === 0 && runs.length === 0) {
    const hint = document.createElement("div");
    hint.id = "chat-empty-hint";
    hint.className = "chat-empty-hint muted";
    if (!state.selectedProjectId) {
      const span = document.createElement("span");
      span.textContent = "打开或登记项目后即可开始对话。";
      const btn = document.createElement("button");
      btn.id = "empty-open-projects-btn";
      btn.setAttribute("data-testid", "empty-open-projects");
      btn.type = "button";
      btn.className = "small-btn";
      btn.textContent = "打开项目";
      btn.addEventListener("click", () => goToPage("projects"));
      hint.append(span, btn);
    } else {
      hint.hidden = true;
    }
    container.appendChild(hint);
    return;
  }
  const timelineEntries = [];
  const projId = state.selectedProjectId;
  const sessId = state.activeSessionId;
  for (const turn of turns) {
    recordTimelineEntry(projId, sessId, "turn", turn.turn_id || turn.seq);
  }
  for (const run of runs) {
    recordTimelineEntry(projId, sessId, "run", run.run_id);
  }
  const orderList = loadTimelineOrder(projId, sessId);
  const orderIndex = new Map();
  orderList.forEach((entry, idx) => {
    if (entry && entry.kind && entry.id) {
      orderIndex.set(`${entry.kind}:${entry.id}`, idx);
    }
  });
  turns.forEach((turn, idx) => {
    const key = `turn:${String(turn.turn_id || turn.seq)}`;
    const pos = orderIndex.has(key) ? orderIndex.get(key) : 100000 + idx;
    timelineEntries.push({ kind: "turn", pos, item: turn });
  });
  runs.forEach((run, idx) => {
    const key = `run:${String(run.run_id)}`;
    const pos = orderIndex.has(key) ? orderIndex.get(key) : 200000 + idx;
    timelineEntries.push({ kind: "run", pos, item: run });
  });
  timelineEntries.sort((a, b) => a.pos - b.pos);

  for (const entry of timelineEntries) {
    if (entry.kind === "run") {
      container.appendChild(renderTimelineRunCard(entry.item));
      continue;
    }
    const turn = entry.item;
    const article = document.createElement("article");
    article.className = "chat-turn";
    article.setAttribute("data-testid", `chat-turn-${turn.seq}`);

    const userRow = document.createElement("div");
    userRow.className = "chat-msg chat-msg-user";
    const userBadge = document.createElement("span");
    userBadge.className = "chat-role-badge";
    userBadge.textContent = "你";
    const userBody = document.createElement("div");
    userBody.className = "chat-msg-text";
    userBody.textContent = turn.user_message || "";
    userRow.append(userBadge, userBody);
    article.appendChild(userRow);

    if (Array.isArray(turn.resolved_evidence) && turn.resolved_evidence.length > 0) {
      const evBar = document.createElement("div");
      evBar.className = "chat-turn-evidence";
      turn.resolved_evidence.forEach((ev, idx) => {
        const chipBtn = document.createElement("button");
        chipBtn.type = "button";
        chipBtn.className = ev.availability === "raw_output_unavailable" ? "chat-ev-chip unavailable" : "chat-ev-chip";
        chipBtn.setAttribute("data-testid", `chat-turn-ev-${turn.seq}-${idx}`);
        chipBtn.textContent = ev.note ? `${ev.label} · ${ev.note}` : ev.label;
        chipBtn.addEventListener("click", () => {
          void jumpToEvidence(ev, chipBtn);
        });
        evBar.appendChild(chipBtn);
      });
      article.appendChild(evBar);
    }

    if (Array.isArray(turn.tool_reads) && turn.tool_reads.length > 0) {
      const details = document.createElement("details");
      details.className = "chat-tool-card";
      details.setAttribute("data-testid", `chat-tool-card-${turn.seq}`);
      const summary = document.createElement("summary");
      summary.textContent = turn.tool_reads.map((r) => r.summary).join(" · ");
      details.appendChild(summary);

      const list = document.createElement("ul");
      list.className = "chat-tool-list";
      for (const readItem of turn.tool_reads) {
        const li = document.createElement("li");
        if (readItem.tool_name === "read_file" && readItem.path && readItem.ok) {
          const openBtn = document.createElement("button");
          openBtn.type = "button";
          openBtn.className = "small-btn chat-tool-open-btn";
          openBtn.setAttribute("data-testid", `chat-open-file-${turn.seq}-${readItem.step}`);
          openBtn.textContent = `查看文件：${readItem.summary}`;
          openBtn.addEventListener("click", () => {
            state.chatCitedRead = readItem;
            setInspectorExpanded(true, openBtn);
            chooseInspectorTab("files");
            void openProjectFile(readItem.path);
          });
          li.appendChild(openBtn);
        } else {
          const span = document.createElement("span");
          span.className = "muted";
          span.textContent = readItem.summary;
          li.appendChild(span);
        }
        list.appendChild(li);
      }
      details.appendChild(list);
      article.appendChild(details);
    }

    if (turn.assistant_message) {
      const botRow = document.createElement("div");
      botRow.className = "chat-msg chat-msg-assistant";
      const botBadge = document.createElement("span");
      botBadge.className = "chat-role-badge assistant";
      botBadge.textContent = "F>";
      const botBody = document.createElement("div");
      botBody.className = "chat-msg-text";
      botBody.textContent = turn.assistant_message;
      botRow.append(botBadge, botBody);
      article.appendChild(botRow);
    } else if (turn.execution_offer) {
      article.appendChild(renderExecutionOfferCard(turn));
    } else if (turn.status === "failed" || turn.status === "interrupted") {
      const errRow = document.createElement("div");
      errRow.className = "chat-msg chat-msg-error";
      errRow.textContent = `${turn.error_code || turn.status}: ${turn.error_message || "对话轮未完成"}`;
      article.appendChild(errRow);
    }

    container.appendChild(article);
  }
  syncPendingApprovalsVisibility();
}

async function refreshChatSessionTitles(projectId) {
  if (!projectId) return;
  const reqToken = ++state.chatTitlesReqToken;
  try {
    const resp = await apiFetch(`/api/v1/projects/${projectId}/chat/session-titles`);
    if (reqToken !== state.chatTitlesReqToken || state.selectedProjectId !== projectId) return;
    if (!resp.ok) return;
    const data = await resp.json();
    if (reqToken !== state.chatTitlesReqToken || state.selectedProjectId !== projectId) return;
    if (data && Array.isArray(data.titles)) {
      const nextMap = new Map();
      for (const item of data.titles) {
        if (item && item.session_id) {
          nextMap.set(item.session_id, item);
        }
      }
      state.chatSessionTitles = nextMap;
      renderChatRail();
    }
  } catch {
    // Keep existing titles on transient read failure.
  }
}

function handleChatInput(ev) {
  const value = ev && ev.target ? ev.target.value : "";
  const confirmPanel = el("execute-confirm-panel");
  if (confirmPanel && !confirmPanel.hidden && state.pendingExecuteGoal !== null && value !== state.pendingExecuteGoal) {
    closeExecuteConfirmPanel({ focusInput: false });
  }
  if (!state.activeSessionId) return;
  if (value) {
    state.chatDrafts.set(state.activeSessionId, value);
  } else {
    state.chatDrafts.delete(state.activeSessionId);
  }
  renderDraftStatus();
}

function parseFirstDiffHunk(diffExcerpt) {
  const m = String(diffExcerpt || "").match(/^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@/m);
  if (!m) return null;
  return {
    old_start: Number(m[1]),
    old_count: m[2] !== undefined ? Number(m[2]) : 1,
    new_start: Number(m[3]),
    new_count: m[4] !== undefined ? Number(m[4]) : 1,
  };
}

function renderChatReferencesBar() {
  const bar = el("chat-references-bar");
  if (!bar) return;
  bar.replaceChildren();
  const refs = state.activeSessionId ? (state.chatReferences.get(state.activeSessionId) || []) : [];
  if (refs.length === 0) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  refs.forEach((ref, idx) => {
    const chip = document.createElement("span");
    chip.className = "chat-ref-chip";
    chip.setAttribute("data-testid", `chat-ref-chip-${idx}`);
    const labelSpan = document.createElement("span");
    labelSpan.textContent = ref._label || (ref.kind === "diff_hunk" ? `${ref.path} · ${ref.run_id.slice(0, 8)}` : `${ref.command_id} · ${ref.run_id.slice(0, 8)}`);
    const rmBtn = document.createElement("button");
    rmBtn.type = "button";
    rmBtn.className = "chat-ref-remove";
    rmBtn.textContent = "×";
    rmBtn.setAttribute("aria-label", "移除引用");
    rmBtn.setAttribute("data-testid", `chat-ref-remove-${idx}`);
    rmBtn.addEventListener("click", () => removeChatReference(idx));
    chip.append(labelSpan, rmBtn);
    bar.appendChild(chip);
  });
}

function addChatReference(ref) {
  if (!state.activeSessionId) return;
  const list = [...(state.chatReferences.get(state.activeSessionId) || [])];
  if (list.length >= 4) {
    setError("TOO_MANY_REFERENCES", "每轮最多附带 4 项运行证据引用。");
    return;
  }
  list.push(ref);
  state.chatReferences.set(state.activeSessionId, list);
  renderChatReferencesBar();
}

function removeChatReference(idx) {
  if (!state.activeSessionId) return;
  const list = [...(state.chatReferences.get(state.activeSessionId) || [])];
  list.splice(idx, 1);
  if (list.length === 0) {
    state.chatReferences.delete(state.activeSessionId);
  } else {
    state.chatReferences.set(state.activeSessionId, list);
  }
  renderChatReferencesBar();
}

function quoteSelectedDiffInChat() {
  const res = state.currentRunView && state.currentRunView.result ? state.currentRunView.result : null;
  const diffObj = res && res.diff && typeof res.diff === "object" ? res.diff : null;
  const entries = diffObj && Array.isArray(diffObj.entries) ? diffObj.entries : [];
  if (!state.activeRunId || entries.length === 0) return;
  const selected = entries.find((e) => e.path === state.selectedDiffPath) || entries[0];
  const hunk = parseFirstDiffHunk(selected.diff_excerpt);
  if (!hunk) return;
  const ref = {
    kind: "diff_hunk",
    run_id: state.activeRunId,
    path: selected.path,
    before_sha256: selected.before_sha256 || null,
    after_sha256: selected.after_sha256 || null,
    old_start: selected.before_sha256 ? hunk.old_start : null,
    old_count: selected.before_sha256 ? hunk.old_count : null,
    new_start: selected.after_sha256 ? hunk.new_start : null,
    new_count: selected.after_sha256 ? hunk.new_count : null,
    _label: `${selected.path} (L${selected.after_sha256 ? hunk.new_start : hunk.old_start}) · Run ${state.activeRunId.slice(0, 8)}`,
  };
  addChatReference(ref);
}

async function jumpToEvidence(ev, openerEl) {
  if (!ev || !ev.run_id) return;
  if (ev.kind === "diff_hunk" && ev.path) {
    state.selectedDiffPath = ev.path;
  }
  await selectRunView(ev.run_id);
  setInspectorExpanded(true, openerEl || el("toggle-inspector-btn"));
  chooseInspectorTab(ev.kind === "diff_hunk" ? "diff" : "commands");
}

function syncChatInputForSession() {
  const input = el("chat-input");
  const statusEl = el("chat-composer-status");
  const sendBtn = el("chat-send-btn");
  if (input) {
    input.value = state.activeSessionId ? (state.chatDrafts.get(state.activeSessionId) || "") : "";
  }
  if (statusEl) {
    statusEl.textContent = "";
  }
  if (sendBtn) {
    sendBtn.disabled = false;
  }
  renderChatReferencesBar();
}

async function refreshChatTurns(sessionId, guard = null) {
  if (!sessionId) return;
  const reqToken = ++state.chatTurnsReqToken;
  const mySessionEpoch = guard ? guard.sessionEpoch : state.sessionEpoch;
  const myProjectEpoch = guard ? guard.projectEpoch : state.projectEpoch;
  const isStillCurrent = () =>
    reqToken === state.chatTurnsReqToken &&
    mySessionEpoch === state.sessionEpoch &&
    myProjectEpoch === state.projectEpoch &&
    state.activeSessionId === sessionId;

  try {
    const allItems = [];
    let afterSeq = 0;
    while (true) {
      const resp = await apiFetch(
        `/api/v1/sessions/${sessionId}/chat/turns?after_seq=${afterSeq}&limit=50`,
        guard && guard.signal ? { signal: guard.signal } : {},
      );
      if (!isStillCurrent()) return;
      if (!resp.ok) return;
      const data = await resp.json();
      if (!isStillCurrent()) return;
      if (!data || !Array.isArray(data.items)) {
        return;
      }
      for (const item of data.items) {
        allItems.push(item);
      }
      if (!data.has_more || data.items.length === 0) {
        break;
      }
      const nextSeq =
        typeof data.next_after_seq === "number"
          ? data.next_after_seq
          : data.items[data.items.length - 1].seq;
      if (typeof nextSeq !== "number" || nextSeq <= afterSeq) {
        break;
      }
      afterSeq = nextSeq;
    }
    if (!isStillCurrent()) return;
    state.chatTurns = allItems;
    renderChatTimeline();
  } catch {
    // Ignore aborted or transient chat turn fetch errors.
  }
}

async function submitChatTurn(ev) {
  ev.preventDefault();
  if (state.chatBusy) return;
  const input = el("chat-input");
  const sendBtn = el("chat-send-btn");
  const statusEl = el("chat-composer-status");
  const rawValue = input ? input.value : "";
  const text = rawValue.trim();
  if (!state.activeSessionId) {
    if (!state.selectedProjectId) {
      setError("NO_SESSION", "请先打开或选择项目后再发送消息。");
      return;
    }
    const createdSid = await ensureSessionForSelection(
      state.selectedProjectId,
      parseModelSelection(),
      state.projectEpoch,
      state.sessionEpoch
    );
    if (!createdSid || state.activeSessionId !== createdSid) {
      return;
    }
  }
  if (!text) {
    setError("INVALID_INPUT", "请输入要询问的问题。");
    return;
  }
  const activeState = state.currentRunView ? state.currentRunView.state : "idle";
  if (activeState === "queued" || activeState === "running" || activeState === "cancelling") {
    if (statusEl) statusEl.textContent = "活动运行中：草稿已保留，未触发取消";
    return;
  }

  const targetSessionId = state.activeSessionId;
  const targetProjectId = state.selectedProjectId;
  const mySessEpoch = state.sessionEpoch;
  const myProjEpoch = state.projectEpoch;
  const mySendToken = ++state.chatSendToken;
  const reqId = _newChatUuid();
  const rawRefs = state.chatReferences.get(targetSessionId) || [];
  const wireRefs = rawRefs.map((r) => {
    const copy = { ...r };
    delete copy._label;
    return copy;
  });
  const isSameChatTarget = () =>
    mySendToken === state.chatSendToken &&
    mySessEpoch === state.sessionEpoch &&
    myProjEpoch === state.projectEpoch &&
    state.activeSessionId === targetSessionId &&
    state.selectedProjectId === targetProjectId;

  state.chatBusy = true;
  if (sendBtn) sendBtn.disabled = true;
  if (statusEl) statusEl.textContent = state.onlyAskMode ? "只问分析中…" : "处理中…";
  setError("", "");

  const intentMode = state.onlyAskMode ? "readonly" : "conversation";
  const reqBody = wireRefs.length > 0
    ? { client_request_id: reqId, text, references: wireRefs, intent_mode: intentMode }
    : { client_request_id: reqId, text, intent_mode: intentMode };

  try {
    const resp = await apiFetch(`/api/v1/sessions/${targetSessionId}/chat/turns`, {
      method: "POST",
      body: reqBody,
    });
    if (!resp.ok) {
      if (!isSameChatTarget()) {
        return;
      }
      const err = await readErrorPayload(resp);
      if (!isSameChatTarget()) {
        return;
      }
      setError(err.code, err.message);
      if (statusEl) statusEl.textContent = `发送失败 (${err.code})`;
      return;
    }
    if (state.chatDrafts.get(targetSessionId) === rawValue) {
      state.chatDrafts.delete(targetSessionId);
    }
    if (wireRefs.length > 0) {
      state.chatReferences.delete(targetSessionId);
    }
    if (!isSameChatTarget()) {
      return;
    }
    renderChatReferencesBar();
    renderDraftStatus();
    if (input && input.value === rawValue) {
      input.value = "";
    } else if (input) {
      state.chatDrafts.set(targetSessionId, input.value);
    }
    if (statusEl) statusEl.textContent = "";
    await Promise.all([
      refreshChatTurns(targetSessionId),
      refreshChatSessionTitles(targetProjectId),
    ]);
  } catch {
    if (isSameChatTarget() && statusEl) {
      statusEl.textContent = "请求异常";
    }
  } finally {
    if (mySendToken === state.chatSendToken) {
      state.chatBusy = false;
      if (sendBtn) sendBtn.disabled = false;
    }
  }
}

function renderDiffDetailBodyV4(detailEl, selected, src, isTrunc, excl) {
  const head = document.createElement("div");
  head.className = "diff-file-head";
  head.textContent = `${selected.path} · ${selected.operation} · ${src}${isTrunc ? " · 已截断" : ""}`;
  detailEl.appendChild(head);
  if (selected.diff_excerpt) {
    detailEl.appendChild(buildDiffCodeBlock(selected.diff_excerpt));
  }
  const tech = document.createElement("details");
  tech.className = "diff-tech-details";
  const sum = document.createElement("summary");
  sum.textContent = "校验与范围摘要";
  const meta = document.createElement("div");
  meta.className = "diff-tech-meta";
  meta.textContent = `path=${selected.path} | operation=${selected.operation} | source=${src} | before_sha256=${selected.before_sha256 || "null"} | after_sha256=${selected.after_sha256 || "null"} | 节选截断=${isTrunc}（以下为有界差异节选，不代表文件全文）${excl}`;
  tech.append(sum, meta);
  detailEl.appendChild(tech);
}

function buildDiffCodeBlock(excerpt) {
  const pre = document.createElement("pre");
  pre.className = "diff-code-block";
  const lines = String(excerpt || "").split("\n");
  lines.forEach((line, idx) => {
    const row = document.createElement("span");
    row.className = "diff-line";
    if (line.startsWith("+") && !line.startsWith("+++")) {
      row.classList.add("diff-line-add");
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      row.classList.add("diff-line-del");
    } else if (line.startsWith("@@")) {
      row.classList.add("diff-line-hunk");
    } else if (line.startsWith("+++") || line.startsWith("---")) {
      row.classList.add("diff-line-hdr");
    }
    row.textContent = line;
    pre.appendChild(row);
    if (idx < lines.length - 1) {
      pre.appendChild(document.createTextNode("\n"));
    }
  });
  return pre;
}

function ensureRunProposalsMap() {
  if (!state.runProposalsByKey) state.runProposalsByKey = new Map();
  if (!state.runProposalsFetching) state.runProposalsFetching = new Set();
  return state.runProposalsByKey;
}

function recordRunProposals(sessionId, runId, items) {
  if (!sessionId || !runId || !Array.isArray(items)) return;
  const map = ensureRunProposalsMap();
  const key = `${sessionId}:${runId}`;
  const byId = new Map(map.get(key) || []);
  for (const item of items) {
    const pid = item && (item.id || item.proposal_id);
    if (pid) byId.set(pid, item);
  }
  map.set(key, byId);
}

function recordRunProposalItem(sessionId, runId, item) {
  if (!sessionId || !runId || !item) return;
  const pid = item.id || item.proposal_id;
  if (!pid) return;
  const map = ensureRunProposalsMap();
  const key = `${sessionId}:${runId}`;
  const byId = new Map(map.get(key) || []);
  byId.set(pid, item);
  map.set(key, byId);
}

function formatProposalTargetBrief(prop) {
  const payload = (prop && prop.payload) || {};
  if (prop.kind === "scope") {
    const exact = Array.isArray(payload.exact_paths) ? payload.exact_paths.join(",") : "";
    const prefixes = Array.isArray(payload.write_prefixes) ? payload.write_prefixes.join(",") : "";
    return `写入范围（${exact || prefixes || "指定路径"}）`;
  }
  if (prop.kind === "verification") {
    const plan = payload.plan || {};
    const checks = Array.isArray(plan.checks) ? plan.checks.map((c) => c.check_id || "check").join("、") : "验证计划";
    return `验证计划（${checks}）`;
  }
  if (prop.kind === "command") {
    const cmd = payload.command || prop.command || {};
    const argv = Array.isArray(cmd.argv) ? cmd.argv.join(" ") : "";
    return `命令（${[cmd.executable || "", argv].filter(Boolean).join(" ").trim() || "本机命令"}）`;
  }
  return prop.kind || "提案";
}

function renderRunProposalSummaryInto(article, sessionId, runId, isTerminal) {
  if (!article || !sessionId || !runId) return;
  const map = ensureRunProposalsMap();
  const key = `${sessionId}:${runId}`;
  const byId = map.get(key);
  if (!byId && isTerminal && !state.runProposalsFetching.has(key)) {
    void ensureRunProposalsLoaded(sessionId, runId);
    return;
  }
  if (!byId || byId.size === 0) return;
  const decided = Array.from(byId.values()).filter((p) => {
    const st = p && (p.state || p.status || "");
    return st === "approved" || st === "rejected" || st === "revoked";
  });
  if (decided.length === 0) return;

  const box = document.createElement("div");
  box.className = "timeline-run-proposals";
  box.setAttribute("data-testid", `timeline-run-proposals-${runId}`);
  const labelMap = { approved: "已批准", rejected: "已拒绝", revoked: "已撤销" };
  for (const p of decided) {
    const st = p.state || p.status || "";
    const row = document.createElement("span");
    row.className = `proposal-decision-pill proposal-decision-${st}`;
    row.setAttribute("data-decision", st);
    const pid = p && (p.id || p.proposal_id);
    if (pid) {
      row.setAttribute("data-proposal-id", String(pid));
    }
    row.textContent = `${labelMap[st] || st} · ${formatProposalTargetBrief(p)}`;
    box.appendChild(row);
  }
  article.appendChild(box);
}

async function ensureRunProposalsLoaded(sessionId, runId) {
  if (!sessionId || !runId) return;
  const map = ensureRunProposalsMap();
  const key = `${sessionId}:${runId}`;
  if (map.has(key) || state.runProposalsFetching.has(key)) return;
  state.runProposalsFetching.add(key);
  const guard = { projectEpoch: state.projectEpoch, sessionEpoch: state.sessionEpoch };
  try {
    const resp = await apiFetch(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/proposals`
    );
    if (!resp.ok) return;
    const items = await resp.json();
    recordRunProposals(sessionId, runId, items);
    if (state.activeSessionId === sessionId && guard.projectEpoch === state.projectEpoch && guard.sessionEpoch === state.sessionEpoch) {
      renderChatTimeline();
    }
  } catch {
    // Ignore read-only background summary error
  } finally {
    state.runProposalsFetching.delete(key);
  }
}

function renderExecutionOfferCard(turn) {
  const offer = turn.execution_offer;
  const card = document.createElement("div");
  card.className = "timeline-offer-card";
  card.setAttribute("data-testid", `timeline-offer-${turn.turn_id}`);
  card.setAttribute("data-offer-id", offer.offer_id);
  card.setAttribute("data-offer-status", offer.status);

  const head = document.createElement("div");
  head.className = "timeline-offer-head";
  const title = document.createElement("span");
  title.textContent = offer.status === "started" ? "已确认启动执行" : "待确认执行提议";
  const badge = document.createElement("span");
  badge.className = "muted";
  badge.textContent = offer.status === "started" ? "已绑定运行" : "尚未写入或运行命令";
  head.append(title, badge);
  card.appendChild(head);

  const sum = document.createElement("div");
  sum.className = "chat-msg-text";
  sum.textContent = offer.summary;
  card.appendChild(sum);

  const goalBox = document.createElement("div");
  goalBox.className = "timeline-offer-goal";
  goalBox.setAttribute("data-testid", `offer-goal-${turn.turn_id}`);
  goalBox.textContent = `目标：${offer.goal}`;
  card.appendChild(goalBox);

  if (offer.status === "pending_confirm") {
    const label = document.createElement("label");
    label.className = "checkbox-label";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.setAttribute("data-testid", `offer-confirm-scope-${turn.turn_id}`);
    const cbText = document.createElement("span");
    cbText.textContent = "我已确认当前项目的授权边界与检查要求";
    label.append(cb, cbText);

    const actions = document.createElement("div");
    actions.className = "actions";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "primary-btn";
    btn.setAttribute("data-testid", `offer-confirm-btn-${turn.turn_id}`);
    btn.textContent = "确认执行";
    btn.addEventListener("click", () => {
      void handleConfirmOffer(turn.session_id, turn.turn_id, offer, cb, btn);
    });
    actions.appendChild(btn);
    card.append(label, actions);
  }
  return card;
}

async function handleConfirmOffer(sessionId, turnId, offer, scopeCb, btn) {
  if (!sessionId || !turnId || !offer) return;
  if (!scopeCb || !scopeCb.checked) {
    setError("SCOPE_NOT_CONFIRMED", "请先勾选确认当前项目的授权边界与检查要求。");
    return;
  }
  const lockKey = `confirm-offer:${sessionId}:${turnId}`;
  if (state.pendingLocks.has(lockKey)) return;
  state.pendingLocks.add(lockKey);
  if (btn) btn.disabled = true;
  setError("", "");
  const guard = { projectEpoch: state.projectEpoch, sessionEpoch: state.sessionEpoch };
  try {
    const resp = await apiFetch(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/chat/turns/${encodeURIComponent(turnId)}/confirm`,
      {
        method: "POST",
        body: {
          offer_id: offer.offer_id,
          confirmed_scope: true,
          selected_skill_ids: Array.from(state.selectedSkillIds || []),
        },
      }
    );
    if (!resp.ok) {
      const err = await readErrorPayload(resp);
      if (guard.projectEpoch === state.projectEpoch && guard.sessionEpoch === state.sessionEpoch) {
        setError(err.code, err.message);
      }
      return;
    }
    const runView = await resp.json();
    if (guard.projectEpoch !== state.projectEpoch || guard.sessionEpoch !== state.sessionEpoch) {
      return;
    }
    state.runEpoch += 1;
    state.activeRunId = runView.run_id;
    sessionStorage.setItem(RUN_KEY, runView.run_id);
    state.runExtensionServers = [];
    state.runSelectedSkills = [];
    state.extensionProposalsById.clear();
    state.extensionCallsById.clear();
    renderIdentityHeader();
    renderDraftStatus();
    renderRun(runView);
    const runGuard = {
      projectEpoch: guard.projectEpoch,
      sessionEpoch: guard.sessionEpoch,
      runEpoch: state.runEpoch,
      projectId: state.selectedProjectId,
      sessionId,
      runId: runView.run_id,
    };
    void (async () => {
      await Promise.all([
        refreshChatTurns(sessionId),
        refreshSessionContextAndHistory(sessionId, {
          projectEpoch: runGuard.projectEpoch,
          sessionEpoch: runGuard.sessionEpoch,
          projectId: runGuard.projectId,
          sessionId,
        }),
      ]);
      if (isEpochCurrent(runGuard)) {
        await syncSnapshotAndStream(sessionId, runView.run_id, { skipTerminalHistoryRefresh: true });
      }
    })();
  } catch {
    if (guard.projectEpoch === state.projectEpoch && guard.sessionEpoch === state.sessionEpoch) {
      setError("NETWORK_ERROR", "确认执行提议时网络异常（未自动重试）");
    }
  } finally {
    state.pendingLocks.delete(lockKey);
    if (btn) btn.disabled = false;
  }
}
