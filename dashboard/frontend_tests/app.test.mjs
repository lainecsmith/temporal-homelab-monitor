// Wiring tests for static/app.js with a minimal fake DOM and fetch. Run with:
//   node --test dashboard/frontend_tests/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const fx = JSON.parse(readFileSync(new URL("./fixtures.json", import.meta.url), "utf8"));
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
const cards = (html) => html.split('class="card host"').length - 1;
let run = 0;

// Installs fakes, loads a fresh copy of app.js, lets its first load finish.
async function boot(routes, hash = "") {
  const calls = [], win = {}, doc = {}, handlers = {};
  const root = { innerHTML: "", addEventListener: (t, fn) => (handlers[t] = fn), querySelectorAll: () => [] };
  Object.assign(globalThis, {
    document: { getElementById: () => root, activeElement: null, hidden: false, addEventListener: (t, fn) => (doc[t] = fn) },
    window: { addEventListener: (t, fn) => (win[t] = fn), scrollTo() {} },
    location: { hash },
    setInterval: () => 0,
    // Short waits (the post-signal settle) run at once; long ones (notice expiry) never fire.
    setTimeout: (fn, ms) => { if (ms <= 5000) Promise.resolve().then(fn); return 0; },
    fetch: async (path, opts = {}) => {
      const method = opts.method || "GET";
      calls.push(`${method} ${path}`);
      const r = routes(method, path);
      if (r === "network") throw new TypeError("Failed to fetch");
      return { ok: r.status < 400, status: r.status, json: async () => r.body };
    },
  });
  await import(`../static/app.js?run=${++run}`);
  await flush();
  const click = async (dataset) => { handlers.click({ target: { closest: () => ({ dataset }) } }); await flush(); };
  return { root, calls, click, win, setHash: (h) => (globalThis.location.hash = h) };
}

const ok = (body, status = 200) => ({ status, body });
const fleetRoutes = (extra = {}) => (m, p) => extra[`${m} ${p}`] ?? (
  p === "/api/hosts" ? ok(fx.fleet_ok)
  : p === "/api/hosts/nas" ? ok(fx.host_nas)
  : p === "/api/health" ? ok({ state: "ok" })
  : p === "/api/poll" ? ok(fx.fleet_ok)
  : p.endsWith("/refresh") || p.endsWith("/check-updates") ? ok({ sent: true, queued: false }, 202)
  : ok({ detail: "Unknown host." }, 404));

test("startup loads the fleet and renders a card per host", async () => {
  const app = await boot(fleetRoutes());
  assert.deepEqual(app.calls, ["GET /api/hosts"]);
  assert.equal(cards(app.root.innerHTML), 5);
});

test("an unreachable dashboard service shows a banner instead of a blank page", async () => {
  const app = await boot(() => "network");
  assert.match(app.root.innerHTML, /Can&#39;t reach the dashboard service/);
  assert.match(app.root.innerHTML, /didn&#39;t respond/);
  assert.match(app.root.innerHTML, /data-action="retry"/);
});

test("retry asks the API to poll, then reloads", async () => {
  const app = await boot(() => "network");
  app.calls.length = 0;
  await app.click({ action: "retry" });
  assert.deepEqual(app.calls, ["POST /api/poll", "GET /api/hosts"]);
});

test("filter chips filter without calling the API", async () => {
  const app = await boot(fleetRoutes());
  app.calls.length = 0;
  await app.click({ action: "filter", value: "offline" });
  assert.equal(cards(app.root.innerHTML), 1);
  assert.deepEqual(app.calls, []);
});

test("refresh all signals every host, polls once, then reloads", async () => {
  const app = await boot(fleetRoutes());
  app.calls.length = 0;
  await app.click({ action: "refresh-all" });
  const names = ["gpu-box", "media-server", "nas", "pi-garage", "pi-hole"];
  assert.deepEqual(app.calls.slice(0, 5).sort(), names.map((n) => `POST /api/hosts/${n}/refresh`).sort());
  assert.deepEqual(app.calls.slice(5), ["POST /api/poll", "GET /api/hosts"]);
  assert.match(app.root.innerHTML, /Refresh requested\./);
  assert.ok(!app.root.innerHTML.includes("Refreshing…"));
});

test("a queued request (control plane down) doesn't wait or poll", async () => {
  const app = await boot(fleetRoutes({ "POST /api/hosts/nas/refresh": ok({ sent: true, queued: true }, 202) }), "#/host/nas");
  app.calls.length = 0;
  await app.click({ action: "refresh", host: "nas" });
  assert.deepEqual(app.calls, ["POST /api/hosts/nas/refresh", "GET /api/hosts/nas", "GET /api/health"]);
  assert.match(app.root.innerHTML, /controller is down\. Your request is queued/);
});

test("a failed signal says so and clears the busy state", async () => {
  const app = await boot(fleetRoutes({ "POST /api/hosts/nas/check-updates": ok({ detail: "Temporal is down" }, 503) }), "#/host/nas");
  await app.click({ action: "check-updates", host: "nas" });
  assert.match(app.root.innerHTML, /Couldn&#39;t send the request\. nas: Temporal is down/);
  assert.ok(!app.root.innerHTML.includes("Checking…"));
});

test("the detail route loads the host and the control plane state", async () => {
  const app = await boot(fleetRoutes(), "#/host/nas");
  assert.deepEqual(app.calls.sort(), ["GET /api/health", "GET /api/hosts/nas"]);
  assert.match(app.root.innerHTML, /openssl/);
});

test("an unknown host shows a not-found page", async () => {
  const app = await boot(fleetRoutes(), "#/host/zzz");
  assert.match(app.root.innerHTML, /Unknown host/);
});

test("navigating to a host loads it, and the old host never lingers", async () => {
  const app = await boot(fleetRoutes());
  app.setHash("#/host/nas");
  app.win.hashchange();
  assert.match(app.root.innerHTML, /Loading…/);
  await flush();
  assert.match(app.root.innerHTML, /openssl/);
});

test("the package toggle expands and collapses, and resets when you leave the host", async () => {
  const app = await boot(fleetRoutes(), "#/host/nas");
  assert.match(app.root.innerHTML, /Show all 21/);
  await app.click({ action: "toggle-packages" });
  assert.match(app.root.innerHTML, /Show fewer/);
  app.setHash("#/host/nas");
  app.win.hashchange();
  await flush();
  assert.match(app.root.innerHTML, /Show all 21/);
});
