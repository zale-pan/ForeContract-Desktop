"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

async function loadMainForLocaleTest({
  rootDir,
  hostOrigin = "http://127.0.0.1:4100",
  preferredLanguages = ["zh-CN"],
  appLocale = "zh-CN",
  trayShouldFail = false,
  statusSnapshot = { hostState: "idle", activeRuns: [], pendingCount: 0 },
  startHostError = null,
} = {}) {
  const mainPath = path.resolve(__dirname, "../../apps/desktop/main.cjs");
  const hostPath = path.resolve(__dirname, "../../apps/desktop/host.cjs");
  const userDataDir = path.join(rootDir, "profile", "userData");

  let trayMenu = null;
  let trayTooltip = "";
  let winMenu = null;
  let readyPromise = null;
  const ipcHandlers = new Map();
  const messageBoxes = [];
  const errorBoxes = [];
  let fakeWebContents = null;

  class FakeTray {
    setToolTip(tip) {
      trayTooltip = tip;
    }
    setContextMenu(menu) {
      trayMenu = menu;
    }
    on() {}
    destroy() {}
    isDestroyed() {
      return false;
    }
  }

  class FakeBrowserWindow {
    constructor(opts) {
      this.opts = opts;
      fakeWebContents = {
        setWindowOpenHandler() {},
        on() {},
        getURL() {
          return `${hostOrigin}/`;
        },
      };
      this.webContents = fakeWebContents;
    }
    setMenu(menu) {
      winMenu = menu;
    }
    setMenuBarVisibility() {}
    on() {}
    async loadURL() {}
    isDestroyed() {
      return false;
    }
    destroy() {}
  }

  const fakeElectron = {
    BrowserWindow: FakeBrowserWindow,
    Menu: {
      buildFromTemplate(tpl) {
        return tpl;
      },
    },
    Tray: FakeTray,
    app: {
      setPath() {},
      getPath(name) {
        if (name === "userData") return userDataDir;
        return path.join(rootDir, name);
      },
      getPreferredSystemLanguages() {
        return preferredLanguages;
      },
      getLocale() {
        return appLocale;
      },
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
      quit() {},
      exit() {},
    },
    ipcMain: {
      handle(channel, fn) {
        ipcHandlers.set(channel, fn);
      },
    },
    dialog: {
      async showMessageBox(_win, opts) {
        messageBoxes.push(opts);
        return { response: 0 };
      },
      showErrorBox(title, content) {
        errorBoxes.push({ title, content });
      },
    },
    nativeImage: {
      createFromPath() {
        return {
          isEmpty() {
            return trayShouldFail;
          },
        };
      },
    },
    session: {
      defaultSession: {
        webRequest: {
          onBeforeRequest() {},
        },
      },
    },
  };

  const fakeHostManager = {
    getState() {
      return { status: "running", origin: hostOrigin };
    },
    async startHost() {
      if (startHostError) throw startHostError;
      return { status: "running", origin: hostOrigin, port: Number(new URL(hostOrigin).port) };
    },
    getAppUrl() {
      return `${hostOrigin}/#token=fake-tok`;
    },
    onUnexpectedExit() {},
    async getStatusSnapshot() {
      return statusSnapshot;
    },
    async cancelActiveRuns() {},
    async stopHost() {
      return { code: 0 };
    },
  };

  const origLoad = Module._load;
  const origEnvRoot = process.env.FC_DESKTOP_ROOT;
  process.env.FC_DESKTOP_ROOT = rootDir;
  delete require.cache[mainPath];

  Module._load = function (request, parent, isMain) {
    if (request === "electron") return fakeElectron;
    if (parent && parent.filename === mainPath && request === "./host.cjs") {
      return {
        createHostManager: () => fakeHostManager,
        sanitizeErrorMessage: require(hostPath).sanitizeErrorMessage,
      };
    }
    return origLoad.call(this, request, parent, isMain);
  };

  try {
    require(mainPath);
    if (readyPromise) await readyPromise;
  } finally {
    Module._load = origLoad;
    if (origEnvRoot === undefined) {
      delete process.env.FC_DESKTOP_ROOT;
    } else {
      process.env.FC_DESKTOP_ROOT = origEnvRoot;
    }
    delete require.cache[mainPath];
  }

  return {
    userDataDir,
    ipcHandlers,
    getWebContents: () => fakeWebContents,
    getTrayMenu: () => trayMenu,
    getTrayTooltip: () => trayTooltip,
    getWinMenu: () => winMenu,
    messageBoxes,
    errorBoxes,
  };
}

