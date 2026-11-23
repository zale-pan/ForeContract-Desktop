"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { buildHostEnv, checkHealth, createHostManager } = require("../../apps/desktop/host.cjs");

function makeFakeSpawn(captured) {
  return (exe, args, opts) => {
    const proc = new EventEmitter();
    proc.pid = 42424;
    proc.stdout = new EventEmitter();
    proc.stderr = new EventEmitter();
    proc.stdin = {
      writable: true,
      write(chunk) {
        if (String(chunk).includes("stop")) {
          setImmediate(() => proc.emit("exit", 0, null));
        }
      },
    };
    proc.kill = () => setImmediate(() => proc.emit("exit", 0, "SIGTERM"));
    captured.push({ exe, args, opts, proc });
    return proc;
  };
}

test("default offline host strips credentials and passes --offline", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fc-host-off-"));
  const fakePy = path.join(tmp, "python.exe");
  fs.writeFileSync(fakePy, "");

  const envOff = buildHostEnv({ PATH: "x", DEEPSEEK_API_KEY: "sk-secret" });
  assert.equal(envOff.DEEPSEEK_API_KEY, undefined);

  let nextHealthOff = { status: "ok", mode: "offline", live_enabled: false };
  const srv = http.createServer((req, res) => {
    if (req.url === "/api/v1/health") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(nextHealthOff));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const port = srv.address().port;

  try {
    const captured = [];
    const mgr = createHostManager({
      rootDir: tmp,
      pythonExe: fakePy,
      spawnFn: makeFakeSpawn(captured),
      baseEnv: { PATH: "x", DEEPSEEK_API_KEY: "sk-secret" },
    });
    const startPromise = mgr.startHost();
    await new Promise((r) => setTimeout(r, 40));
    // Override checkHealth via real server on port by testing checkHealth directly + args/env
    assert.ok(captured[0].args.includes("--offline"));
    assert.equal(captured[0].opts.env.DEEPSEEK_API_KEY, undefined);
    captured[0].proc.kill();
    await startPromise.catch(() => {});
    const health = await checkHealth(`http://127.0.0.1:${port}`, "", 2000, { expectLive: false });
    assert.equal(health.mode, "offline");
    nextHealthOff = { status: "ok", mode: "unconfigured", live_enabled: false };
    assert.equal((await checkHealth(`http://127.0.0.1:${port}`, "", 2000, { expectLive: false })).mode, "unconfigured");
    for (const bad of [
      { status: "ok", mode: "live", live_enabled: true },
      { status: "ok", mode: "offline", live_enabled: true },
      { status: "ok", mode: "unconfigured", live_enabled: true },
    ]) {
      nextHealthOff = bad;
      await assert.rejects(() => checkHealth(`http://127.0.0.1:${port}`, "", 2000, { expectLive: false }), /离线模式/);
    }
  } finally {
    srv.close();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("FC_DESKTOP_LIVE_CHAT=1 passes --live-deepseek 50/1 and DEEPSEEK_API_KEY to host only", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fc-host-live-"));
  const fakePy = path.join(tmp, "python.exe");
  fs.writeFileSync(fakePy, "");

  const envLive = buildHostEnv(
    { PATH: "x", DEEPSEEK_API_KEY: "sk-synth-live", OPENAI_API_KEY: "sk-other" },
    { allowDeepSeekKey: true }
  );
  assert.equal(envLive.DEEPSEEK_API_KEY, "sk-synth-live");
  assert.equal(envLive.OPENAI_API_KEY, undefined);

  let nextHealthLive = {
    status: "ok",
    mode: "live",
    live_enabled: true,
    model: "deepseek-flash",
    readiness: "ready",
  };
  const srv = http.createServer((req, res) => {
    if (req.url === "/api/v1/health") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(nextHealthLive));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const port = srv.address().port;

  try {
    const captured = [];
    const mgr = createHostManager({
      rootDir: tmp,
      pythonExe: fakePy,
      spawnFn: makeFakeSpawn(captured),
      baseEnv: { PATH: "x", FC_DESKTOP_LIVE_CHAT: "1", DEEPSEEK_API_KEY: "sk-synth-live" },
    });
    const startPromise = mgr.startHost();
    await new Promise((r) => setTimeout(r, 40));
    const args = captured[0].args;
    assert.ok(args.includes("--live-deepseek"));
    assert.equal(args[args.indexOf("--live-max-requests") + 1], "50");
    assert.equal(args[args.indexOf("--live-budget-cny") + 1], "1");
    assert.equal(captured[0].opts.env.DEEPSEEK_API_KEY, "sk-synth-live");
    captured[0].proc.kill();
    await startPromise.catch(() => {});

    const health = await checkHealth(`http://127.0.0.1:${port}`, "", 2000, { expectLive: true });
    assert.equal(health.mode, "live");
    assert.equal(health.live_enabled, true);

    const capturedNoKey = [];
    const mgrNoKey = createHostManager({
      rootDir: tmp,
      pythonExe: fakePy,
      spawnFn: makeFakeSpawn(capturedNoKey),
      baseEnv: { PATH: "x" },
      liveChat: true,
    });
    await assert.rejects(() => mgrNoKey.startHost(), /缺少在线模型配置.*DEEPSEEK_API_KEY/);
    assert.equal(capturedNoKey.length, 0);

    nextHealthLive = { status: "ok", mode: "unconfigured", live_enabled: false };
    await assert.rejects(() => checkHealth(`http://127.0.0.1:${port}`, "", 2000, { expectLive: true }), /在线模式未就绪/);
    for (const bad of [
      { status: "ok", mode: "offline", live_enabled: false },
      { status: "ok", mode: "live", live_enabled: false },
      { status: "ok", mode: "offline", live_enabled: true },
    ]) {
      nextHealthLive = bad;
      await assert.rejects(() => checkHealth(`http://127.0.0.1:${port}`, "", 2000, { expectLive: true }), /在线模式/);
    }
  } finally {
    srv.close();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
