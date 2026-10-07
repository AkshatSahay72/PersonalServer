// PersonalServer v1.1 - Client Application Logic

let currentPath = "";
let selectedStorageNode = null;
let cachedStorageNodes = [];
let cachedNodes = [];
let cachedJobs = [];

// API Helper
async function apiFetch(endpoint, options = {}) {
  try {
    const res = await fetch(endpoint, options);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status} ${res.statusText}`);
    }
    return await res.json();
  } catch (err) {
    console.error(`API error on ${endpoint}:`, err);
    throw err;
  }
}

// Router
function navigate() {
  const hash = window.location.hash.replace("#", "") || "dashboard";
  document.querySelectorAll(".page-view").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".nav-link").forEach(el => el.classList.remove("active"));

  const targetView = document.getElementById(`view-${hash}`);
  const targetNav = document.querySelector(`.nav-link[data-page="${hash}"]`);
  const heading = document.getElementById("page-heading");
  const subHeading = document.getElementById("page-subheading");

  if (targetView) targetView.classList.add("active");
  if (targetNav) targetNav.classList.add("active");

  const titles = {
    dashboard: { main: "Dashboard", sub: "Cluster overview" },
    nodes: { main: "Nodes", sub: "Cluster inventory & telemetry" },
    jobs: { main: "Jobs", sub: "Workload execution & lifecycle" },
    storage: { main: "Storage", sub: "Personal file manager" }
  };

  const meta = titles[hash] || { main: "Dashboard", sub: "Cluster overview" };
  if (heading) heading.textContent = meta.main;
  if (subHeading) subHeading.textContent = meta.sub;

  loadSession();
  if (hash === "dashboard") loadDashboard();
  else if (hash === "nodes") loadNodes();
  else if (hash === "jobs") loadJobs();
  else if (hash === "storage") {
    loadStorageNodes().then(() => loadStorage(currentPath, selectedStorageNode));
  }
}

window.addEventListener("hashchange", navigate);

document.getElementById("refresh-btn")?.addEventListener("click", () => {
  navigate();
});

// Formatters
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
    if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
    return Math.floor(sec / 86400) + "d ago";
  } catch {
    return "-";
  }
}

function getFileTypeCategory(filename, isDir) {
  if (isDir) return "Folder";
  const ext = (filename.split('.').pop() || "").toLowerCase();
  if (["zip", "tar", "gz", "tgz", "bz2", "7z", "rar"].includes(ext)) return "Archive";
  if (["jpg", "jpeg", "png", "gif", "svg", "webp", "ico"].includes(ext)) return "Image";
  if (["mp4", "mkv", "webm", "mov", "avi"].includes(ext)) return "Video";
  if (["mp3", "wav", "flac", "ogg"].includes(ext)) return "Audio";
  if (["pdf", "doc", "docx", "txt", "md", "csv", "json", "yml", "yaml", "xml"].includes(ext)) return "Document";
  if (["py", "sh", "js", "ts", "html", "css", "c", "cpp", "go", "rs"].includes(ext)) return "Code";
  return "File";
}

function getFileIcon(typeCategory) {
  switch (typeCategory) {
    case "Folder": return "📁";
    case "Archive": return "📦";
    case "Image": return "🖼️";
    case "Video": return "🎬";
    case "Audio": return "🎵";
    case "Code": return "📜";
    default: return "📄";
  }
}

function renderStatusPill(state) {
  const s = (state || "").toUpperCase();
  if (s === "ONLINE" || s === "SUCCEEDED") {
    return `<span class="status-pill pill-online"><span class="status-dot dot-online">●</span> ${s === "ONLINE" ? "Online" : "Succeeded"}</span>`;
  } else if (s === "RUNNING") {
    return `<span class="status-pill pill-online"><span class="status-dot dot-online">●</span> Running</span>`;
  } else if (s === "QUEUED" || s === "CLAIMED") {
    return `<span class="status-pill pill-warning"><span class="status-dot dot-warning">●</span> ${s === "QUEUED" ? "Queued" : "Claimed"}</span>`;
  } else if (s === "RECOVERING") {
    return `<span class="status-pill pill-warning"><span class="status-dot dot-warning">●</span> Recovering</span>`;
  } else if (s === "FAILED" || s === "TIMEOUT" || s === "REJECTED") {
    return `<span class="status-pill pill-failed"><span class="status-dot dot-error">●</span> ${s}</span>`;
  } else if (s === "OFFLINE") {
    return `<span class="status-pill pill-offline"><span class="status-dot dot-offline">○</span> Offline</span>`;
  }
  return `<span class="status-pill pill-neutral"><span class="status-dot dot-offline">○</span> ${s || "Unknown"}</span>`;
}

function updateLastRefreshed() {
  const el = document.getElementById("last-updated-text");
  if (el) el.textContent = new Date().toLocaleTimeString();
}

// 0. Session Info
async function loadSession() {
  try {
    const session = await apiFetch("/api/session").catch(() => null);
    const userEl = document.getElementById("side-session-user");
    const topUserEl = document.getElementById("topbar-user-email");
    if (session && session.authenticated) {
      const userText = session.user || "Signed in";
      if (userEl) userEl.textContent = userText;
      if (topUserEl) topUserEl.textContent = userText;
    } else {
      if (userEl) userEl.textContent = "Signed in";
      if (topUserEl) topUserEl.textContent = "Signed in";
    }
  } catch {
    const userEl = document.getElementById("side-session-user");
    const topUserEl = document.getElementById("topbar-user-email");
    if (userEl) userEl.textContent = "Signed in";
    if (topUserEl) topUserEl.textContent = "Signed in";
  }
}

// 1. Dashboard
async function loadDashboard() {
  updateLastRefreshed();
  try {
    const [clusterData, jobsData, storageData, srvData] = await Promise.allSettled([
      apiFetch("/api/cluster"),
      apiFetch("/api/jobs"),
      apiFetch("/storage/usage"),
      apiFetch("/api/services")
    ]);

    // Cluster Summary & Nodes Table
    let nodes = [];
    if (clusterData.status === "fulfilled") {
      const c = clusterData.value;
      nodes = (c.nodes || []).filter(n => n.status !== "REMOVED");
      cachedNodes = nodes;

      const onlineCount = nodes.filter(n => (n.status || "").toUpperCase() === "ONLINE").length;
      const offlineCount = nodes.length - onlineCount;

      document.getElementById("sum-card-cluster-main").textContent = `${onlineCount} online`;
      document.getElementById("sum-card-cluster-sub").textContent = `${offlineCount} offline`;
      document.getElementById("side-cluster-online").textContent = `${onlineCount} online`;

      const tbody = document.getElementById("dash-nodes-tbody");
      if (nodes.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="cell-muted">No cluster nodes registered.</td></tr>`;
      } else {
        tbody.innerHTML = nodes.map(n => {
          const isOnline = (n.status || "").toUpperCase() === "ONLINE";
          const memStr = n.last_heartbeat?.system?.memory || (n.resources?.ram_mb ? `${n.resources.ram_mb} MB` : "-");
          const cores = n.last_heartbeat?.system?.cpu_cores || n.resources?.cpu_cores || "-";
          const lastSeen = isOnline ? timeAgo(n.last_seen) : (n.last_seen ? timeAgo(n.last_seen) : "offline");

          return `
            <tr style="cursor: pointer;" onclick="window.location.hash='#nodes'; setTimeout(() => viewNodeById('${n.node_id}'), 50);">
              <td>${renderStatusPill(n.status)}</td>
              <td><strong>${n.name || n.node_id}</strong></td>
              <td class="cell-muted">${n.role || 'compute'}</td>
              <td class="mono">${cores} cores</td>
              <td class="mono">${memStr}</td>
              <td class="mono cell-muted">${lastSeen}</td>
            </tr>
          `;
        }).join("");

        // Populate System Panel with Primary Node details
        const primary = nodes[0];
        if (primary) {
          const sys = primary.last_heartbeat?.system || {};
          document.getElementById("dash-sys-node-id").textContent = primary.node_id.slice(0, 16);
          document.getElementById("sys-metric-host").textContent = primary.name || primary.node_id;
          document.getElementById("sys-metric-arch").textContent = `${primary.platform || 'Termux'} · ${primary.architecture || 'aarch64'}`;
          document.getElementById("sys-metric-cpu").textContent = `${sys.cpu_cores || primary.resources?.cpu_cores || '8'} cores`;
          document.getElementById("sys-metric-mem").textContent = sys.memory || (primary.resources?.ram_mb ? `${primary.resources.ram_mb} MB` : '5.6 GB');
        }
      }
    }

    // Jobs Summary & Recent Jobs Table
    if (jobsData.status === "fulfilled") {
      const jData = jobsData.value;
      const allJobs = jData.jobs || [];
      cachedJobs = allJobs;

      const runningCount = allJobs.filter(j => j.status === "RUNNING" || j.status === "CLAIMED").length;
      const queuedCount = allJobs.filter(j => j.status === "QUEUED" || j.status === "RECOVERING").length;

      document.getElementById("sum-card-jobs-main").textContent = `${runningCount} running`;
      document.getElementById("sum-card-jobs-sub").textContent = `${queuedCount} queued · ${allJobs.length} total`;

      const jobsTbody = document.getElementById("dash-jobs-tbody");
      if (allJobs.length === 0) {
        jobsTbody.innerHTML = `<tr><td colspan="6" class="cell-muted">No workload jobs executed yet.</td></tr>`;
      } else {
        const recent = [...allJobs].reverse().slice(0, 6);
        jobsTbody.innerHTML = recent.map(j => {
          const jobId = j.job_id || j.id || "";
          const target = j.assigned_node || j.target_node || j.target || "auto";
          const duration = j.result?.duration_ms ? (j.result.duration_ms + "ms") : (j.execution_duration_sec ? formatDuration(j.execution_duration_sec) : "-");
          const createdStr = j.created_at ? new Date(j.created_at).toLocaleTimeString() : "-";

          return `
            <tr>
              <td class="mono"><a href="#jobs" onclick="setTimeout(() => viewJobById('${jobId}'), 50)"><strong>${jobId}</strong></a></td>
              <td>${j.type}</td>
              <td class="mono">${target}</td>
              <td>${renderStatusPill(j.status || j.state)}</td>
              <td class="mono cell-muted">${duration}</td>
              <td class="mono cell-muted">${createdStr}</td>
            </tr>
          `;
        }).join("");
      }
    }

    // Storage Summary
    if (storageData.status === "fulfilled") {
      const s = storageData.value;
      const freeStr = s.disk?.available ? `${s.disk.available} free` : "Available";
      const usedPct = s.disk?.used_percent ? `${s.disk.used_percent} used` : "-";
      document.getElementById("sum-card-storage-main").textContent = freeStr;
      document.getElementById("sum-card-storage-sub").textContent = `${usedPct} · ${s.files_count || 0} files`;
      document.getElementById("sys-metric-storage").textContent = s.storage_root || "~/PersonalServer/storage";
    }

    // Services Status
    if (srvData.status === "fulfilled") {
      const srv = srvData.value;
      let activeCount = 0;
      if (srv.node_api === "running") activeCount++;
      if (srv.cloudflare === "connected") activeCount++;
      if (srv.controller === "connected") activeCount++;

      document.getElementById("sum-card-services-main").textContent = `${activeCount} active`;
      document.getElementById("sum-card-services-sub").textContent = `Node API, Tunnel, Controller`;
      document.getElementById("sys-metric-services").innerHTML = `
        <span class="status-dot dot-online">●</span> ${activeCount} connected
      `;
    }

  } catch (err) {
    console.error("Dashboard render error:", err);
  }
}

