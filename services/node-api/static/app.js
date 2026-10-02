// PersonalServer v1.0 - Self-Hosted Server Admin Logic

let currentPath = "";
let authToken = localStorage.getItem("ps_auth_token") || "";
let cachedNodes = [];
let cachedJobs = [];

function getHeaders() {
  const headers = {};
  if (authToken) {
    headers["Authorization"] = "Bearer " + authToken;
    headers["X-Auth-Token"] = authToken;
  }
  return headers;
}

// Router
function navigate() {
  const hash = window.location.hash.replace("#", "") || "dashboard";
  document.querySelectorAll(".page-view").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));

  const targetView = document.getElementById(`view-${hash}`);
  const targetNav = document.querySelector(`.nav-item[data-page="${hash}"]`);
  const heading = document.getElementById("page-heading");

  if (targetView) targetView.classList.add("active");
  if (targetNav) targetNav.classList.add("active");

  const titles = {
    dashboard: "Dashboard",
    nodes: "Nodes",
    jobs: "Workload jobs",
    storage: "Personal storage"
  };
  if (heading) heading.textContent = titles[hash] || "Admin";

  if (hash === "dashboard") loadDashboard();
  else if (hash === "nodes") loadNodes();
  else if (hash === "jobs") loadJobs();
  else if (hash === "storage") loadStorage(currentPath);
}

window.addEventListener("hashchange", navigate);

// Auth management
document.getElementById("auth-btn")?.addEventListener("click", () => {
  const key = prompt("Enter Server Auth Token / Key:", authToken);
  if (key !== null) {
    authToken = key.trim();
    localStorage.setItem("ps_auth_token", authToken);
    navigate();
  }
});

document.getElementById("refresh-btn")?.addEventListener("click", () => {
  navigate();
});

