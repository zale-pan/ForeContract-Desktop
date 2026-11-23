"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { BrowserWindow, Menu, Tray, app, dialog, ipcMain, nativeImage, session } = require("electron");
const { createHostManager, sanitizeErrorMessage } = require("./host.cjs");

const SUPPORTED_LOCALES = new Set(["zh-CN", "en"]);

const SHELL_MESSAGES = {
  "zh-CN": {
    trayStarting: "ForeContract — 正在启动",
    trayDisconnected: "ForeContract — 连接中断",
    trayWaiting: (active, pending) => `ForeContract — 等待操作（运行中 ${active}，待审批 ${pending}）`,
    trayRunning: (active) => `ForeContract — 运行中（${active} 个任务）`,
    trayIdle: "ForeContract — 空闲",
    openApp: "打开 ForeContract",
    quitApp: "退出 ForeContract",
    fileMenu: "文件",
    balloonBody: "ForeContract 仍在后台运行，可从托盘重新打开或退出。",
    quitUnknownMsg: "无法确认后台宿主任务状态",
    quitUnknownDetail: "本地宿主当前状态不可确认，已保留托盘以便稍后重试或诊断。",
    keepTray: "保留托盘",
    quitActiveMsg: (active, pending) => `仍有 ${active} 个后台任务运行中（待审批 ${pending} 项）`,
    quitActiveDetail: "默认继续后台运行；选择“停止任务并退出”将先取消活动运行并等待进入终态后再关闭宿主。",
    keepRunning: "继续后台运行",
    stopAndQuit: "停止任务并退出",
    quitCheckFailed: "ForeContract 退出检查失败",
    startupFailed: "ForeContract 启动失败",
    errDefault: "本地宿主启动失败，请检查日志或配置后重试；如需本地离线测试请设置 FC_DESKTOP_LIVE_CHAT=0",
    errMissingLive: "缺少在线模型配置 (DEEPSEEK_API_KEY)，请配置后重试；如需本地离线测试请设置 FC_DESKTOP_LIVE_CHAT=0",
    errRoot: "FC_DESKTOP_ROOT 必须是存在的绝对路径",
    errPython: "FC_DESKTOP_PYTHON 必须是存在的绝对路径",
    errTimeout: "等待本地宿主启动超时，请稍后重试",
    errModeMismatch: "本地宿主模式与预期不一致，请检查启动配置后重试",
  },
  en: {
    trayStarting: "ForeContract — Starting",
    trayDisconnected: "ForeContract — Disconnected",
    trayWaiting: (active, pending) => `ForeContract — Waiting (${active} running, ${pending} pending)`,
    trayRunning: (active) => `ForeContract — Running (${active} active)`,
    trayIdle: "ForeContract — Idle",
    openApp: "Open ForeContract",
    quitApp: "Quit ForeContract",
    fileMenu: "File",
    balloonBody: "ForeContract is still running in the background. Reopen or quit from the tray.",
    quitUnknownMsg: "Unable to confirm background host task state",
    quitUnknownDetail: "The local host state could not be confirmed. The tray remains active so you can retry or diagnose later.",
    keepTray: "Keep in Tray",
    quitActiveMsg: (active, pending) => `${active} background task(s) still running (${pending} pending approval)`,
    quitActiveDetail: 'Background execution continues by default. Choose "Stop tasks and quit" to cancel active runs and wait for terminal state before shutting down the host.',
    keepRunning: "Keep running in background",
    stopAndQuit: "Stop tasks and quit",
    quitCheckFailed: "ForeContract Quit Check Failed",
    startupFailed: "ForeContract Startup Failed",
    errDefault: "Local host failed to start. Check logs or configuration and retry, or set FC_DESKTOP_LIVE_CHAT=0 for offline testing.",
    errMissingLive: "Missing online model configuration (DEEPSEEK_API_KEY). Configure it and retry, or set FC_DESKTOP_LIVE_CHAT=0 for offline testing.",
    errRoot: "FC_DESKTOP_ROOT must be an existing absolute path.",
    errPython: "FC_DESKTOP_PYTHON must be an existing absolute path.",
    errTimeout: "Timed out waiting for the local host to start. Please try again.",
    errModeMismatch: "Local host mode did not match the expected configuration. Check startup settings and retry.",
  },
};

function normalizeLocaleTag(raw) {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const lower = trimmed.toLowerCase();
  if (lower === "zh" || lower.startsWith("zh-") || lower.startsWith("zh_")) {
    return "zh-CN";
  }
  if (lower === "en" || lower.startsWith("en-") || lower.startsWith("en_")) {
    return "en";
  }
  return "en";
}