test("initial system locale resolution and cross-port persistence", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fc-desk-i18n-"));
  try {
    // 1. zh-Hans -> zh-CN
    const rZh = await loadMainForLocaleTest({
      rootDir: path.join(tmp, "r-zh"),
      preferredLanguages: ["zh-Hans-CN"],
      appLocale: "zh-CN",
    });
    const getLocZh = rZh.ipcHandlers.get("forecontract:get-locale");
    assert.equal(
      getLocZh({ sender: rZh.getWebContents(), senderFrame: { url: "http://127.0.0.1:4100/" } }),
      "zh-CN"
    );

    // 2. zh-TW -> zh-CN; fr-FR -> en fallback
    const rTw = await loadMainForLocaleTest({
      rootDir: path.join(tmp, "r-tw"),
      preferredLanguages: ["zh-TW"],
      appLocale: "zh-TW",
    });
    assert.equal(
      rTw.ipcHandlers.get("forecontract:get-locale")({
        sender: rTw.getWebContents(),
        senderFrame: { url: "http://127.0.0.1:4100/" },
      }),
      "zh-CN"
    );

    const rFr = await loadMainForLocaleTest({
      rootDir: path.join(tmp, "r-fr"),
      preferredLanguages: ["fr-FR"],
      appLocale: "fr-FR",
    });
    assert.equal(
      rFr.ipcHandlers.get("forecontract:get-locale")({
        sender: rFr.getWebContents(),
        senderFrame: { url: "http://127.0.0.1:4100/" },
      }),
      "en"
    );

    // 3. Switch to en, verify tray & prefs file, then restart on another port
    const persistRoot = path.join(tmp, "r-persist");
    const r1 = await loadMainForLocaleTest({
      rootDir: persistRoot,
      hostOrigin: "http://127.0.0.1:4201",
      preferredLanguages: ["zh-CN"],
      statusSnapshot: { hostState: "waiting", activeRuns: ["run-1"], pendingCount: 2 },
    });
    const setLoc = r1.ipcHandlers.get("forecontract:set-locale");
    const okRes = setLoc(
      { sender: r1.getWebContents(), senderFrame: { url: "http://127.0.0.1:4201/" } },
      "en"
    );
    assert.deepEqual(okRes, { ok: true, locale: "en" });
    assert.equal(r1.getTrayMenu()[0].label, "Open ForeContract");
    assert.equal(r1.getTrayMenu()[2].label, "Quit ForeContract");
    assert.equal(r1.getTrayTooltip(), "ForeContract — Waiting (1 running, 2 pending)");

    const prefsFile = path.join(r1.userDataDir, "desktop-prefs.json");
    assert.equal(JSON.parse(fs.readFileSync(prefsFile, "utf8")).locale, "en");
    assert.ok(!fs.existsSync(path.join(persistRoot, "data", "desktop-prefs.json")));

    // Invalid locale rejected without modifying desktop-prefs.json
    for (const bad of ["fr", "zh-TW", "", null, 123, "__proto__"]) {
      const badRes = setLoc(
        { sender: r1.getWebContents(), senderFrame: { url: "http://127.0.0.1:4201/" } },
        bad
      );
      assert.equal(badRes.ok, false);
      assert.equal(badRes.error, "unsupported_locale");
    }
    assert.equal(JSON.parse(fs.readFileSync(prefsFile, "utf8")).locale, "en");

    // Reopen on another port (4202) -> still "en"
    const r2 = await loadMainForLocaleTest({
      rootDir: persistRoot,
      hostOrigin: "http://127.0.0.1:4202",
      preferredLanguages: ["zh-CN"],
      trayShouldFail: true,
    });
    assert.equal(
      r2.ipcHandlers.get("forecontract:get-locale")({
        sender: r2.getWebContents(),
        senderFrame: { url: "http://127.0.0.1:4202/" },
      }),
      "en"
    );
    assert.equal(r2.getWinMenu()[0].label, "File");
    assert.equal(r2.getWinMenu()[0].submenu[0].label, "Quit ForeContract");
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("IPC rejects wrong origin or foreign sender window and preload exposes narrow API", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fc-desk-ipc-"));
  try {
    const r = await loadMainForLocaleTest({
      rootDir: tmp,
      hostOrigin: "http://127.0.0.1:4301",
    });
    const getLoc = r.ipcHandlers.get("forecontract:get-locale");
    const setLoc = r.ipcHandlers.get("forecontract:set-locale");

    assert.throws(
      () => getLoc({ sender: r.getWebContents(), senderFrame: { url: "http://127.0.0.1:9999/" } }),
      /unauthorized_ipc_origin/
    );
    assert.throws(
      () =>
        setLoc(
          { sender: {}, senderFrame: { url: "http://127.0.0.1:4301/" } },
          "en"
        ),
      /unauthorized_ipc_origin/
    );

    // Verify preload.cjs exposes only getLocale and setLocale
    const preloadPath = path.resolve(__dirname, "../../apps/desktop/preload.cjs");
    const exposed = {};
    const fakeElectronPreload = {
      contextBridge: {
        exposeInMainWorld(key, api) {
          exposed[key] = api;
        },
      },
      ipcRenderer: {
        invoke(ch, ...args) {
          return { ch, args };
        },
      },
    };
    const origLoad = Module._load;
    delete require.cache[preloadPath];
    Module._load = function (request, parent, isMain) {
      if (request === "electron") return fakeElectronPreload;
      return origLoad.call(this, request, parent, isMain);
    };
    try {
      require(preloadPath);
    } finally {
      Module._load = origLoad;
      delete require.cache[preloadPath];
    }
    assert.deepEqual(Object.keys(exposed.forecontractDesktop).sort(), ["getLocale", "setLocale"]);
    assert.deepEqual(exposed.forecontractDesktop.getLocale(), {
      ch: "forecontract:get-locale",
      args: [],
    });
    assert.deepEqual(exposed.forecontractDesktop.setLocale("en"), {
      ch: "forecontract:set-locale",
      args: ["en"],
    });
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("English startup failure dialog is localized without leaking tokens", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fc-desk-err-"));
  try {
    const r = await loadMainForLocaleTest({
      rootDir: tmp,
      preferredLanguages: ["en-US"],
      appLocale: "en-US",
      startHostError: new Error("DEEPSEEK_API_KEY missing sk-secret #token=tok999"),
    });
    assert.equal(r.errorBoxes.length, 1);
    assert.equal(r.errorBoxes[0].title, "ForeContract Startup Failed");
    assert.ok(r.errorBoxes[0].content.includes("DEEPSEEK_API_KEY"));
    assert.ok(!r.errorBoxes[0].content.includes("sk-secret"));
    assert.ok(!r.errorBoxes[0].content.includes("tok999"));
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("FC_DESKTOP_LOCALE overrides stale persisted prefs on reinstall", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fc-desk-inst-"));
  const origLocaleEnv = process.env.FC_DESKTOP_LOCALE;
  try {
    const prefsDir = path.join(tmp, "profile", "userData");
    fs.mkdirSync(prefsDir, { recursive: true });
    fs.writeFileSync(path.join(prefsDir, "desktop-prefs.json"), JSON.stringify({ locale: "zh-CN" }), "utf8");
    process.env.FC_DESKTOP_LOCALE = "en";
    const r = await loadMainForLocaleTest({
      rootDir: tmp,
      preferredLanguages: ["zh-CN"],
      appLocale: "zh-CN",
    });
    assert.equal(
      r.ipcHandlers.get("forecontract:get-locale")({
        sender: r.getWebContents(),
        senderFrame: { url: "http://127.0.0.1:4100/" },
      }),
      "en"
    );
  } finally {
    if (origLocaleEnv === undefined) {
      delete process.env.FC_DESKTOP_LOCALE;
    } else {
      process.env.FC_DESKTOP_LOCALE = origLocaleEnv;
    }
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