// Helper formatting
function formatBytes(bytes) {
  if (bytes === 0 || bytes === "0") return "0 B";
  const num = parseInt(bytes, 10);
  if (isNaN(num)) return bytes || "-";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(num) / Math.log(k));
  return parseFloat((num / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function formatDuration(sec) {
  if (sec === null || sec === undefined) return "-";
  if (sec < 1) return Math.round(sec * 1000) + "ms";
  return parseFloat(sec).toFixed(2) + "s";
}

function timeAgo(isoStr) {
  if (!isoStr) return "-";
  try {
    const d = new Date(isoStr);
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 5) return "just now";
    if (sec < 60) return sec + "s ago";
    if (sec < 3600) return Math.floor(sec / 60) + "m ago";
    return Math.floor(sec / 3600) + "h ago";
  } catch {
    return "-";
  }
}

function renderStatusDot(status) {
  const s = (status || "").toUpperCase();
  if (s === "ONLINE" || s === "SUCCEEDED" || s === "RUNNING") {
    return `<span class="status-dot dot-online" title="${s}">●</span>`;
  } else if (s === "QUEUED" || s === "CLAIMED" || s === "RECOVERING") {
    return `<span class="status-dot dot-warning" title="${s}">●</span>`;
  } else if (s === "FAILED" || s === "TIMEOUT" || s === "OFFLINE" || s === "REJECTED") {
    return `<span class="status-dot dot-error" title="${s}">●</span>`;
  }
  return `<span class="status-dot dot-offline" title="${s}">○</span>`;
}

function renderStatusTag(state) {
  const s = (state || "").toUpperCase();
  let tagClass = "tag-neutral";
  if (s === "ONLINE" || s === "SUCCEEDED") tagClass = "tag-succeeded";
  else if (s === "FAILED" || s === "OFFLINE" || s === "TIMEOUT") tagClass = "tag-failed";
  else if (s === "RUNNING" || s === "CLAIMED" || s === "QUEUED" || s === "RECOVERING") tagClass = "tag-queued";
  return `<span class="status-tag ${tagClass}">${s || "UNKNOWN"}</span>`;
}

function updateLastRefreshed() {
  const el = document.getElementById("last-updated-text");
  if (el) el.textContent = new Date().toLocaleTimeString();
}

// 1. Dashboard
async function loadDashboard() {
  updateLastRefreshed();
  try {
    const [healthRes, clusterRes, jobsRes, storageRes] = await Promise.allSettled([
      fetch("/health"),
      fetch("/api/cluster", { headers: getHeaders() }),
      fetch("/api/jobs", { headers: getHeaders() }),
      fetch("/storage/usage", { headers: getHeaders() })
    ]);

    // Local Node Health & Sidebar
    let localNodeName = "vivo-y31";
    let localNodeStatus = "ONLINE";
    if (healthRes.status === "fulfilled" && healthRes.value.ok) {
      const h = await healthRes.value.json();
      localNodeName = h.node?.name || "vivo-y31";
      localNodeStatus = (h.status || "ONLINE").toUpperCase();
      document.getElementById("side-node-name").textContent = localNodeName;
      document.getElementById("side-node-state").textContent = localNodeStatus;
      const dot = document.getElementById("side-node-dot");
      if (dot) {
        dot.className = "status-dot " + (localNodeStatus === "ONLINE" ? "dot-online" : "dot-offline");
      }
    }

    // Cluster Summary & Nodes table
    let nodesList = [];
    if (clusterRes.status === "fulfilled" && clusterRes.value.ok) {
      const c = await clusterRes.value.json();
      nodesList = (c.nodes || []).filter(n => n.status !== "REMOVED");
      cachedNodes = nodesList;

      const onlineCount = nodesList.filter(n => (n.status || "").toUpperCase() === "ONLINE").length;
      const offlineCount = nodesList.length - onlineCount;
      const nodeWord = nodesList.length === 1 ? "node" : "nodes";
      document.getElementById("sum-cluster-text").textContent = 
        `${nodesList.length} ${nodeWord} · ${onlineCount} online · ${offlineCount} offline`;

      // Render Dashboard Nodes table
      const nodesTbody = document.getElementById("dash-nodes-tbody");
      if (nodesList.length === 0) {
        nodesTbody.innerHTML = `<tr><td colspan="6" class="muted">No active cluster nodes.</td></tr>`;
      } else {
        nodesTbody.innerHTML = nodesList.map(n => {
          const isOnline = (n.status || "").toUpperCase() === "ONLINE";
          const memStr = n.last_heartbeat?.system?.memory || (n.resources?.ram_mb ? `${n.resources.ram_mb} MB` : "-");
          const cores = n.last_heartbeat?.system?.cpu_cores || n.resources?.cpu_cores || "-";
          const lastSeen = isOnline ? timeAgo(n.last_seen) : (n.last_seen ? timeAgo(n.last_seen) : "offline");

          return `
            <tr>
              <td>${renderStatusDot(n.status)}</td>
              <td><a href="#nodes" onclick="viewNodeById('${n.node_id}')"><strong>${n.name || n.node_id}</strong></a></td>
              <td class="muted">${n.role || 'compute'}</td>
              <td class="mono">${cores} cores</td>
              <td class="mono">${memStr}</td>
              <td class="mono muted">${lastSeen}</td>
            </tr>
          `;
        }).join("");
      }
    }

    // Jobs Summary & Recent table
    if (jobsRes.status === "fulfilled" && jobsRes.value.ok) {
      const jData = await jobsRes.value.json();
      const allJobs = jData.jobs || [];
      cachedJobs = allJobs;

      const runningCount = allJobs.filter(j => j.status === "RUNNING" || j.status === "CLAIMED").length;
      const queuedCount = allJobs.filter(j => j.status === "QUEUED" || j.status === "RECOVERING").length;
      document.getElementById("sum-jobs-text").textContent = 
        `${allJobs.length} total · ${runningCount} running · ${queuedCount} queued`;

      const jobsTbody = document.getElementById("dash-jobs-tbody");
      if (allJobs.length === 0) {
        jobsTbody.innerHTML = `<tr><td colspan="5" class="muted">No jobs recorded.</td></tr>`;
      } else {
        const recentJobs = [...allJobs].reverse().slice(0, 5);
        jobsTbody.innerHTML = recentJobs.map(j => {
          const duration = j.result?.duration_ms ? (j.result.duration_ms + "ms") : (j.execution_duration_sec ? formatDuration(j.execution_duration_sec) : "-");
          const target = j.assigned_node || j.target_node || j.target || "auto";
          const displayId = (j.job_id || j.id || "").slice(0, 12);

          return `
            <tr>
              <td class="mono"><a href="#jobs" onclick="viewJobById('${j.job_id || j.id}')">${displayId}</a></td>
              <td>${j.type}</td>
              <td class="mono">${target}</td>
              <td>${renderStatusTag(j.status || j.state)}</td>
              <td class="mono muted">${duration}</td>
            </tr>
          `;
        }).join("");
      }
    }

    // Storage Summary
    if (storageRes.status === "fulfilled" && storageRes.value.ok) {
      const sData = await storageRes.value.json();
      const freeStr = sData.disk?.available || "Available";
      const usedPct = sData.disk?.used_percent || "-";
      document.getElementById("sum-storage-text").textContent = `${freeStr} free · ${usedPct} used`;
      document.getElementById("dash-storage-root").textContent = sData.storage_root || "~/PersonalServer/storage";
      document.getElementById("dash-storage-stats").textContent = 
        `${freeStr} free · ${usedPct} used · ${sData.files_count || 0} files · ${sData.folders_count || 0} folders`;
    }

  } catch (err) {
    console.error("Dashboard refresh error:", err);
  }
}

// 2. Nodes View
async function loadNodes() {
  updateLastRefreshed();
  const tbody = document.getElementById("nodes-tbody");
  const targetSelect = document.getElementById("job-target");

  try {
    const res = await fetch("/api/cluster", { headers: getHeaders() });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    cachedNodes = (data.nodes || []).filter(n => n.status !== "REMOVED");

    if (targetSelect) {
      const currentVal = targetSelect.value;
      targetSelect.innerHTML = `<option value="auto">Auto (Scheduler)</option>` +
        cachedNodes.map(n => `<option value="${n.node_id}">${n.name || n.node_id} (${n.node_id.slice(0, 8)})</option>`).join("");
      targetSelect.value = currentVal;
    }

    if (cachedNodes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="muted">No cluster nodes registered.</td></tr>`;
      return;
    }

    tbody.innerHTML = cachedNodes.map(n => {
      const isOnline = (n.status || "").toUpperCase() === "ONLINE";
      const memStr = n.last_heartbeat?.system?.memory || (n.resources?.ram_mb ? `${n.resources.ram_mb} MB` : "-");
      const cores = n.last_heartbeat?.system?.cpu_cores || n.resources?.cpu_cores || "-";
      const platStr = `${n.platform || '-'} / ${n.architecture || '-'}`;

      return `
        <tr>
          <td>${renderStatusDot(n.status)}</td>
          <td><strong>${n.name || 'node'}</strong></td>
          <td class="mono muted">${n.node_id}</td>
          <td class="muted">${n.role || 'compute'}</td>
          <td class="muted">${platStr}</td>
          <td class="mono">${cores}</td>
          <td class="mono">${memStr}</td>
          <td>${renderStatusTag(n.status)}</td>
          <td>
            <button class="btn btn-sm" onclick="viewNodeById('${n.node_id}')">Details</button>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" class="tag-failed">Error loading nodes: ${err.message}</td></tr>`;
  }
}

window.viewNodeById = function(nodeId) {
  const node = cachedNodes.find(n => n.node_id === nodeId);
  if (!node) return;

  const panel = document.getElementById("node-detail-panel");
  const heading = document.getElementById("node-detail-heading");
  const grid = document.getElementById("node-detail-grid");

  if (heading) heading.textContent = `Node: ${node.name || node.node_id}`;

  const hb = node.last_heartbeat || {};
  const sys = hb.system || {};
  const srv = hb.services || {};
  const caps = Object.keys(node.capabilities || {}).filter(k => node.capabilities[k]).join(", ") || "none";

  grid.innerHTML = `
    <div class="detail-row"><span class="detail-label">Status</span><span class="detail-val">${renderStatusTag(node.status)}</span></div>
    <div class="detail-row"><span class="detail-label">Node ID</span><span class="detail-val mono">${node.node_id}</span></div>
    <div class="detail-row"><span class="detail-label">Role</span><span class="detail-val">${node.role || 'compute'}</span></div>
    <div class="detail-row"><span class="detail-label">Platform</span><span class="detail-val">${node.platform || '-'} (${node.os || '-'})</span></div>
    <div class="detail-row"><span class="detail-label">Architecture</span><span class="detail-val mono">${node.architecture || '-'}</span></div>
    <div class="detail-row"><span class="detail-label">CPU Cores</span><span class="detail-val mono">${sys.cpu_cores || node.resources?.cpu_cores || '-'}</span></div>
    <div class="detail-row"><span class="detail-label">Memory</span><span class="detail-val mono">${sys.memory || (node.resources?.ram_mb ? node.resources.ram_mb + ' MB' : '-')}</span></div>
    <div class="detail-row"><span class="detail-label">Storage</span><span class="detail-val mono">${sys.storage || (node.resources?.storage_gb ? node.resources.storage_gb + ' GB' : '-')}</span></div>
    <div class="detail-row"><span class="detail-label">Capabilities</span><span class="detail-val mono">${caps}</span></div>
    <div class="detail-row"><span class="detail-label">Last Heartbeat</span><span class="detail-val mono">${node.last_seen || '-'} (${timeAgo(node.last_seen)})</span></div>
    <div class="detail-row"><span class="detail-label">Services</span><span class="detail-val mono">Node API: ${srv.node_api || srv['node-api'] || 'active'} · Tunnel: ${srv.cloudflare || 'connected'}</span></div>
    <div class="detail-row"><span class="detail-label">Load Average</span><span class="detail-val mono">${(sys.load_average || []).join(', ') || '-'}</span></div>
  `;

  panel.style.display = "block";
  panel.scrollIntoView({ behavior: "smooth" });
};

window.closeNodeDetail = function() {
  const panel = document.getElementById("node-detail-panel");
  if (panel) panel.style.display = "none";
};

// 3. Jobs View
async function loadJobs() {
  updateLastRefreshed();
  const tbody = document.getElementById("jobs-tbody");

  try {
    const res = await fetch("/api/jobs", { headers: getHeaders() });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    cachedJobs = data.jobs || [];

    if (cachedJobs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted">No workload jobs recorded.</td></tr>`;
      return;
    }

    const sorted = [...cachedJobs].reverse();
    tbody.innerHTML = sorted.map(j => {
      const jobId = j.job_id || j.id || "";
      const target = j.assigned_node || j.target_node || j.target || "auto";
      const duration = j.result?.duration_ms ? (j.result.duration_ms + "ms") : (j.execution_duration_sec ? formatDuration(j.execution_duration_sec) : "-");
      const createdStr = j.created_at ? new Date(j.created_at).toLocaleTimeString() : "-";
      const attemptStr = `${j.attempt || 1}/${j.max_attempts || 3}`;

      return `
        <tr>
          <td class="mono"><strong>${jobId}</strong></td>
          <td>${j.type}</td>
          <td class="mono">${target}</td>
          <td>${renderStatusTag(j.status || j.state)}</td>
          <td class="mono">${attemptStr}</td>
          <td class="mono muted">${duration}</td>
          <td class="mono muted">${createdStr}</td>
          <td>
            <button class="btn btn-sm" onclick="viewJobById('${jobId}')">Details</button>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="tag-failed">Error loading jobs: ${err.message}</td></tr>`;
  }
}

window.viewJobById = function(jobId) {
  const job = cachedJobs.find(j => (j.job_id === jobId || j.id === jobId));
  const panel = document.getElementById("job-detail-panel");
  const heading = document.getElementById("job-detail-heading");
  const jsonBox = document.getElementById("job-detail-json");

  if (job) {
    if (heading) heading.textContent = `Job details: ${job.job_id || job.id}`;
    if (jsonBox) jsonBox.textContent = JSON.stringify(job, null, 2);
    if (panel) {
      panel.style.display = "block";
      panel.scrollIntoView({ behavior: "smooth" });
    }
  } else {
    fetch(`/api/jobs/${jobId}`, { headers: getHeaders() })
      .then(r => r.json())
      .then(data => {
        if (heading) heading.textContent = `Job details: ${jobId}`;
        if (jsonBox) jsonBox.textContent = JSON.stringify(data.job || data, null, 2);
        if (panel) {
          panel.style.display = "block";
          panel.scrollIntoView({ behavior: "smooth" });
        }
      })
      .catch(e => alert("Could not fetch job details: " + e.message));
  }
};

window.closeJobDetail = function() {
  const panel = document.getElementById("job-detail-panel");
  if (panel) panel.style.display = "none";
};

// Submit job form
document.getElementById("job-submit-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const type = document.getElementById("job-type").value;
  const target = document.getElementById("job-target").value;
  const timeout = parseInt(document.getElementById("job-timeout").value, 10) || 60;
  const paramsRaw = document.getElementById("job-params").value.trim();

  let params = {};
  if (paramsRaw) {
    try {
      params = JSON.parse(paramsRaw);
    } catch (err) {
      alert("Invalid JSON parameters: " + err.message);
      return;
    }
  }

  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...getHeaders()
      },
      body: JSON.stringify({
        type: type,
        target: target,
        timeout: timeout,
        parameters: params
      })
    });

    const result = await res.json();
    if (!res.ok) {
      alert("Submission error: " + (result.error || res.statusText));
      return;
    }

    loadJobs();
  } catch (err) {
    alert("Error submitting job: " + err.message);
  }
});

