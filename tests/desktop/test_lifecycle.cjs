"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const {
  buildHostEnv,
  createHostManager,
  parseStartupUrl,
  pickFreeLoopbackPort,
  sanitizeErrorMessage,
} = require("../../apps/desktop/host.cjs");

function writeMockHostScript(dir) {
  const scriptPath = path.join(dir, "mock_host.js");
  fs.writeFileSync(
    scriptPath,
    `
const http = require("node:http");
const fs = require("node:fs");
const args = process.argv.slice(2);
const portIdx = args.indexOf("--port");
const port = Number(args[portIdx + 1]);
const token = "mock-secret-tok-123";
const srv = http.createServer((req, res) => {
  if (req.url === "/api/v1/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", mode: "offline", live_enabled: false }));
    return;
  }
  if (req.url === "/api/v1/sessions") {
    if (req.headers.authorization !== "Bearer " + token) {
      res.writeHead(401);
      res.end("{}");
      return;
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end("[]");
    return;
  }
  res.writeHead(404);
  res.end("{}");
});
srv.listen(port, "127.0.0.1", () => {
  process.stdout.write("FORECONTRACT_APP_URL=http://127.0.0.1:" + port + "/#token=" + token + "\\n");
});
let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => {
  buf += c;
  if (buf.split(/\\r?\\n/).some((l) => l.trim() === "stop")) {
    srv.close(() => process.exit(0));
  }
});
// Do NOT exit on stdin EOF alone — wait explicitly for "stop\\n"
process.stdin.on("end", () => {});
`,
    "utf8"
  );
  return scriptPath;
}

test("parseStartupUrl enforces exact loopback host, expected port, and token", () => {
  const ok = parseStartupUrl("FORECONTRACT_APP_URL=http://127.0.0.1:45678/#token=abc123", 45678);
  assert.equal(ok.origin, "http://127.0.0.1:45678");
  assert.equal(ok.port, 45678);
  assert.equal(ok.token, "abc123");

  assert.equal(parseStartupUrl("other log line", 45678), null);
  assert.throws(() => parseStartupUrl("FORECONTRACT_APP_URL=http://localhost:45678/#token=abc", 45678));
  assert.throws(() => parseStartupUrl("FORECONTRACT_APP_URL=http://127.0.0.1:9999/#token=abc", 45678));
  assert.throws(() => parseStartupUrl("FORECONTRACT_APP_URL=http://127.0.0.1:45678/#notoken=1", 45678));
});

test("sanitizeErrorMessage and buildHostEnv strip credentials and tokens", () => {
  const cleaned = sanitizeErrorMessage(
    "failed at http://127.0.0.1:8765/#token=secret_val Bearer tok_xyz FORECONTRACT_APP_URL=http://127.0.0.1:1/#token=x"
  );
  assert.ok(!cleaned.includes("secret_val"));
  assert.ok(!cleaned.includes("tok_xyz"));

  const env = buildHostEnv({
    PATH: "C:\\Windows",
    DEEPSEEK_API_KEY: "sk-secret",
    OPENAI_API_KEY: "sk-openai",
  });
  assert.equal(env.DEEPSEEK_API_KEY, undefined);
  assert.equal(env.OPENAI_API_KEY, undefined);
  assert.equal(env.PYTHONDONTWRITEBYTECODE, "1");
});

test("createHostManager starts bounded host, verifies offline health, and stops via control-stdin", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fc-desk-test-"));
  try {
    const mockScript = writeMockHostScript(tmp);
    const mgr = createHostManager({
      rootDir: tmp,
      pythonExe: process.execPath,
      spawnArgsOverride: [mockScript, "--port", "{PORT}"],
      startupTimeoutMs: 8000,
    });
    const state = await mgr.startHost();
    assert.equal(state.status, "running");
    assert.ok(Number.isInteger(state.port) && state.port > 0);
    assert.ok(fs.existsSync(path.join(tmp, "data")));
    assert.ok(fs.existsSync(path.join(tmp, "workspaces")));
    assert.ok(fs.existsSync(path.join(tmp, "project")));
    assert.ok(fs.existsSync(path.join(tmp, "profile")));

    const snap = await mgr.getStatusSnapshot();
    assert.equal(snap.hostState, "idle");
    assert.deepEqual(snap.activeRuns, []);

    const exitRes = await mgr.stopHost({ timeoutMs: 5000 });
    assert.equal(exitRes.code, 0);
    assert.equal(mgr.getState().status, "stopped");
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("createHostManager rejects early exit and never attaches to wrong port", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fc-desk-fail-"));
  try {
    const busyPort = await pickFreeLoopbackPort();
    assert.ok(busyPort > 0);
    const mgr = createHostManager({
      rootDir: tmp,
      pythonExe: process.execPath,
      spawnArgsOverride: ["-e", "process.stderr.write('boom #token=leaked\\n'); process.exit(2);"],
      startupTimeoutMs: 5000,
    });
    await assert.rejects(
      () => mgr.startHost(),
      (err) => {
        assert.ok(!err.message.includes("leaked"));
        return true;
      }
    );
    assert.equal(mgr.getState().status, "stopped");
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