function getShellStrings(locale) {
  return SHELL_MESSAGES[locale === "zh-CN" ? "zh-CN" : "en"];
}

const rootDir = process.env.FC_DESKTOP_ROOT ? path.resolve(process.env.FC_DESKTOP_ROOT) : "";
const projectRoot = process.env.FC_DESKTOP_PROJECT ? path.resolve(process.env.FC_DESKTOP_PROJECT) : "";
const pythonExe = process.env.FC_DESKTOP_PYTHON ? path.resolve(process.env.FC_DESKTOP_PYTHON) : "";

if (!rootDir) {
  console.error("FC_DESKTOP_ROOT is required");
  app.exit(1);
}

const profileRoot = path.join(rootDir, "profile");
fs.mkdirSync(path.join(profileRoot, "userData"), { recursive: true });
fs.mkdirSync(path.join(profileRoot, "sessionData"), { recursive: true });
app.setPath("userData", path.join(profileRoot, "userData"));
app.setPath("sessionData", path.join(profileRoot, "sessionData"));

const gotSingleLock = app.requestSingleInstanceLock();
if (!gotSingleLock) {
  app.quit();
}

let mainWindow = null;
let tray = null;
let trayCreated = false;
let firstHideNotified = false;
let isQuitting = false;
let quitInProgress = false;
let statusTimer = null;
let hostManager = null;
let hostOriginBound = "";
let lastStatusSnapshot = null;

function getPrefsPath() {
  const userDataDir = typeof app.getPath === "function" ? app.getPath("userData") : path.join(profileRoot, "userData");
  return path.join(userDataDir, "desktop-prefs.json");
}

function readPersistedLocale() {
  try {
    const raw = fs.readFileSync(getPrefsPath(), "utf8");
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && SUPPORTED_LOCALES.has(parsed.locale)) {
      return parsed.locale;
    }
  } catch {
    // missing or invalid prefs falls back to system locale
  }
  return null;
}

function writePersistedLocale(locale) {
  if (!SUPPORTED_LOCALES.has(locale)) {
    return false;
  }
  const prefsPath = getPrefsPath();
  fs.mkdirSync(path.dirname(prefsPath), { recursive: true });
  fs.writeFileSync(prefsPath, JSON.stringify({ locale }, null, 2) + "\n", "utf8");
  return true;
}

function resolveInitialLocale() {
  const envLocale = normalizeLocaleTag(process.env.FC_DESKTOP_LOCALE);
  if (envLocale && SUPPORTED_LOCALES.has(envLocale)) {
    return envLocale;
  }
  const persisted = readPersistedLocale();
  if (persisted) return persisted;
  const preferred =
    typeof app.getPreferredSystemLanguages === "function" ? app.getPreferredSystemLanguages() : [];
  if (Array.isArray(preferred) && preferred.length > 0) {
    for (const lang of preferred) {
      const norm = normalizeLocaleTag(lang);
      if (norm) return norm;
    }
  }
  if (typeof app.getLocale === "function") {
    const norm = normalizeLocaleTag(app.getLocale());
    if (norm) return norm;
    return "en";
  }
  return "zh-CN";
}

let currentLocale = resolveInitialLocale();

function applyWindowMenu(win) {
  if (!win || win.isDestroyed()) return;
  const msg = getShellStrings(currentLocale);
  if (!trayCreated) {
    const fallbackMenu = Menu.buildFromTemplate([
      {
        label: msg.fileMenu,
        submenu: [{ label: msg.quitApp, click: () => void requestGracefulQuit() }],
      },
    ]);
    win.setMenu(fallbackMenu);
  } else {
    win.setMenuBarVisibility(false);
  }
}

function refreshShellLocale() {
  const msg = getShellStrings(currentLocale);
  if (tray && !tray.isDestroyed()) {
    const contextMenu = Menu.buildFromTemplate([
      {
        label: msg.openApp,
        click: () => showMainWindow(),
      },
      { type: "separator" },
      {
        label: msg.quitApp,
        click: () => void requestGracefulQuit(),
      },
    ]);
    tray.setContextMenu(contextMenu);
    if (lastStatusSnapshot) {
      updateTrayTooltip(lastStatusSnapshot);
    } else {
      tray.setToolTip(msg.trayStarting);
    }
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    applyWindowMenu(mainWindow);
  }
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.setSkipTaskbar(false);
  mainWindow.show();
  mainWindow.focus();
}

