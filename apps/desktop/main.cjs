"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { BrowserWindow, Menu, Tray, app, dialog, nativeImage, session } = require("electron");
const { createHostManager, sanitizeErrorMessage } = require("./host.cjs");

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
  if (!tray || tray.isDestroyed()) return;
  if (!snapshot || snapshot.hostState === "disconnected") {
    tray.setToolTip("ForeContract — 连接中断");
    return;
  }
  if (snapshot.hostState === "waiting") {
    tray.setToolTip(
      `ForeContract — 等待操作（运行中 ${snapshot.activeRuns.length}，待审批 ${snapshot.pendingCount}）`
    );
    return;
  }
  if (snapshot.hostState === "running") {
    tray.setToolTip(`ForeContract — 运行中（${snapshot.activeRuns.length} 个任务）`);
    return;
  }
  tray.setToolTip("ForeContract — 空闲");
}

function buildTray() {
  const iconPath = path.join(__dirname, "tray.png");
  const icon = nativeImage.createFromPath(iconPath);
  if (icon.isEmpty()) {
    return false;
  }
  try {
    tray = new Tray(icon);
    const contextMenu = Menu.buildFromTemplate([
      {
        label: "打开 ForeContract",
        click: () => showMainWindow(),
      },
      { type: "separator" },
      {
        label: "退出 ForeContract",
        click: () => void requestGracefulQuit(),
      },
    ]);
    tray.setToolTip("ForeContract — 正在启动");
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
      await dialog.showMessageBox(mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined, {
        type: "warning",
        title: "ForeContract",
        message: "无法确认后台宿主任务状态",
        detail: "本地宿主当前状态不可确认，已保留托盘以便稍后重试或诊断。",
        buttons: ["保留托盘"],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
      return;
    }

    if (snapshot.activeRuns.length > 0) {
      const choice = await dialog.showMessageBox(mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined, {
        type: "question",
        title: "ForeContract",
        message: `仍有 ${snapshot.activeRuns.length} 个后台任务运行中（待审批 ${snapshot.pendingCount} 项）`,
        detail: "默认继续后台运行；选择“停止任务并退出”将先取消活动运行并等待进入终态后再关闭宿主。",
        buttons: ["继续后台运行", "停止任务并退出"],
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
    dialog.showErrorBox("ForeContract 退出检查失败", sanitizeErrorMessage(err.message));
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

  if (!trayCreated) {
    // Fallback visible exit menu when system tray could not be created
    const fallbackMenu = Menu.buildFromTemplate([
      {
        label: "文件",
        submenu: [{ label: "退出 ForeContract", click: () => void requestGracefulQuit() }],
      },
    ]);
    win.setMenu(fallbackMenu);
  } else {
    win.setMenuBarVisibility(false);
  }

  win.on("close", (event) => {
    if (isQuitting) return;
    if (trayCreated) {
      event.preventDefault();
      win.setSkipTaskbar(true);
      win.hide();
      if (!firstHideNotified && tray && !tray.isDestroyed() && process.platform === "win32") {
        firstHideNotified = true;
        try {
          tray.displayBalloon({
            title: "ForeContract",
            content: "ForeContract 仍在后台运行，可从托盘重新打开或退出。",
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
    } catch (err) {
      const raw = String(err && err.message ? err.message : "");
      let userMsg = "本地宿主启动失败，请检查日志或配置后重试；如需本地离线测试请设置 FC_DESKTOP_LIVE_CHAT=0";
      if (raw.includes("缺少在线模型配置") || raw.includes("DEEPSEEK_API_KEY") || raw.includes("在线模式未就绪")) {
        userMsg = "缺少在线模型配置 (DEEPSEEK_API_KEY)，请配置后重试；如需本地离线测试请设置 FC_DESKTOP_LIVE_CHAT=0";
      } else if (raw.includes("FC_DESKTOP_ROOT")) {
        userMsg = "FC_DESKTOP_ROOT 必须是存在的绝对路径";
      } else if (raw.includes("FC_DESKTOP_PYTHON")) {
        userMsg = "FC_DESKTOP_PYTHON 必须是存在的绝对路径";
      } else if (raw.includes("超时")) {
        userMsg = "等待本地宿主启动超时，请稍后重试";
      } else if (raw.includes("离线模式") || raw.includes("在线模式")) {
        userMsg = "本地宿主模式与预期不一致，请检查启动配置后重试";
      }
      dialog.showErrorBox("ForeContract 启动失败", sanitizeErrorMessage(userMsg));
      isQuitting = true;
      app.exit(1);
    }
  });
}
