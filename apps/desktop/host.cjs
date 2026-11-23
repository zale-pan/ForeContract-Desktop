"use strict";

const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");

const TERMINAL_RUN_STATES = new Set(["succeeded", "failed", "cancelled", "unknown"]);
const CREDENTIAL_ENV_KEYS = new Set([
  "DEEPSEEK_API_KEY",
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "AZURE_OPENAI_API_KEY",
  "GOOGLE_API_KEY",
  "GEMINI_API_KEY",
]);

function sanitizeErrorMessage(raw) {
  let msg = String(raw || "未知宿主错误");
  msg = msg.replace(/#token=[^\s"'&]+/gi, "#token=[REDACTED]");
  msg = msg.replace(/Bearer\s+[A-Za-z0-9._-]+/gi, "Bearer [REDACTED]");
  msg = msg.replace(/FORECONTRACT_APP_URL=[^\r\n]+/gi, "FORECONTRACT_APP_URL=[REDACTED]");
  return msg;
}

function pickFreeLoopbackPort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      const port = addr && typeof addr === "object" ? addr.port : 0;
      srv.close((closeErr) => {
        if (closeErr) {
          reject(closeErr);
          return;
        }
        if (!Number.isInteger(port) || port <= 0) {
          reject(new Error("无法获取空闲 loopback 端口"));
          return;
        }
        resolve(port);
      });
    });
  });
}

function buildHostEnv(baseEnv = process.env, { allowDeepSeekKey = false } = {}) {
  const env = {};
  for (const [k, v] of Object.entries(baseEnv)) {
    if (v === undefined || v === null) continue;
    const upper = k.toUpperCase();
    if (CREDENTIAL_ENV_KEYS.has(upper)) {
      if (allowDeepSeekKey && upper === "DEEPSEEK_API_KEY") {
        env[k] = String(v);
      }
      continue;
    }
    env[k] = String(v);
  }
  env.PYTHONDONTWRITEBYTECODE = "1";
  env.PYTHONIOENCODING = "utf-8";
  env.NO_PROXY = "127.0.0.1,localhost";
  env.PIP_NO_INDEX = "1";
  return env;
}

function parseStartupUrl(line, expectedPort) {
  const trimmed = String(line || "").trim();
  const prefix = "FORECONTRACT_APP_URL=";
  if (!trimmed.startsWith(prefix)) {
    return null;
  }
  const rawUrl = trimmed.slice(prefix.length).trim();
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error("宿主返回了无效的启动地址");
  }
  if (parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1") {
    throw new Error("宿主启动地址必须绑定 http://127.0.0.1");
  }
  const port = Number(parsed.port);
  if (!Number.isInteger(port) || port !== expectedPort) {
    throw new Error("宿主启动端口与约定端口不一致");
  }
  const hash = parsed.hash.startsWith("#") ? parsed.hash.slice(1) : "";
  const params = new URLSearchParams(hash);
  const token = params.get("token") || "";
  if (!token.trim()) {
    throw new Error("宿主启动地址缺少临时认证 token");
  }
  return {
    origin: `http://127.0.0.1:${port}`,
    port,
    token,
    appUrl: `http://127.0.0.1:${port}/#token=${encodeURIComponent(token)}`,
  };
}

function httpJsonRequest(urlStr, { method = "GET", token = "", body = null, timeoutMs = 4000 } = {}) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(urlStr);
    const headers = { Accept: "application/json" };
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    let payload = null;
    if (body !== null && body !== undefined) {
      payload = Buffer.from(JSON.stringify(body), "utf8");
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = String(payload.length);
    }
    const req = http.request(
      {
        protocol: parsed.protocol,
        hostname: parsed.hostname,
        port: Number(parsed.port),
        path: `${parsed.pathname}${parsed.search}`,
        method,
        headers,
        timeout: timeoutMs,
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let json = null;
          try {
            json = text ? JSON.parse(text) : null;
          } catch {
            json = null;
          }
          resolve({ status: res.statusCode || 0, json, text });
        });
      }
    );
    req.on("timeout", () => {
      req.destroy(new Error("请求本地宿主超时"));
    });
    req.on("error", (err) => {
      reject(new Error(sanitizeErrorMessage(err.message)));
    });
    if (payload) {
      req.write(payload);
    }
    req.end();
  });
}

