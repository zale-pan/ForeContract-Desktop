export const SUPPORTED_LOCALES = ["zh-CN", "en"];
export const LOCALE_STORAGE_KEY = "forecontract_ui_locale";

export function normalizeLocale(raw) {
  if (typeof raw !== "string") return "en";
  const trimmed = raw.trim();
  if (!trimmed) return "en";
  const lower = trimmed.toLowerCase();
  if (lower === "zh" || lower.startsWith("zh-") || lower.startsWith("zh_")) {
    return "zh-CN";
  }
  if (lower === "en" || lower.startsWith("en-") || lower.startsWith("en_")) {
    return "en";
  }
  return "en";
}

export function detectBrowserLocale(storageKey = LOCALE_STORAGE_KEY) {
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved === "zh-CN" || saved === "en") {
      return saved;
    }
  } catch {
    // ignore storage errors
  }
  if (typeof navigator !== "undefined") {
    const langs = Array.isArray(navigator.languages) && navigator.languages.length > 0
      ? navigator.languages
      : [navigator.language || ""];
    for (const lang of langs) {
      if (typeof lang === "string" && lang.trim()) {
        return normalizeLocale(lang);
      }
    }
  }
  return "en";
}

const MESSAGES = {
  "zh-CN": {
    "app.title": "ForeContract Studio · 本地工作台",
    "nav.aria": "主导航",
    "nav.shortcutsAria": "工作区快捷入口",
    "nav.pagesAria": "工作台页面",
    "nav.toggle": "导航",
    "nav.projectLabel": "项目",
    "nav.sessions": "会话列表",
    "nav.newSessionShort": "+ 新建",
    "nav.chat": "会话",
    "nav.chatTitle": "当前会话",
    "nav.projects": "项目",
    "nav.projectsTitle": "项目",
    "nav.extensions": "扩展",
    "nav.extensionsTitle": "扩展",
    "nav.inbox": "审批收件箱",
    "nav.history": "运行历史",
    "nav.settings": "设置",
    "host.connecting": "正在连接宿主…",
    "host.checking": "正在检查",
    "host.offline": "离线",
    "host.liveReady": "在线模式（{model}）",
    "host.liveNotReady": "在线模式未就绪（{reason}）",
    "host.unreachable": "宿主连接不可用",
    "topbar.aria": "当前工作区上下文",
    "topbar.noProject": "未选择项目",
    "topbar.newChat": "新对话",
    "topbar.constraintsPill": "{count} 约束",
    "topbar.connTitle": "连接断开不等于运行失败或成功",
    "topbar.reconnect": "重连事件流",
    "topbar.expandInspector": "展开详情",
    "topbar.collapseInspector": "收起详情",
    "projects.heading": "项目与会话",
    "projects.sub": "创建受控示例或显式只读登记本地目录，按项目隔离并打开会话。",
    "projects.createTitle": "创建受控示例项目",
    "projects.nameLabel": "项目名（[a-z][a-z0-9-]{0,31}）",
    "projects.createBtn": "创建示例项目",
    "projects.registerTitle": "登记本机已有目录",
    "projects.rootLabel": "目录绝对路径（需与宿主允许根精确一致）",
    "projects.confirmRead": "确认只读登记已有项目",
    "projects.registerBtn": "登记已有项目",
    "projects.registeredHeading": "已登记项目",
    "projects.sessionsHeading": "当前项目会话（按 project_id 过滤）",
    "projects.newSessionBtn": "新建会话",
    "projects.emptyProjects": "暂无已登记项目。",
    "projects.emptySessions": "当前项目暂无会话。",
    "chat.emptyHint": "打开或登记项目后即可开始对话。",
    "chat.openProjectsBtn": "打开项目",
    "chat.pendingHeading": "待审批与运行提问",
    "chat.inputAria": "会话输入",
    "chat.inputPlaceholder": "给 ForeContract 发消息…",
    "chat.onlyAsk": "只问",
    "chat.onlyAskTitle": "仅使用只读工具回答，不提议执行",
    "chat.stopRun": "停止运行",
    "chat.sendAria": "发送消息",
    "chat.sendFailed": "发送失败 ({code})",
    "chat.noRunsOption": "（尚无运行记录）",
    "chat.execRole": "执行",
    "chat.runRecordLabel": "运行记录",
    "timeline.scopeBrief": "写入范围（{target}）",
    "timeline.verifBrief": "验证计划（{target}）",
    "timeline.cmdBrief": "命令（{target}）",
    "timeline.decisionApproved": "已批准",
    "timeline.decisionRejected": "已拒绝",
    "timeline.decisionRevoked": "已撤销",
    "chat.emptyTimeline": "暂无消息。请先发送只读问题或任务请求。",
    "chat.noPendingApprovals": "当前没有待审批提案或提问。",
    "chat.offerConfirmLabel": "我已确认当前项目的授权边界与检查要求",
    "chat.offerConfirmBtn": "确认执行",
    "chat.offerConfirmed": "已确认执行",
    "chat.userRole": "用户",
    "chat.assistantRole": "ForeContract",
    "chat.toolsUsed": "只读工具调用（{count}）",
    "chat.viewDiff": "查看 Diff",
    "chat.viewChecks": "查看验证",
    "chat.modeReadonly": "只读模式",
    "chat.modeConversation": "对话模式",
    "inspector.aria": "运行详情与工作区审阅",
    "inspector.heading": "运行详情",
    "inspector.badgeCurrent": "当前活动",
    "inspector.readonlyEvidence": "只读证据",
    "inspector.diffSummary": "校验与范围摘要",
    "run.historyActiveOption": "{id}（当前活动）",
    "run.historyReadonlyOption": "{id}（历史只读）",
    "run.modeActive": "当前活动运行",
    "run.modeReadonly": "只读查看（历史或已终态，不可提交变更）",
    "storage.quotaLine": "存储用量（SQLite sessions.sqlite3）：已用 {bytes} 字节；无自动删除或 GC，用户项目数据与临时开发证据分离",
    "storage.bytesUnit": "{bytes} 字节",
    "history.emptyFiltered": "当前已加载历史中没有匹配筛选条件的记录。",
    "history.emptyNone": "尚无历史条目。",
    "history.runLine": "run={id}{state}{goal} (条目={count})",
    "history.sessionLine": "session={id} (条目={count})",
    "history.viewRun": "查看运行",
    "ext.skillsSampleUnsupported": "受控示例项目不支持 Skill（请登记已有本机项目以查看 .forecontract/skills）。",
    "ext.skillsEmpty": "当前项目未发现本地 Skill（.forecontract/skills/<id>/SKILL.md）。",
    "inbox.rejectBtn": "拒绝",
    "inspector.badgeHistory": "只读回看",
    "inspector.close": "关闭",
    "inspector.tabsAria": "详情视图",
    "inspector.tabDiff": "运行差异",
    "inspector.tabFiles": "文件",
    "inspector.tabGit": "Git",
    "inspector.tabCommands": "命令",
    "inspector.tabChecks": "验证",
    "inspector.tabEvents": "事件",
    "inspector.diffLabel": "差异文件",
    "inspector.diffEmptyOption": "（尚无文件差异）",
    "inspector.quoteDiff": "带入对话",
    "inspector.diffEmpty": "暂无可查看的运行差异。",
    "inspector.changedPaths": "最终变更文件（changed_paths）",
    "inspector.allDiffs": "全部运行差异条目",
    "inspector.filesNote": "当前工作区磁盘文件（只读）",
    "inspector.refreshFiles": "刷新文件",
    "inspector.pathPrefix": "路径：{path}",
    "inspector.loadMoreFiles": "加载更多文件",
    "inspector.filePreviewEmpty": "选择文件查看当前磁盘文本。",
    "inspector.gitNote": "本机只读 Git 审阅（不自动轮询）",
    "inspector.refreshGit": "刷新 Git",
    "inspector.gitNotLoaded": "尚未加载 Git 状态",
    "inspector.gitIndex": "暂存区（HEAD → 暂存区）",
    "inspector.gitWorktree": "工作树（暂存区 → 磁盘）",
    "inspector.gitDiffEmpty": "选择变更路径查看对应区域补丁。",
    "inspector.commandsHeading": "命令执行与增量输出",
    "inspector.commandsEmpty": "暂无命令执行记录。",
    "inspector.checksHeading": "验证与覆盖来源",
    "inspector.checksEmpty": "暂无验证或测试覆盖记录。",
    "inspector.eventsHeading": "事件与工具反馈流",
    "inspector.eventsEmpty": "暂无事件记录。",
    "inspector.sessionId": "Session ID：",
    "inspector.runId": "Run ID：",
    "inspector.state": "状态：",
    "inspector.verification": "验收：",
    "inspector.reasonCode": "原因码：",
    "inspector.usageSummary": "用量与资源详情",
    "inbox.heading": "审批收件箱",
    "inbox.sub": "当前项目 · 活动运行（聚合全部会话当前活动运行的待审批提案与扩展调用）",
    "inbox.refresh": "刷新待办",
    "inbox.empty": "当前没有待审批的提案。",
    "inbox.summary": "待审批共 {count} 项（当前项目 {sessions} 个会话）",
    "history.heading": "运行历史",
    "history.sub": "按所选会话分页加载持久化运行记录；筛选仅作用于当前已加载记录，点击仅只读回看。",
    "history.sessionLabel": "目标会话",
    "history.runLabel": "快速切换运行",
    "history.emptyOption": "（尚无运行记录）",
    "history.filterLabel": "筛选已加载运行（任务名称 / 状态）",
    "history.filterPlaceholder": "输入关键词或终态筛选当前已加载条目",
    "history.scopeNote": "仅筛选当前会话已加载的运行记录（无全库搜索接口）。",
    "history.loadMore": "加载更多历史",
    "history.empty": "暂无历史运行记录。",
    "ext.heading": "扩展能力（Skill 与 MCP）",
    "ext.sub": "仅浏览目录与只读摘要；打开本页不拉起任何进程，MCP 连接与单次工具调用需在运行中逐项审批。",
    "ext.tabSkill": "Skill 目录",
    "ext.tabMcp": "MCP 服务器",
    "ext.skillHeading": "项目本机 Skill（选入下次启动，切换项目自动清空）",
    "ext.skillEmpty": "当前项目未发现可用 Skill。",
    "ext.mcpHeading": "宿主配置 MCP 服务器清单（只读摘要，不等同于已连接）",
    "ext.mcpEmpty": "未配置 MCP 服务器。",
    "ext.callsHeading": "当前运行实际 MCP 连接与调用结果",
    "ext.callsEmpty": "暂无 MCP 调用记录。",
    "settings.heading": "设置",
    "settings.searchPlaceholder": "搜索设置项（语言、外观、桌面托盘、模型、审批、约束、存储）...",
    "settings.searchAria": "搜索设置项（快捷键 Ctrl+K）",
    "settings.searchEmpty": "未找到匹配的设置项",
    "settings.navAria": "设置分类",
    "settings.secAppearance": "外观与编辑",
    "settings.secDesktop": "桌面与托盘",
    "settings.secModel": "模型与连接",
    "settings.secPermissions": "审批与命令",
    "settings.secConstraints": "会话约束",
    "settings.secStorage": "存储与保留",
    "settings.ovDesktop": "桌面与托盘",
    "settings.ovCloseWin": "关闭窗口",
    "settings.ovCloseWinVal": "收起到托盘",
    "settings.ovTrayAction": "托盘操作",
    "settings.ovTrayActionVal": "打开 / 退出",
    "settings.ovDiff": "Diff 与证据",
    "settings.ovDiffLoc": "展示位置",
    "settings.ovDiffLocVal": "右侧详情抽屉",
    "settings.ovQuickJump": "快捷定位",
    "settings.ovQuickJumpVal": "首屏可见",
    "settings.ovModel": "模型与连接",
    "settings.ovConnState": "连接状态",
    "settings.ovCurModel": "当前模型",
    "settings.ovStorage": "存储与保留",
    "settings.ovUsedSpace": "已用空间",
    "settings.ovUsedWait": "等待会话上下文",
    "settings.ovRetention": "保留策略",
    "settings.ovRetentionVal": "本地持久保留",
    "settings.appearanceTitle": "外观与编辑（偏好保存在本机）",
    "settings.languageLabel": "界面语言",
    "settings.languageNote": "桌面端偏好保存在 Electron 用户配置目录，不写入项目目录或会话数据库；浏览器端保存在同源本地存储。",
    "settings.themeLabel": "界面主题",
    "settings.themeLight": "冰白（light）",
    "settings.themeDark": "石墨黑（dark）",
    "settings.densityLabel": "信息密度",
    "settings.densityNormal": "标准（normal）",
    "settings.densityCompact": "紧凑（compact）",
    "settings.diffNote": "运行文件差异（Run Diff）、工作区只读文件与本机 Git 差异（暂存区 / 工作树）均通过会话工作台右侧「运行详情」证据抽屉展示，可直接在运行卡点击「查看 Diff」或顶部「展开详情」打开，并支持将选定差异块带入只读对话。",
    "settings.desktopTitle": "桌面与托盘（Windows 桌面外壳只读说明）",
    "settings.desktopNote": "当前桌面外壳由 Electron 本地宿主驱动，窗口关闭与系统托盘策略为固定受控行为（只读展示，无前端可写开关）。",
    "settings.desktopLi1": "关窗收起至系统托盘：点击主窗口右上角关闭按钮时，窗口隐藏并收起至 Windows 系统托盘，后台本地 Python 宿主与活动运行继续保持连接，不会中断正在执行的任务。",
    "settings.desktopLi2": "通过托盘恢复或退出：点击系统托盘图标或托盘菜单「打开 ForeContract」可恢复当前主窗口与同一会话；完全退出应用须通过系统托盘菜单「退出 ForeContract」（若有活动运行将先提示确认）。",
    "settings.desktopLi3": "单实例与同源边界：同一数据根保持单实例锁；桌面渲染进程仅允许访问本次宿主绑定的 127.0.0.1 loopback 源。",
    "settings.modelTitle": "模型与连接（只读宿主状态）",
    "settings.modelNote": "状态来源：本机宿主启动配置与 /api/v1/health 实时读回；API 凭据仅由宿主进程持有，不进入前端页面或浏览器偏好存储，创建会话后历史会话绑定模型保持不变。",
    "settings.permissionsTitle": "审批与命令（真实授权边界）",
    "settings.permissionsLi1": "目录范围：受控项目与本机已有项目严格隔离；已有项目从只读起步，写入须先批准精确路径与操作。",
    "settings.permissionsLi2": "逐提案审批：修改范围、验证计划、本机命令及 MCP 连接/调用分别独立审批，待审批项内联展示在会话工作台与审批收件箱。",
    "settings.permissionsLi3": "受信任本机执行：命令批准时须显式勾选 trusted_local_execution 表示受信任本机执行，而非 OS 沙箱隔离。",
    "settings.constraintsTitle": "会话约束（按当前会话持久化）",
    "settings.draftClean": "无未保存草稿（草稿未保存，刷新会丢失）",
    "settings.draftDirty": "有未保存草稿（刷新会丢失）",
    "settings.constraintsLabel": "固定约束（UTF-8 <= 8192 字节，每次当前会话只读问答与项目运行原样携带）",
    "settings.constraintsPlaceholder": "例如：禁止修改 tests/，仅在 src/ 修复并报告实际检查输出",
    "settings.saveConstraints": "保存固定约束",
    "settings.storageTitle": "存储与保留（SQLite 只读展示）",
    "settings.storageLi1": "真实用量：数据来源于当前会话 /api/v1/sessions/{sid}/context 的 storage_used_bytes（SQLite sessions.sqlite3 实际字节数），仅作只读展示。",
    "settings.storageLi2": "数据保留策略：用户项目与会话持久数据保存在本机 SQLite，与开发验收临时目录严格分离，前端不提供后台静默回收操作。",
    "proposal.approve": "批准",
    "proposal.reject": "拒绝",
    "proposal.trustedCheck": "已知悉并允许在当前受信任本机环境直接执行该命令（trusted_local_execution，非 OS 沙箱）",
    "proposal.notePlaceholder": "可选审批备注",
    "proposal.kind.scope": "写入范围",
    "proposal.kind.verification": "验证计划",
    "proposal.kind.command": "本机命令",
    "proposal.kind.extension": "扩展调用",
    "proposal.meaning.scope": "授权模型按列出的精确路径/前缀与操作写入，但不批准执行命令。",
    "proposal.meaning.verification": "授权按计划中的检查项与预期测试核对结果，每条执行命令仍需单独审批。",
    "proposal.meaning.command": "授权在当前受信任本机环境一次性执行该精确命令（非 OS 沙箱隔离）。",
    "proposal.meaning.extension": "授权当前运行调用指定的外部扩展能力。",
    "proposal.targetPaths": "目标路径：{paths}",
    "proposal.targetOps": "操作：{ops}",
    "proposal.targetChecks": "检查项：{checks}",
    "proposal.targetCommand": "命令：{cmd}",
    "proposal.pendingBadge": "待审批",
    "run.state.idle": "空闲 (idle)",
    "run.state.running": "运行中 (running)",
    "run.state.waiting": "等待审批 (waiting)",
    "run.state.succeeded": "已成功 (succeeded)",
    "run.state.failed": "已失败 (failed)",
    "run.state.cancelled": "已取消 (cancelled)",
    "run.state.unknown": "未知 (unknown)",
    "verif.not_run": "未运行",
    "verif.failed": "失败",
    "verif.passed": "通过",
    "verif.unknown": "未知",
    "err.NETWORK_ERROR": "网络请求失败",
    "err.LIVE_QUOTA_EXCEEDED": "在线额度或预算已耗尽",
    "err.LIVE_PROVIDER_INVALID": "模型响应格式无效",
    "err.LIVE_PROVIDER_TIMEOUT": "模型请求超时",
    "err.SCOPE_NOT_CONFIRMED": "请先勾选确认授权边界",
  },
  en: {
    "app.title": "ForeContract Studio · Local Workbench",
    "nav.aria": "Primary navigation",
    "nav.shortcutsAria": "Workspace shortcuts",
    "nav.pagesAria": "Workbench pages",
    "nav.toggle": "Menu",
    "nav.projectLabel": "Project",
    "nav.sessions": "Sessions",
    "nav.newSessionShort": "+ New",
    "nav.chat": "Chat",
    "nav.chatTitle": "Current Chat",
    "nav.projects": "Projects",
    "nav.projectsTitle": "Projects",
    "nav.extensions": "Extensions",
    "nav.extensionsTitle": "Extensions",
    "nav.inbox": "Approval Inbox",
    "nav.history": "Run History",
    "nav.settings": "Settings",
    "host.connecting": "Connecting to host…",
    "host.checking": "Checking",
    "host.offline": "Offline",
    "host.liveReady": "Online ({model})",
    "host.liveNotReady": "Online not ready ({reason})",
    "host.unreachable": "Host unavailable",
    "topbar.aria": "Current workspace context",
    "topbar.noProject": "No project selected",
    "topbar.newChat": "New Chat",
    "topbar.constraintsPill": "{count} constraints",
    "topbar.connTitle": "Stream disconnection does not mean a run failed or succeeded",
    "topbar.reconnect": "Reconnect stream",
    "topbar.expandInspector": "Show Details",
    "topbar.collapseInspector": "Hide Details",
    "projects.heading": "Projects & Sessions",
    "projects.sub": "Create a controlled sample or explicitly register a local directory read-only, isolated per project.",
    "projects.createTitle": "Create Controlled Sample Project",
    "projects.nameLabel": "Project name ([a-z][a-z0-9-]{0,31})",
    "projects.createBtn": "Create Sample Project",
    "projects.registerTitle": "Register Existing Local Directory",
    "projects.rootLabel": "Absolute directory path (must match an allowed host root)",
    "projects.confirmRead": "Confirm read-only registration of existing project",
    "projects.registerBtn": "Register Existing Project",
    "projects.registeredHeading": "Registered Projects",
    "projects.sessionsHeading": "Project Sessions (filtered by project_id)",
    "projects.newSessionBtn": "New Session",
    "projects.emptyProjects": "No registered projects yet.",
    "projects.emptySessions": "No sessions for this project yet.",
    "chat.emptyHint": "Open or register a project to start a conversation.",
    "chat.openProjectsBtn": "Open Projects",
    "chat.pendingHeading": "Pending Approvals & Run Questions",
    "chat.inputAria": "Chat input",
    "chat.inputPlaceholder": "Message ForeContract…",
    "chat.onlyAsk": "Ask Only",
    "chat.onlyAskTitle": "Answer with read-only tools only; do not propose execution",
    "chat.stopRun": "Stop Run",
    "chat.sendAria": "Send message",
    "chat.sendFailed": "Send failed ({code})",
    "chat.noRunsOption": "(No runs yet)",
    "chat.execRole": "Execution",
    "chat.runRecordLabel": "Run Record",
    "timeline.scopeBrief": "Write Scope ({target})",
    "timeline.verifBrief": "Verification Plan ({target})",
    "timeline.cmdBrief": "Command ({target})",
    "timeline.decisionApproved": "Approved",
    "timeline.decisionRejected": "Rejected",
    "timeline.decisionRevoked": "Revoked",
    "chat.emptyTimeline": "No messages yet. Ask a read-only question or request a task.",
    "chat.noPendingApprovals": "No pending proposals or questions.",
    "chat.offerConfirmLabel": "I confirm the authorization boundary and verification requirements for this project",
    "chat.offerConfirmBtn": "Confirm & Execute",
    "chat.offerConfirmed": "Execution confirmed",
    "chat.userRole": "User",
    "chat.assistantRole": "ForeContract",
    "chat.toolsUsed": "Read-only tool calls ({count})",
    "chat.viewDiff": "View Diff",
    "chat.viewChecks": "View Checks",
    "chat.modeReadonly": "Read-only mode",
    "chat.modeConversation": "Conversation mode",
    "inspector.aria": "Run details and workspace review",
    "inspector.heading": "Run Details",
    "inspector.badgeCurrent": "Active",
    "inspector.readonlyEvidence": "Read-only evidence",
    "inspector.diffSummary": "Verification & Scope Summary",
    "run.historyActiveOption": "{id} (Active)",
    "run.historyReadonlyOption": "{id} (Read-only history)",
    "run.modeActive": "Active run",
    "run.modeReadonly": "Read-only view (historical or terminal; mutations disabled)",
    "storage.quotaLine": "Storage (SQLite sessions.sqlite3): {bytes} bytes used; no automatic deletion or GC, user project data separated from temporary dev evidence",
    "storage.bytesUnit": "{bytes} bytes",
    "history.emptyFiltered": "No loaded history items match the current filter.",
    "history.emptyNone": "No history items yet.",
    "history.runLine": "run={id}{state}{goal} (items={count})",
    "history.sessionLine": "session={id} (items={count})",
    "history.viewRun": "View Run",
    "ext.skillsSampleUnsupported": "Controlled sample projects do not support Skills (register an existing local project to view .forecontract/skills).",
    "ext.skillsEmpty": "No local Skills found in this project (.forecontract/skills/<id>/SKILL.md).",
    "inbox.rejectBtn": "Reject",
    "inspector.badgeHistory": "Read-only replay",
    "inspector.close": "Close",
    "inspector.tabsAria": "Details tabs",
    "inspector.tabDiff": "Run Diff",
    "inspector.tabFiles": "Files",
    "inspector.tabGit": "Git",
    "inspector.tabCommands": "Commands",
    "inspector.tabChecks": "Verification",
    "inspector.tabEvents": "Events",
    "inspector.diffLabel": "Diff file",
    "inspector.diffEmptyOption": "(no file diffs yet)",
    "inspector.quoteDiff": "Quote in Chat",
    "inspector.diffEmpty": "No run diffs available yet.",
    "inspector.changedPaths": "Changed Paths (changed_paths)",
    "inspector.allDiffs": "All Run Diff Entries",
    "inspector.filesNote": "Current workspace files on disk (read-only)",
    "inspector.refreshFiles": "Refresh Files",
    "inspector.pathPrefix": "Path: {path}",
    "inspector.loadMoreFiles": "Load More Files",
    "inspector.filePreviewEmpty": "Select a file to preview disk contents.",
    "inspector.gitNote": "Local read-only Git review (no auto-polling)",
    "inspector.refreshGit": "Refresh Git",
    "inspector.gitNotLoaded": "Git status not loaded yet",
    "inspector.gitIndex": "Staged (HEAD -> Index)",
    "inspector.gitWorktree": "Working Tree (Index -> Disk)",
    "inspector.gitDiffEmpty": "Select a changed path to inspect its patch.",
    "inspector.commandsHeading": "Command Execution & Output",
    "inspector.commandsEmpty": "No commands executed yet.",
    "inspector.checksHeading": "Verification & Coverage Sources",
    "inspector.checksEmpty": "No verification checks recorded yet.",
    "inspector.eventsHeading": "Events & Tool Feedback",
    "inspector.eventsEmpty": "No events recorded yet.",
    "inspector.sessionId": "Session ID:",
    "inspector.runId": "Run ID:",
    "inspector.state": "State:",
    "inspector.verification": "Verification:",
    "inspector.reasonCode": "Reason Code:",
    "inspector.usageSummary": "Usage & Resources",
    "inbox.heading": "Approval Inbox",
    "inbox.sub": "Current project · active runs (pending proposals and extension calls across sessions)",
    "inbox.refresh": "Refresh Inbox",
    "inbox.empty": "No pending proposals.",
    "inbox.summary": "{count} pending item(s) across {sessions} session(s) in this project",
    "history.heading": "Run History",
    "history.sub": "Paginated persistent runs for the selected session; filtering applies to loaded items only.",
    "history.sessionLabel": "Target session",
    "history.runLabel": "Quick switch run",
    "history.emptyOption": "(no runs yet)",
    "history.filterLabel": "Filter loaded runs (goal / state)",
    "history.filterPlaceholder": "Filter loaded runs by keyword or terminal state",
    "history.scopeNote": "Filters only the runs already loaded for this session (no global DB search).",
    "history.loadMore": "Load More History",
    "history.empty": "No run history yet.",
    "ext.heading": "Extensions (Skills & MCP)",
    "ext.sub": "Read-only catalog & summary; opening this page starts no processes. MCP connections and calls require per-action approval.",
    "ext.tabSkill": "Skill Catalog",
    "ext.tabMcp": "MCP Servers",
    "ext.skillHeading": "Project Skills (selected for next run; cleared on project switch)",
    "ext.skillEmpty": "No project skills found.",
    "ext.mcpHeading": "Configured MCP Servers (read-only summary, not live connections)",
    "ext.mcpEmpty": "No MCP servers configured.",
    "ext.callsHeading": "Current Run MCP Connections & Calls",
    "ext.callsEmpty": "No MCP calls recorded yet.",
    "settings.heading": "Settings",
    "settings.searchPlaceholder": "Search settings (language, appearance, tray, model, approvals, constraints, storage)...",
    "settings.searchAria": "Search settings (Ctrl+K)",
    "settings.searchEmpty": "No matching settings found",
    "settings.navAria": "Settings sections",
    "settings.secAppearance": "Appearance & Editor",
    "settings.secDesktop": "Desktop & Tray",
    "settings.secModel": "Model & Connection",
    "settings.secPermissions": "Approvals & Commands",
    "settings.secConstraints": "Session Constraints",
    "settings.secStorage": "Storage & Retention",
    "settings.ovDesktop": "Desktop & Tray",
    "settings.ovCloseWin": "Close window",
    "settings.ovCloseWinVal": "Minimize to tray",
    "settings.ovTrayAction": "Tray actions",
    "settings.ovTrayActionVal": "Open / Quit",
    "settings.ovDiff": "Diff & Evidence",
    "settings.ovDiffLoc": "Location",
    "settings.ovDiffLocVal": "Right details drawer",
    "settings.ovQuickJump": "Quick jump",
    "settings.ovQuickJumpVal": "Visible on main view",
    "settings.ovModel": "Model & Connection",
    "settings.ovConnState": "Connection",
    "settings.ovCurModel": "Current model",
    "settings.ovStorage": "Storage & Retention",
    "settings.ovUsedSpace": "Used space",
    "settings.ovUsedWait": "Waiting for session context",
    "settings.ovRetention": "Retention",
    "settings.ovRetentionVal": "Persisted locally",
    "settings.appearanceTitle": "Appearance & Language",
    "settings.languageLabel": "Interface language",
    "settings.languageNote": "Desktop preference is saved in the Electron user profile (outside project and session databases); web browser preference is saved in same-origin localStorage.",
    "settings.themeLabel": "Theme",
    "settings.themeLight": "Ice White (light)",
    "settings.themeDark": "Graphite Dark (dark)",
    "settings.densityLabel": "Density",
    "settings.densityNormal": "Normal",
    "settings.densityCompact": "Compact",
    "settings.diffNote": "Run diffs, read-only workspace files, and local Git diffs (staged / working tree) appear in the right-hand Run Details drawer. Click View Diff on a run card or Show Details in the top bar.",
    "settings.desktopTitle": "Desktop & Tray (Windows Shell Read-only Info)",
    "settings.desktopNote": "The desktop shell is driven by the Electron local host. Window close and tray behavior are fixed controlled policies.",
    "settings.desktopLi1": "Close to tray: clicking the window close button hides the window to the Windows system tray while the local Python host and active runs stay connected.",
    "settings.desktopLi2": "Restore or quit from tray: click the tray icon or choose Open ForeContract to restore the window; choose Quit ForeContract from the tray menu to shut down cleanly.",
    "settings.desktopLi3": "Single instance & origin boundary: one instance per data root; renderer access is restricted to the bound 127.0.0.1 loopback host origin.",
    "settings.modelTitle": "Model & Connection (Read-only Host State)",
    "settings.modelNote": "State is read from host startup config and /api/v1/health. API credentials stay in the host process and never enter browser storage.",
    "settings.permissionsTitle": "Approvals & Commands (Authorization Boundary)",
    "settings.permissionsLi1": "Directory scope: controlled sample projects and existing local directories are strictly isolated; existing projects start read-only until write scope is approved.",
    "settings.permissionsLi2": "Per-proposal approval: write scope, verification plans, local commands, and MCP calls are each approved independently.",
    "settings.permissionsLi3": "Trusted local execution: approving a command requires checking trusted_local_execution (not OS sandbox isolation).",
    "settings.constraintsTitle": "Session Constraints (Persisted per Session)",
    "settings.draftClean": "No unsaved draft (unsaved drafts are lost on refresh)",
    "settings.draftDirty": "Unsaved draft (will be lost on refresh)",
    "settings.constraintsLabel": "Fixed constraints (UTF-8 <= 8192 bytes, carried verbatim on read-only chat and runs)",
    "settings.constraintsPlaceholder": "e.g., Do not modify tests/; fix only in src/ and report actual check output",
    "settings.saveConstraints": "Save Constraints",
    "settings.storageTitle": "Storage & Retention (SQLite Read-only)",
    "settings.storageLi1": "Actual usage: read from /api/v1/sessions/{sid}/context storage_used_bytes (sessions.sqlite3 size on disk).",
    "settings.storageLi2": "Retention policy: project and session data are stored in local SQLite, separate from temporary verification directories.",
    "proposal.approve": "Approve",
    "proposal.reject": "Reject",
    "proposal.trustedCheck": "I understand and allow running this exact command in the trusted local environment (trusted_local_execution, not OS-sandboxed)",
    "proposal.notePlaceholder": "Optional approval note",
    "proposal.kind.scope": "Write Scope",
    "proposal.kind.verification": "Verification Plan",
    "proposal.kind.command": "Local Command",
    "proposal.kind.extension": "Extension Call",
    "proposal.meaning.scope": "Authorizes the model to write only the listed exact paths/prefixes and operations; does not approve commands.",
    "proposal.meaning.verification": "Authorizes checking results against these checks and expected tests; each local command still needs separate approval.",
    "proposal.meaning.command": "Authorizes one-time execution of this exact command in the trusted local environment (not OS-sandboxed).",
    "proposal.meaning.extension": "Authorizes this run to invoke the specified extension capability.",
    "proposal.targetPaths": "Target paths: {paths}",
    "proposal.targetOps": "Operations: {ops}",
    "proposal.targetChecks": "Checks: {checks}",
    "proposal.targetCommand": "Command: {cmd}",
    "proposal.pendingBadge": "Pending Approval",
    "run.state.idle": "Idle (idle)",
    "run.state.running": "Running (running)",
    "run.state.waiting": "Waiting Approval (waiting)",
    "run.state.succeeded": "Succeeded (succeeded)",
    "run.state.failed": "Failed (failed)",
    "run.state.cancelled": "Cancelled (cancelled)",
    "run.state.unknown": "Unknown (unknown)",
    "verif.not_run": "Not run",
    "verif.failed": "Failed",
    "verif.passed": "Passed",
    "verif.unknown": "Unknown",
    "err.NETWORK_ERROR": "Network request failed",
    "err.LIVE_QUOTA_EXCEEDED": "Online quota or budget exhausted",
    "err.LIVE_PROVIDER_INVALID": "Provider returned an invalid response",
    "err.LIVE_PROVIDER_TIMEOUT": "Provider request timed out",
    "err.SCOPE_NOT_CONFIRMED": "Please confirm the authorization boundary first",
  },
};