function updateTrayTooltip(snapshot) {
  lastStatusSnapshot = snapshot;
  if (!tray || tray.isDestroyed()) return;
  const msg = getShellStrings(currentLocale);
  if (!snapshot || snapshot.hostState === "disconnected") {
    tray.setToolTip(msg.trayDisconnected);
    return;
  }
  if (snapshot.hostState === "waiting") {
    tray.setToolTip(msg.trayWaiting(snapshot.activeRuns.length, snapshot.pendingCount));
    return;
  }
  if (snapshot.hostState === "running") {
    tray.setToolTip(msg.trayRunning(snapshot.activeRuns.length));
    return;
  }
  tray.setToolTip(msg.trayIdle);
}

function buildTray() {
  const iconPath = path.join(__dirname, "tray.png");
  const icon = nativeImage.createFromPath(iconPath);
  if (icon.isEmpty()) {
    return false;
  }
  try {
    const msg = getShellStrings(currentLocale);
    tray = new Tray(icon);
    const contextMenu = Menu.buildFromTemplate([
      {
        label: msg.openApp,
        click: () => showMainWindow(),
      },
      { type: "separator" },
      {
        label: msg.quitApp,
        click: () => void requestGracefulQuit(),
      },
    ]);
    tray.setToolTip(msg.trayStarting);
    tray.setContextMenu(contextMenu);
    tray.on("click", () => showMainWindow());
    trayCreated = true;
    return true;
  } catch {
    tray = null;
    trayCreated = false;
    return false;
  }
}

async function performShutdown() {
  if (statusTimer) {
    clearInterval(statusTimer);
    statusTimer = null;
  }
  isQuitting = true;
  if (hostManager) {
    try {
      await hostManager.stopHost({ timeoutMs: 10000 });
    } catch {
      // host already exited or killed on timeout
    }
  }
  if (tray && !tray.isDestroyed()) {
    tray.destroy();
    tray = null;
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.destroy();
    mainWindow = null;
  }
  app.quit();
}

