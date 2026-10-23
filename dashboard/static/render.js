// Pure functions: API payload in, HTML string out. No DOM access, so they run under node in tests.
// Every value that comes from the API goes through esc(): hostnames, container names, and
// error text are all untrusted. Missing data is never drawn as zero or healthy: the payload
// says which kind of "nothing" it is, and each area has wording for it.

export const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ---------------------------------------------------------------- small helpers

export function ago(iso, now) {
  const t = Date.parse(iso);
  if (!iso || Number.isNaN(t)) return "never";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function clock(iso) {
  const d = new Date(iso);
  return iso && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "unknown";
}

export const PACKAGE_LIMIT = 10;  // longer lists collapse behind a toggle
const hoursLabel = (h) => (h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} d`);
const gb = (mb) => (mb / 1024).toFixed(1);
const level = (pct) => (pct >= 90 ? "bad" : pct >= 80 ? "warn" : "");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function pill(text, tone = "muted", title = "") {
  return `<span class="pill ${tone}"${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</span>`;
}

function bar(label, pct, dim) {
  const has = typeof pct === "number";
  const width = has ? Math.min(100, Math.max(0, pct)) : 0;
  return `<div><div class="bar-label"><span>${esc(label)}</span><span>${has ? Math.round(pct) + "%" : "n/a"}</span></div>` +
    `<div class="bar"><span class="${dim ? "dim" : level(pct)}" style="width:${width}%"></span></div></div>`;
}

const note = (text, tone = "") => `<p class="note ${tone}">${esc(text)}</p>`;
const panel = (title, right, inner) =>
  `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2><span class="muted small">${esc(right)}</span></div>${inner}</section>`;

// ---------------------------------------------------------------- banner and notice

const BANNERS = {
  controller_down: (cp) => ({
    tone: "bad", retry: true,
    title: "Controller isn't responding",
    body: "Host status can't update until a controller worker is running. Refresh and update checks are queued and will run when it's back.",
    hint: "Start one with: uv run worker.py --role controller", detail: cp.detail,
  }),
  temporal_unreachable: (cp) => ({
    tone: "bad", retry: true,
    title: "Can't reach Temporal",
    body: "The dashboard can't read or signal any host. Showing the last data it received.",
    detail: cp.detail,
  }),
  dashboard_error: (cp) => ({
    tone: "bad", retry: true,
    title: "The dashboard hit an error",
    body: "Showing the last data it received.",
    detail: cp.detail,
  }),
  starting: () => ({
    tone: "info", retry: false,
    title: "Dashboard is starting",
    body: "Showing stored data until the first check with Temporal finishes.",
  }),
};

export function renderBanner(cp, opts = {}) {
  let b;
  if (opts.apiError) {
    b = {
      tone: "bad", retry: true, title: "Can't reach the dashboard service",
      body: opts.asOf ? `Showing data from ${clock(opts.asOf)}.` : "There's nothing to show yet.",
      detail: opts.apiError,
    };
  } else if (cp && BANNERS[cp.state]) {
    b = BANNERS[cp.state](cp);
  } else {
    return "";
  }
  return `<div class="banner ${b.tone}" role="status"><div><strong>${esc(b.title)}</strong><p>${esc(b.body)}</p>` +
    (b.hint ? `<p class="mono small">${esc(b.hint)}</p>` : "") +
    (b.detail ? `<p class="mono small muted">${esc(b.detail)}</p>` : "") +
    `</div>${b.retry ? `<button data-action="retry">Retry now</button>` : ""}</div>`;
}

export const renderNotice = (n) => (n ? `<div class="notice ${esc(n.tone || "")}" role="status">${esc(n.text)}</div>` : "");

// ---------------------------------------------------------------- shared host pieces

function statePill(h, now) {
  const m = h.meta || {};
  switch (m.stale_reason) {
    case "controller_down":
    case "temporal_unreachable":
    case "dashboard_error":
    case "starting":
      return m.fetched_at ? pill(`Last known · ${clock(m.fetched_at)}`, "muted") : pill("No data yet", "muted");
    case "entity_not_responding":
      return pill("Not responding", "bad", m.error || "");
    case "workflow_not_running":
      return pill("Workflow stopped", "muted", "This host's workflow isn't running. Restart its agent to register it again.");
  }
  if (h.state === "online") return pill("Online", "ok");
  if (h.state === "offline") return pill(h.last_seen ? `Offline · seen ${ago(h.last_seen, now)}` : "Offline", "bad");
  return pill("Collecting…", "info");
}

export function updatePills(h) {
  const u = h.updates || {};
  if (u.state === "never_checked") return [{ text: "Updates not checked yet", tone: "muted" }];
  const os = u.os || {}, im = u.images || {}, out = [];
  if (u.state === "last_check_failed") out.push({ text: "Last update check failed", tone: "warn" });
  if (os.state === "ok") {
    if (os.count > 0) {
      out.push({ text: `${os.count} OS` + (os.security_count > 0 ? ` · ${os.security_count} security` : ""),
                 tone: os.security_count > 0 ? "warn" : "muted" });
    } else if (os.zero_is_stale) {
      out.push({ text: "No OS updates (index is old)", tone: "muted" });
    }
    if (os.reboot_required === true) out.push({ text: "Reboot", tone: "warn" });
  }
  if (im.state === "ok" && im.updates > 0) out.push({ text: plural(im.updates, "image"), tone: "info" });
  if (!out.length && u.state === "ok") {
    // Only claim "up to date" when everything that applies was actually checked.
    const imagesFine = im.state === "ok" || im.state === "not_applicable";
    if (os.state === "ok" && imagesFine) out.push({ text: "Up to date", tone: "ok" });
    else if (os.state === "untracked" && im.state === "ok") out.push({ text: "Images up to date", tone: "ok" });
    else if (os.state === "untracked" && im.state === "not_applicable") out.push({ text: "Nothing to check", tone: "muted" });
    else out.push({ text: "Some checks failed", tone: "warn" });
  }
  return out;
}

const dockerSummary = (h) => {
  const d = h.docker || {};
  return d.state === "ok" ? `${d.running} of ${d.total} running`
    : d.state === "empty" ? "No containers"
    : d.state === "error" ? "Docker unavailable" : "";
};

function extras(h) {
  const m = h.metrics;
  if (!m) return "";
  const parts = [];
  if (m.gpus && m.gpus.length && typeof m.gpus[0].utilization_pct === "number") parts.push(`GPU ${m.gpus[0].utilization_pct}%`);
  const t = m.pi && /temp=([\d.]+)/.exec(m.pi.soc_temp || "");
  if (t) parts.push(`SoC ${t[1]} °C`);
  return parts.join(" · ");
}

// ---------------------------------------------------------------- fleet view

export function filterHosts(hosts, filter) {
  if (filter === "offline") return hosts.filter((h) => h.state === "offline");
  if (filter === "updates") return hosts.filter((h) => h.summary.os_updates > 0 || h.summary.image_updates > 0);
  return hosts;
}

export function hostCard(h, now) {
  const dim = Boolean(h.meta && h.meta.stale) || h.state !== "online";
  const m = h.metrics;
  const body = m
    ? `<div class="bars">${bar("CPU", m.cpu_pct, dim)}${bar("Memory", m.mem_pct, dim)}${bar("Disk", m.disk_used_pct, dim)}</div>`
    : `<p class="muted small">${esc(h.last_error || "Waiting for the first snapshot.")}</p>`;
  const pills = h.has_data ? updatePills(h).map((p) => pill(p.text, p.tone)).join("") : "";
  return `<a class="card host" href="#/host/${encodeURIComponent(h.host)}">` +
    `<div class="card-head"><div><div class="strong ${dim ? "muted" : ""}">${esc(h.host)}</div>` +
    `<div class="muted small">${esc(m ? m.os : "")}</div></div>${statePill(h, now)}</div>` +
    `${body}<div class="pills">${pills}</div>` +
    `<div class="card-foot muted small"><span>${esc(dockerSummary(h))}</span><span>${esc(extras(h))}</span></div></a>`;
}

const metric = (label, value, sub, muted) =>
  `<div class="metric"><p class="muted small">${esc(label)}</p><p class="value ${muted ? "muted" : ""}">${esc(value)}</p>` +
  `<p class="muted small">${esc(sub)}</p></div>`;

function metricCards(data) {
  const f = data.fleet, degraded = data.control_plane.state !== "ok";
  const fetched = data.hosts.map((h) => h.meta && h.meta.fetched_at).filter(Boolean).sort();
  const asOf = degraded && fetched.length ? `as of ${clock(fetched[fetched.length - 1])}` : "";
  const coverage = (t) => (t.covered > 0 && t.covered < t.of ? `${t.covered} of ${t.of} hosts checked` : "");
  const join = (...parts) => parts.filter(Boolean).join(" · ");

  const online = degraded ? ["Unknown", "status can't update"]
    : [`${f.hosts_online} of ${f.hosts_total}`,
       join(f.hosts_offline ? `${f.hosts_offline} offline` : "", f.hosts_collecting ? `${f.hosts_collecting} collecting` : "")];
  const os = f.os_updates, sec = f.security_updates, img = f.image_updates, rb = f.reboot_required;
  return `<div class="metrics">` +
    metric("Hosts online", online[0], online[1], degraded) +
    metric("OS updates", os.covered ? String(os.total) : "—",
           os.covered ? join(sec.covered ? `${sec.total} security` : "", coverage(os), asOf) : "not checked yet", degraded) +
    metric("Image updates", img.covered ? String(img.total) : "—",
           img.covered ? join(coverage(img), asOf) : "not checked yet", degraded) +
    metric("Reboot needed", rb.covered ? String(rb.hosts.length) : "—",
           rb.covered ? join(rb.hosts.join(", "), asOf) : "not checked yet", degraded) +
    `</div>`;
}

const busyLabel = (opts, key, idle, busy) => ((opts.busy || []).includes(key) ? busy : idle);

export function renderFleet(data, opts = {}) {
  const now = opts.now ?? Date.now(), filter = opts.filter || "all";
  const fetched = data.hosts.map((h) => h.meta && h.meta.fetched_at).filter(Boolean).sort();
  const banner = renderBanner(data.control_plane, { apiError: opts.apiError, asOf: fetched[fetched.length - 1] });
  const shown = filterHosts(data.hosts, filter);
  const chip = (value, label) => `<button class="chip ${filter === value ? "on" : ""}" data-action="filter" data-value="${value}">${label}</button>`;

  let list;
  if (!data.hosts.length) {
    list = `<div class="empty"><strong>No hosts yet</strong><p>Start a host agent with <code>uv run worker.py --role host</code> and it will appear here.</p></div>`;
  } else if (!shown.length) {
    list = `<div class="empty"><strong>No hosts match this filter</strong><p><button data-action="filter" data-value="all">Show all hosts</button></p></div>`;
  } else {
    list = `<div class="grid">${shown.map((h) => hostCard(h, now)).join("")}</div>`;
  }
  const failing = data.fleet.failed_check_hosts || [];
  return banner + renderNotice(opts.notice) +
    `<div class="topbar"><h1>Homelab</h1><div class="actions">` +
    `<button data-action="refresh-all">${busyLabel(opts, "all:refresh", "Refresh all", "Refreshing…")}</button>` +
    `<button data-action="check-all">${busyLabel(opts, "all:check", "Check for updates", "Checking…")}</button></div></div>` +
    metricCards(data) +
    (failing.length ? note(`Update checks are failing on ${failing.join(", ")}.`, "warn") : "") +
    `<div class="chips">${chip("all", "All")}${chip("updates", "Has updates")}${chip("offline", "Offline")}</div>` + list;
}

// ---------------------------------------------------------------- host detail

function metricRow(h) {
  const m = h.metrics;
  if (!m) return "";
  const stale = h.meta && h.meta.stale;
  const pct = (v) => (typeof v === "number" ? `${Math.round(v)}%` : "n/a");
  const card = (label, v, sub) => `<div class="metric"><p class="muted small">${label}</p>` +
    `<p class="value ${stale ? "muted" : level(v)}">${pct(v)}</p><p class="muted small">${esc(sub)}</p></div>`;
  return `<div class="metrics">` +
    card("CPU", m.cpu_pct, `load ${(m.load_avg || []).join(" ")}`) +
    card("Memory", m.mem_pct, `${gb(m.mem_used_mb)} of ${gb(m.mem_total_mb)} GB`) +
    card("Disk /", m.disk_used_pct, `of ${Math.round(m.disk_total_gb)} GB`) +
    `<div class="metric"><p class="muted small">Uptime</p><p class="value">${m.uptime_s < 86400 ? `${Math.floor(m.uptime_s / 3600)} h` : `${Math.floor(m.uptime_s / 86400)} d`}</p>` +
    `<p class="muted small">${esc(plural(m.cpu_cores, "core"))}</p></div></div>`;
}

function hardwarePanel(h) {
  const m = h.metrics;
  if (!m) return "";
  const rows = (m.gpus || []).map((g) =>
    `<div class="row"><span>${esc(g.name)}</span><span class="small">${esc(g.utilization_pct)}% · ${esc(g.memory_used_mb)} of ${esc(g.memory_total_mb)} MB · ${esc(g.temp_c)} °C</span></div>`);
  if (m.pi) {
    const temp = /temp=([\d.]+)/.exec(m.pi.soc_temp || "");
    const thr = /throttled=(0x[0-9a-f]+)/i.exec(m.pi.throttled || "");
    const throttled = thr && parseInt(thr[1], 16) !== 0;
    rows.push(`<div class="row"><span>Raspberry Pi</span><span class="small">${temp ? `SoC ${esc(temp[1])} °C` : ""}` +
      `${thr ? ` · ${throttled ? pill(`Throttled (${thr[1]})`, "warn") : "not throttled"}` : ""}</span></div>`);
  }
  return rows.length ? panel("Hardware", "", rows.join("")) : "";
}

function osPanel(h, now, ages, expanded) {
  const u = h.updates || {}, os = u.os || {};
  let right = "", body = "";
  if (u.state === "ok") {
    right = `checked ${ago(u.checked_at, now)}` + (os.state === "ok" && os.index_age_h != null ? ` · package index ${hoursLabel(os.index_age_h)} old` : "");
  } else if (u.state === "last_check_failed") {
    right = `last attempt ${ago(u.last_attempt_at, now)}`;
    body += note(`The last check failed: ${u.error}.` + (u.data_is_stale ? " Showing results from an earlier check." : ""), "warn");
  }
  if (u.state === "never_checked") body += note("Not checked yet. The first check runs shortly after the host comes online.");
  else if (os.state === "untracked") body += note("Not tracked. No supported package manager was found on this host.");
  else if (os.state === "error") body += note(`Couldn't check: ${os.error}`, "warn");
  else if (os.state === "not_checked") body += note("No successful check yet.");
  else if (os.state === "ok") {
    const pills = [pill(plural(os.count, "package"), "muted")];
    if (os.security_count != null) pills.push(pill(`${os.security_count} security`, os.security_count > 0 ? "warn" : "muted"));
    if (os.reboot_required === true) pills.push(pill("Reboot required", "warn"));
    body += `<div class="pills">${pills.join("")}</div>`;
    if (os.count === 0) {
      body += note(os.zero_is_stale
        ? `No updates pending, but the package index is ${hoursLabel(os.index_age_h)} old, so this may be out of date.`
        : "No updates pending.");
    } else {
      const shown = expanded ? os.packages : os.packages.slice(0, PACKAGE_LIMIT);
      body += shown.map((p) => {
        const age = ages[`os_package|${p.name}`];
        return `<div class="row"><span class="mono small">${esc(p.name)} <span class="muted">${esc(p.current ? `${p.current} → ${p.available}` : p.available)}</span></span>` +
          `<span class="small">${p.security ? pill("security", "warn") : ""} <span class="muted">${age == null ? "" : age < 1 ? "new today" : `waiting ${age} d`}</span></span></div>`;
      }).join("");
      if (os.packages.length > PACKAGE_LIMIT) {
        body += `<div class="row"><button class="small-btn" data-action="toggle-packages">${expanded ? "Show fewer" : `Show all ${os.packages.length}`}</button></div>`;
      }
      if (os.hidden_count > 0) body += `<div class="row muted small">and ${os.hidden_count} more</div>`;
    }
  }
  return panel("OS updates", right, body);
}

const containerState = (c) => {
  if (c.health === "healthy") return pill("healthy", "ok");
  if (c.health === "unhealthy") return pill("unhealthy", "warn");
  if (c.health === "starting") return pill("starting", "info");
  if (c.state === "running") return pill("running", "ok");
  if (c.state === "restarting") return pill("restarting", "warn");
  return pill(c.state || "unknown", "muted");
};

function updateCell(c, ages) {
  const u = c.update || {};
  const waits = [ages[`image_tag|${c.image}`], ages[`image_build|${c.image}`]].filter((d) => d != null);
  const wait = waits.length ? `<div class="small muted">${Math.max(...waits) < 1 ? "new today" : `waiting ${Math.max(...waits)} d`}</div>` : "";
  switch (u.state) {
    case "update_available": {
      const parts = [];
      if (u.newest_tag_same_major) parts.push(pill(`${u.newest_tag_same_major} available`, "info"));
      if (u.newest_tag && u.newest_tag !== u.newest_tag_same_major) {
        parts.push(u.newest_tag_same_major ? `<div class="small muted">${esc(u.newest_tag)} is a new major</div>` : pill(`${u.newest_tag} available`, "info"));
      }
      if (u.new_build) parts.push(pill("Newer build of this tag", "info"));
      return parts.join("") + wait;
    }
    case "up_to_date": return `<span class="muted">Up to date</span>`;
    case "unknown": return `<span class="muted">Unknown</span><div class="small muted">${esc(u.reason || "")}</div>`;
    default: return `<span class="muted">${esc(u.reason || "Not checked yet")}</span>`;
  }
}

function containersPanel(h, ages) {
  const d = h.docker || {};
  if (d.state === "absent" || d.state === "unknown") return "";
  const pending = h.updates && h.updates.images && typeof h.updates.images.updates === "number" ? ` · ${h.updates.images.updates} with updates` : "";
  const right = d.state === "ok" ? `${d.running} running${pending}` : "";
  if (d.state === "error") {
    return panel("Containers", "", note(`Docker isn't readable: ${d.error}`, "warn") +
      (d.hint === "docker_permission" ? note("Add the agent's user to the docker group, then restart the worker: sudo usermod -aG docker $USER") : ""));
  }
  if (d.state === "empty") return panel("Containers", "", note("No containers on this host."));
  const rows = d.containers.map((c) => {
    const cpu = typeof c.cpu_pct_of_host === "number" ? `${c.cpu_pct_of_host}%` : "n/a";
    const mem = typeof c.mem_used_mb === "number" ? `${Math.round(c.mem_used_mb)} MB` : "n/a";
    return `<div class="crow"><div class="cname"><div class="strong">${esc(c.name)}</div><div class="mono small muted">${esc(c.image)}</div></div>` +
      `<div>${containerState(c)}</div><div class="small">${cpu} · ${mem}</div><div>${updateCell(c, ages)}</div></div>`;
  }).join("");
  return panel("Containers", right,
    `<div class="crow head small muted"><div>Container</div><div>State</div><div>CPU · Mem</div><div>Image update</div></div>${rows}`);
}

export function renderHost(h, ctx = {}) {
  const now = ctx.now ?? Date.now();
  const ages = {};
  for (const p of h.pending_updates || []) ages[`${p.kind}|${p.name}`] = p.age_days;
  const m = h.metrics;
  const sub = m ? `${m.os} · kernel ${m.kernel} · ${m.arch}` : "";
  const seen = h.last_seen ? ` · seen ${ago(h.last_seen, now)}` : "";
  const enc = encodeURIComponent(h.host);
  const busy = ctx.busy || [];
  const noData = !h.has_data
    ? panel("No snapshot yet", "", note(h.last_error ? `Last error: ${h.last_error}` : "Waiting for the first snapshot."))
    : "";
  return renderBanner(ctx.controlPlane, { apiError: ctx.apiError, asOf: h.meta && h.meta.fetched_at }) +
    renderNotice(ctx.notice) +
    `<a class="back" href="#/">Back to fleet</a>` +
    `<div class="topbar"><div><div class="title-row"><h1>${esc(h.host)}</h1>${statePill(h, now)}</div>` +
    `<p class="muted small">${esc(sub + seen)}</p></div><div class="actions">` +
    `<button data-action="refresh" data-host="${esc(enc)}">${busy.includes(`${h.host}:refresh`) ? "Refreshing…" : "Refresh"}</button>` +
    `<button data-action="check-updates" data-host="${esc(enc)}">${busy.includes(`${h.host}:check`) ? "Checking…" : "Check for updates"}</button></div></div>` +
    noData + metricRow(h) + hardwarePanel(h) +
    (h.has_data ? osPanel(h, now, ages, ctx.expandPackages) : "") + (h.has_data ? containersPanel(h, ages) : "");
}

export function renderNotFound(name) {
  return `<a class="back" href="#/">Back to fleet</a><div class="empty"><strong>Unknown host</strong><p>${esc(name)} isn't registered with the dashboard.</p></div>`;
}
