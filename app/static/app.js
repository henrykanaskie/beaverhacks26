const API = "http://localhost:8000";

window.__repoId = null;
window.__scope = null;
window.__onIndexed = (repoId) => {};
window.__onFileSelected = (path) => {};

let _lastIndexedUrl = null;

async function startIndexing() {
  const url = document.getElementById("repo-url").value.trim();
  if (!url) return;

  if (window.__repoId && _lastIndexedUrl === url) {
    onIndexingComplete(window.__repoId, null);
    return;
  }

  document.getElementById("index-btn").disabled = true;
  document.getElementById("index-error").hidden = true;

  try {
    const res = await fetch(`${API}/index`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_url: url })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Indexing failed");

    const repoId = data.repo_id;
    window.__repoId = repoId;
    _lastIndexedUrl = url;

    if (data.status === "already_indexed") {
      onIndexingComplete(repoId, data.chunk_count);
    } else {
      pollProgress(repoId);
    }
  } catch (err) {
    showError(err.message);
  }
}


function pollProgress(repoId) {
  const fill   = document.getElementById("progress-fill");
  const pctEl  = document.getElementById("progress-pct");
  const stage  = document.getElementById("progress-stage");
  const detail = document.getElementById("progress-detail");
  const status = document.getElementById("index-status");
  if (stage) stage.textContent = "";
  if (detail) detail.textContent = "";
  if (fill) fill.style.width = "0%";
  status.classList.remove("bar-active");
  status.classList.add("active");

  const interval = setInterval(async () => {
    try {
      const res = await fetch(`${API}/index/progress/${repoId}`);
      if (!res.ok) return;
      const d = await res.json();
      const pct = d.percent ?? 0;
      if (fill) fill.style.width = pct + "%";
      if (pctEl) pctEl.textContent = pct + "%";
      status.classList.add("bar-active");
      if (stage) stage.textContent = d.stage || "";
      if (detail) detail.textContent = d.current_file || d.message || "";
      if (d.stage === "done") {
        clearInterval(interval);
        onIndexingComplete(repoId, null);
      } else if (d.stage === "error") {
        clearInterval(interval);
        showError(d.error || "Indexing failed");
      }
    } catch (e) { /* keep polling */ }
  }, 1500);
}

function onIndexingComplete(repoId, chunkCount) {
  window.__repoId = repoId;
  const viewUrl = `view.html?repo_id=${encodeURIComponent(repoId)}&url=${encodeURIComponent(_lastIndexedUrl || "")}`;
  window.location.href = viewUrl;
}

function showError(msg) {
  const el = document.getElementById("index-error");
  el.textContent = msg;
  el.hidden = false;
  const s = document.getElementById("index-status");
  s.classList.remove("active", "bar-active");
  document.getElementById("index-btn").disabled = false;
}

// ---------- FE-02: File tree ----------

window.__onIndexed = async (repoId) => {
  try {
    const res = await fetch(`${API}/files/${repoId}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load file list");
    renderFileTree(data.files, repoId);
  } catch (err) {
    const main = document.getElementById("main-section");
    main.insertAdjacentHTML("beforeend", `<div class="error">Could not load files: ${escapeHtml(err.message)}</div>`);
  }
};

function renderFileTree(files, repoId) {
  // Drop any previous tree (re-index of the same session)
  const existing = document.getElementById("file-tree");
  if (existing) existing.remove();

  const tree = {};
  files.forEach(f => {
    const parts = f.split("/");
    let node = tree;
    parts.forEach((part, i) => {
      const isLeaf = i === parts.length - 1;
      if (isLeaf) {
        node[part] = null;
      } else {
        if (node[part] == null || typeof node[part] !== "object") {
          node[part] = {};
        }
        node = node[part];
      }
    });
  });

  const container = document.createElement("div");
  container.id = "file-tree";
  container.innerHTML = "<h3>Files</h3>";
  container.appendChild(buildTreeNode(tree, ""));
  document.getElementById("main-section").prepend(container);
}

function buildTreeNode(node, prefix) {
  const ul = document.createElement("ul");

  // Folders first (value is an object), files second.
  const entries = Object.keys(node).sort();
  const folders = entries.filter(k => node[k] !== null && typeof node[k] === "object");
  const fileNames = entries.filter(k => node[k] === null);

  [...folders, ...fileNames].forEach(name => {
    const li = document.createElement("li");
    const fullPath = prefix ? `${prefix}/${name}` : name;
    const isFolder = node[name] !== null && typeof node[name] === "object";

    if (!isFolder) {
      li.textContent = name;
      li.className = "file-node";
      li.onclick = (e) => {
        e.stopPropagation();
        document.querySelectorAll(".file-node.selected, .folder-node.selected")
                .forEach(el => el.classList.remove("selected"));
        li.classList.add("selected");
        window.__scope = fullPath;
        window.__onFileSelected(fullPath);
      };
    } else {
      const label = document.createElement("span");
      label.className = "folder-label";
      label.textContent = name + "/";
      li.appendChild(label);
      li.className = "folder-node";

      const children = buildTreeNode(node[name], fullPath);
      children.hidden = true;
      li.appendChild(children);

      li.onclick = (e) => {
        e.stopPropagation();
        document.querySelectorAll(".file-node.selected, .folder-node.selected")
                .forEach(el => el.classList.remove("selected"));
        li.classList.add("selected");
        children.hidden = !children.hidden;
        window.__scope = fullPath + "/";
      };
    }
    ul.appendChild(li);
  });
  return ul;
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.appendChild(document.createTextNode(String(str)));
  return d.innerHTML;
}

// ---------- Recent projects ----------

function deriveProjectName(url, repoId = "", displayName = "") {
  if (displayName) return displayName;
  if (!url) return "Unlabeled indexed repo";
  try { return new URL(url).pathname.replace(/^\/+|\.git$/g, ""); }
  catch { return url; }
}

function relTime(iso) {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!isFinite(diff) || diff < 0) return "";
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

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
      const li = document.createElement("li");
      const href = `view.html?repo_id=${encodeURIComponent(p.repo_id)}&url=${encodeURIComponent(p.repo_url)}`;
      li.innerHTML =
        `<a href="${href}">` +
          `<span class="name">${escapeHtml(deriveProjectName(p.repo_url, p.repo_id, p.display_name))}</span>` +
          `<span class="rel">${escapeHtml(relTime(p.indexed_at))}</span>` +
        `</a>`;
      ul.appendChild(li);
    }
  } catch (e) { /* silent */ }
}

document.addEventListener("DOMContentLoaded", () => {
  // Reset any state the bfcache may have restored from a previous session
  const status = document.getElementById("index-status");
  if (status) {
    status.classList.remove("active", "bar-active");
    const fill  = document.getElementById("progress-fill");
    const pctEl = document.getElementById("progress-pct");
    const stage = document.getElementById("progress-stage");
    const detail = document.getElementById("progress-detail");
    if (fill) fill.style.width = "0%";
    if (pctEl) pctEl.textContent = "0%";
    if (stage) stage.textContent = "";
    if (detail) detail.textContent = "";
  }
  document.getElementById("index-btn").disabled = false;
  document.getElementById("index-error").hidden = true;

  loadRecentProjects();
});
