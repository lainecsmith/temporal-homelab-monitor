// Run with: node --test dashboard/frontend_tests
// Fixtures come from the real Fleet.view() (see dashboard/make_fixtures.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { PACKAGE_LIMIT, esc, ago, renderBanner, renderFleet, renderHost, renderNotFound, hostCard, updatePills, filterHosts } from "../static/render.js";

const fx = JSON.parse(readFileSync(new URL("./fixtures.json", import.meta.url), "utf8"));
const NOW = Date.parse("2026-10-23T14:02:15+00:00");
const fleet = () => structuredClone(fx.fleet_ok);
const nas = () => structuredClone(fx.host_nas);
const host = (name) => fleet().hosts.find((h) => h.host === name);
const count = (html, text) => html.split(text).length - 1;

// ---------------------------------------------------------------- helpers and safety

test("esc escapes the characters that matter", () => {
  assert.equal(esc(`<a href="x">'&`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;");
  assert.equal(esc(null), "");
});

test("untrusted strings never reach the page as markup", () => {
  const evil = `"><script>alert(1)</script>`;
  const f = fleet();
  Object.assign(f.hosts[0], { host: evil, last_error: evil });
  f.hosts[0].meta.error = evil;
  f.fleet.failed_check_hosts = [evil];
  assert.ok(!renderFleet(f, { now: NOW }).includes("<script>"));

  const h = nas();
  h.host = evil;
  h.docker.containers[0].name = evil;
  h.docker.containers[0].image = evil;
  h.updates.os.packages[0].name = evil;
  h.updates.error = evil;
  h.last_error = evil;
  assert.ok(!renderHost(h, { now: NOW }).includes("<script>"));

  h.docker = { state: "error", error: evil, hint: null, containers: [] };
  assert.ok(!renderHost(h, { now: NOW }).includes("<script>"));
  assert.ok(!renderNotFound(evil).includes("<script>"));
  assert.ok(!renderBanner({ state: "temporal_unreachable", detail: evil }).includes("<script>"));
});

test("ago", () => {
  assert.equal(ago("2026-10-23T14:02:13+00:00", NOW), "just now");
  assert.equal(ago("2026-10-23T14:01:15+00:00", NOW), "1 min ago");
  assert.equal(ago("2026-10-23T12:02:15+00:00", NOW), "2 h ago");
  assert.equal(ago("2026-10-21T14:02:15+00:00", NOW), "2 d ago");
  assert.equal(ago(null, NOW), "never");
});

// ---------------------------------------------------------------- banners

test("no banner when the control plane is healthy", () => {
  assert.equal(renderBanner({ state: "ok" }), "");
});

test("each degraded state has its own banner", () => {
  const down = renderBanner({ state: "controller_down", detail: "No worker is polling the controller queue." });
  assert.match(down, /Controller isn&#39;t responding/);
  assert.match(down, /uv run worker\.py --role controller/);
  assert.match(down, /data-action="retry"/);
  assert.match(renderBanner({ state: "temporal_unreachable", detail: "no route to host" }), /Can&#39;t reach Temporal[\s\S]*no route to host/);
  assert.match(renderBanner({ state: "dashboard_error", detail: "boom" }), /dashboard hit an error/);
});

test("starting is informational and has no retry button", () => {
  const html = renderBanner({ state: "starting" });
  assert.match(html, /class="banner info"/);
  assert.ok(!html.includes("data-action=\"retry\""));
});

test("an unreachable dashboard service overrides the control plane banner", () => {
  const html = renderBanner({ state: "ok" }, { apiError: "Failed to fetch", asOf: "2026-10-23T14:02:15+00:00" });
  assert.match(html, /Can&#39;t reach the dashboard service/);
  assert.match(html, /Showing data from/);
  assert.match(html, /Failed to fetch/);
});

// ---------------------------------------------------------------- fleet view

test("healthy fleet shows counts and cards", () => {
  const html = renderFleet(fleet(), { now: NOW });
  assert.match(html, /4 of 5/);
  assert.match(html, /1 offline/);
  assert.equal(count(html, 'class="card host"'), 5);
  assert.ok(!html.includes('class="banner'));
});

test("when the control plane is down, status is unknown and every host reads as last known", () => {
  const html = renderFleet(structuredClone(fx.fleet_controller_down), { now: NOW });
  assert.match(html, /Controller isn&#39;t responding/);
  assert.match(html, /Unknown/);
  assert.match(html, /status can&#39;t update/);
  assert.equal(count(html, "Last known"), 5);
  assert.ok(!html.includes(">Online<") && !html.includes("Offline ·"));
  assert.match(html, /as of /);
});

test("totals never draw missing data as zero, and say what they cover", () => {
  const none = fleet();
  Object.assign(none.fleet.os_updates, { total: 0, covered: 0 });
  assert.match(renderFleet(none, { now: NOW }), /—[\s\S]*not checked yet/);

  const partial = fleet();
  Object.assign(partial.fleet.os_updates, { covered: 2, of: 5 });
  assert.match(renderFleet(partial, { now: NOW }), /2 of 5 hosts checked/);
});

test("hosts whose update checks are failing are named", () => {
  const f = fleet();
  f.fleet.failed_check_hosts = ["nas"];
  assert.match(renderFleet(f, { now: NOW }), /Update checks are failing on nas/);
});

test("filters keep hosts with unknown update counts out of 'has updates'", () => {
  const hosts = fleet().hosts;
  const unknown = structuredClone(hosts[0]);
  unknown.host = "unchecked";
  Object.assign(unknown.summary, { os_updates: null, image_updates: null });
  const names = (list) => list.map((h) => h.host).sort();
  assert.deepEqual(names(filterHosts([...hosts, unknown], "updates")), ["gpu-box", "media-server", "nas", "pi-garage", "pi-hole"]);
  assert.deepEqual(names(filterHosts(hosts, "offline")), ["pi-garage"]);
  assert.equal(filterHosts(hosts, "all").length, 5);
});

test("empty states for no hosts and for a filter with no matches", () => {
  const none = fleet();
  none.hosts = [];
  assert.match(renderFleet(none, { now: NOW }), /No hosts yet[\s\S]*uv run worker\.py --role host/);
  const f = fleet();
  f.hosts = f.hosts.filter((h) => h.state === "online");
  assert.match(renderFleet(f, { now: NOW, filter: "offline" }), /No hosts match this filter/);
});

test("busy buttons say so", () => {
  const html = renderFleet(fleet(), { now: NOW, busy: ["all:refresh"] });
  assert.match(html, /Refreshing…/);
  assert.match(html, />Check for updates</);
});

// ---------------------------------------------------------------- host cards

test("host card states", () => {
  assert.match(hostCard(host("pi-garage"), NOW), /Offline · seen/);
  assert.match(hostCard(host("nas"), NOW), />Online</);

  const stuck = host("nas");
  Object.assign(stuck.meta, { stale: true, stale_reason: "entity_not_responding", error: "workflow task failing" });
  assert.match(hostCard(stuck, NOW), /Not responding/);
  assert.match(hostCard(stuck, NOW), /title="workflow task failing"/);

  const gone = host("nas");
  Object.assign(gone.meta, { stale: true, stale_reason: "workflow_not_running" });
  assert.match(hostCard(gone, NOW), /Workflow stopped/);
});

test("a host that has never answered shows no metrics or update pills", () => {
  const h = host("nas");
  Object.assign(h, { state: "collecting", has_data: false, metrics: null, last_error: null });
  const html = hostCard(h, NOW);
  assert.match(html, /Collecting…/);
  assert.match(html, /Waiting for the first snapshot/);
  assert.ok(!html.includes('class="pill warn"') && !html.includes("class=\"bars\""));
});

test("cards show security updates, reboot, image updates, GPU and SoC temperature", () => {
  const media = hostCard(host("media-server"), NOW);
  assert.match(media, /12 OS · 3 security/);
  assert.match(media, />Reboot</);
  assert.match(media, /1 image</);
  assert.match(hostCard(host("gpu-box"), NOW), /GPU 12%/);
  assert.match(hostCard(host("pi-hole"), NOW), /SoC 52\.1 °C/);
});

// ---------------------------------------------------------------- update pills

test("update pills only claim 'up to date' when everything that applies was checked", () => {
  const base = () => ({ updates: { state: "ok", os: { state: "ok", count: 0, zero_is_stale: false, reboot_required: false, security_count: 0 }, images: { state: "ok", updates: 0 } } });
  assert.deepEqual(updatePills(base()).map((p) => p.text), ["Up to date"]);

  const staleIndex = base();
  staleIndex.updates.os.zero_is_stale = true;
  assert.ok(!updatePills(staleIndex).some((p) => p.text === "Up to date"));

  const osError = base();
  osError.updates.os = { state: "error" };
  assert.deepEqual(updatePills(osError).map((p) => p.text), ["Some checks failed"]);

  const untracked = base();
  untracked.updates.os = { state: "untracked" };
  assert.deepEqual(updatePills(untracked).map((p) => p.text), ["Images up to date"]);

  assert.deepEqual(updatePills({ updates: { state: "never_checked" } }).map((p) => p.text), ["Updates not checked yet"]);
});

test("unknown reboot and security values draw nothing", () => {
  const h = host("media-server");
  h.updates.os.reboot_required = null;
  h.updates.os.security_count = null;
  const texts = updatePills(h).map((p) => p.text);
  assert.ok(!texts.includes("Reboot"));
  assert.ok(texts.every((t) => !t.includes("security")));
});

// ---------------------------------------------------------------- host detail

test("detail shows packages with how long they've waited", () => {
  const html = renderHost(nas(), { now: NOW });
  assert.match(html, /openssl/);
  assert.match(html, /waiting 41 d/);
  assert.match(html, /waiting 7 d/);
  assert.match(html, /checked 2 h ago/);
  const hidden = nas();
  hidden.updates.os.hidden_count = 5;
  assert.match(renderHost(hidden, { now: NOW }), /and 5 more/);
});

test("containers: new versions, new builds and unknowns are told apart", () => {
  const html = renderHost(nas(), { now: NOW });
  assert.match(html, /10\.11\.2 available/);
  assert.match(html, /16\.15 available/);
  assert.match(html, /18\.6 is a new major/);
  assert.match(html, /Newer build of this tag/);
  assert.match(html, /image has no registry digest/);
  assert.match(html, /unhealthy/);
  assert.match(html, /exited/);
  // redis and restic are up to date; the unknown image must not be counted among them
  assert.equal(count(html, ">Up to date<"), 2);
});

test("OS panel wording for each empty state", () => {
  const never = nas();
  never.updates = { state: "never_checked", checked_at: null, last_attempt_at: null, error: null, data_is_stale: false,
    os: { state: "not_checked" }, images: { state: "not_checked", updates: null, items: [] } };
  assert.match(renderHost(never, { now: NOW }), /Not checked yet\. The first check runs shortly/);

  const untracked = nas();
  untracked.updates.os = { state: "untracked" };
  assert.match(renderHost(untracked, { now: NOW }), /No supported package manager/);

  const errored = nas();
  errored.updates.os = { state: "error", error: "apt exited 100" };
  assert.match(renderHost(errored, { now: NOW }), /Couldn&#39;t check: apt exited 100/);

  const zero = nas();
  Object.assign(zero.updates.os, { count: 0, packages: [], hidden_count: 0, zero_is_stale: true, index_age_h: 200 });
  const html = renderHost(zero, { now: NOW });
  assert.match(html, /may be out of date/);
  assert.match(html, /8 d old/);

  const fine = nas();
  Object.assign(fine.updates.os, { count: 0, packages: [], hidden_count: 0, zero_is_stale: false });
  assert.match(renderHost(fine, { now: NOW }), /No updates pending\./);
});

test("a failed check doesn't present itself as fresh", () => {
  const h = nas();
  Object.assign(h.updates, { state: "last_check_failed", checked_at: null, error: "boom", data_is_stale: true });
  const html = renderHost(h, { now: NOW });
  assert.match(html, /The last check failed: boom\./);
  assert.match(html, /results from an earlier check/);
  assert.ok(!/checked \d+ \w+ ago/.test(html) && !html.includes("checked just now"));
});

test("security count is omitted when the package manager doesn't report it", () => {
  const h = nas();
  h.updates.os.security_count = null;
  h.updates.os.reboot_required = null;
  h.updates.os.packages.forEach((p) => (p.security = null));
  const html = renderHost(h, { now: NOW });
  assert.ok(!html.includes("security"));
  assert.ok(!html.includes("Reboot required"));
});

test("docker states", () => {
  const absent = nas();
  absent.docker = { state: "absent", containers: [], running: 0, total: 0 };
  assert.ok(!renderHost(absent, { now: NOW }).includes("Containers"));

  const empty = nas();
  empty.docker = { state: "empty", containers: [], running: 0, total: 0 };
  assert.match(renderHost(empty, { now: NOW }), /No containers on this host/);

  const denied = nas();
  denied.docker = { state: "error", error: "permission denied", hint: "docker_permission", containers: [] };
  const html = renderHost(denied, { now: NOW });
  assert.match(html, /Docker isn&#39;t readable: permission denied/);
  assert.match(html, /usermod -aG docker/);
});

test("hardware panel: GPU and Raspberry Pi throttling", () => {
  const gpu = renderHost(host("gpu-box"), { now: NOW });
  assert.match(gpu, /RTX 4070/);
  assert.match(gpu, /12% · 1500 of 12282 MB · 51 °C/);

  const pi = host("pi-hole");
  assert.match(renderHost(pi, { now: NOW }), /not throttled/);
  pi.metrics.pi.throttled = "throttled=0x50005";
  assert.match(renderHost(pi, { now: NOW }), /Throttled \(0x50005\)/);
});

test("detail for a host with no snapshot, and for an unknown host", () => {
  const h = host("nas");
  Object.assign(h, { state: "collecting", has_data: false, metrics: null, last_error: "schedule to start timeout" });
  const html = renderHost(h, { now: NOW });
  assert.match(html, /No snapshot yet/);
  assert.match(html, /Last error: schedule to start timeout/);
  assert.ok(!html.includes("OS updates"));
  assert.match(renderNotFound("zzz"), /Unknown host/);
});

test("detail shows the control plane banner and busy buttons", () => {
  const html = renderHost(nas(), { now: NOW, controlPlane: { state: "controller_down" }, busy: ["nas:refresh"] });
  assert.match(html, /Controller isn&#39;t responding/);
  assert.match(html, /Refreshing…/);
  assert.match(html, /data-host="nas"/);
});

test("long package lists collapse behind a toggle, and expand on request", () => {
  const rows = (html) => count(html.split("<h2>Containers</h2>")[0], "waiting ");  // OS panel only
  const collapsed = renderHost(nas(), { now: NOW });
  assert.equal(rows(collapsed), PACKAGE_LIMIT);
  assert.match(collapsed, /data-action="toggle-packages">Show all 21</);

  const expanded = renderHost(nas(), { now: NOW, expandPackages: true });
  assert.equal(rows(expanded), 21);
  assert.match(expanded, />Show fewer</);

  const short = nas();
  short.updates.os.packages = short.updates.os.packages.slice(0, 5);
  assert.ok(!renderHost(short, { now: NOW }).includes("toggle-packages"));
});

test("uptime under a day is shown in hours", () => {
  const h = nas();
  h.metrics.uptime_s = 7200;
  assert.match(renderHost(h, { now: NOW }), />2 h</);
  h.metrics.uptime_s = 3 * 86400 + 100;
  assert.match(renderHost(h, { now: NOW }), />3 d</);
});