export function t(locale, key, params = {}) {
  const lang = locale === "zh-CN" ? "zh-CN" : "en";
  const dict = MESSAGES[lang] || MESSAGES.en;
  let template = Object.prototype.hasOwnProperty.call(dict, key)
    ? dict[key]
    : Object.prototype.hasOwnProperty.call(MESSAGES.en, key)
      ? MESSAGES.en[key]
      : key;
  for (const [k, v] of Object.entries(params)) {
    template = template.replaceAll(`{${k}}`, String(v ?? ""));
  }
  return template;
}

export function formatRunState(locale, rawState) {
  const s = String(rawState || "idle");
  const key = `run.state.${s}`;
  const dict = MESSAGES[locale === "zh-CN" ? "zh-CN" : "en"];
  return Object.prototype.hasOwnProperty.call(dict, key) ? t(locale, key) : s;
}

export function formatVerificationStatus(locale, rawStatus) {
  const s = String(rawStatus || "not_run");
  const key = `verif.${s}`;
  const dict = MESSAGES[locale === "zh-CN" ? "zh-CN" : "en"];
  return Object.prototype.hasOwnProperty.call(dict, key) ? t(locale, key) : s;
}

export function formatProposalKind(locale, kind) {
  const k = String(kind || "");
  const key = `proposal.kind.${k}`;
  const dict = MESSAGES[locale === "zh-CN" ? "zh-CN" : "en"];
  return Object.prototype.hasOwnProperty.call(dict, key) ? t(locale, key) : k;
}

