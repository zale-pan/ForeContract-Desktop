"use strict";

const assert = require("node:assert/strict");
const Module = require("node:module");
const path = require("node:path");
const { test } = require("node:test");

const { sanitizeErrorMessage } = require("../../apps/desktop/host.cjs");

async function loadRealMainWithHarness({
  hostOrigin = "http://127.0.0.1:4100",
  statusSteps = [],
  liveChatEnv = undefined,
  startHostError = null,
} = {}) {
  const mainPath = path.resolve(__dirname, "../../apps/desktop/main.cjs");
  const hostPath = path.resolve(__dirname, "../../apps/desktop/host.cjs");

  const calls = {
    statusQueries: 0,
    stopHost: 0,
    cancelActiveRuns: 0,
    trayDestroy: 0,
    winDestroy: 0,
    appQuit: 0,
    messageBoxes: [],
    errorBoxes: [],
    exitCodes: [],
    hostManagerOptions: [],
  };
  let menuTemplate = null;
  let onBeforeRequestHandler = null;
  let readyPromise = null;

  class FakeTray {
    setToolTip() {}
    setContextMenu() {}
    on() {}
    destroy() {
      calls.trayDestroy += 1;
    }
    isDestroyed() {
      return false;
    }
  }

  class FakeBrowserWindow {
    constructor() {
      this.webContents = {
        setWindowOpenHandler() {},
        on() {},
      };
    }
    setMenuBarVisibility() {}
    on() {}
    async loadURL() {}
    isDestroyed() {
      return false;
    }
    destroy() {
      calls.winDestroy += 1;
    }
  }

  const fakeElectron = {
    BrowserWindow: FakeBrowserWindow,
    Menu: {
      buildFromTemplate(tpl) {
        menuTemplate = tpl;
        return tpl;
      },
    },
    Tray: FakeTray,
    app: {
      setPath() {},
      requestSingleInstanceLock() {
        return true;
      },
      on() {},
      whenReady() {
        return {
          then(fn) {
            readyPromise = Promise.resolve().then(fn);
            return readyPromise;
          },
        };
      },
      quit() {
        calls.appQuit += 1;
      },
      exit(code) {
        calls.exitCodes.push(code);
      },
    },
    dialog: {
      async showMessageBox(_win, opts) {
        calls.messageBoxes.push(opts);
        return { response: 0 };
      },
      showErrorBox(title, content) {
        calls.errorBoxes.push({ title, content });
      },
    },
    nativeImage: {
      createFromPath() {
        return {
          isEmpty() {
            return false;
          },
        };
      },
    },
    session: {
      defaultSession: {
        webRequest: {
          onBeforeRequest(handler) {
            onBeforeRequestHandler = handler;
          },
        },
      },
    },
  };

  let stepIdx = 0;
  const fakeHostModule = {
    sanitizeErrorMessage,
    createHostManager(opts) {
      calls.hostManagerOptions.push(opts);
      return {
        async startHost() {
          if (startHostError) {
            throw startHostError;
          }
          return { status: "running", origin: hostOrigin, port: Number(new URL(hostOrigin).port) };
        },
        getState() {
          return { status: "running", origin: hostOrigin };
        },
        getAppUrl() {
          return `${hostOrigin}/#token=mem-only`;
        },
        onUnexpectedExit() {
          return () => {};
        },
        async getStatusSnapshot() {
          calls.statusQueries += 1;
          const step = statusSteps[Math.min(stepIdx, Math.max(0, statusSteps.length - 1))];
          stepIdx += 1;
          if (typeof step === "function") {
            return await step();
          }
          return step || { hostState: "idle", activeRuns: [], pendingCount: 0 };
        },
        async cancelActiveRuns() {
          calls.cancelActiveRuns += 1;
        },
        async stopHost() {
          calls.stopHost += 1;
          return { code: 0, signal: null };
        },
      };
    },
  };

  const fakeFs = {
    mkdirSync() {},
  };

  const prevRoot = process.env.FC_DESKTOP_ROOT;
  const prevLiveChat = process.env.FC_DESKTOP_LIVE_CHAT;
  process.env.FC_DESKTOP_ROOT = path.resolve(__dirname, "../../apps/desktop");
  if (liveChatEnv === undefined) {
    delete process.env.FC_DESKTOP_LIVE_CHAT;
  } else {
    process.env.FC_DESKTOP_LIVE_CHAT = liveChatEnv;
  }

  const origLoad = Module._load;
  const origSetInterval = globalThis.setInterval;
  globalThis.setInterval = (...args) => {
    const t = origSetInterval(...args);
    if (t && typeof t.unref === "function") t.unref();
    return t;
  };
  delete require.cache[mainPath];
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return fakeElectron;
    if (request === "node:fs" || request === "fs") return fakeFs;
    if (parent && parent.filename === mainPath && request === "./host.cjs") return fakeHostModule;
    if (path.resolve(parent?.filename ? path.dirname(parent.filename) : "", request) === hostPath) {
      return fakeHostModule;
    }
    return origLoad.call(this, request, parent, isMain);
  };

  try {
    require(mainPath);
    await readyPromise;
  } finally {
    globalThis.setInterval = origSetInterval;
    Module._load = origLoad;
    delete require.cache[mainPath];
    if (prevRoot === undefined) {
      delete process.env.FC_DESKTOP_ROOT;
    } else {
      process.env.FC_DESKTOP_ROOT = prevRoot;
    }
    if (prevLiveChat === undefined) {
      delete process.env.FC_DESKTOP_LIVE_CHAT;
    } else {
      process.env.FC_DESKTOP_LIVE_CHAT = prevLiveChat;
    }
  }

  if (startHostError) {
    return { calls, quitItem: null, onBeforeRequestHandler: null };
  }

  const quitItem = Array.isArray(menuTemplate)
    ? menuTemplate.find((it) => it && it.label === "退出 ForeContract")
    : null;
  assert.ok(quitItem && typeof quitItem.click === "function", "expected real tray quit handler");
  assert.equal(typeof onBeforeRequestHandler, "function", "expected real onBeforeRequest handler");

  return { calls, quitItem, onBeforeRequestHandler };
}