async function checkHealth(origin, token = "", timeoutMs = 4000, { expectLive = false } = {}) {
  const res = await httpJsonRequest(`${origin}/api/v1/health`, { token, timeoutMs });
  if (res.status !== 200 || !res.json || res.json.status !== "ok") {
    throw new Error("宿主健康检查未通过");
  }
  if (expectLive) {
    if (res.json.mode !== "live" || res.json.live_enabled !== true) {
      if (res.json.mode === "unconfigured" && res.json.live_enabled === false) {
        throw new Error(
          "在线模式未就绪：缺少在线模型配置 (DEEPSEEK_API_KEY)，请配置后重试；如需本地离线测试请设置 FC_DESKTOP_LIVE_CHAT=0"
        );
      }
      throw new Error("宿主未处于预期的在线模式");
    }
  } else if (
    (res.json.mode !== "offline" && res.json.mode !== "unconfigured") ||
    res.json.live_enabled !== false
  ) {
    throw new Error("宿主未处于预期的离线模式");
  }
  return res.json;
}

async function fetchHostStatus(origin, token, timeoutMs = 4000) {
  const sessRes = await httpJsonRequest(`${origin}/api/v1/sessions`, { token, timeoutMs });
  if (sessRes.status !== 200 || !Array.isArray(sessRes.json)) {
    throw new Error("读取宿主会话状态失败");
  }
  const activeRuns = [];
  let pendingCount = 0;
  for (const sess of sessRes.json) {
    if (!sess || !sess.active_run_id) continue;
    const sid = sess.session_id;
    const rid = sess.active_run_id;
    const [runRes, propRes, extRes] = await Promise.all([
      httpJsonRequest(`${origin}/api/v1/sessions/${sid}/runs/${rid}`, { token, timeoutMs }),
      httpJsonRequest(`${origin}/api/v1/sessions/${sid}/runs/${rid}/proposals`, { token, timeoutMs }),
      httpJsonRequest(`${origin}/api/v1/sessions/${sid}/runs/${rid}/extensions`, { token, timeoutMs }),
    ]);
    if (runRes.status !== 200 || !runRes.json) {
      throw new Error("读取活动运行快照失败");
    }
    if (!TERMINAL_RUN_STATES.has(runRes.json.state)) {
      activeRuns.push({
        session_id: sid,
        run_id: rid,
        state: runRes.json.state,
      });
    }
    if (propRes.status === 200 && Array.isArray(propRes.json)) {
      pendingCount += propRes.json.filter((p) => p && p.state === "pending").length;
    }
    if (extRes.status === 200 && extRes.json && Array.isArray(extRes.json.proposals)) {
      pendingCount += extRes.json.proposals.filter((p) => p && p.state === "pending").length;
    }
  }
  const hostState =
    activeRuns.length > 0 ? (pendingCount > 0 ? "waiting" : "running") : "idle";
  return { hostState, activeRuns, pendingCount };
}