export function formatApprovalMeaning(locale, kind) {
  const k = String(kind || "");
  const key = `proposal.meaning.${k}`;
  const dict = MESSAGES[locale === "zh-CN" ? "zh-CN" : "en"];
  return Object.prototype.hasOwnProperty.call(dict, key) ? t(locale, key) : "";
}

export function formatErrorCode(locale, rawCode) {
  const c = String(rawCode || "");
  const key = `err.${c}`;
  const dict = MESSAGES[locale === "zh-CN" ? "zh-CN" : "en"];
  return Object.prototype.hasOwnProperty.call(dict, key) ? t(locale, key) : c;
}

export function getPageNames(locale) {
  return {
    chat: t(locale, "nav.chat"),
    projects: t(locale, "nav.projects"),
    extensions: t(locale, "nav.extensions"),
    inbox: t(locale, "nav.inbox"),
    history: t(locale, "nav.history"),
    settings: t(locale, "nav.settings"),
  };
}

export function getSettingsCatalog(locale) {
  if (locale === "en") {
    return [
      {
        section: "appearance",
        title: "Appearance & Editor",
        summary: "Interface language (简体中文 / English), theme (light / dark), density, and Run Diff drawer info",
        keywords: "language locale english chinese appearance theme light dark density normal compact diff drawer",
        focusId: "locale-select",
      },
      {
        section: "desktop",
        title: "Desktop & Tray",
        summary: "Read-only info: minimize to tray on close, restore or quit from tray menu",
        keywords: "desktop tray minimize close background quit single instance electron",
        focusId: "settings-panel-desktop",
      },
      {
        section: "model",
        title: "Model & Connection",
        summary: "Host configuration source, offline stub, and controlled online mode status",
        keywords: "model connection host offline online health deepseek provider",
        focusId: "settings-panel-model",
      },
      {
        section: "permissions",
        title: "Approvals & Commands",
        summary: "Per-proposal approval and trusted_local_execution boundary (not OS-sandboxed)",
        keywords: "permissions approvals commands scope verification proposal trusted_local_execution",
        focusId: "settings-panel-permissions",
      },
      {
        section: "constraints",
        title: "Session Constraints",
        summary: "Persistent fixed session constraints with expected_revision conflict protection",
        keywords: "constraints session revision conflict draft utf-8 8192",
        focusId: "constraints-input",
      },
      {
        section: "storage",
        title: "Storage & Retention",
        summary: "SQLite (sessions.sqlite3) actual byte usage and local retention policy",
        keywords: "storage retention data usage bytes sqlite sessions.sqlite3",
        focusId: "settings-panel-storage",
      },
    ];
  }
  return [
    {
      section: "appearance",
      title: "外观与编辑",
      summary: "界面语言（简体中文 / English）、主题（冰白 / 石墨黑）、信息密度与证据抽屉说明",
      keywords: "language locale 语言 中文 english appearance 外观 编辑 主题 冰白 石墨黑 密度 标准 紧凑 diff",
      focusId: "locale-select",
    },
    {
      section: "desktop",
      title: "桌面与托盘",
      summary: "只读说明：关窗收起至系统托盘保持后台任务，通过系统托盘菜单恢复或退出",
      keywords: "desktop 桌面 托盘 系统托盘 关窗 后台 退出 单实例 tray close exit electron",
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
}

export function applyStaticTranslations(doc, locale) {
  if (!doc) return;
  const lang = locale === "zh-CN" ? "zh-CN" : "en";
  if (doc.documentElement) {
    doc.documentElement.lang = lang;
  }
  doc.title = t(lang, "app.title");

  for (const node of doc.querySelectorAll("[data-i18n]")) {
    const key = node.getAttribute("data-i18n");
    if (key) node.textContent = t(lang, key);
  }
  for (const node of doc.querySelectorAll("[data-i18n-placeholder]")) {
    const key = node.getAttribute("data-i18n-placeholder");
    if (key) node.setAttribute("placeholder", t(lang, key));
  }
  for (const node of doc.querySelectorAll("[data-i18n-title]")) {
    const key = node.getAttribute("data-i18n-title");
    if (key) node.setAttribute("title", t(lang, key));
  }
  for (const node of doc.querySelectorAll("[data-i18n-aria-label]")) {
    const key = node.getAttribute("data-i18n-aria-label");
    if (key) node.setAttribute("aria-label", t(lang, key));
  }
  const localeSel = doc.getElementById("locale-select");
  if (localeSel) {
    localeSel.value = lang;
  }
}