async function requestGracefulQuit() {
  if (quitInProgress) return;
  quitInProgress = true;
  try {
    if (!hostManager || hostManager.getState().status !== "running") {
      await performShutdown();
      return;
    }
    let snapshot;
    try {
      snapshot = await hostManager.getStatusSnapshot();
    } catch {
      snapshot = { hostState: "disconnected", activeRuns: [], pendingCount: 0 };
    }

    if (!snapshot || !["idle", "running", "waiting"].includes(snapshot.hostState)) {
      const msg = getShellStrings(currentLocale);
      await dialog.showMessageBox(mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined, {
        type: "warning",
        title: "ForeContract",
        message: msg.quitUnknownMsg,
        detail: msg.quitUnknownDetail,
        buttons: [msg.keepTray],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
      return;
    }

    if (snapshot.activeRuns.length > 0) {
      const msg = getShellStrings(currentLocale);
      const choice = await dialog.showMessageBox(mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined, {
        type: "question",
        title: "ForeContract",
        message: msg.quitActiveMsg(snapshot.activeRuns.length, snapshot.pendingCount),
        detail: msg.quitActiveDetail,
        buttons: [msg.keepRunning, msg.stopAndQuit],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
      if (choice.response !== 1) {
        return;
      }
      await hostManager.cancelActiveRuns(snapshot.activeRuns, 10000);
    }

    await performShutdown();
  } catch (err) {
    const msg = getShellStrings(currentLocale);
    dialog.showErrorBox(msg.quitCheckFailed, sanitizeErrorMessage(err.message));
  } finally {
    quitInProgress = false;
  }
}

function createMainWindow(hostOrigin) {
  const win = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    show: true,
    title: "ForeContract",
    icon: path.join(__dirname, "tray.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  });

  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event, targetUrl) => {
    try {
      const parsed = new URL(targetUrl);
      if (parsed.origin !== hostOrigin) {
        event.preventDefault();
      }
    } catch {
      event.preventDefault();
    }
  });

  applyWindowMenu(win);

  win.on("close", (event) => {
    if (isQuitting) return;
    if (trayCreated) {
      event.preventDefault();
      win.setSkipTaskbar(true);
      win.hide();
      if (!firstHideNotified && tray && !tray.isDestroyed() && process.platform === "win32") {
        firstHideNotified = true;
        try {
          const msg = getShellStrings(currentLocale);
          tray.displayBalloon({
            title: "ForeContract",
            content: msg.balloonBody,
          });
        } catch {
          // ignore notification failures
        }
      }
    } else {
      event.preventDefault();
      void requestGracefulQuit();
    }
  });

  return win;
}

function isAuthorizedIpcEvent(event) {
  if (!event || !mainWindow || mainWindow.isDestroyed()) return false;
  if (event.sender !== mainWindow.webContents) return false;
  if (!hostOriginBound) return false;
  const senderUrl =
    event.senderFrame && typeof event.senderFrame.url === "string"
      ? event.senderFrame.url
      : typeof event.sender.getURL === "function"
        ? event.sender.getURL()
        : "";
  try {
    return new URL(senderUrl).origin === hostOriginBound;
  } catch {
    return false;
  }
}

if (ipcMain && typeof ipcMain.handle === "function") {
  ipcMain.handle("forecontract:get-locale", (event) => {
    if (!isAuthorizedIpcEvent(event)) {
      throw new Error("unauthorized_ipc_origin");
    }
    return currentLocale;
  });

  ipcMain.handle("forecontract:set-locale", (event, locale) => {
    if (!isAuthorizedIpcEvent(event)) {
      throw new Error("unauthorized_ipc_origin");
    }
    if (typeof locale !== "string" || !SUPPORTED_LOCALES.has(locale)) {
      return { ok: false, error: "unsupported_locale", locale: currentLocale };
    }
    currentLocale = locale;
    writePersistedLocale(locale);
    refreshShellLocale();
    return { ok: true, locale: currentLocale };
  });
}

if (gotSingleLock) {
  app.on("second-instance", () => {
    showMainWindow();
  });

  app.on("window-all-closed", (event) => {
    if (!isQuitting && trayCreated) {
      event.preventDefault();
    }
  });

  app.on("before-quit", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      void requestGracefulQuit();
    }
  });

  app.whenReady().then(async () => {
    try {
      const liveChat = process.env.FC_DESKTOP_LIVE_CHAT !== "0";
      hostManager = createHostManager({
        rootDir,
        projectRoot,
        pythonExe,
        liveChat,
      });
      const state = await hostManager.startHost();
      const hostOrigin = state.origin;
      hostOriginBound = hostOrigin;

      session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
        const rawUrl = details && typeof details.url === "string" ? details.url : "";
        let allowed = false;
        try {
          const parsedReq = new URL(rawUrl);
          const parsedHost = new URL(hostOrigin);
          const effectivePort = (u) => {
            if (u.port) return u.port;
            if (u.protocol === "http:") return "80";
            if (u.protocol === "https:") return "443";
            return "";
          };
          allowed =
            parsedReq.protocol === parsedHost.protocol &&
            parsedReq.hostname === parsedHost.hostname &&
            effectivePort(parsedReq) === effectivePort(parsedHost);
        } catch {
          allowed = false;
        }
        callback({ cancel: !allowed });
      });

      buildTray();
      mainWindow = createMainWindow(hostOrigin);
      await mainWindow.loadURL(hostManager.getAppUrl());

      hostManager.onUnexpectedExit(() => {
        updateTrayTooltip({ hostState: "disconnected", activeRuns: [], pendingCount: 0 });
        if (mainWindow && !mainWindow.isDestroyed()) {
          showMainWindow();
        }
      });

      const pollStatus = async () => {
        if (!hostManager || isQuitting) return;
        try {
          const snap = await hostManager.getStatusSnapshot();
          updateTrayTooltip(snap);
        } catch {
          updateTrayTooltip({ hostState: "disconnected", activeRuns: [], pendingCount: 0 });
        }
      };
      void pollStatus();
      statusTimer = setInterval(() => void pollStatus(), 2000);
      if (statusTimer && typeof statusTimer.unref === "function") {
        statusTimer.unref();
      }
    } catch (err) {
      const msg = getShellStrings(currentLocale);
      const raw = String(err && err.message ? err.message : "");
      let userMsg = msg.errDefault;
      if (raw.includes("缺少在线模型配置") || raw.includes("DEEPSEEK_API_KEY") || raw.includes("在线模式未就绪")) {
        userMsg = msg.errMissingLive;
      } else if (raw.includes("FC_DESKTOP_ROOT")) {
        userMsg = msg.errRoot;
      } else if (raw.includes("FC_DESKTOP_PYTHON")) {
        userMsg = msg.errPython;
      } else if (raw.includes("超时")) {
        userMsg = msg.errTimeout;
      } else if (raw.includes("离线模式") || raw.includes("在线模式")) {
        userMsg = msg.errModeMismatch;
      }
      dialog.showErrorBox(msg.startupFailed, sanitizeErrorMessage(userMsg));
      isQuitting = true;
      app.exit(1);
    }
  });
}
