// Backpacker Index — Initial Fill dashboard
// Vanilla JS + SSE. No build step.

const $ = (sel, ctx) => (ctx || document).querySelector(sel);
const $$ = (sel, ctx) => Array.from((ctx || document).querySelectorAll(sel));

// Tabs
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$("main section").forEach((s) => s.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    document.getElementById("tab-" + tab).classList.add("active");
    if (tab === "config") loadConfig();
    if (tab === "logs") loadLogs();
  });
});

function fmtDuration(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.floor(s));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}h${m % 60}m`;
  }
  return `${m}m${sec.toString().padStart(2, "0")}s`;
}

function fmtSize(n) {
  if (n == null) return "—";
  if (n >= 1000) return (n / 1000).toFixed(1) + "K";
  return String(n);
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString();
  } catch {
    return iso;
  }
}

// --- Run tab ----------------------------------------------------------------

async function refreshStatus() {
  try {
    const r = await fetch("/api/status");
    const data = await r.json();
    const s = data.orchestrator;
    // New unified progress block. Falls back to legacy aggregate
    // keys so older deployments keep rendering.
    const p = data.progress || {};
    const total = p.total ?? data.aggregate?.total_articles ?? 0;
    const done = p.done ?? data.aggregate?.total_done ?? 0;
    const inProgress = p.in_progress ?? data.aggregate?.total_in_progress ?? 0;
    const waiting = p.waiting ?? 0;
    const failedPerm = p.failed_perm ?? p.failed_permanent ?? data.aggregate?.total_failed_permanent ?? 0;
    const pending = p.pending ?? 0;
    const pct = p.pct ?? (total ? Math.round((done / total) * 1000) / 10 : 0);
    $("#run-state").textContent = s.state;
    $("#run-state").className = "status-pill " + s.state;
    $("#elapsed").textContent = fmtDuration(s.elapsed_s);
    $("#total-articles").textContent = total;
    $("#total-done").textContent = done;
    $("#total-in-progress").textContent = inProgress;
    $("#total-pending").textContent = pending;
    $("#total-waiting").textContent = waiting;
    $("#total-failed").textContent = failedPerm;
    $("#global-progress-pct").textContent = pct.toFixed(1) + "%";
    $("#global-progress-detail").textContent =
      total > 0
        ? `${done} / ${total} (${pending} claimable · ${waiting} in cooldown)`
        : "no candidates loaded";
    // Unified multi-segment bar. Each segment is sized as a
    // percentage of the total candidate count. The bar always
    // sums to 100% width (the segments fill in turn).
    const sum = Math.max(1, total);
    const pctDone = (done / sum) * 100;
    const pctInflight = (inProgress / sum) * 100;
    const pctWaiting = (waiting / sum) * 100;
    const pctFailed = (failedPerm / sum) * 100;
    const pctPending = (pending / sum) * 100;
    // The unified bar is a SINGLE div with a multi-stop linear-gradient
    // background. Earlier implementations used flex children with
    // per-segment widths, but flex children can collapse to 0 in some
    // edge cases (flex-shrink + min-width interactions, browser
    // inconsistencies). A gradient renders the same way everywhere.
    const bar = $("#global-progress-bar");
    if (bar) {
      // Clamp to [0, 100] so the gradient stops never exceed 100%.
      const d = Math.max(0, Math.min(100, pctDone));
      const i = d + Math.max(0, Math.min(100 - d, pctInflight));
      const w = i + Math.max(0, Math.min(100 - i, pctWaiting));
      const f = w + Math.max(0, Math.min(100 - w, pctFailed));
      const p = f + Math.max(0, Math.min(100 - f, pctPending));
      bar.style.background =
        `linear-gradient(to right,` +
        ` var(--ok) 0% ${d.toFixed(3)}%,` +
        ` var(--progress) ${d.toFixed(3)}% ${i.toFixed(3)}%,` +
        ` var(--warn) ${i.toFixed(3)}% ${w.toFixed(3)}%,` +
        ` var(--err) ${w.toFixed(3)}% ${f.toFixed(3)}%,` +
        ` transparent ${f.toFixed(3)}% ${p.toFixed(3)}%,` +
        ` transparent ${p.toFixed(3)}% 100%)`;
    }
    // Update tasks cards too
    renderTasks(data.progress && data.progress.per_task ? data.progress.per_task : {});
  } catch (e) {
    console.error(e);
  }
}

function renderTasks(perTask) {
  const grid = $("#tasks-grid");
  const taskNames = Object.keys(perTask);
  if (taskNames.length === 0) {
    grid.innerHTML = '<p class="empty">No tasks registered. Add a task to <code>fill_state.TASK_DEFINITIONS</code> to see it here.</p>';
    return;
  }
  grid.innerHTML = taskNames.map((name) => {
    const t = perTask[name] || {};
    const total = t.total || 0;
    const done = t.done || 0;
    const inP = t.in_progress || 0;
    const wait = t.waiting || 0;
    const fail = t.failed_perm || 0;
    const pend = t.pending || 0;
    const pct = t.pct || 0;
    const sum = Math.max(1, total);
    const w = (n) => ((n / sum) * 100).toFixed(2) + "%";
    const state = pend > 0 ? "claimable" : (wait > 0 ? "waiting" : "complete");
    const cardCls = "task-card task-" + state;
    return `
      <div class="${cardCls}">
        <div class="task-head">
          <strong>${escapeHtml(t.label || name)}</strong>
          <span class="muted">${escapeHtml(name)}</span>
          <span class="task-stat">${pct.toFixed(1)}%</span>
        </div>
        <div class="task-desc">${escapeHtml(t.description || "")}</div>
        <div class="task-mini-bar" title="done | in flight | cooldown | failed | pending">
          <div class="seg seg-done" style="width:${w(done)}"></div>
          <div class="seg seg-inflight" style="width:${w(inP)}"></div>
          <div class="seg seg-waiting" style="width:${w(wait)}"></div>
          <div class="seg seg-failed" style="width:${w(fail)}"></div>
          <div class="seg seg-pending" style="width:${w(pend)}"></div>
        </div>
        <div class="task-stats">
          <span class="stat-done">${done} done</span>
          <span class="stat-progress">${inP} in flight</span>
          <span class="warn">${wait} in cooldown</span>
          <span class="stat-failed">${fail} failed</span>
          <span class="stat-remaining">${pend} pending</span>
        </div>
        <div class="lane-meta muted">${done} / ${total}</div>
        <div class="task-actions">
          <button class="task-btn primary" data-task-action="start" data-task="${escapeHtml(name)}">Start</button>
          <button class="task-btn danger" data-task-action="stop" data-task="${escapeHtml(name)}">Stop</button>
        </div>
      </div>
    `;
  }).join("");
  $$(".task-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.taskAction;
      const task = btn.dataset.task;
      btn.disabled = true;
      try {
        const r = await fetch(`/api/tasks/${encodeURIComponent(task)}/${action}`, { method: "POST" });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          showError(`${action} ${task} failed`, body);
        } else {
          showInlineFeedback(body.message || `${action} ${task} OK`, "ok");
          refreshAll();
        }
      } catch (e) {
        showError(`${action} ${task} failed`, String(e));
      } finally {
        btn.disabled = false;
      }
    });
  });
}

async function checkKeys() {
  // Show a banner if any enabled lane doesn't have an api_key set.
  const r = await fetch("/api/lanes");
  const lanes = (await r.json()).lanes.filter((l) => l.enabled && l.provider !== "local");
  const missing = lanes.filter((l) => !l.api_key_set);
  const banner = $("#keys-banner");
  if (missing.length === 0) {
    banner.classList.add("hidden");
    $("#btn-start").disabled = false;
  } else {
    banner.classList.remove("hidden");
    banner.innerHTML =
      "<strong>Missing API keys:</strong> " +
      missing.map((l) => `<code>${escapeHtml(l.name)}</code>`).join(", ") +
      " &mdash; paste each one in its lane card on the Configure tab.";
    $("#btn-start").disabled = true;
  }
}

async function refreshLanes() {
  // Lane-worker cards were removed from the UI by request. The
  // underlying /api/lanes endpoint and per-lane start/stop endpoints
  // are still available for direct API use, but the dashboard no
  // longer surfaces them. (Use the top-bar Start/Stop to control
  // the whole run, and the Tasks section to track per-task progress.)
  return;
}

async function refreshActivity() {
  try {
    const r = await fetch("/api/activity?limit=30");
    const data = await r.json();
    const tbody = $("#activity-body");
    if (!data.activity.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No activity yet.</td></tr>';
      return;
    }
    tbody.innerHTML = data.activity
      .slice(0, 30)
      .map((a) => {
        const cls =
          a.status === "done" ? "ok" :
          a.status === "in_progress" ? "progress" :
          a.status === "failed_permanent" ? "err" : "warn";
        const slug = a.slug || "";
        let db = "—";
        if (a.status === "done") {
          if (a.db_written === true) {
            db = '<span class="stat-done" title="Article was written to the staging DB.">written</span>';
          } else if (a.db_written === false) {
            const reason = a.db_error ? String(a.db_error) : "unknown error";
            const short = reason.length > 60 ? reason.slice(0, 57) + "…" : reason;
            db = `<span class="stat-failed" title="${escapeAttr(reason)}">failed: ${escapeHtml(short)}</span>`;
          } else {
            db = '<span class="muted" title="DB write not attempted or status unknown.">unknown</span>';
          }
        }
        const task = a.task || "guide_fill";
        return `
          <tr>
            <td><span class="status-dot ${cls}"></span> ${escapeHtml(a.status)}</td>
            <td><a href="#" class="article-link" data-slug="${escapeHtml(slug)}" title="Open on staging site">${escapeHtml(slug || "—")}</a></td>
            <td>${escapeHtml(task)}</td>
            <td>${escapeHtml(a.lane || "—")}</td>
            <td>${fmtSize(a.size || a.input_chars)}</td>
            <td>${a.elapsed_s != null ? a.elapsed_s.toFixed(1) + "s" : "—"}</td>
            <td>${db}</td>
            <td class="muted">${fmtTime(a.at)}</td>
          </tr>
        `;
      })
      .join("");
    // Wire up the article links: look up the staging URL then open it.
    $$(".article-link").forEach((a) => {
      a.addEventListener("click", async (ev) => {
        ev.preventDefault();
        const slug = a.dataset.slug;
        if (!slug) return;
        const originalText = a.textContent;
        a.textContent = "loading…";
        a.style.opacity = "0.6";
        try {
          const r = await fetch(`/api/staging/article/${encodeURIComponent(slug)}`);
          const data = await r.json();
          if (!r.ok) {
            showError("Open on staging failed", data);
          } else if (!data.found) {
            showError("Article not on staging", data.hint || "Staging doesn't have this slug yet.");
          } else {
            window.open(data.url, "_blank", "noopener");
            a.textContent = `→ ${slug}`;
            setTimeout(() => { a.textContent = originalText; a.style.opacity = ""; }, 2000);
            return;
          }
        } catch (e) {
          showError("Open on staging failed", String(e));
        }
        a.textContent = originalText;
        a.style.opacity = "";
      });
    });
  } catch (e) {
    console.error(e);
  }
}

$("#btn-start").addEventListener("click", async () => {
  const btn = $("#btn-start");
  btn.disabled = true;
  try {
    const r = await fetch("/api/run/start", { method: "POST" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      showError("Start failed", await parseError(r));
      showInlineFeedback(`Start failed: ${await parseErrorText(r)}`, "err");
      return;
    }
    // Show the server's message immediately so the user sees
    // something happened even if the SSE / 15s poll hasn't
    // updated the status pill yet.
    const msg = body.message || `Start requested. State: ${body.state}`;
    if (body.state === "error") {
      showInlineFeedback(msg, "err");
    } else if (body.state === "complete" || body.state === "waiting") {
      showInlineFeedback(msg, "warn");
    } else {
      showInlineFeedback(msg, "ok");
    }
    refreshAll();
  } catch (e) {
    showError("Start failed", String(e));
    showInlineFeedback(`Start failed: ${e}`, "err");
  } finally {
    btn.disabled = false;
  }
});
$("#btn-stop").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/run/stop", { method: "POST" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      showError("Stop failed", await parseError(r));
      return;
    }
    showInlineFeedback(body.message || "Stop requested.", "ok");
    refreshAll();
  } catch (e) {
    showError("Stop failed", String(e));
  }
});

function showInlineFeedback(message, kind) {
  const el = $("#start-feedback");
  if (!el) return;
  el.textContent = message;
  el.className = "toast-inline " + (kind || "ok");
  el.classList.remove("hidden");
  // Auto-hide after 6s for ok/warn, 12s for err.
  const ttl = kind === "err" ? 12000 : 6000;
  clearTimeout(showInlineFeedback._t);
  showInlineFeedback._t = setTimeout(() => el.classList.add("hidden"), ttl);
}

async function parseErrorText(r) {
  const text = await r.text();
  try {
    const data = JSON.parse(text);
    if (data && data.detail && data.detail.errors) return data.detail.errors.join("; ");
    if (data && data.detail) return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    return JSON.stringify(data);
  } catch {
    return text || `HTTP ${r.status}`;
  }
}

// --- Config tab -------------------------------------------------------------

async function loadConfig() {
  await loadLanesConfig();
}

// API keys are no longer a separate UI section. Each lane card carries
// its own key input — see ``laneEditor`` and the save handler in
// ``#btn-save-lanes``.

let _providerDefaults = {};

async function loadLanesConfig() {
  const [cfgR, defaultsR] = await Promise.all([
    fetch("/api/lanes/config"),
    fetch("/api/lanes/defaults"),
  ]);
  const data = await cfgR.json();
  _providerDefaults = (await defaultsR.json()).defaults || {};
  const container = $("#lanes-config");
  if (!data.lanes.length) {
    container.innerHTML = '<p class="empty">No lanes. Click "Add lane" to start.</p>';
    return;
  }
  container.innerHTML = data.lanes
    .map((l, i) => laneEditor(l, i))
    .join("") + `<button id="btn-add-lane" class="ghost">+ Add lane</button>`;
  $("#btn-add-lane").addEventListener("click", () => addLaneRow());
  $$("#lanes-config .btn-remove").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.closest(".lane-edit").remove();
    });
  });
  $$("#lanes-config .btn-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.closest(".lane-edit").classList.toggle("disabled");
    });
  });
  // Refresh placeholders when provider changes.
  $$("#lanes-config input[data-k='provider']").forEach((inp) => {
    inp.addEventListener("input", () => refreshBaseUrlPlaceholder(inp));
  });
  $$("#lanes-config .lane-edit").forEach(refreshBaseUrlPlaceholder);
  // Wire up the per-card key touch tracking and clear button.
  $$("#lanes-config .lane-edit").forEach((card) => {
    wireUpKeyTouchTracking(card);
    wireUpKeyClearButton(card);
  });
}

function refreshBaseUrlPlaceholder(scope) {
  const card = scope.closest ? scope.closest(".lane-edit") : scope;
  if (!card) return;
  const provider = card.querySelector("input[data-k='provider']").value.trim();
  const baseInput = card.querySelector("input[data-k='base_url']");
  const def = _providerDefaults[provider];
  baseInput.placeholder = def ? `(default: ${def})` : "(no default for this provider)";
}

function laneEditor(l, i) {
  return `
    <div class="lane-edit ${l.enabled ? "" : "disabled"}" data-index="${i}">
      <div class="row">
        <label>Name <input data-k="name" value="${escapeAttr(l.name || "")}"></label>
        <label>Provider <input data-k="provider" value="${escapeAttr(l.provider || "")}"></label>
        <label>Model <input data-k="model" value="${escapeAttr(l.model || "")}"></label>
      </div>
      <div class="row">
        <label class="grow">API key
          <input data-k="api_key" type="password" autocomplete="off" spellcheck="false"
                 placeholder="${escapeAttr(l.api_key_fingerprint ? "current: " + l.api_key_fingerprint : "(no key set — paste to set)")}">
          <span class="muted api-key-hint" data-hint-for="api_key">
            ${l.api_key_set
              ? `<code>${escapeHtml(l.api_key_fingerprint || "****")}</code> <button class="link btn-clear-key" type="button">clear</button>`
              : `<span class="warn">no key set</span>`}
          </span>
        </label>
      </div>
      <div class="row">
        <label>Min chars <input data-k="min_chars" type="number" value="${l.min_chars ?? 0}"></label>
        <label>Max chars <input data-k="max_chars" type="number" value="${l.max_chars ?? ""}"></label>
        <label>Workers <input data-k="workers" type="number" value="${l.workers ?? 1}"></label>
        <label>Priority <input data-k="priority" type="number" value="${l.priority ?? 100}"></label>
      </div>
      <div class="row">
        <label class="grow">Base URL <input data-k="base_url" placeholder="(use provider default)" value="${escapeAttr(l.base_url || "")}"></label>
      </div>
      <div class="row">
        <button class="btn-toggle ghost">${l.enabled ? "Enabled" : "Disabled"} (click to flip)</button>
        <button class="btn-remove link danger">remove</button>
      </div>
    </div>
  `;
}

function addLaneRow() {
  const container = $("#lanes-config");
  const i = container.querySelectorAll(".lane-edit").length;
  const div = document.createElement("div");
  div.innerHTML = laneEditor({ enabled: true }, i);
  container.insertBefore(div.firstElementChild, $("#btn-add-lane"));
  div.querySelector(".btn-remove").addEventListener("click", () => div.firstElementChild.remove());
  wireUpKeyTouchTracking(div);
  wireUpKeyClearButton(div);
}

// Track, per card, whether the user has touched the api_key input.
// This lets the save handler distinguish "user didn't change it" (send
// null to preserve) from "user typed something new" (send the value).
function wireUpKeyTouchTracking(card) {
  const input = card.querySelector("input[data-k='api_key']");
  if (!input) return;
  input.addEventListener("input", () => {
    input.dataset.touched = "1";
  });
}
function wireUpKeyClearButton(card) {
  const btn = card.querySelector(".btn-clear-key");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const card2 = btn.closest(".lane-edit");
    const input = card2.querySelector("input[data-k='api_key']");
    input.dataset.cleared = "1";
    input.value = "";
    input.placeholder = "(cleared on save — type a new key, or leave blank to keep cleared)";
    const hint = card2.querySelector(".api-key-hint");
    if (hint) hint.innerHTML = '<span class="warn">will be cleared on save</span>';
  });
}

$("#btn-save-lanes").addEventListener("click", async () => {
  const lanes = $$("#lanes-config .lane-edit").map((card) => {
    const out = {};
    $$("input", card).forEach((inp) => {
      // For the api_key field, three cases:
      //   user clicked "clear"          -> send "" (server clears the key)
      //   user typed a new value        -> send the value (server sets it)
      //   user did NOT touch the field  -> OMIT the field entirely
      //                                  (server preserves the existing key)
      // This way saving one lane never wipes the keys on the others.
      if (inp.dataset.k === "api_key") {
        if (inp.dataset.cleared === "1") {
          out.api_key = "";
        } else if (inp.dataset.touched === "1") {
          out.api_key = inp.value;
        }
        // else: omit the key field; server will preserve
      } else {
        out[inp.dataset.k] = inp.value;
      }
    });
    out.enabled = !card.classList.contains("disabled");
    if (out.max_chars === "") out.max_chars = null;
    return out;
  });
  // Drop lanes with empty names client-side to avoid the most
  // obvious footgun.
  const named = lanes.filter((l) => (l.name || "").trim());
  if (named.length !== lanes.length) {
    showError("Some lanes are missing a name", "Empty-name lanes were dropped before save.");
  }
  // Build the keys map: only include lanes where the user actually
  // touched the key field (typed a new value OR clicked clear). The
  // server overlays this on top of the existing keys file, so
  // untouched keys are never even sent over the wire.
  const keys = {};
  named.forEach((l) => {
    if (l.api_key === "") {
      keys[l.name] = "";  // explicit clear
    } else if (typeof l.api_key === "string" && l.api_key.length > 0) {
      keys[l.name] = l.api_key;  // explicit new value
    }
    // else: untouched, don't include; server preserves
  });
  const r = await fetch("/api/lanes/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lanes: named, keys: keys }),
  });
  if (r.ok) {
    showToast("Lanes saved.");
    loadLanesConfig();
  } else {
    showError("Save failed", await parseError(r));
  }
});

$("#btn-reset-lanes").addEventListener("click", async () => {
  // Reset is destructive: it wipes lanes.json and replaces it with
  // the built-in defaults. It does NOT touch api_keys.json (the
  // server endpoint refuses to delete the keys file), so the user's
  // keys survive. Confirm twice because even a "safe" reset wipes
  // any custom provider/model/range tuning.
  if (!confirm("Reset lane config to the built-in defaults?\n\nYour API keys are stored separately and will NOT be affected.\n\nAny custom provider, model, size range, or worker count will be lost.")) return;
  if (!confirm("Are you sure? This cannot be undone (other than re-saving the config).")) return;
  const r = await fetch("/api/lanes/config/reset?confirm=1", { method: "POST" });
  if (r.ok) {
    showToast("Lanes reset to defaults. Keys preserved.");
    loadLanesConfig();
  } else {
    showError("Reset failed", await parseError(r));
  }
});

// --- Logs tab ---------------------------------------------------------------

async function loadLogs() {
  const r = await fetch("/api/logs?tail=2000");
  $("#logs").textContent = await r.text();
}

// --- Helpers ----------------------------------------------------------------

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function escapeAttr(s) { return escapeHtml(s); }

async function parseError(r) {
  // Read the body once as text so we don't double-consume the stream
  // when the response is non-JSON (e.g. 500 with an HTML error page).
  const text = await r.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    // Non-JSON body. Show the raw text if short, else the status code.
    return text ? text.slice(0, 500) : `HTTP ${r.status} (empty body)`;
  }
  if (data && data.detail) {
    if (data.detail.errors && Array.isArray(data.detail.errors)) {
      return data.detail.errors;
    }
    if (typeof data.detail === "string") return data.detail;
    return data.detail;
  }
  return data ?? `HTTP ${r.status}`;
}

function showError(title, detail) {
  let body;
  if (Array.isArray(detail)) {
    body = "<ul>" + detail.map((e) => "<li>" + escapeHtml(String(e)) + "</li>").join("") + "</ul>";
  } else if (typeof detail === "object") {
    body = "<pre>" + escapeHtml(JSON.stringify(detail, null, 2)) + "</pre>";
  } else {
    body = "<p>" + escapeHtml(String(detail)) + "</p>";
  }
  const overlay = document.createElement("div");
  overlay.className = "error-overlay";
  overlay.innerHTML = `
    <div class="error-card">
      <h3>${escapeHtml(title)}</h3>
      <div class="error-body">${body}</div>
      <div class="error-actions"><button class="primary">Close</button></div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector("button").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) overlay.remove(); });
}

function showToast(msg, durationMs = 2200) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.classList.add("show"), 10);
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 300); }, durationMs);
}

function refreshAll() {
  refreshStatus();
  refreshLanes();
  refreshActivity();
  checkKeys();
}

// --- SSE --------------------------------------------------------------------

let es;
function connectSSE() {
  if (es) es.close();
  es = new EventSource("/events");
  es.addEventListener("state_changed", () => { refreshStatus(); refreshActivity(); });
  es.addEventListener("lanes_changed", () => { refreshLanes(); if (currentTab() === "config") loadLanesConfig(); });
  es.addEventListener("keys_changed", () => { /* legacy event, no-op */ });
  es.addEventListener("run_state_changed", () => refreshStatus());
  es.addEventListener("heartbeat", () => {});
  es.onerror = () => setTimeout(connectSSE, 2000);
}
function currentTab() {
  const a = $$(".tab").find((b) => b.classList.contains("active"));
  return a ? a.dataset.tab : "run";
}

// Boot
refreshAll();
connectSSE();
setInterval(refreshAll, 15000);