// 2. Nodes View
async function loadNodes() {
  updateLastRefreshed();
  const tbody = document.getElementById("nodes-tbody");
  const targetSelect = document.getElementById("job-target");

  try {
    const data = await apiFetch("/api/cluster");
    cachedNodes = (data.nodes || []).filter(n => n.status !== "REMOVED");

    const onlineCount = cachedNodes.filter(n => (n.status || "").toUpperCase() === "ONLINE").length;
    const countHdr = document.getElementById("nodes-count-header");
    if (countHdr) countHdr.textContent = `${onlineCount} online · ${cachedNodes.length} total`;

    if (targetSelect) {
      const cur = targetSelect.value;
      targetSelect.innerHTML = `<option value="auto">Auto (Scheduler)</option>` +
        cachedNodes.map(n => `<option value="${n.node_id}">${n.name || n.node_id} (${n.node_id.slice(0, 8)})</option>`).join("");
      targetSelect.value = cur;
    }

    if (cachedNodes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="cell-muted">No cluster nodes registered.</td></tr>`;
      return;
    }

    tbody.innerHTML = cachedNodes.map(n => {
      const isOnline = (n.status || "").toUpperCase() === "ONLINE";
      const memStr = n.last_heartbeat?.system?.memory || (n.resources?.ram_mb ? `${n.resources.ram_mb} MB` : "-");
      const cores = n.last_heartbeat?.system?.cpu_cores || n.resources?.cpu_cores || "-";
      const lastSeen = isOnline ? timeAgo(n.last_seen) : (n.last_seen ? timeAgo(n.last_seen) : "offline");

      return `
        <tr>
          <td>${renderStatusPill(n.status)}</td>
          <td><strong>${n.name || n.node_id}</strong></td>
          <td class="cell-muted">${n.role || 'compute'}</td>
          <td class="cell-muted">${n.platform || '-'}</td>
          <td class="mono cell-muted">${n.architecture || '-'}</td>
          <td class="mono">${cores} cores</td>
          <td class="mono">${memStr}</td>
          <td class="mono cell-muted">${lastSeen}</td>
          <td style="text-align: right;">
            <button class="btn btn-sm" onclick="viewNodeById('${n.node_id}')">Details</button>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" class="pill-failed">Unable to load nodes: ${err.message}</td></tr>`;
  }
}

window.viewNodeById = function(nodeId) {
  const node = cachedNodes.find(n => n.node_id === nodeId);
  if (!node) return;

  const panel = document.getElementById("node-detail-panel");
  const title = document.getElementById("node-detail-title");
  const grid = document.getElementById("node-detail-grid");

  if (title) title.textContent = `Node: ${node.name || node.node_id}`;

  const hb = node.last_heartbeat || {};
  const sys = hb.system || {};
  const srv = hb.services || {};
  const caps = Object.keys(node.capabilities || {}).filter(k => node.capabilities[k]).join(", ") || "compute, storage, network";

  grid.innerHTML = `
    <div class="detail-item"><span class="detail-label">Status</span><span class="detail-value">${renderStatusPill(node.status)}</span></div>
    <div class="detail-item"><span class="detail-label">Node ID</span><span class="detail-value mono">${node.node_id}</span></div>
    <div class="detail-item"><span class="detail-label">Role</span><span class="detail-value">${node.role || 'compute'}</span></div>
    <div class="detail-item"><span class="detail-label">Platform</span><span class="detail-value">${node.platform || '-'} (${node.os || 'Linux'})</span></div>
    <div class="detail-item"><span class="detail-label">Architecture</span><span class="detail-value mono">${node.architecture || '-'}</span></div>
    <div class="detail-item"><span class="detail-label">CPU Cores</span><span class="detail-value mono">${sys.cpu_cores || node.resources?.cpu_cores || '-'} cores</span></div>
    <div class="detail-item"><span class="detail-label">Memory</span><span class="detail-value mono">${sys.memory || (node.resources?.ram_mb ? node.resources.ram_mb + ' MB' : '-')}</span></div>
    <div class="detail-item"><span class="detail-label">Storage</span><span class="detail-value mono">${sys.storage || (node.resources?.storage_gb ? node.resources.storage_gb + ' GB' : '-')}</span></div>
    <div class="detail-item"><span class="detail-label">Capabilities</span><span class="detail-value mono">${caps}</span></div>
    <div class="detail-item"><span class="detail-label">Last Heartbeat</span><span class="detail-value mono">${node.last_seen || '-'} (${timeAgo(node.last_seen)})</span></div>
    <div class="detail-item"><span class="detail-label">Services</span><span class="detail-value mono">Node API: ${srv.node_api || srv['node-api'] || 'running'} · Tunnel: ${srv.cloudflare || 'connected'}</span></div>
    <div class="detail-item"><span class="detail-label">Load Average</span><span class="detail-value mono">${(sys.load_average || []).join(', ') || '-'}</span></div>
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
    const data = await apiFetch("/api/jobs");
    cachedJobs = data.jobs || [];

    const countHdr = document.getElementById("jobs-count-header");
    if (countHdr) countHdr.textContent = `${cachedJobs.length} total jobs`;

    if (cachedJobs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="cell-muted">No workload jobs recorded.</td></tr>`;
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
          <td>${renderStatusPill(j.status || j.state)}</td>
          <td class="mono">${attemptStr}</td>
          <td class="mono cell-muted">${duration}</td>
          <td class="mono cell-muted">${createdStr}</td>
          <td style="text-align: right;">
            <button class="btn btn-sm" onclick="viewJobById('${jobId}')">Details</button>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="pill-failed">Unable to load jobs: ${err.message}</td></tr>`;
  }
}

window.viewJobById = function(jobId) {
  const job = cachedJobs.find(j => (j.job_id === jobId || j.id === jobId));
  const panel = document.getElementById("job-detail-panel");
  const title = document.getElementById("job-detail-title");
  const sumGrid = document.getElementById("job-detail-summary");
  const stdoutBox = document.getElementById("job-detail-stdout");

  const renderJobInfo = (j) => {
    if (title) title.textContent = `Job: ${j.job_id || j.id}`;
    
    const sched = j.scheduler || {};
    const res = j.result || {};
    
    sumGrid.innerHTML = `
      <div class="detail-item"><span class="detail-label">Status</span><span class="detail-value">${renderStatusPill(j.status || j.state)}</span></div>
      <div class="detail-item"><span class="detail-label">Workload</span><span class="detail-value">${j.type}</span></div>
      <div class="detail-item"><span class="detail-label">Target</span><span class="detail-value mono">${j.target || 'auto'}</span></div>
      <div class="detail-item"><span class="detail-label">Assigned Node</span><span class="detail-value mono">${j.assigned_node || j.target_node || '-'}</span></div>
      <div class="detail-item"><span class="detail-label">Attempt</span><span class="detail-value mono">${j.attempt || 1}/${j.max_attempts || 3}</span></div>
      <div class="detail-item"><span class="detail-label">Duration</span><span class="detail-value mono">${res.duration_ms ? res.duration_ms + 'ms' : (j.execution_duration_sec ? formatDuration(j.execution_duration_sec) : '-')}</span></div>
      <div class="detail-item"><span class="detail-label">Exit Code</span><span class="detail-value mono">${res.exit_code !== undefined ? res.exit_code : '-'}</span></div>
      <div class="detail-item"><span class="detail-label">Created At</span><span class="detail-value mono">${j.created_at || '-'}</span></div>
      <div class="detail-item"><span class="detail-label">Scheduler Decision</span><span class="detail-value">${sched.reason || 'Explicit selection'}</span></div>
      <div class="detail-item"><span class="detail-label">Retry Reason</span><span class="detail-value">${j.retry_reason || 'None'}</span></div>
    `;

    const outText = res.stdout || res.stderr || (j.result ? JSON.stringify(j.result, null, 2) : "No output recorded.");
    stdoutBox.textContent = outText;

    panel.style.display = "block";
    panel.scrollIntoView({ behavior: "smooth" });
  };

  if (job) {
    renderJobInfo(job);
  } else {
    apiFetch(`/api/jobs/${jobId}`)
      .then(d => renderJobInfo(d.job || d))
      .catch(e => alert("Failed to fetch job details: " + e.message));
  }
};

window.closeJobDetail = function() {
  const panel = document.getElementById("job-detail-panel");
  if (panel) panel.style.display = "none";
};

// Dispatch job
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
    await apiFetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, target, timeout, parameters: params })
    });
    loadJobs();
  } catch (err) {
    alert("Job submission failed: " + err.message);
  }
});

// 4. Storage View & Multi-Node Discovery
async function loadStorageNodes() {
  try {
    const data = await apiFetch("/storage/nodes").catch(() => null);
    if (data && data.nodes && data.nodes.length > 0) {
      cachedStorageNodes = data.nodes;
    } else {
      cachedStorageNodes = [{
        node_id: "server-5387a86bf36116b1",
        name: "vivo-y31",
        status: "ONLINE",
        is_local: true
      }];
    }
  } catch {
    cachedStorageNodes = [{
      node_id: "server-5387a86bf36116b1",
      name: "vivo-y31",
      status: "ONLINE",
      is_local: true
    }];
  }

  const found = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  if (!found) {
    const localNode = cachedStorageNodes.find(n => n.is_local) || cachedStorageNodes[0];
    selectedStorageNode = localNode ? (localNode.name || localNode.node_id) : "vivo-y31";
  }

  renderStorageNodePills();
}

function renderStorageNodePills() {
  const container = document.getElementById("storage-node-pills");
  if (!container) return;

  container.innerHTML = cachedStorageNodes.map(n => {
    const isOnline = (n.status || "").toUpperCase() === "ONLINE";
    const isSelected = n.name === selectedStorageNode || n.node_id === selectedStorageNode;
    const freeStr = n.storage?.available ? `${n.storage.available} free` : "";

    return `
      <div class="storage-node-pill ${isSelected ? 'active' : ''}" onclick="switchStorageNode('${n.name || n.node_id}')">
        <span class="status-dot ${isOnline ? 'dot-online' : 'dot-offline'}">●</span>
        <span>${n.name || n.node_id}${n.is_local ? ' (Local)' : ''}</span>
        ${freeStr ? `<span class="node-pill-storage">${freeStr}</span>` : ''}
      </div>
    `;
  }).join("");
}

window.switchStorageNode = function(nodeNameOrId) {
  selectedStorageNode = nodeNameOrId;
  currentPath = "";
  renderStorageNodePills();
  loadStorage("", selectedStorageNode);
};

async function loadStorage(path = "", node = selectedStorageNode) {
  updateLastRefreshed();
  currentPath = path;

  let nodeObj = cachedStorageNodes.find(n => n.name === node || n.node_id === node);
  if (!nodeObj && cachedStorageNodes.length > 0) {
    nodeObj = cachedStorageNodes.find(n => n.is_local) || cachedStorageNodes[0];
    selectedStorageNode = nodeObj ? (nodeObj.name || nodeObj.node_id) : "vivo-y31";
    node = selectedStorageNode;
  }
  const isLocal = !nodeObj || nodeObj.is_local;
  const nodeDisplayName = nodeObj ? (nodeObj.name || nodeObj.node_id) : (node || "Local Node");

  renderStorageNodePills();
  renderBreadcrumbs(path, nodeDisplayName);

  const rootPathEl = document.getElementById("storage-root-path");
  if (rootPathEl) {
    rootPathEl.textContent = isLocal ? "~/PersonalServer/storage (Local)" : `~/PersonalServer/storage on ${nodeDisplayName}`;
  }

  const isOnline = (nodeObj?.status || "").toUpperCase() === "ONLINE";
  const actionsContainer = document.getElementById("storage-actions-container");
  if (actionsContainer) {
    if (isOnline) {
      actionsContainer.innerHTML = `
        <input type="file" id="file-upload-input" style="display:none">
        <button id="upload-file-btn" class="btn btn-sm btn-primary">Upload</button>
        <button id="create-folder-btn" class="btn btn-sm">New folder</button>
      `;
      document.getElementById("upload-file-btn")?.addEventListener("click", () => {
        document.getElementById("file-upload-input")?.click();
      });
      document.getElementById("file-upload-input")?.addEventListener("change", handleFileUpload);
      document.getElementById("create-folder-btn")?.addEventListener("click", handleCreateFolder);
    } else {
      actionsContainer.innerHTML = `
        <span class="storage-badge-ro">⚠️ Node Offline (Write Disabled)</span>
      `;
    }
  }

  const tbody = document.getElementById("storage-tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="cell-muted">Loading ${nodeDisplayName} storage items...</td></tr>`;

  try {
    const listUrl = isLocal 
      ? `/storage/list?path=${encodeURIComponent(path)}` 
      : `/storage/list?node=${encodeURIComponent(node)}&path=${encodeURIComponent(path)}`;

    const data = await apiFetch(listUrl);
    const items = data.items || [];

    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="cell-muted">Folder is empty on ${nodeDisplayName}.</td></tr>`;
    } else {
      tbody.innerHTML = items.map(item => {
        const itemRelPath = path ? `${path}/${item.name}` : item.name;
        const isDir = item.is_dir;
        const typeCategory = getFileTypeCategory(item.name, isDir);
        const icon = getFileIcon(typeCategory);
        const modStr = item.modified ? new Date(item.modified * 1000).toLocaleDateString() : "-";
        const sizeStr = isDir ? "—" : formatBytes(item.size_bytes);

        const downloadUrl = isLocal
          ? `/storage/download?path=${encodeURIComponent(itemRelPath)}`
          : `/storage/download?node=${encodeURIComponent(node)}&path=${encodeURIComponent(itemRelPath)}`;

        return `
          <tr>
            <td>
              ${isDir 
                ? `<a href="javascript:void(0)" onclick="loadStorage('${itemRelPath}', '${node}')" style="font-weight: 500;">${icon} ${item.name}</a>`
                : `<span class="mono">${icon} ${item.name}</span>`
              }
            </td>
            <td class="cell-muted">${typeCategory}</td>
            <td class="mono cell-muted">${sizeStr}</td>
            <td class="mono cell-muted">${modStr}</td>
            <td style="text-align: right;">
              <div style="display: inline-flex; gap: 6px;">
                ${!isDir ? `<a href="${downloadUrl}" class="btn btn-sm btn-primary" download>Download</a>` : ''}
                ${isOnline ? `<button class="btn btn-sm" onclick="renameItem('${itemRelPath}')">Rename</button>` : ''}
                ${isOnline ? `<button class="btn btn-sm btn-danger" onclick="deleteItem('${itemRelPath}', ${isDir})">Delete</button>` : ''}
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    // Load usage stats for the active node
    const usageUrl = isLocal ? `/storage/usage` : `/storage/usage?node=${encodeURIComponent(node)}`;
    const usage = await apiFetch(usageUrl).catch(() => null);
    if (usage) {
      const freeStr = usage.disk?.available ? `${usage.disk.available} free` : (nodeObj?.storage?.available || "Available");
      const usedPct = usage.disk?.used_percent ? `${usage.disk.used_percent} used` : (nodeObj?.storage?.used_percent || "-");
      document.getElementById("storage-footer-stats").textContent = 
        `[${nodeDisplayName}] ${freeStr} · ${usedPct} · ${usage.files_count || 0} files · ${usage.folders_count || 0} folders`;
    }

  } catch (err) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" class="pill-failed">
          Unable to load storage on ${nodeDisplayName}: ${err.message}
          <button class="btn btn-sm" style="margin-left: 10px;" onclick="loadStorage('${path}', '${node}')">Retry</button>
        </td>
      </tr>
    `;
  }
}

function renderBreadcrumbs(path, nodeName = "vivo-y31") {
  const container = document.getElementById("storage-breadcrumbs");
  if (!container) return;

  const parts = path ? path.split("/").filter(Boolean) : [];
  let html = `<span class="crumb ${parts.length === 0 ? 'current' : ''}" onclick="loadStorage('', '${selectedStorageNode}')">[ ${nodeName} ] Home / storage</span>`;

  let accumulated = "";
  parts.forEach((p, index) => {
    accumulated += (accumulated ? "/" : "") + p;
    const isLast = index === parts.length - 1;
    const clickPath = accumulated;
    html += ` <span class="cell-muted">/</span> <span class="crumb ${isLast ? 'current' : ''}" ${!isLast ? `onclick="loadStorage('${clickPath}', '${selectedStorageNode}')"` : ''}>${p}</span>`;
  });

  container.innerHTML = html;
}

// Upload file handler
async function handleFileUpload(e) {
  const files = e.target.files;
  if (!files || files.length === 0) return;
  const file = files[0];

  const formData = new FormData();
  formData.append("file", file);

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const isLocal = !nodeObj || nodeObj.is_local;
  const uploadUrl = isLocal
    ? `/storage/upload?path=${encodeURIComponent(currentPath)}`
    : `/storage/upload?node=${encodeURIComponent(selectedStorageNode)}&path=${encodeURIComponent(currentPath)}`;

  try {
    const res = await fetch(uploadUrl, {
      method: "POST",
      body: formData
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || res.statusText);
    }
    e.target.value = "";
    loadStorage(currentPath, selectedStorageNode);
  } catch (err) {
    alert("Upload failed: " + err.message);
  }
}

// Create folder handler
async function handleCreateFolder() {
  const name = prompt("Folder name:");
  if (!name || !name.trim()) return;

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const isLocal = !nodeObj || nodeObj.is_local;
  const mkdirUrl = isLocal
    ? `/storage/mkdir?path=${encodeURIComponent(currentPath)}`
    : `/storage/mkdir?node=${encodeURIComponent(selectedStorageNode)}&path=${encodeURIComponent(currentPath)}`;

  try {
    await apiFetch(mkdirUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), path: currentPath })
    });
    loadStorage(currentPath, selectedStorageNode);
  } catch (err) {
    alert("Failed to create folder: " + err.message);
  }
}

// Rename item
window.renameItem = async function(itemRelPath) {
  const oldName = itemRelPath.split("/").pop();
  const newName = prompt("Rename to:", oldName);
  if (!newName || newName.trim() === oldName) return;

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const isLocal = !nodeObj || nodeObj.is_local;
  const renameUrl = isLocal
    ? `/storage/rename`
    : `/storage/rename?node=${encodeURIComponent(selectedStorageNode)}`;

  try {
    await apiFetch(renameUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: itemRelPath,
        new_name: newName.trim()
      })
    });
    loadStorage(currentPath, selectedStorageNode);
  } catch (err) {
    alert("Rename failed: " + err.message);
  }
};

// Delete item
window.deleteItem = async function(itemRelPath, isDir) {
  const itemName = itemRelPath.split("/").pop();
  if (!confirm(`Are you sure you want to delete ${isDir ? 'folder' : 'file'} "${itemName}"?`)) return;

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const isLocal = !nodeObj || nodeObj.is_local;
  const deleteUrl = isLocal
    ? `/storage?path=${encodeURIComponent(itemRelPath)}`
    : `/storage?node=${encodeURIComponent(selectedStorageNode)}&path=${encodeURIComponent(itemRelPath)}`;

  try {
    await apiFetch(deleteUrl, {
      method: "DELETE"
    });
    loadStorage(currentPath, selectedStorageNode);
  } catch (err) {
    alert("Delete failed: " + err.message);
  }
};

// Initialize
navigate();