async function cancelRunAndWait(origin, token, sessionId, runId, timeoutMs = 10000) {
  const cancelRes = await httpJsonRequest(
    `${origin}/api/v1/sessions/${sessionId}/runs/${runId}/cancel`,
    { method: "POST", token, body: {}, timeoutMs: 4000 }
  );
  if (cancelRes.status !== 200 && cancelRes.status !== 202) {
    throw new Error(`取消运行失败 (HTTP ${cancelRes.status})`);
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const runRes = await httpJsonRequest(
      `${origin}/api/v1/sessions/${sessionId}/runs/${runId}`,
      { token, timeoutMs: 4000 }
    );
    if (runRes.status === 200 && runRes.json && TERMINAL_RUN_STATES.has(runRes.json.state)) {
      return runRes.json.state;
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error("等待运行进入终态超时");
}

function createHostManager({
  rootDir,
  projectRoot = "",
  pythonExe,
  spawnArgsOverride = null,
  startupTimeoutMs = 15000,
  spawnFn = spawn,
  baseEnv = process.env,
  liveChat = undefined,
} = {}) {
  const isLiveChat = liveChat !== undefined ? Boolean(liveChat) : baseEnv.FC_DESKTOP_LIVE_CHAT === "1";
  if (!rootDir || !path.isAbsolute(rootDir)) {
    throw new Error("FC_DESKTOP_ROOT 必须是存在的绝对路径");
  }
  if (!pythonExe || !path.isAbsolute(pythonExe) || !fs.existsSync(pythonExe)) {
    throw new Error("FC_DESKTOP_PYTHON 必须是存在的绝对路径");
  }
  const resolvedRoot = path.resolve(rootDir);
  const dataRoot = path.join(resolvedRoot, "data");
  const workspacesRoot = path.join(resolvedRoot, "workspaces");
  const resolvedProject = projectRoot ? path.resolve(projectRoot) : path.join(resolvedRoot, "project");
  const profileRoot = path.join(resolvedRoot, "profile");
  for (const dir of [resolvedRoot, dataRoot, workspacesRoot, resolvedProject, profileRoot]) {
    fs.mkdirSync(dir, { recursive: true });
  }

  let child = null;
  let hostInfo = null;
  let expectedStop = false;
  let exitPromise = null;
  const unexpectedListeners = new Set();

  function getState() {
    return {
      status: child && hostInfo ? "running" : "stopped",
      pid: child ? child.pid : null,
      port: hostInfo ? hostInfo.port : null,
      origin: hostInfo ? hostInfo.origin : null,
      dataRoot,
      workspacesRoot,
      projectRoot: resolvedProject,
      profileRoot,
      liveChat: isLiveChat,
    };
  }

  function getAppUrl() {
    return hostInfo ? hostInfo.appUrl : null;
  }

  function onUnexpectedExit(cb) {
    unexpectedListeners.add(cb);
    return () => unexpectedListeners.delete(cb);
  }

  async function startHost() {
    if (child && hostInfo) {
      return getState();
    }
    const hasCustomArgs = Array.isArray(spawnArgsOverride) && spawnArgsOverride.length > 0;
    if (isLiveChat && !hasCustomArgs) {
      const apiKey = typeof baseEnv.DEEPSEEK_API_KEY === "string" ? baseEnv.DEEPSEEK_API_KEY.trim() : "";
      if (!apiKey) {
        throw new Error(
          "缺少在线模型配置 (DEEPSEEK_API_KEY)，请配置后重试；如需本地离线测试请设置 FC_DESKTOP_LIVE_CHAT=0"
        );
      }
    }
    const port = await pickFreeLoopbackPort();
    const modeArgs = isLiveChat
      ? ["--live-deepseek", "--live-max-requests", "50", "--live-budget-cny", "1"]
      : ["--offline"];
    const args =
      Array.isArray(spawnArgsOverride) && spawnArgsOverride.length > 0
        ? spawnArgsOverride.map((a) => String(a).replaceAll("{PORT}", String(port)))
        : [
            "-B",
            "-m",
            "forecontract_app",
            ...modeArgs,
            "--control-stdin",
            "--data-root",
            dataRoot,
            "--workspace-parent",
            workspacesRoot,
            "--project-root",
            resolvedProject,
            "--port",
            String(port),
          ];

    expectedStop = false;
    const proc = spawnFn(pythonExe, args, {
      cwd: resolvedRoot,
      env: buildHostEnv(baseEnv, { allowDeepSeekKey: isLiveChat }),
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    child = proc;

    let stderrTail = "";
    let stdoutBuf = "";
    let settled = false;

    exitPromise = new Promise((resolveExit) => {
      proc.once("exit", (code, signal) => {
        if (!expectedStop && settled) {
          for (const cb of unexpectedListeners) {
            try {
              cb({ code, signal });
            } catch {
              // ignore listener errors
            }
          }
        }
        if (child === proc) {
          child = null;
          hostInfo = null;
        }
        resolveExit({ code, signal });
      });
    });

    const started = await new Promise((resolveStart, rejectStart) => {
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        try {
          proc.kill();
        } catch {
          // ignore
        }
        rejectStart(new Error("等待本地宿主启动超时"));
      }, startupTimeoutMs);

      const failStart = (msg) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try {
          proc.kill();
        } catch {
          // ignore
        }
        rejectStart(new Error(sanitizeErrorMessage(msg)));
      };

      proc.stderr.on("data", (chunk) => {
        stderrTail = (stderrTail + chunk.toString("utf8")).slice(-1024);
      });

      proc.on("error", (err) => {
        failStart(`启动宿主子进程失败: ${err.message}`);
      });

      proc.once("exit", (code) => {
        if (!settled) {
          const detail = stderrTail.trim() ? `: ${stderrTail.trim().splitlines?.()[0] || stderrTail.trim()}` : "";
          failStart(`宿主进程提前退出 (code=${code})${detail}`);
        }
      });

      proc.stdout.on("data", (chunk) => {
        if (settled) return;
        stdoutBuf += chunk.toString("utf8");
        const lines = stdoutBuf.split(/\r?\n/);
        stdoutBuf = lines.pop() || "";
        for (const line of lines) {
          let parsed = null;
          try {
            parsed = parseStartupUrl(line, port);
          } catch (err) {
            failStart(err.message);
            return;
          }
          if (parsed) {
            checkHealth(parsed.origin, parsed.token, 4000, { expectLive: isLiveChat })
              .then(() => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolveStart(parsed);
              })
              .catch((err) => {
                failStart(err.message);
              });
            return;
          }
        }
      });
    });

    hostInfo = started;
    return getState();
  }

  async function getStatusSnapshot() {
    if (!hostInfo || !child) {
      return { hostState: "disconnected", activeRuns: [], pendingCount: 0 };
    }
    return fetchHostStatus(hostInfo.origin, hostInfo.token, 4000);
  }

  async function cancelActiveRuns(runs, timeoutMs = 10000) {
    if (!hostInfo || !child) {
      throw new Error("宿主未运行，无法取消任务");
    }
    for (const r of runs) {
      await cancelRunAndWait(hostInfo.origin, hostInfo.token, r.session_id, r.run_id, timeoutMs);
    }
  }

  async function stopHost({ timeoutMs = 10000 } = {}) {
    const proc = child;
    if (!proc) {
      return { code: 0, signal: null };
    }
    expectedStop = true;
    try {
      if (proc.stdin && !proc.stdin.destroyed && proc.stdin.writable) {
        proc.stdin.write("stop\n");
      }
    } catch {
      // fall through to wait/kill
    }
    const waitResult = await Promise.race([
      exitPromise,
      new Promise((resolve) =>
        setTimeout(() => {
          resolve({ timedOut: true });
        }, timeoutMs)
      ),
    ]);
    if (waitResult && waitResult.timedOut) {
      try {
        proc.kill();
      } catch {
        // ignore
      }
      return await exitPromise;
    }
    return waitResult;
  }

  return {
    startHost,
    stopHost,
    getState,
    getAppUrl,
    getStatusSnapshot,
    cancelActiveRuns,
    onUnexpectedExit,
  };
}

module.exports = {
  buildHostEnv,
  checkHealth,
  createHostManager,
  fetchHostStatus,
  parseStartupUrl,
  pickFreeLoopbackPort,
  sanitizeErrorMessage,
};
