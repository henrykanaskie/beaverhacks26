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
  document.getElementById("index-status").hidden = false;
  document.getElementById("status-text").textContent = "Cloning and indexing...";

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
      pollStatus(repoId);
    }
  } catch (err) {
    showError(err.message);
  }
}

function pollStatus(repoId) {
  document.getElementById("status-text").textContent = "Embedding chunks...";
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`${API}/status/${repoId}`);
      if (res.ok) {
        const data = await res.json();
        if (data.indexed) {
          clearInterval(interval);
          onIndexingComplete(repoId, null);
        }
      }
    } catch (e) { /* keep polling */ }
  }, 3000);
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
  document.getElementById("index-status").hidden = true;
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