// 4. Storage View
async function loadStorage(path = "") {
  updateLastRefreshed();
  currentPath = path;
  renderBreadcrumbs(path);
  const tbody = document.getElementById("storage-tbody");

  try {
    const res = await fetch(`/storage/list?path=${encodeURIComponent(path)}`, { headers: getHeaders() });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || ("HTTP " + res.status));
    }
    const data = await res.json();
    const items = data.items || [];

    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted">Folder is empty.</td></tr>`;
      return;
    }

    tbody.innerHTML = items.map(item => {
      const itemRelPath = path ? `${path}/${item.name}` : item.name;
      const isDir = item.is_dir;
      const modStr = item.modified ? new Date(item.modified * 1000).toLocaleDateString() : "-";
      const sizeStr = isDir ? "-" : formatBytes(item.size_bytes);

      return `
        <tr>
          <td>
            ${isDir 
              ? `<a href="javascript:void(0)" onclick="loadStorage('${itemRelPath}')"><strong>📁 ${item.name}</strong></a>`
              : `<span class="mono">📄 ${item.name}</span>`
            }
          </td>
          <td class="mono muted">${sizeStr}</td>
          <td class="mono muted">${modStr}</td>
          <td style="text-align:right">
            <div style="display:inline-flex; gap:4px">
              ${!isDir ? `<a href="/storage/download?path=${encodeURIComponent(itemRelPath)}" class="btn btn-sm btn-primary" download>Download</a>` : ''}
              <button class="btn btn-sm" onclick="renameItem('${itemRelPath}')">Rename</button>
              <button class="btn btn-sm btn-danger" onclick="deleteItem('${itemRelPath}', ${isDir})">Delete</button>
            </div>
          </td>
        </tr>
      `;
    }).join("");

    // Update footer stats
    const usageRes = await fetch("/storage/usage", { headers: getHeaders() }).then(r => r.json()).catch(() => ({}));
    const freeStr = usageRes.disk?.available || "-";
    const usedPct = usageRes.disk?.used_percent || "-";
    document.getElementById("storage-footer-stats").textContent = 
      `${usageRes.storage_root || '~/PersonalServer/storage'} · ${freeStr} free (${usedPct} used) · ${usageRes.files_count || 0} files`;

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="tag-failed">Error listing folder: ${err.message}</td></tr>`;
  }
}

function renderBreadcrumbs(path) {
  const container = document.getElementById("storage-breadcrumbs");
  if (!container) return;

  const parts = path ? path.split("/").filter(Boolean) : [];
  let html = `<span class="crumb ${parts.length === 0 ? 'current' : ''}" onclick="loadStorage('')">~/PersonalServer/storage</span>`;

  let accumulated = "";
  parts.forEach((p, index) => {
    accumulated += (accumulated ? "/" : "") + p;
    const isLast = index === parts.length - 1;
    const clickPath = accumulated;
    html += ` <span class="muted">/</span> <span class="crumb ${isLast ? 'current' : ''}" ${!isLast ? `onclick="loadStorage('${clickPath}')"` : ''}>${p}</span>`;
  });

  container.innerHTML = html;
}

// Upload file
document.getElementById("upload-file-btn")?.addEventListener("click", () => {
  document.getElementById("file-upload-input")?.click();
});

document.getElementById("file-upload-input")?.addEventListener("change", async (e) => {
  const files = e.target.files;
  if (!files || files.length === 0) return;
  const file = files[0];

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch(`/storage/upload?path=${encodeURIComponent(currentPath)}`, {
      method: "POST",
      headers: getHeaders(),
      body: formData
    });

    const result = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert("Upload failed: " + (result.error || res.statusText));
      return;
    }

    e.target.value = "";
    loadStorage(currentPath);
  } catch (err) {
    alert("Upload error: " + err.message);
  }
});

// Create folder
document.getElementById("create-folder-btn")?.addEventListener("click", async () => {
  const name = prompt("Folder name:");
  if (!name) return;

  try {
    const res = await fetch(`/storage/mkdir?path=${encodeURIComponent(currentPath)}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...getHeaders()
      },
      body: JSON.stringify({ name: name.trim() })
    });

    const result = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert("Folder creation failed: " + (result.error || res.statusText));
      return;
    }

    loadStorage(currentPath);
  } catch (err) {
    alert("Error: " + err.message);
  }
});

// Rename item
window.renameItem = async function(itemRelPath) {
  const oldName = itemRelPath.split("/").pop();
  const newName = prompt("Rename to:", oldName);
  if (!newName || newName.trim() === oldName) return;

  try {
    const res = await fetch(`/storage/rename`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...getHeaders()
      },
      body: JSON.stringify({
        path: itemRelPath,
        new_name: newName.trim()
      })
    });

    const result = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert("Rename failed: " + (result.error || res.statusText));
      return;
    }

    loadStorage(currentPath);
  } catch (err) {
    alert("Rename error: " + err.message);
  }
};

// Delete item
window.deleteItem = async function(itemRelPath, isDir) {
  const itemName = itemRelPath.split("/").pop();
  if (!confirm(`Delete ${isDir ? 'folder' : 'file'} "${itemName}"?`)) return;

  try {
    const res = await fetch(`/storage?path=${encodeURIComponent(itemRelPath)}`, {
      method: "DELETE",
      headers: getHeaders()
    });

    const result = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert("Delete failed: " + (result.error || res.statusText));
      return;
    }

    loadStorage(currentPath);
  } catch (err) {
    alert("Delete error: " + err.message);
  }
};

// Init
navigate();
