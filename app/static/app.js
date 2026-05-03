const API = "http://localhost:8000";

// Track active polling intervals keyed by repo_id
const _activePollers = {};

// ─── Indexing entry point ───────────────────────────────────────────────────

async function startIndexing(force = false) {
  const urlInput = document.getElementById("repo-url");
  const url = urlInput.value.trim();
  if (!url) return;

  const btn = document.getElementById("index-btn");
  const errorEl = document.getElementById("index-error");
  errorEl.hidden = true;
  btn.disabled = true;

  try {
    const res = await fetch(`${API}/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_url: url, force: !!force })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Indexing failed");

    const repoId = data.repo_id;

    if (data.status === "already_indexed") {
      handleAlreadyIndexed(repoId, url);
    } else {
      addIndexingCard(repoId, url);
    }

    urlInput.value = "";
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
  }
}

// ─── Already-indexed: highlight existing card or add ready card ─────────────

function handleAlreadyIndexed(repoId, repoUrl) {
  const existing = document.querySelector(`[data-repo-id="${repoId}"]`);
  if (existing) {
    existing.classList.remove("card-highlight");
    void existing.offsetWidth; // reflow to restart animation
    existing.classList.add("card-highlight");
    existing.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } else {
    const ul = document.getElementById("recent-list");
    const emptyEl = document.getElementById("recent-empty");
    if (emptyEl) emptyEl.hidden = true;
    const li = buildReadyCard(repoId, repoUrl);
    ul.prepend(li);
  }
}

// ─── Add an indexing-in-progress card to the list ───────────────────────────

function addIndexingCard(repoId, repoUrl) {
  const ul = document.getElementById("recent-list");
  const emptyEl = document.getElementById("recent-empty");
  if (emptyEl) emptyEl.hidden = true;

  // Don't duplicate if card already exists (e.g. re-submit same URL)
  const existing = document.querySelector(`[data-repo-id="${repoId}"]`);
  if (existing) {
    existing.classList.remove("card-highlight");
    void existing.offsetWidth;
    existing.classList.add("card-highlight");
    return;
  }

  const li = document.createElement("li");
  li.className = "card-indexing";
  li.dataset.repoId = repoId;
  li.dataset.repoUrl = repoUrl;

  const name = deriveProjectName(repoUrl);

  li.innerHTML =
    `<div class="card-header">` +
      `<span class="card-name">${escapeHtml(name)}</span>` +
      `<span class="card-stage">queued</span>` +
      `<button class="card-dismiss" title="Dismiss">✕</button>` +
    `</div>` +
    `<div class="card-detail"></div>` +
    `<div class="card-track"><div class="card-fill"></div></div>`;

  li.querySelector(".card-dismiss").addEventListener("click", (e) => {
    e.stopPropagation();
    stopPolling(repoId);
    li.remove();
    checkEmpty();
  });

  ul.prepend(li);
  pollCardProgress(repoId, li);
}

// ─── Poll progress for a specific card ──────────────────────────────────────

function pollCardProgress(repoId, cardEl) {
  if (_activePollers[repoId]) return;

  const interval = setInterval(async () => {
    try {
      const res = await fetch(`${API}/index/progress/${repoId}`);
      if (!res.ok) return;
      const d = await res.json();

      const fill = cardEl.querySelector(".card-fill");
      const stage = cardEl.querySelector(".card-stage");
      const detail = cardEl.querySelector(".card-detail");

      if (fill) fill.style.width = (d.percent || 0) + "%";
      if (stage) stage.textContent = d.stage || "";
      if (detail) detail.textContent = d.current_file || d.message || "";

      if (d.stage === "done") {
        stopPolling(repoId);
        transitionToReady(cardEl);
      } else if (d.stage === "error") {
        stopPolling(repoId);
        transitionToError(cardEl, d.error || "Indexing failed");
      }
    } catch (e) { /* keep polling */ }
  }, 1500);

  _activePollers[repoId] = interval;
}

function stopPolling(repoId) {
  if (_activePollers[repoId]) {
    clearInterval(_activePollers[repoId]);
    delete _activePollers[repoId];
  }
}

// ─── Card state transitions ─────────────────────────────────────────────────

function transitionToReady(cardEl) {
  const repoId = cardEl.dataset.repoId;
  const repoUrl = cardEl.dataset.repoUrl || "";
  const readyLi = buildReadyCard(repoId, repoUrl);
  cardEl.replaceWith(readyLi);
}

function transitionToError(cardEl, errorMsg) {
  const repoId = cardEl.dataset.repoId;
  const repoUrl = cardEl.dataset.repoUrl || "";
  const name = deriveProjectName(repoUrl);

  cardEl.className = "card-error";
  cardEl.innerHTML =
    `<div class="card-header">` +
      `<span class="card-name">${escapeHtml(name)}</span>` +
      `<button class="card-retry">Retry</button>` +
      `<button class="card-dismiss" title="Dismiss">✕</button>` +
    `</div>` +
    `<div class="card-error-msg">${escapeHtml(errorMsg)}</div>`;

  cardEl.querySelector(".card-retry").addEventListener("click", (e) => {
    e.stopPropagation();
    cardEl.remove();
    checkEmpty();
    const urlInput = document.getElementById("repo-url");
    urlInput.value = repoUrl;
    startIndexing(true);
  });

  cardEl.querySelector(".card-dismiss").addEventListener("click", (e) => {
    e.stopPropagation();
    cardEl.remove();
    checkEmpty();
  });
}

// ─── Build a ready (completed) project card ─────────────────────────────────

function buildReadyCard(repoId, repoUrl, relTimeStr = "") {
  const li = document.createElement("li");
  li.dataset.repoId = repoId;
  li.dataset.repoUrl = repoUrl;

  const name = deriveProjectName(repoUrl);
  const href = `view.html?repo_id=${encodeURIComponent(repoId)}&url=${encodeURIComponent(repoUrl)}`;

  li.innerHTML =
    `<a href="${href}">` +
      `<span class="name">${escapeHtml(name)}</span>` +
      `<span class="rel">${escapeHtml(relTimeStr)}</span>` +
    `</a>` +
    `<button class="del-btn" title="Delete" data-id="${escapeHtml(repoId)}">✕</button>`;

  li.querySelector(".del-btn").addEventListener("click", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      const res = await fetch(`${API}/projects/${encodeURIComponent(repoId)}`, { method: "DELETE" });
      if (res.ok) {
        li.remove();
        checkEmpty();
      }
    } catch (err) { console.error("Error deleting project:", err); }
  });

  return li;
}

// ─── Load recent projects (handles page refresh with in-flight jobs) ────────

async function loadRecentProjects() {
  const ul = document.getElementById("recent-list");
  const empty = document.getElementById("recent-empty");
  if (!ul || !empty) return;
  try {
    const res = await fetch(`${API}/projects`);
    if (!res.ok) return;
    const data = await res.json();
    const projects = data.projects || [];
    ul.innerHTML = "";

    if (projects.length === 0) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    for (const p of projects) {
      if (p.status === "indexing") {
        const li = document.createElement("li");
        li.className = "card-indexing";
        li.dataset.repoId = p.repo_id;
        li.dataset.repoUrl = p.repo_url || "";
        const name = deriveProjectName(p.repo_url, p.repo_id, p.display_name);

        li.innerHTML =
          `<div class="card-header">` +
            `<span class="card-name">${escapeHtml(name)}</span>` +
            `<span class="card-stage">indexing</span>` +
            `<button class="card-dismiss" title="Dismiss">✕</button>` +
          `</div>` +
          `<div class="card-detail"></div>` +
          `<div class="card-track"><div class="card-fill"></div></div>`;

        li.querySelector(".card-dismiss").addEventListener("click", (e) => {
          e.stopPropagation();
          stopPolling(p.repo_id);
          li.remove();
          checkEmpty();
        });

        ul.appendChild(li);
        pollCardProgress(p.repo_id, li);
      } else {
        const li = buildReadyCard(
          p.repo_id,
          p.repo_url || "",
          relTime(p.indexed_at)
        );
        ul.appendChild(li);
      }
    }
  } catch (e) { /* silent */ }
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function deriveProjectName(url, repoId = "", displayName = "") {
  if (displayName) return displayName;
  if (!url) return repoId ? `repo ${repoId.slice(0, 8)}…` : "Unlabeled repo";
  try { return new URL(url).pathname.replace(/^\/+|\.git$/g, ""); }
  catch { return url; }
}

function relTime(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(diff) || diff < 0) return "";
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function showError(msg) {
  const el = document.getElementById("index-error");
  el.textContent = msg;
  el.hidden = false;
}

function checkEmpty() {
  const ul = document.getElementById("recent-list");
  const empty = document.getElementById("recent-empty");
  if (ul && empty) {
    empty.hidden = ul.children.length > 0;
  }
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.appendChild(document.createTextNode(String(str)));
  return d.innerHTML;
}

// ─── Init ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("index-btn").disabled = false;
  document.getElementById("index-error").hidden = true;

  loadRecentProjects();

  // Auto-trigger re-index if redirected with ?force=1&url=...
  const _p = new URLSearchParams(location.search);
  if (_p.get("force") === "1" && _p.get("url")) {
    const urlInput = document.getElementById("repo-url");
    urlInput.value = decodeURIComponent(_p.get("url"));
    startIndexing(true);
  }
});
