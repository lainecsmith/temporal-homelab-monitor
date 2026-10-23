// The only file that touches the DOM, the network, and timers. All HTML comes from render.js.
import { renderBanner, renderFleet, renderHost, renderNotFound } from "./render.js";

const POLL_MS = 10_000;          // how often the page re-reads the API (the API polls Temporal on its own schedule)
const SETTLE_MS = 4_000;         // after a signal, give the host time to collect before asking for fresh data
const NOTICE_MS = 10_000;

const root = document.getElementById("app");
const state = {
  fleet: null,          // /api/hosts payload
  detail: undefined,    // host payload; undefined = loading, null = unknown host
  controlPlane: null,   // /api/health, for the detail view's banner
  filter: "all",
  apiError: null,       // set when the dashboard service itself can't be reached
  notice: null,
  busy: [],
  expandPackages: false,
  html: "",
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function api(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw new Error("The dashboard service didn't respond.");
  }
  if (!res.ok) {
    let message = `Request failed (${res.status}).`;
    try { message = String((await res.json()).detail || message); } catch { /* keep the generic message */ }
    throw Object.assign(new Error(message), { status: res.status });
  }
  return res.json();
}

function route() {
  const m = /^#\/host\/(.+)$/.exec(location.hash);
  if (!m) return { view: "fleet" };
  try { return { view: "host", name: decodeURIComponent(m[1]) }; } catch { return { view: "host", name: m[1] }; }
}

// ---------------------------------------------------------------- render

function currentHtml() {
  const r = route(), now = Date.now();
  const common = { now, apiError: state.apiError, notice: state.notice, busy: state.busy, expandPackages: state.expandPackages };
  if (r.view === "fleet") {
    if (state.fleet) return renderFleet(state.fleet, { ...common, filter: state.filter });
    return state.apiError ? renderBanner(null, { apiError: state.apiError }) : `<p class="muted">Loading…</p>`;
  }
  if (state.detail) return renderHost(state.detail, { ...common, controlPlane: state.controlPlane });
  if (state.detail === null) return renderNotFound(r.name);
  return `<a class="back" href="#/">Back to fleet</a>` +
    (state.apiError ? renderBanner(null, { apiError: state.apiError }) : `<p class="muted">Loading…</p>`);
}

function render() {
  const html = currentHtml();
  if (html === state.html) return;
  // Replacing the markup would drop keyboard focus, so put it back on the matching control.
  const a = document.activeElement && document.activeElement.dataset;
  root.innerHTML = html;
  state.html = html;
  if (a && a.action && root.querySelector) {
    const same = [...root.querySelectorAll("[data-action]")].find(
      (el) => el.dataset.action === a.action && el.dataset.host === a.host && el.dataset.value === a.value);
    if (same) same.focus();
  }
}

// ---------------------------------------------------------------- data

async function load() {
  const r = route();
  try {
    if (r.view === "fleet") {
      state.fleet = await api("/api/hosts");
    } else {
      const [detail, health] = await Promise.all([
        api(`/api/hosts/${encodeURIComponent(r.name)}`).catch((e) => { if (e.status === 404) return null; throw e; }),
        api("/api/health"),
      ]);
      state.detail = detail;
      state.controlPlane = health;
    }
    state.apiError = null;
  } catch (e) {
    state.apiError = e.message;
  }
  render();
}

function setNotice(notice) {
  state.notice = notice;
  render();
  setTimeout(() => { if (state.notice === notice) { state.notice = null; render(); } }, NOTICE_MS);
}

async function retry() {
  try { await api("/api/poll", { method: "POST" }); } catch { /* load() reports the problem */ }
  await load();
}

// Sends the signal to each host. If the control plane is down the API says the request was
// queued, and there's nothing to wait for. Otherwise wait a moment, then ask the API to poll.
async function signalHosts(names, kind) {
  const path = kind === "refresh" ? "refresh" : "check-updates";
  const keys = [...names.map((n) => `${n}:${kind}`), ...(names.length > 1 ? [`all:${kind}`] : [])];
  state.busy.push(...keys);
  render();
  let queued = false;
  const failures = [];
  await Promise.all(names.map(async (n) => {
    try {
      const res = await api(`/api/hosts/${encodeURIComponent(n)}/${path}`, { method: "POST" });
      queued = queued || res.queued;
    } catch (e) {
      failures.push(`${n}: ${e.message}`);
    }
  }));
  if (failures.length) {
    setNotice({ tone: "warn", text: `Couldn't send the request. ${failures.join(" ")}` });
  } else if (queued) {
    setNotice({ tone: "warn", text: "The controller is down. Your request is queued and will run when it's back." });
  } else {
    setNotice({ tone: "", text: kind === "refresh" ? "Refresh requested." : "Update check started. It can take a few minutes." });
    await sleep(SETTLE_MS);
    try { await api("/api/poll", { method: "POST" }); } catch { /* the next regular load will show the state */ }
  }
  state.busy = state.busy.filter((k) => !keys.includes(k));
  await load();
}

// ---------------------------------------------------------------- events

root.addEventListener("click", (event) => {
  const el = event.target.closest("[data-action]");
  if (!el) return;
  const { action, host, value } = el.dataset;
  const name = host ? decodeURIComponent(host) : null;
  const allHosts = () => (state.fleet ? state.fleet.hosts.map((h) => h.host) : []);
  if (action === "filter") { state.filter = value; render(); }
  else if (action === "toggle-packages") { state.expandPackages = !state.expandPackages; render(); }
  else if (action === "retry") retry();
  else if (action === "refresh" && name) signalHosts([name], "refresh");
  else if (action === "check-updates" && name) signalHosts([name], "check");
  else if (action === "refresh-all") signalHosts(allHosts(), "refresh");
  else if (action === "check-all") signalHosts(allHosts(), "check");
});

window.addEventListener("hashchange", () => {
  state.detail = undefined;
  state.notice = null;
  state.expandPackages = false;
  window.scrollTo(0, 0);
  render();
  load();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
setInterval(() => { if (!document.hidden) load(); }, POLL_MS);

load();