test("F01 unknown host keeps tray", async () => {
  const { calls, quitItem } = await loadRealMainWithHarness({
    statusSteps: [
      { hostState: "idle", activeRuns: [], pendingCount: 0 }, // initial pollStatus
      () => {
        throw new Error("snapshot fetch failed");
      },
      { hostState: "disconnected", activeRuns: [], pendingCount: 0 },
      { hostState: "idle", activeRuns: [], pendingCount: 0 },
    ],
  });

  const invokeTrayQuitAndWait = async () => {
    quitItem.click();
    for (let i = 0; i < 20; i += 1) {
      await new Promise((r) => setTimeout(r, 10));
    }
  };

  const queriesBefore = calls.statusQueries;

  // 1) Status query throws -> keep tray only, no shutdown/cancel/quit
  await invokeTrayQuitAndWait();
  assert.equal(calls.statusQueries, queriesBefore + 1);
  assert.equal(calls.messageBoxes.length, 1);
  assert.deepEqual(calls.messageBoxes[0].buttons, ["保留托盘"]);
  assert.ok(!JSON.stringify(calls.messageBoxes[0]).includes("强制退出"));
  assert.equal(calls.cancelActiveRuns, 0);
  assert.equal(calls.stopHost, 0);
  assert.equal(calls.trayDestroy, 0);
  assert.equal(calls.winDestroy, 0);
  assert.equal(calls.appQuit, 0);

  // 2) Status returns disconnected -> still keep tray only, re-queries cleanly
  await invokeTrayQuitAndWait();
  assert.equal(calls.statusQueries, queriesBefore + 2);
  assert.equal(calls.messageBoxes.length, 2);
  assert.deepEqual(calls.messageBoxes[1].buttons, ["保留托盘"]);
  assert.equal(calls.cancelActiveRuns, 0);
  assert.equal(calls.stopHost, 0);
  assert.equal(calls.trayDestroy, 0);
  assert.equal(calls.winDestroy, 0);
  assert.equal(calls.appQuit, 0);

  // 3) Subsequent idle status on same handler -> normal shutdown calls stopHost once
  await invokeTrayQuitAndWait();
  assert.equal(calls.statusQueries, queriesBefore + 3);
  assert.equal(calls.stopHost, 1);
  assert.equal(calls.trayDestroy, 1);
  assert.equal(calls.winDestroy, 1);
  assert.equal(calls.appQuit, 1);
});

test("F02 subresource origin", async () => {
  const { onBeforeRequestHandler } = await loadRealMainWithHarness({
    hostOrigin: "http://127.0.0.1:4100",
  });

  const checkUrl = (url) => {
    const seen = [];
    onBeforeRequestHandler({ url }, (res) => seen.push(res));
    assert.equal(seen.length, 1, `expected exactly 1 callback for ${url}`);
    return seen[0].cancel;
  };

  assert.equal(checkUrl("http://127.0.0.1:4100/app.js"), false);
  assert.equal(checkUrl("http://127.0.0.1:4100/api/v1/sessions?after=0&limit=50"), false);

  assert.equal(checkUrl("http://127.0.0.1:41001/app.js"), true);
  assert.equal(checkUrl("http://localhost:4100/app.js"), true);
  assert.equal(checkUrl("https://127.0.0.1:4100/app.js"), true);
  assert.equal(checkUrl("data:text/html,<h1>x</h1>"), true);
  assert.equal(checkUrl("devtools://devtools/bundled/inspector.html"), true);
  assert.equal(checkUrl("/relative/path.js"), true);
  assert.equal(checkUrl(""), true);
  assert.equal(checkUrl("http://[::1]:4100/"), true);
  assert.equal(checkUrl("not a valid url"), true);
});

test("ordinary desktop startup mode", async () => {
  const unsetRes = await loadRealMainWithHarness({ liveChatEnv: undefined });
  assert.equal(unsetRes.calls.hostManagerOptions.length, 1);
  assert.equal(unsetRes.calls.hostManagerOptions[0].liveChat, true);

  const oneRes = await loadRealMainWithHarness({ liveChatEnv: "1" });
  assert.equal(oneRes.calls.hostManagerOptions.length, 1);
  assert.equal(oneRes.calls.hostManagerOptions[0].liveChat, true);

  const zeroRes = await loadRealMainWithHarness({ liveChatEnv: "0" });
  assert.equal(zeroRes.calls.hostManagerOptions.length, 1);
  assert.equal(zeroRes.calls.hostManagerOptions[0].liveChat, false);

  const errRes = await loadRealMainWithHarness({
    liveChatEnv: undefined,
    startHostError: new Error("缺少在线模型配置 (DEEPSEEK_API_KEY) sk-secret-xyz #token=tok123"),
  });
  assert.equal(errRes.calls.errorBoxes.length, 1);
  assert.equal(errRes.calls.errorBoxes[0].title, "ForeContract 启动失败");
  assert.ok(errRes.calls.errorBoxes[0].content.includes("缺少在线模型配置"));
  assert.ok(!errRes.calls.errorBoxes[0].content.includes("sk-secret-xyz"));
  assert.ok(!errRes.calls.errorBoxes[0].content.includes("tok123"));
  assert.deepEqual(errRes.calls.exitCodes, [1]);
});
