// PersonalServer v1.1 - Client Application Logic

let currentPath = "";
let selectedStorageNode = null;
let cachedStorageNodes = [];
let cachedNodes = [];
let cachedJobs = [];
let cachedPlatformConfig = null;

async function loadPlatformConfig() {
  if (cachedPlatformConfig) return cachedPlatformConfig;
  try {
    cachedPlatformConfig = await apiFetch("/api/platform");
  } catch (err) {
    // Non-blocking fallback
  }
  return cachedPlatformConfig;
}

function getPlatformBaseUrl() {
  if (cachedPlatformConfig?.application?.base_url) {
    return cachedPlatformConfig.application.base_url;
  }
  if (cachedPlatformConfig?.urls?.primary) {
    return cachedPlatformConfig.urls.primary;
  }
  try {
    const host = window.location.hostname;
    if (host.startsWith("server.")) {
      return `${window.location.protocol}//${host.slice(7)}`;
    }
  } catch (e) {}
  return "https://akshatsahay.space";
}

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
    storage: { main: "Storage", sub: "Personal file manager" },
    apps: { main: "Applications", sub: "Containerized application manager" },
    settings: { main: "Settings", sub: "Cluster configuration" }
  };

  const meta = titles[hash] || { main: "Dashboard", sub: "Cluster overview" };
  if (heading) heading.textContent = meta.main;
  if (subHeading) subHeading.textContent = meta.sub;

  loadSession();
  loadPlatformConfig();
  if (hash === "dashboard") loadDashboard();
  else if (hash === "nodes") loadNodes();
  else if (hash === "jobs") loadJobs();
  else if (hash === "storage") {
    loadStorageNodes().then(() => loadStorage(currentPath, selectedStorageNode));
  } else if (hash === "apps") {
    loadApps();
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
  if (["pdf"].includes(ext)) return "PDF Document";
  if (["doc", "docx", "txt", "md", "csv", "json", "yml", "yaml", "xml"].includes(ext)) return "Document";
  if (["py", "sh", "js", "ts", "html", "css", "c", "cpp", "go", "rs"].includes(ext)) return "Code";
  return "File";
}

function getFileIcon(filename, isDir) {
  if (isDir) return "📁";
  const ext = (filename.split('.').pop() || "").toLowerCase();
  if (["zip", "tar", "gz", "tgz", "bz2", "7z", "rar"].includes(ext)) return "📦";
  if (["jpg", "jpeg", "png", "gif", "svg", "webp", "ico"].includes(ext)) return "🖼️";
  if (["mp4", "mkv", "webm", "mov", "avi"].includes(ext)) return "🎬";
  if (["mp3", "wav", "flac", "ogg"].includes(ext)) return "🎵";
  if (["py", "sh", "js", "ts", "html", "css", "c", "cpp", "go", "rs"].includes(ext)) return "💻";
  return "📄";
}

function renderStatusPill(state) {
  const s = (state || "").toUpperCase();
  if (s === "ONLINE" || s === "SUCCEEDED") {
    return `<span class="text-online">${s}</span>`;
  } else if (s === "RUNNING") {
    return `<span class="text-online">RUNNING</span>`;
  } else if (s === "QUEUED" || s === "CLAIMED" || s === "RECOVERING" || s === "DEPLOYING" || s === "REMOVING") {
    return `<span class="text-warning">${s}</span>`;
  } else if (s === "STOPPED" || s === "CREATED") {
    return `<span class="cell-muted">${s}</span>`;
  } else if (s === "FAILED" || s === "TIMEOUT" || s === "REJECTED") {
    return `<span class="text-offline">${s}</span>`;
  } else if (s === "OFFLINE") {
    return `<span class="text-offline">OFFLINE</span>`;
  }
  return `<span class="cell-muted">${s || "UNKNOWN"}</span>`;
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
      if (userEl) userEl.textContent = "Local Admin";
      if (topUserEl) topUserEl.textContent = "Local Admin";
    }
  } catch {
    const userEl = document.getElementById("side-session-user");
    const topUserEl = document.getElementById("topbar-user-email");
    if (userEl) userEl.textContent = "Local Admin";
    if (topUserEl) topUserEl.textContent = "Local Admin";
  }
}

// 1. Dashboard
async function loadDashboard() {
  updateLastRefreshed();
  try {
    const [clusterData, jobsData, storageData, srvData, utilData] = await Promise.allSettled([
      apiFetch("/api/cluster"),
      apiFetch("/api/jobs"),
      apiFetch("/storage/usage"),
      apiFetch("/api/services"),
      apiFetch("/api/cluster/utilization")
    ]);

    // Populate Cluster Utilization Strip if available
    let nodesUtil = {};
    if (utilData.status === "fulfilled" && utilData.value?.cluster) {
      const u = utilData.value.cluster;
      nodesUtil = utilData.value.nodes || {};

      const cpuEl = document.getElementById("util-cluster-cpu");
      if (cpuEl) cpuEl.textContent = `${u.cpu_cores_total} Cores`;

      const memEl = document.getElementById("util-cluster-mem");
      if (memEl) {
        const memUsedGb = (u.memory_used_mb / 1024).toFixed(1);
        const memTotGb = (u.memory_total_mb / 1024).toFixed(1);
        memEl.textContent = `${memUsedGb} / ${memTotGb} GB`;
      }
      const memSub = document.getElementById("util-cluster-mem-sub");
      if (memSub) {
        const pct = u.memory_total_mb > 0 ? ((u.memory_used_mb / u.memory_total_mb) * 100).toFixed(0) : 0;
        const availGb = (u.memory_available_mb / 1024).toFixed(1);
        memSub.textContent = `${pct}% used · ${availGb} GB available`;
      }

      const stEl = document.getElementById("util-cluster-storage");
      if (stEl) {
        stEl.textContent = `${u.storage_used_gb} / ${u.storage_total_gb} GB`;
      }
      const stSub = document.getElementById("util-cluster-storage-sub");
      if (stSub) {
        const pct = u.storage_total_gb > 0 ? ((u.storage_used_gb / u.storage_total_gb) * 100).toFixed(0) : 0;
        stSub.textContent = `${pct}% used · ${u.storage_available_gb} GB available`;
      }

      const wlEl = document.getElementById("util-cluster-workloads");
      if (wlEl) {
        wlEl.textContent = `${u.active_jobs} jobs · ${u.running_containers} containers`;
      }
      const wlSub = document.getElementById("util-cluster-workloads-sub");
      if (wlSub) {
        wlSub.textContent = `${u.running_jobs} currently running jobs`;
      }
    }

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
      const topbarStatus = document.getElementById("topbar-cluster-status");
      if (topbarStatus) topbarStatus.textContent = `${onlineCount} online`;

      const tbody = document.getElementById("dash-nodes-tbody");
      if (nodes.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="cell-muted">No cluster nodes registered.</td></tr>`;
      } else {
        tbody.innerHTML = nodes.map(n => {
          const uNode = nodesUtil[n.node_id];
          const isOnline = (n.status || "").toUpperCase() === "ONLINE";

          let coresStr = "-";
          if (uNode?.cpu?.cores) {
            coresStr = `${uNode.cpu.cores}c`;
            if (uNode.cpu.cpu_percent != null) coresStr += ` (${uNode.cpu.cpu_percent}%)`;
            else if (uNode.cpu.load_average?.length) coresStr += ` (L:${uNode.cpu.load_average[0]})`;
          } else {
            coresStr = `${n.last_heartbeat?.system?.cpu_cores || n.resources?.cpu_cores || "-"} cores`;
          }

          let memStr = "-";
          if (uNode?.memory?.total_mb) {
            memStr = `${(uNode.memory.used_mb/1024).toFixed(1)}/${(uNode.memory.total_mb/1024).toFixed(1)}G (${uNode.memory.used_percent}%)`;
          } else {
            memStr = n.last_heartbeat?.system?.memory || (n.resources?.ram_mb ? `${(n.resources.ram_mb/1024).toFixed(1)} GB` : "-");
          }

          let storageStr = "-";
          if (uNode?.storage?.total_gb) {
            storageStr = `${uNode.storage.used_gb}/${uNode.storage.total_gb}G (${uNode.storage.used_percent}%)`;
          } else {
            storageStr = n.last_heartbeat?.system?.storage || (n.resources?.storage_gb ? `${n.resources.storage_gb} GB` : "-");
          }

          let wlStr = "-";
          if (uNode?.workloads) {
            wlStr = `${uNode.workloads.active_jobs} jobs · ${uNode.workloads.running_containers} cnt`;
          } else {
            wlStr = "0 jobs · 0 cnt";
          }

          return `
            <tr style="cursor: pointer;" onclick="window.location.hash='#nodes'; setTimeout(() => viewNodeById('${n.node_id}'), 50);">
              <td><strong>${n.name || n.node_id}</strong></td>
              <td>${renderStatusPill(n.status)}</td>
              <td class="mono">${coresStr}</td>
              <td class="mono">${memStr}</td>
              <td class="mono">${storageStr}</td>
              <td class="mono cell-muted">${wlStr}</td>
            </tr>
          `;
        }).join("");

        // Populate System Panel with Primary Node details
        const primary = nodes[0];
        if (primary) {
          const sys = primary.last_heartbeat?.system || {};
          const sysNodeId = document.getElementById("dash-sys-node-id");
          if (sysNodeId) sysNodeId.textContent = primary.node_id ? primary.node_id.slice(0, 16) : "";
          if (document.getElementById("sys-metric-cpu")) document.getElementById("sys-metric-cpu").textContent = `${sys.cpu_cores || primary.resources?.cpu_cores || '8'} cores`;
          if (document.getElementById("sys-metric-mem")) document.getElementById("sys-metric-mem").textContent = sys.memory || (primary.resources?.ram_mb ? `${(primary.resources.ram_mb/1024).toFixed(1)} GB` : '5.7 GB');
          if (document.getElementById("sys-metric-storage")) document.getElementById("sys-metric-storage").textContent = sys.storage || (primary.resources?.storage_gb ? `${primary.resources.storage_gb} GB` : '106 GB');
          if (document.getElementById("sys-metric-arch")) document.getElementById("sys-metric-arch").textContent = primary.architecture || 'aarch64';
          if (document.getElementById("sys-metric-os")) document.getElementById("sys-metric-os").textContent = primary.os || 'Linux';
          if (document.getElementById("sys-metric-uptime")) document.getElementById("sys-metric-uptime").textContent = sys.uptime ? formatDuration(sys.uptime) : (primary.last_seen ? timeAgo(primary.last_seen) : 'active');
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
        jobsTbody.innerHTML = `<tr><td colspan="4" class="cell-muted">No workload jobs executed yet.</td></tr>`;
      } else {
        const recent = [...allJobs].reverse().slice(0, 6);
        jobsTbody.innerHTML = recent.map(j => {
          const jobId = j.job_id || j.id || "";
          const target = j.assigned_node || j.target_node || j.target || "auto";
          const duration = j.result?.duration_ms ? (j.result.duration_ms + "ms") : (j.execution_duration_sec ? formatDuration(j.execution_duration_sec) : "-");

          return `
            <tr>
              <td class="mono"><a href="#jobs" onclick="setTimeout(() => viewJobById('${jobId}'), 50)"><strong>${jobId}</strong></a></td>
              <td>${renderStatusPill(j.status || j.state)}</td>
              <td class="mono">${target}</td>
              <td class="mono cell-muted">${duration}</td>
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
    }

  } catch (err) {
    console.error("Dashboard render error:", err);
  }
}

// 2. Nodes View
let cachedUtilMap = {};

async function loadNodes() {
  updateLastRefreshed();
  const tbody = document.getElementById("nodes-tbody");
  const targetSelect = document.getElementById("job-target");

  try {
    const [clusterRes, utilRes] = await Promise.allSettled([
      apiFetch("/api/cluster"),
      apiFetch("/api/cluster/utilization")
    ]);
    const data = clusterRes.status === "fulfilled" ? clusterRes.value : { nodes: [] };
    cachedUtilMap = (utilRes.status === "fulfilled" && utilRes.value?.nodes) ? utilRes.value.nodes : {};

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
      tbody.innerHTML = `<tr><td colspan="11" class="cell-muted">No cluster nodes registered.</td></tr>`;
      return;
    }

    tbody.innerHTML = cachedNodes.map(n => {
      const isOnline = (n.status || "").toUpperCase() === "ONLINE";
      const uNode = cachedUtilMap[n.node_id];

      let coresStr = "-";
      if (uNode?.cpu?.cores) {
        coresStr = `${uNode.cpu.cores}c`;
        if (uNode.cpu.cpu_percent != null) coresStr += ` (${uNode.cpu.cpu_percent}%)`;
        else if (uNode.cpu.load_average?.length) coresStr += ` (${uNode.cpu.load_average[0]})`;
      } else {
        coresStr = `${n.last_heartbeat?.system?.cpu_cores || n.resources?.cpu_cores || "-"} cores`;
      }

      let memStr = "-";
      if (uNode?.memory?.total_mb) {
        memStr = `${(uNode.memory.used_mb/1024).toFixed(1)} / ${(uNode.memory.total_mb/1024).toFixed(1)} GB (${uNode.memory.used_percent}%)`;
      } else {
        memStr = n.last_heartbeat?.system?.memory || (n.resources?.ram_mb ? `${(n.resources.ram_mb/1024).toFixed(1)} GB` : "-");
      }

      let storageStr = "-";
      if (uNode?.storage?.total_gb) {
        storageStr = `${uNode.storage.used_gb} / ${uNode.storage.total_gb} GB (${uNode.storage.used_percent}%)`;
      } else {
        storageStr = n.last_heartbeat?.system?.storage || (n.resources?.storage_gb ? `${n.resources.storage_gb} GB` : "-");
      }

      const activeJobs = uNode?.workloads?.active_jobs ?? 0;
      const runningJobs = uNode?.workloads?.running_jobs ?? 0;
      const runningContainers = uNode?.workloads?.running_containers ?? 0;

      const lastSeen = isOnline ? timeAgo(n.last_seen) : (n.last_seen ? timeAgo(n.last_seen) : "offline");

      return `
        <tr>
          <td><strong>${n.name || n.node_id}</strong></td>
          <td>${renderStatusPill(n.status)}</td>
          <td class="cell-muted">${n.role || 'compute'}</td>
          <td class="cell-muted">${n.platform || '-'}</td>
          <td class="mono">${coresStr}</td>
          <td class="mono">${memStr}</td>
          <td class="mono">${storageStr}</td>
          <td class="mono">${activeJobs} active (${runningJobs} run)</td>
          <td class="mono">${runningContainers} running</td>
          <td class="mono cell-muted">${lastSeen}</td>
          <td style="text-align: right;">
            <button class="btn btn-sm" onclick="viewNodeById('${n.node_id}')">Details</button>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="11" class="text-offline">Unable to load nodes: ${err.message}</td></tr>`;
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
  const uNode = cachedUtilMap[node.node_id] || {};

  const wlStr = `${uNode.workloads?.active_jobs || 0} active jobs · ${uNode.workloads?.running_jobs || 0} running jobs · ${uNode.workloads?.running_containers || 0} running containers`;

  grid.innerHTML = `
    <div class="detail-item"><span class="detail-label">Status</span><span class="detail-value">${renderStatusPill(node.status)}</span></div>
    <div class="detail-item"><span class="detail-label">Node ID</span><span class="detail-value mono">${node.node_id}</span></div>
    <div class="detail-item"><span class="detail-label">Role</span><span class="detail-value">${node.role || 'compute'}</span></div>
    <div class="detail-item"><span class="detail-label">Platform</span><span class="detail-value">${node.platform || '-'} (${node.os || 'Linux'})</span></div>
    <div class="detail-item"><span class="detail-label">Architecture</span><span class="detail-value mono">${node.architecture || '-'}</span></div>
    <div class="detail-item"><span class="detail-label">CPU Cores</span><span class="detail-value mono">${sys.cpu_cores || node.resources?.cpu_cores || '-'} cores ${uNode.cpu?.cpu_percent != null ? '(' + uNode.cpu.cpu_percent + '%)' : ''}</span></div>
    <div class="detail-item"><span class="detail-label">Memory</span><span class="detail-value mono">${sys.memory || (node.resources?.ram_mb ? (node.resources.ram_mb/1024).toFixed(1) + ' GB' : '-')}</span></div>
    <div class="detail-item"><span class="detail-label">Storage</span><span class="detail-value mono">${sys.storage || (node.resources?.storage_gb ? node.resources.storage_gb + ' GB' : '-')}</span></div>
    <div class="detail-item"><span class="detail-label">Workloads</span><span class="detail-value mono">${wlStr}</span></div>
    <div class="detail-item"><span class="detail-label">Capabilities</span><span class="detail-value mono">${caps}</span></div>
    <div class="detail-item"><span class="detail-label">Last Heartbeat</span><span class="detail-value mono">${node.last_seen || '-'} (${timeAgo(node.last_seen)})</span></div>
    <div class="detail-item"><span class="detail-label">Services</span><span class="detail-value mono">API: ${srv.node_api || srv['node-api'] || 'running'} · Tunnel: ${srv.cloudflare || 'connected'}</span></div>
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
    tbody.innerHTML = `<tr><td colspan="8" class="text-offline">Unable to load jobs: ${err.message}</td></tr>`;
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
      <div class="detail-item"><span class="detail-label">Scheduler Reason</span><span class="detail-value">${sched.reason || 'Explicit selection'}</span></div>
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

// 4. Storage View (Windows File Explorer Subsystem)
let cachedStorageItems = [];
let storageFilterQuery = "";
let pathHistory = [""];
let historyIndex = 0;
let isNavigatingHistory = false;
let selectedItemName = null;

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

  // Populate address bar location dropdown & left pane location list
  const nodeSelect = document.getElementById("storage-node-select");
  if (nodeSelect) {
    const currentVal = selectedStorageNode || "auto";
    nodeSelect.innerHTML = `<option value="auto">Storage: Auto (Cluster)</option>` +
      cachedStorageNodes.map(n => `<option value="${n.name || n.node_id}">Node: ${n.name || n.node_id}${n.is_local ? ' (Local)' : ''} [${n.status}]</option>`).join("");
    nodeSelect.value = selectedStorageNode ? (selectedStorageNode === "vivo-y31" ? "auto" : selectedStorageNode) : "auto";
  }

  const nodeList = document.getElementById("explorer-node-list");
  if (nodeList) {
    nodeList.innerHTML = cachedStorageNodes.map(n => {
      const isOnline = (n.status || "").toUpperCase() === "ONLINE";
      const isSelected = n.name === selectedStorageNode || n.node_id === selectedStorageNode;
      const isAutoSelected = !selectedStorageNode && n.is_local;
      const activeClass = (isSelected || isAutoSelected) ? "active" : "";
      return `
        <a href="javascript:void(0)" class="explorer-node-item ${activeClass}" onclick="switchStorageNode('${n.name || n.node_id}')">
          <span class="${isOnline ? 'text-online' : 'text-offline'}" style="font-size: 9px;">●</span>
          <span>${n.name || n.node_id}${n.is_local ? ' (Local)' : ''}</span>
        </a>
      `;
    }).join("");
  }
}

window.switchStorageNode = function(nodeNameOrId) {
  selectedStorageNode = (nodeNameOrId === "auto") ? null : nodeNameOrId;
  currentPath = "";
  pathHistory = [""];
  historyIndex = 0;
  loadStorageNodes().then(() => loadStorage("", selectedStorageNode, true));
};

// Location select dropdown in Address Bar
document.getElementById("storage-node-select")?.addEventListener("change", (e) => {
  const val = e.target.value;
  switchStorageNode(val);
});

// Navigation Toolbar Buttons: Back, Forward, Up, Refresh
document.getElementById("storage-back-btn")?.addEventListener("click", () => {
  if (historyIndex > 0) {
    isNavigatingHistory = true;
    historyIndex--;
    loadStorage(pathHistory[historyIndex], selectedStorageNode, false);
    isNavigatingHistory = false;
  }
});

document.getElementById("storage-forward-btn")?.addEventListener("click", () => {
  if (historyIndex < pathHistory.length - 1) {
    isNavigatingHistory = true;
    historyIndex++;
    loadStorage(pathHistory[historyIndex], selectedStorageNode, false);
    isNavigatingHistory = false;
  }
});

document.getElementById("storage-up-btn")?.addEventListener("click", () => {
  if (!currentPath) return;
  const parts = currentPath.split("/").filter(Boolean);
  parts.pop();
  const parentPath = parts.join("/");
  loadStorage(parentPath, selectedStorageNode, true);
});

document.getElementById("storage-refresh-btn")?.addEventListener("click", () => {
  loadStorage(currentPath, selectedStorageNode, false);
});

// Search input listener
document.getElementById("storage-filter-input")?.addEventListener("input", (e) => {
  storageFilterQuery = (e.target.value || "").trim().toLowerCase();
  renderStorageTable();
});

// Quick access shortcut navigation
window.navigateToShortcut = function(navPath) {
  loadStorage(navPath, selectedStorageNode, true);
};

function updateNavButtonsAndShortcuts() {
  const backBtn = document.getElementById("storage-back-btn");
  const fwdBtn = document.getElementById("storage-forward-btn");
  const upBtn = document.getElementById("storage-up-btn");

  if (backBtn) backBtn.disabled = (historyIndex <= 0);
  if (fwdBtn) fwdBtn.disabled = (historyIndex >= pathHistory.length - 1);
  if (upBtn) upBtn.disabled = !currentPath;

  // Highlight active shortcut
  document.querySelectorAll(".explorer-shortcut").forEach(el => {
    const navPath = el.getAttribute("data-nav-path");
    if (navPath === currentPath) {
      el.classList.add("active");
    } else {
      el.classList.remove("active");
    }
  });
}

async function loadStorage(path = "", node = selectedStorageNode, pushHistory = true) {
  updateLastRefreshed();
  currentPath = path;

  // Manage History Stack
  if (pushHistory && !isNavigatingHistory) {
    if (pathHistory[historyIndex] !== path) {
      pathHistory = pathHistory.slice(0, historyIndex + 1);
      pathHistory.push(path);
      historyIndex = pathHistory.length - 1;
    }
  }

  updateNavButtonsAndShortcuts();
  renderBreadcrumbs(path);

  let nodeObj = cachedStorageNodes.find(n => n.name === node || n.node_id === node);
  if (!nodeObj && cachedStorageNodes.length > 0) {
    nodeObj = cachedStorageNodes.find(n => n.is_local) || cachedStorageNodes[0];
    selectedStorageNode = nodeObj ? (nodeObj.name || nodeObj.node_id) : "vivo-y31";
    node = selectedStorageNode;
  }
  const isLocal = !nodeObj || nodeObj.is_local;
  const isOnline = (nodeObj?.status || "").toUpperCase() === "ONLINE";

  const actionsContainer = document.getElementById("storage-actions-container");
  if (actionsContainer) {
    if (isOnline) {
      actionsContainer.innerHTML = `
        <input type="file" id="file-upload-input" style="display:none" multiple>
        <button id="upload-file-btn" class="btn btn-sm btn-primary">Upload</button>
        <button id="create-folder-btn" class="btn btn-sm">New Folder</button>
      `;
      document.getElementById("upload-file-btn")?.addEventListener("click", () => {
        document.getElementById("file-upload-input")?.click();
      });
      document.getElementById("file-upload-input")?.addEventListener("change", (e) => {
        if (e.target.files && e.target.files.length > 0) {
          uploadFiles(e.target.files);
          e.target.value = "";
        }
      });
      document.getElementById("create-folder-btn")?.addEventListener("click", handleCreateFolder);
    } else {
      actionsContainer.innerHTML = `
        <span class="storage-badge-ro">Node Offline (Read Only)</span>
      `;
    }
  }

  const tbody = document.getElementById("storage-tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="cell-muted" style="text-align: center; padding: 24px;">Loading storage items...</td></tr>`;

  try {
    const listUrl = isLocal 
      ? `/storage/list?path=${encodeURIComponent(path)}` 
      : `/storage/list?node=${encodeURIComponent(node)}&path=${encodeURIComponent(path)}`;

    const data = await apiFetch(listUrl);
    cachedStorageItems = data.items || [];
    renderStorageTable();

    // Fetch usage statistics for footer status bar
    const usageUrl = isLocal ? `/storage/usage` : `/storage/usage?node=${encodeURIComponent(node)}`;
    const usage = await apiFetch(usageUrl).catch(() => null);
    
    const leftStats = document.getElementById("storage-footer-stats-left");
    const rightStats = document.getElementById("storage-footer-stats-right");
    const oldFooter = document.getElementById("storage-footer-stats");

    const filesCount = cachedStorageItems.filter(i => !i.is_dir).length;
    const foldersCount = cachedStorageItems.filter(i => i.is_dir).length;

    if (leftStats) {
      leftStats.textContent = `${filesCount} file${filesCount === 1 ? '' : 's'} · ${foldersCount} folder${foldersCount === 1 ? '' : 's'}`;
    }

    if (usage && usage.disk) {
      const availStr = usage.disk.available || "Available";
      if (rightStats) {
        rightStats.textContent = `${availStr} available`;
      }
      if (oldFooter) {
        oldFooter.textContent = `${availStr} free · ${usage.disk.used_percent || '-'} · ${usage.files_count || 0} files · ${usage.folders_count || 0} folders`;
      }
    }

  } catch (err) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" class="text-offline" style="text-align: center; padding: 20px;">
          Unable to load storage: ${err.message}
          <button class="btn btn-sm" style="margin-left: 10px;" onclick="loadStorage('${path}', '${node}', false)">Retry</button>
        </td>
      </tr>
    `;
  }
}

function renderStorageTable() {
  const tbody = document.getElementById("storage-tbody");
  if (!tbody) return;

  let items = cachedStorageItems;
  if (storageFilterQuery) {
    items = items.filter(i => (i.name || "").toLowerCase().includes(storageFilterQuery));
  }

  const leftStats = document.getElementById("storage-footer-stats-left");
  if (leftStats) {
    const filesCount = items.filter(i => !i.is_dir).length;
    const foldersCount = items.filter(i => i.is_dir).length;
    leftStats.textContent = `${filesCount} file${filesCount === 1 ? '' : 's'} · ${foldersCount} folder${foldersCount === 1 ? '' : 's'}`;
  }

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const isLocal = !nodeObj || nodeObj.is_local;
  const isOnline = (nodeObj?.status || "").toUpperCase() === "ONLINE";
  const node = selectedStorageNode;

  if (items.length === 0) {
    if (storageFilterQuery) {
      tbody.innerHTML = `<tr><td colspan="5" class="cell-muted" style="text-align: center; padding: 32px;">No items match "${escapeHtml(storageFilterQuery)}".</td></tr>`;
    } else {
      tbody.innerHTML = `
        <tr>
          <td colspan="5" style="padding: 0;">
            <div class="storage-empty-state">
              <span class="storage-empty-icon">📁</span>
              <span class="storage-empty-title">This folder is empty.</span>
              <div class="storage-empty-actions">
                ${isOnline ? `<button class="btn btn-sm btn-primary" onclick="document.getElementById('file-upload-input')?.click()">Upload File</button>` : ''}
                ${isOnline ? `<button class="btn btn-sm" onclick="handleCreateFolder()">New Folder</button>` : ''}
              </div>
            </div>
          </td>
        </tr>
      `;
    }
    return;
  }

  // Sort folders first, then alphabetical
  items.sort((a, b) => {
    if (a.is_dir && !b.is_dir) return -1;
    if (!a.is_dir && b.is_dir) return 1;
    return a.name.localeCompare(b.name);
  });

  tbody.innerHTML = items.map(item => {
    const itemRelPath = currentPath ? `${currentPath}/${item.name}` : item.name;
    const isDir = item.is_dir;
    const typeCategory = getFileTypeCategory(item.name, isDir);
    const icon = getFileIcon(item.name, isDir);
    const modStr = item.modified ? new Date(item.modified * 1000).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    }) : "-";
    const sizeStr = isDir ? "—" : formatBytes(item.size_bytes);

    const downloadUrl = isLocal
      ? `/storage/download?path=${encodeURIComponent(itemRelPath)}`
      : `/storage/download?node=${encodeURIComponent(node)}&path=${encodeURIComponent(itemRelPath)}`;

    const escapedPath = itemRelPath.replace(/'/g, "\\'");
    const escapedName = item.name.replace(/'/g, "\\'");
    const isSelected = selectedItemName === item.name ? "selected" : "";

    return `
      <tr class="explorer-row ${isSelected}" onclick="selectStorageRow(event, '${escapedName}')" ondblclick="${isDir ? `loadStorage('${escapedPath}', '${node}', true)` : `viewStorageProperties('${escapedPath}', false)`}">
        <td>
          <div class="file-name-cell">
            <span class="file-icon">${icon}</span>
            ${isDir 
              ? `<a href="javascript:void(0)" class="mono" style="font-weight: 500;" onclick="event.stopPropagation(); loadStorage('${escapedPath}', '${node}', true)">${escapeHtml(item.name)}</a>`
              : `<span class="mono">${escapeHtml(item.name)}</span>`
            }
          </div>
        </td>
        <td class="cell-muted">${typeCategory}</td>
        <td class="mono cell-muted">${sizeStr}</td>
        <td class="mono cell-muted">${modStr}</td>
        <td style="text-align: right;">
          <div class="explorer-row-actions">
            ${!isDir ? `<a href="${downloadUrl}" class="btn btn-sm" download title="Download file" onclick="event.stopPropagation()">Download</a>` : ''}
            ${isOnline ? `<button class="btn btn-sm" onclick="event.stopPropagation(); renameItem('${escapedPath}')" title="Rename">Rename</button>` : ''}
            ${isOnline ? `<button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); deleteItem('${escapedPath}', ${isDir})" title="Delete">Delete</button>` : ''}
            <button class="btn btn-sm" onclick="event.stopPropagation(); viewStorageProperties('${escapedPath}', ${isDir})" title="Item Properties">Props</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

window.selectStorageRow = function(event, itemName) {
  selectedItemName = itemName;
  document.querySelectorAll(".explorer-row").forEach(tr => tr.classList.remove("selected"));
  const row = event.currentTarget;
  if (row) row.classList.add("selected");
};

function renderBreadcrumbs(path) {
  const container = document.getElementById("storage-breadcrumbs");
  if (!container) return;

  const parts = path ? path.split("/").filter(Boolean) : [];
  let html = `<span class="crumb ${parts.length === 0 ? 'current' : ''}" onclick="loadStorage('', '${selectedStorageNode}', true)">Home</span>`;

  let accumulated = "";
  parts.forEach((p, index) => {
    accumulated += (accumulated ? "/" : "") + p;
    const isLast = index === parts.length - 1;
    const clickPath = accumulated;
    html += ` <span class="cell-muted">&gt;</span> <span class="crumb ${isLast ? 'current' : ''}" ${!isLast ? `onclick="loadStorage('${clickPath}', '${selectedStorageNode}', true)"` : ''}>${escapeHtml(p)}</span>`;
  });

  container.innerHTML = html;
}

// Drag and Drop File Dropzone
const dropzone = document.getElementById("storage-dropzone");
if (dropzone) {
  ['dragenter', 'dragover'].forEach(eventName => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add('drag-active');
    }, false);
  });

  ['dragleave', 'dragend'].forEach(eventName => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove('drag-active');
    }, false);
  });

  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropzone.classList.remove('drag-active');
    const dt = e.dataTransfer;
    if (dt && dt.files && dt.files.length > 0) {
      uploadFiles(dt.files);
    }
  }, false);
}

// Upload file(s) helper
async function uploadFiles(fileList) {
  if (!fileList || fileList.length === 0) return;

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const isLocal = !nodeObj || nodeObj.is_local;
  const uploadUrl = isLocal
    ? `/storage/upload?path=${encodeURIComponent(currentPath)}`
    : `/storage/upload?node=${encodeURIComponent(selectedStorageNode)}&path=${encodeURIComponent(currentPath)}`;

  for (let i = 0; i < fileList.length; i++) {
    const file = fileList[i];
    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch(uploadUrl, {
        method: "POST",
        body: formData
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || res.statusText);
      }
    } catch (err) {
      showCustomAlert({ title: "Upload Failed", message: `Upload failed for "${file.name}": ${err.message}`, isError: true });
      break;
    }
  }

  loadStorage(currentPath, selectedStorageNode, false);
}

// Create folder handler (using custom modal)
async function handleCreateFolder() {
  const name = await showCustomPrompt({
    title: "New folder",
    label: "Folder name",
    placeholder: "e.g. documents",
    confirmText: "Create",
    validate: (val) => {
      if (!val) return "Folder name cannot be empty.";
      if (/[\\/:\*\?"<>\|\x00]/.test(val) || val.includes("..")) return "Invalid directory name characters.";
      return "";
    }
  });
  if (!name) return;

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const isLocal = !nodeObj || nodeObj.is_local;
  const mkdirUrl = isLocal
    ? `/storage/mkdir?path=${encodeURIComponent(currentPath)}`
    : `/storage/mkdir?node=${encodeURIComponent(selectedStorageNode)}&path=${encodeURIComponent(currentPath)}`;

  try {
    await apiFetch(mkdirUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path: currentPath })
    });
    loadStorage(currentPath, selectedStorageNode, false);
  } catch (err) {
    showCustomAlert({ title: "Folder Creation Failed", message: err.message, isError: true });
  }
}

// Rename item (using custom modal)
window.renameItem = async function(itemRelPath) {
  const oldName = itemRelPath.split("/").pop();
  const newName = await showCustomPrompt({
    title: "Rename item",
    label: "New name",
    initialValue: oldName,
    confirmText: "Rename",
    validate: (val) => {
      if (!val) return "Name cannot be empty.";
      if (val === oldName) return "New name must be different.";
      if (/[\\/:\*\?"<>\|\x00]/.test(val) || val.includes("..")) return "Invalid filename characters.";
      return "";
    }
  });
  if (!newName) return;

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
        new_name: newName
      })
    });
    loadStorage(currentPath, selectedStorageNode, false);
  } catch (err) {
    showCustomAlert({ title: "Rename Failed", message: err.message, isError: true });
  }
};

// Delete item (using custom modal)
window.deleteItem = async function(itemRelPath, isDir) {
  const itemName = itemRelPath.split("/").pop();
  const confirmed = await showCustomConfirm({
    title: `Delete ${isDir ? 'Folder' : 'File'}`,
    message: `Delete "${itemName}"?\n\nThis action cannot be undone.`,
    confirmText: "Delete",
    isDanger: true
  });
  if (!confirmed) return;

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const isLocal = !nodeObj || nodeObj.is_local;
  const deleteUrl = isLocal
    ? `/storage?path=${encodeURIComponent(itemRelPath)}`
    : `/storage?node=${encodeURIComponent(selectedStorageNode)}&path=${encodeURIComponent(itemRelPath)}`;

  try {
    await apiFetch(deleteUrl, {
      method: "DELETE"
    });
    loadStorage(currentPath, selectedStorageNode, false);
  } catch (err) {
    showCustomAlert({ title: "Delete Failed", message: err.message, isError: true });
  }
};

// View Storage Item Properties (Windows Explorer Style)
window.viewStorageProperties = function(itemRelPath, isDir) {
  const itemName = itemRelPath.split("/").pop();
  const item = cachedStorageItems.find(i => i.name === itemName);

  const panel = document.getElementById("storage-properties-panel");
  const title = document.getElementById("storage-props-title");
  const grid = document.getElementById("storage-props-grid");

  if (title) title.textContent = `Properties: ${itemName}`;

  let nodeObj = cachedStorageNodes.find(n => n.name === selectedStorageNode || n.node_id === selectedStorageNode);
  const nodeName = nodeObj ? (nodeObj.name || nodeObj.node_id) : (selectedStorageNode || "vivo-y31");
  const status = (nodeObj?.status || "ONLINE").toUpperCase();
  const sizeStr = item ? (isDir ? "—" : formatBytes(item.size_bytes)) : "—";
  const modStr = item?.modified ? new Date(item.modified * 1000).toLocaleString() : "-";
  const typeStr = isDir ? "Folder" : getFileTypeCategory(itemName, false);
  const physPath = `~/PersonalServer/storage/${itemRelPath}`;

  grid.innerHTML = `
    <div class="detail-item"><span class="detail-label">Name</span><span class="detail-value mono">${escapeHtml(itemName)}</span></div>
    <div class="detail-item"><span class="detail-label">Type</span><span class="detail-value">${typeStr}</span></div>
    <div class="detail-item"><span class="detail-label">Logical Path</span><span class="detail-value mono">Home/${escapeHtml(itemRelPath)}</span></div>
    <div class="detail-item"><span class="detail-label">Size</span><span class="detail-value mono">${sizeStr}</span></div>
    <div class="detail-item"><span class="detail-label">Modified</span><span class="detail-value mono">${modStr}</span></div>
    <div class="detail-item"><span class="detail-label">Storage Node</span><span class="detail-value mono">${nodeName}</span></div>
    <div class="detail-item"><span class="detail-label">Physical Path</span><span class="detail-value mono">${physPath}</span></div>
    <div class="detail-item"><span class="detail-label">Status</span><span class="detail-value text-online">${status === 'ONLINE' ? 'Healthy' : status}</span></div>
  `;

  panel.style.display = "block";
  panel.scrollIntoView({ behavior: "smooth" });
};

window.closeStorageProperties = function() {
  const panel = document.getElementById("storage-properties-panel");
  if (panel) panel.style.display = "none";
};

// ==========================================
// 5. Applications Logic (Phase 13: Source & Blueprint)
// ==========================================

let cachedApps = [];
let currentActiveAppId = null;
let currentActiveAppTab = "overview";
let currentLogAppId = null;
let currentAppFilesId = null;
let currentAppFilesPath = "";
let currentAppFilesItems = [];
let detectedBlueprint = null;

async function loadApps() {
  const tbody = document.getElementById("apps-tbody");
  const countHeader = document.getElementById("apps-count-header");
  const targetSelect = document.getElementById("app-target");

  try {
    const res = await apiFetch("/api/apps");
    cachedApps = res.apps || [];
    
    if (countHeader) {
      countHeader.textContent = `${cachedApps.length} application${cachedApps.length === 1 ? '' : 's'}`;
    }

    // Populate node targets in create form
    if (targetSelect) {
      const nodesRes = await apiFetch("/api/nodes").catch(() => ({ nodes: [] }));
      const nodes = nodesRes.nodes || [];
      const currentVal = targetSelect.value || "auto";
      targetSelect.innerHTML = `<option value="auto">Auto (Scheduler)</option>` +
        nodes.map(n => `<option value="${n.name || n.node_id}">${n.name || n.node_id} (${n.status})</option>`).join("");
      targetSelect.value = currentVal;
    }

    if (cachedApps.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="cell-muted" style="text-align:center; padding: 20px;">No applications created yet. Click "+ Deploy from GitHub" above to deploy your first application.</td></tr>`;
      return;
    }

    tbody.innerHTML = cachedApps.map(app => {
      const statusPill = renderStatusPill(app.status);
      const hostPort = app.port || app.host_port || "-";
      const contPort = app.container_port || "-";
      const portDisplay = hostPort !== "-" ? `${hostPort}:${contPort}` : `${contPort}`;
      const nodeDisplay = app.selected_node || app.target || "auto";
      const failureNote = app.failure_reason ? `<div class="cell-muted mono" style="font-size: 10px; color: var(--status-red);">${escapeHtml(app.failure_reason)}</div>` : '';

      const isGithub = app.source && app.source.type === "github";
      const sourceBadge = isGithub
        ? `<span class="source-badge" title="GitHub: ${escapeHtml(app.source.repository)}@${escapeHtml(app.source.branch || 'main')}">GitHub: ${escapeHtml(app.source.repository?.split('/')[1] || app.source.repository || 'repo')}</span>`
        : `<span class="source-badge source-badge-manual">Manual</span>`;

      const routePath = app.route?.path || (app.route ? app.route.path : "-");
      const routeDisplay = routePath && routePath !== "-"
        ? `<a href="${routePath}/" target="_blank" class="mono text-online" style="font-weight: 500;" title="Open application route">${escapeHtml(routePath)}/</a>`
        : `<span class="cell-muted mono">—</span>`;

      const canDeploy = ["CREATED", "STOPPED", "FAILED"].includes(app.status);
      const canRedeploy = isGithub && ["RUNNING", "FAILED", "STOPPED"].includes(app.status);
      const canStop = ["RUNNING"].includes(app.status);
      const canRestart = ["RUNNING", "STOPPED"].includes(app.status);

      return `
        <tr>
          <td>
            <a href="javascript:void(0)" onclick="openAppDetails('${app.app_id}')" class="mono" style="font-weight: 600;">${escapeHtml(app.name)}</a>
            <div class="cell-muted mono" style="font-size: 10px;">${escapeHtml(app.app_id)}</div>
          </td>
          <td>${sourceBadge}</td>
          <td>
            ${statusPill}
            ${failureNote}
          </td>
          <td>${routeDisplay}</td>
          <td class="mono">${escapeHtml(nodeDisplay)}</td>
          <td class="mono">${portDisplay}</td>
          <td style="text-align: right;">
            <div style="display: inline-flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end;">
              <button class="btn btn-sm" onclick="openAppDetails('${app.app_id}')">Details</button>
              ${canRedeploy ? `<button class="btn btn-sm btn-primary" onclick="redeployApp('${app.app_id}')">Redeploy</button>` : ''}
              ${(!canRedeploy && canDeploy) ? `<button class="btn btn-sm btn-primary" onclick="deployApp('${app.app_id}')">Deploy</button>` : ''}
              ${canStop ? `<button class="btn btn-sm" onclick="stopApp('${app.app_id}')">Stop</button>` : ''}
              ${canRestart ? `<button class="btn btn-sm" onclick="restartApp('${app.app_id}')">Restart</button>` : ''}
              <button class="btn btn-sm btn-danger" onclick="deleteApp('${app.app_id}')">Delete</button>
            </div>
          </td>
        </tr>
      `;
    }).join("");

    if (currentActiveAppId) {
      const currentApp = cachedApps.find(a => a.app_id === currentActiveAppId);
      if (currentApp) {
        renderAppDetailsTabs(currentApp);
      }
    }

    updateLastRefreshed();
  } catch (err) {
    console.error("Failed to load apps:", err);
    tbody.innerHTML = `<tr><td colspan="7" class="text-offline">Error loading applications: ${err.message}</td></tr>`;
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// -----------------------------------------------------------------------------
// Deploy from GitHub Flow
// -----------------------------------------------------------------------------

window.showGithubDeploy = function() {
  const ghContainer = document.getElementById("github-deploy-container");
  const manualContainer = document.getElementById("manual-deploy-container");
  if (manualContainer) manualContainer.style.display = "none";
  if (ghContainer) {
    ghContainer.style.display = "block";
    ghContainer.scrollIntoView({ behavior: "smooth" });
  }
};

window.hideGithubDeploy = function() {
  const ghContainer = document.getElementById("github-deploy-container");
  if (ghContainer) ghContainer.style.display = "none";
};

window.toggleManualDeploy = function() {
  const manualContainer = document.getElementById("manual-deploy-container");
  const ghContainer = document.getElementById("github-deploy-container");
  if (ghContainer) ghContainer.style.display = "none";
  if (manualContainer) {
    manualContainer.style.display = manualContainer.style.display === "none" ? "block" : "none";
    if (manualContainer.style.display === "block") {
      manualContainer.scrollIntoView({ behavior: "smooth" });
    }
  }
};

document.getElementById("btn-show-github-deploy")?.addEventListener("click", showGithubDeploy);
document.getElementById("btn-toggle-manual-deploy")?.addEventListener("click", toggleManualDeploy);

// Inspect GitHub Repo & Detect Blueprint / Dockerfile
document.getElementById("gh-inspect-btn")?.addEventListener("click", async () => {
  const repoInput = document.getElementById("gh-repo");
  const branchInput = document.getElementById("gh-branch");
  const rootDirInput = document.getElementById("gh-root-dir");
  const banner = document.getElementById("gh-detection-banner");
  const appNameInput = document.getElementById("gh-app-name");
  const routeInput = document.getElementById("gh-route");
  const dockerfileInput = document.getElementById("gh-dockerfile");

  const repo = repoInput.value.trim();
  const branch = branchInput.value.trim() || "main";
  const rootDir = rootDirInput.value.trim() || ".";

  if (!repo || !repo.includes("/")) {
    showCustomAlert({ title: "Invalid Repository", message: "Please specify repository in 'owner/repository' format.", isError: true });
    return;
  }

  banner.style.display = "block";
  banner.style.backgroundColor = "rgba(88, 166, 255, 0.1)";
  banner.style.border = "1px solid rgba(88, 166, 255, 0.3)";
  banner.style.color = "var(--text-main)";
  banner.textContent = `Inspecting repository ${repo} on branch ${branch}...`;

  try {
    const res = await apiFetch("/api/apps/github/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository: repo, branch, root_directory: rootDir })
    });

    detectedBlueprint = res.blueprint;
    const repoName = repo.split("/")[1].toLowerCase().replace(/[^a-z0-9_-]/g, '-');

    if (res.has_blueprint && res.blueprint) {
      const svc = res.blueprint.services[0] || {};
      appNameInput.value = svc.name || repoName;
      routeInput.value = svc.route || `/${svc.name || repoName}`;
      rootDirInput.value = svc.rootDir || rootDir;
      dockerfileInput.value = svc.dockerfile || "Dockerfile";

      banner.style.backgroundColor = "rgba(63, 185, 80, 0.12)";
      banner.style.border = "1px solid rgba(63, 185, 80, 0.35)";
      banner.style.color = "var(--status-green)";
      banner.textContent = `✓ Detected PersonalServer Blueprint (personalserver.yaml) for '${svc.name}'. Pre-configured services and environment.`;

      // Pre-fill environment variables from blueprint
      const envList = document.getElementById("gh-env-list");
      envList.innerHTML = "";
      if (Array.isArray(svc.envVars)) {
        svc.envVars.forEach(ev => {
          const key = typeof ev === 'object' ? ev.key : ev;
          addGithubEnvRow(key, "", true);
        });
      }
    } else {
      appNameInput.value = repoName;
      routeInput.value = `/${repoName}`;
      dockerfileInput.value = "Dockerfile";

      banner.style.backgroundColor = "rgba(210, 153, 34, 0.12)";
      banner.style.border = "1px solid rgba(210, 153, 34, 0.35)";
      banner.style.color = "var(--status-yellow)";
      banner.textContent = `✓ Detected Docker configuration (Dockerfile). Automatic Docker build will be configured.`;
    }
  } catch (err) {
    banner.style.backgroundColor = "rgba(248, 81, 73, 0.12)";
    banner.style.border = "1px solid rgba(248, 81, 73, 0.35)";
    banner.style.color = "var(--status-red)";
    banner.textContent = `Inspection notice: ${err.message}`;
  }
});

function addGithubEnvRow(key = "", value = "", isSecret = true) {
  const container = document.getElementById("gh-env-list");
  if (!container) return;

  const rowId = `gh-env-row-${Date.now()}-${Math.random().toString(36).substr(2, 4)}`;
  const div = document.createElement("div");
  div.id = rowId;
  div.style.display = "flex";
  div.style.gap = "8px";
  div.style.alignItems = "center";

  div.innerHTML = `
    <input type="text" class="form-input mono gh-env-k" placeholder="KEY_NAME" value="${escapeHtml(key)}" style="flex: 1; min-width: 140px;" required pattern="^[a-zA-Z_][a-zA-Z0-9_]*$">
    <input type="password" class="form-input mono gh-env-v" placeholder="Value..." value="${escapeHtml(value)}" style="flex: 2; min-width: 180px;">
    <label class="modal-label mono" style="display: inline-flex; align-items: center; gap: 4px; cursor: pointer; white-space: nowrap;">
      <input type="checkbox" class="gh-env-sec" ${isSecret ? 'checked' : ''}> Secret
    </label>
    <button type="button" class="btn btn-sm btn-danger" onclick="document.getElementById('${rowId}')?.remove()">✕</button>
  `;
  container.appendChild(div);
}

document.getElementById("gh-add-env-btn")?.addEventListener("click", () => addGithubEnvRow());

// GitHub Source Deploy Form Submit
document.getElementById("app-github-deploy-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const repo = document.getElementById("gh-repo").value.trim();
  const branch = document.getElementById("gh-branch").value.trim() || "main";
  const name = document.getElementById("gh-app-name").value.trim();
  const rootDir = document.getElementById("gh-root-dir").value.trim() || ".";
  const dockerfile = document.getElementById("gh-dockerfile").value.trim() || "Dockerfile";
  const routePath = document.getElementById("gh-route").value.trim();

  // Collect environment variables
  const envVars = {};
  const rows = document.querySelectorAll("#gh-env-list > div");
  rows.forEach(r => {
    const k = r.querySelector(".gh-env-k")?.value.trim();
    const v = r.querySelector(".gh-env-v")?.value || "";
    const isSec = r.querySelector(".gh-env-sec")?.checked ?? true;
    if (k) {
      envVars[k] = { value: v, is_secret: isSec };
    }
  });

  const payload = {
    name,
    source: {
      type: "github",
      repository: repo,
      branch,
      root_directory: rootDir
    },
    route: {
      enabled: true,
      type: "path",
      path: routePath,
      strip_prefix: true,
      public_access: true
    },
    env_vars: envVars,
    blueprint: detectedBlueprint
  };

  try {
    const createRes = await apiFetch("/api/apps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    hideGithubDeploy();
    loadApps();

    // Trigger deployment immediately
    if (createRes.app_id) {
      deployApp(createRes.app_id);
    }
  } catch (err) {
    showCustomAlert({ title: "Deployment Error", message: err.message, isError: true });
  }
});

// Create Application (Manual)
document.getElementById("app-create-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const nameInput = document.getElementById("app-name");
  const imageInput = document.getElementById("app-image");
  const portInput = document.getElementById("app-container-port");
  const routeInput = document.getElementById("app-route-input");
  const targetSelect = document.getElementById("app-target");

  const name = nameInput.value.trim();
  const image = imageInput.value.trim();
  const containerPort = parseInt(portInput.value, 10);
  const target = targetSelect.value || "auto";
  const routePath = routeInput?.value.trim() || `/${name.toLowerCase()}`;

  try {
    await apiFetch("/api/apps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        image,
        container_port: containerPort,
        target,
        source: { type: "manual" },
        route: {
          enabled: true,
          type: "path",
          path: routePath,
          strip_prefix: true,
          public_access: true
        }
      })
    });
    nameInput.value = "";
    imageInput.value = "";
    portInput.value = "8000";
    if (routeInput) routeInput.value = "";
    toggleManualDeploy();
    loadApps();
  } catch (err) {
    showCustomAlert({ title: "Create Application Failed", message: err.message, isError: true });
  }
});

// -----------------------------------------------------------------------------
// Multi-Tab Application Details Management Drawer
// -----------------------------------------------------------------------------

window.openAppDetails = function(appId, tab = "overview") {
  currentActiveAppId = appId;
  currentActiveAppTab = tab;

  const panel = document.getElementById("app-details-panel");
  const app = cachedApps.find(a => a.app_id === appId);
  if (!app) return;

  const titleEl = document.getElementById("app-panel-title");
  const statusPill = document.getElementById("app-panel-status-pill");
  const redeployBtn = document.getElementById("app-panel-redeploy-btn");

  if (titleEl) titleEl.textContent = `Application: ${app.name} (${app.app_id})`;
  if (statusPill) statusPill.innerHTML = renderStatusPill(app.status);

  if (redeployBtn) {
    const isGithub = app.source && app.source.type === "github";
    redeployBtn.style.display = isGithub ? "inline-flex" : "none";
    redeployBtn.onclick = () => redeployApp(appId);
  }

  panel.style.display = "block";
  switchAppTab(tab);
  panel.scrollIntoView({ behavior: "smooth" });
};

window.closeAppDetails = function() {
  currentActiveAppId = null;
  const panel = document.getElementById("app-details-panel");
  if (panel) panel.style.display = "none";
};

window.switchAppTab = function(tabName) {
  currentActiveAppTab = tabName;

  document.querySelectorAll(".app-tabs-header .tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-tab") === tabName);
  });

  document.querySelectorAll("#app-details-panel .tab-pane").forEach(pane => {
    pane.classList.toggle("active", pane.id === `app-tab-${tabName}`);
  });

  if (!currentActiveAppId) return;
  const app = cachedApps.find(a => a.app_id === currentActiveAppId);
  if (!app) return;

  if (tabName === "overview") renderAppOverview(app);
  else if (tabName === "deployments") renderAppDeployments(app);
  else if (tabName === "logs") viewAppLogs(app.app_id);
  else if (tabName === "files") viewAppFiles(app.app_id, "");
  else if (tabName === "env") renderAppEnv(app);
};

function renderAppDetailsTabs(app) {
  if (currentActiveAppTab === "overview") renderAppOverview(app);
  else if (currentActiveAppTab === "deployments") renderAppDeployments(app);
  else if (currentActiveAppTab === "env") renderAppEnv(app);
}

function renderAppOverview(app) {
  const container = document.getElementById("app-overview-kv");
  if (!container) return;

  const isGithub = app.source && app.source.type === "github";
  const sourceStr = isGithub 
    ? `GitHub: ${escapeHtml(app.source.repository)} (branch: ${escapeHtml(app.source.branch || 'main')}, dir: ${escapeHtml(app.source.root_directory || '.')})`
    : `Manual Docker Image (${escapeHtml(app.image)})`;

  const routePath = app.route?.path || "-";
  const publicUrl = app.public_url || (routePath !== "-" ? `${getPlatformBaseUrl()}${routePath}/` : "-");

  container.innerHTML = `
    <div class="sys-kv-row"><span class="sys-kv-k">Application ID</span><span class="sys-kv-v">${escapeHtml(app.app_id)}</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Name</span><span class="sys-kv-v">${escapeHtml(app.name)}</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Source</span><span class="sys-kv-v">${sourceStr}</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Status</span><span class="sys-kv-v">${renderStatusPill(app.status)}</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Assigned Node</span><span class="sys-kv-v">${escapeHtml(app.selected_node || 'Pending scheduler')}</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Host / Container Port</span><span class="sys-kv-v">${app.host_port || app.port || '-'}:${app.container_port || 8000}</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Public Route</span><span class="sys-kv-v">${routePath !== '-' ? `<a href="${publicUrl}" target="_blank" class="text-online">${publicUrl}</a>` : 'Disabled'}</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Container ID</span><span class="sys-kv-v">${escapeHtml(app.container_id || `ps-${app.name}`)}</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Resource Limits</span><span class="sys-kv-v">${escapeHtml(app.cpu_limit || '0.5')} CPU · ${escapeHtml(app.memory_limit || '256m')} RAM</span></div>
    <div class="sys-kv-row"><span class="sys-kv-k">Created At</span><span class="sys-kv-v cell-muted">${escapeHtml(app.created_at || '-')}</span></div>
  `;
}

function renderAppDeployments(app) {
  const tbody = document.getElementById("app-deployments-tbody");
  if (!tbody) return;

  const deps = app.deployments || [];
  if (deps.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="cell-muted" style="text-align: center; padding: 20px;">No deployment history recorded for this application.</td></tr>`;
    return;
  }

  tbody.innerHTML = deps.slice().reverse().map(d => {
    const statusPill = renderStatusPill(d.status);
    const durStr = d.duration_ms ? `${(d.duration_ms / 1000).toFixed(1)}s` : "-";
    return `
      <tr>
        <td class="mono" style="font-weight: 600;">#${d.number || 1}</td>
        <td class="mono cell-muted">${escapeHtml(d.commit || 'latest')}</td>
        <td class="mono">${escapeHtml(d.branch || 'main')}</td>
        <td class="mono cell-muted">${escapeHtml(d.trigger || 'manual')}</td>
        <td>${statusPill}</td>
        <td class="mono">${durStr}</td>
        <td class="cell-muted">${escapeHtml(d.started_at || '-')}</td>
      </tr>
    `;
  }).join("");
}

function renderAppEnv(app) {
  const tbody = document.getElementById("app-env-tbody");
  if (!tbody) return;

  const envVars = app.env_vars || {};
  const keys = Object.keys(envVars);

  if (keys.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" class="cell-muted" style="text-align: center; padding: 20px;">No environment variables configured.</td></tr>`;
    return;
  }

  tbody.innerHTML = keys.map(k => {
    const item = envVars[k];
    const isSecret = typeof item === 'object' ? Boolean(item.is_secret) : false;
    const valDisplay = typeof item === 'object' ? item.value : String(item);
    const escapedKey = k.replace(/'/g, "\\'");

    return `
      <tr id="env-row-${k}">
        <td class="mono" style="font-weight: 600;">${escapeHtml(k)}</td>
        <td class="mono">
          <span id="env-val-${k}" class="cell-muted">${escapeHtml(valDisplay)}</span>
          ${isSecret ? '<span class="source-badge source-badge-manual" style="margin-left: 6px; font-size: 9px;">SECRET</span>' : ''}
        </td>
        <td style="text-align: right;">
          <div style="display: inline-flex; gap: 4px;">
            ${isSecret ? `<button class="btn btn-sm" onclick="revealAppEnv('${app.app_id}', '${escapedKey}')" title="Reveal secret value">Reveal</button>` : ''}
            <button class="btn btn-sm btn-danger" onclick="deleteAppEnv('${app.app_id}', '${escapedKey}')" title="Delete variable">Delete</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

// Add Environment Variable
document.getElementById("app-env-add-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!currentActiveAppId) return;

  const keyInput = document.getElementById("env-new-key");
  const valInput = document.getElementById("env-new-value");
  const secInput = document.getElementById("env-new-is-secret");

  const key = keyInput.value.trim();
  const value = valInput.value;
  const is_secret = secInput.checked;

  try {
    await apiFetch(`/api/apps/${currentActiveAppId}/env`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value, is_secret })
    });
    keyInput.value = "";
    valInput.value = "";
    secInput.checked = true;
    loadApps();
  } catch (err) {
    showCustomAlert({ title: "Failed to Save Variable", message: err.message, isError: true });
  }
});

window.revealAppEnv = async function(appId, key) {
  try {
    const res = await apiFetch(`/api/apps/${appId}/env/reveal`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key })
    });
    const valEl = document.getElementById(`env-val-${key}`);
    if (valEl) {
      valEl.textContent = res.value;
      valEl.className = "mono text-online";
    }
  } catch (err) {
    showCustomAlert({ title: "Reveal Error", message: err.message, isError: true });
  }
};

window.deleteAppEnv = async function(appId, key) {
  const confirmed = await showCustomConfirm({
    title: "Delete Variable",
    message: `Remove environment variable "${key}" from this application?`,
    confirmText: "Delete",
    isDanger: true
  });
  if (!confirmed) return;

  try {
    await apiFetch(`/api/apps/${appId}/env/${encodeURIComponent(key)}`, { method: "DELETE" });
    loadApps();
  } catch (err) {
    showCustomAlert({ title: "Delete Error", message: err.message, isError: true });
  }
};

// -----------------------------------------------------------------------------
// Lifecycle Deployment Actions
// -----------------------------------------------------------------------------

window.deployApp = async function(appId) {
  try {
    const res = await apiFetch(`/api/apps/${appId}/deploy`, { method: "POST" });
    if (res.app && res.app.status === "FAILED") {
      showCustomAlert({ title: "Deployment Rejected", message: res.app.failure_reason, isError: true });
    }
    loadApps();
  } catch (err) {
    showCustomAlert({ title: "Deploy Error", message: err.message, isError: true });
    loadApps();
  }
};

window.redeployApp = async function(appId) {
  try {
    const res = await apiFetch(`/api/apps/${appId}/redeploy`, { method: "POST" });
    if (res.app && res.app.status === "FAILED") {
      showCustomAlert({ title: "Redeploy Rejected", message: res.app.failure_reason, isError: true });
    }
    loadApps();
  } catch (err) {
    showCustomAlert({ title: "Redeploy Error", message: err.message, isError: true });
    loadApps();
  }
};

window.stopApp = async function(appId) {
  try {
    await apiFetch(`/api/apps/${appId}/stop`, { method: "POST" });
    loadApps();
  } catch (err) {
    showCustomAlert({ title: "Stop Error", message: err.message, isError: true });
    loadApps();
  }
};

window.restartApp = async function(appId) {
  try {
    await apiFetch(`/api/apps/${appId}/restart`, { method: "POST" });
    loadApps();
  } catch (err) {
    showCustomAlert({ title: "Restart Error", message: err.message, isError: true });
    loadApps();
  }
};

window.deleteApp = async function(appId) {
  const app = cachedApps.find(a => a.app_id === appId);
  const name = app ? app.name : appId;
  const confirmed = await showCustomConfirm({
    title: "Delete Application",
    message: `Permanently delete application "${name}" (${appId})?\n\nThis will remove the container and release host port ${app?.port || app?.host_port || ''}.`,
    confirmText: "Delete",
    isDanger: true
  });
  if (!confirmed) return;

  try {
    await apiFetch(`/api/apps/${appId}`, { method: "DELETE" });
    if (currentActiveAppId === appId) closeAppDetails();
    if (currentLogAppId === appId) closeAppLogs();
    if (currentAppFilesId === appId) closeAppFiles();
    loadApps();
  } catch (err) {
    showCustomAlert({ title: "Delete Error", message: err.message, isError: true });
    loadApps();
  }
};

window.viewAppLogs = async function(appId) {
  currentLogAppId = appId;
  const stdout = document.getElementById("app-logs-stdout");
  if (stdout) stdout.textContent = "Loading logs...";

  try {
    const res = await apiFetch(`/api/apps/${appId}/logs`);
    if (stdout) stdout.textContent = res.logs || "(No log output recorded)";
  } catch (err) {
    if (stdout) stdout.textContent = `Error fetching logs: ${err.message}`;
  }
};

document.getElementById("app-logs-refresh-btn")?.addEventListener("click", () => {
  if (currentActiveAppId) viewAppLogs(currentActiveAppId);
  else if (currentLogAppId) viewAppLogs(currentLogAppId);
});

window.closeAppLogs = function() {
  currentLogAppId = null;
};

// ==========================================
// Application Files Explorer View
// ==========================================

window.viewAppFiles = async function(appId, path = "") {
  currentAppFilesId = appId;
  currentAppFilesPath = path;

  const tbody = document.getElementById("app-files-tbody");
  const rootCrumb = document.getElementById("app-files-root-crumb");
  const upBtn = document.getElementById("app-files-up-btn");

  const app = cachedApps.find(a => a.app_id === appId);
  const appName = app ? app.name : appId;

  if (rootCrumb) {
    rootCrumb.textContent = appName;
    rootCrumb.onclick = () => viewAppFiles(appId, "");
  }

  if (upBtn) {
    upBtn.disabled = !path;
    upBtn.onclick = () => {
      if (!currentAppFilesPath) return;
      const parts = currentAppFilesPath.split("/").filter(Boolean);
      parts.pop();
      viewAppFiles(currentAppFilesId, parts.join("/"));
    };
  }

  renderAppFilesBreadcrumbs(appName, path);

  if (tbody) tbody.innerHTML = `<tr><td colspan="4" class="cell-muted" style="text-align: center; padding: 24px;">Loading project files...</td></tr>`;

  try {
    const res = await apiFetch(`/api/apps/${appId}/files?path=${encodeURIComponent(path)}`);
    currentAppFilesItems = res.items || [];
    renderAppFilesTable();

    const leftStats = document.getElementById("app-files-stats-left");
    const filesCount = currentAppFilesItems.filter(i => !i.is_dir).length;
    const foldersCount = currentAppFilesItems.filter(i => i.is_dir).length;
    if (leftStats) {
      leftStats.textContent = `${filesCount} file${filesCount === 1 ? '' : 's'} · ${foldersCount} folder${foldersCount === 1 ? '' : 's'}`;
    }
  } catch (err) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="4" class="text-offline" style="text-align: center; padding: 20px;">Unable to load files: ${err.message}</td></tr>`;
  }
};

function renderAppFilesBreadcrumbs(appName, path) {
  const container = document.getElementById("app-files-breadcrumbs");
  if (!container) return;

  const parts = path ? path.split("/").filter(Boolean) : [];
  let html = `<span class="crumb ${parts.length === 0 ? 'current' : ''}" onclick="viewAppFiles('${currentAppFilesId}', '')">${escapeHtml(appName)}</span>`;

  let accumulated = "";
  parts.forEach((p, index) => {
    accumulated += (accumulated ? "/" : "") + p;
    const isLast = index === parts.length - 1;
    const clickPath = accumulated;
    html += ` <span class="cell-muted">&gt;</span> <span class="crumb ${isLast ? 'current' : ''}" ${!isLast ? `onclick="viewAppFiles('${currentAppFilesId}', '${clickPath}')"` : ''}>${escapeHtml(p)}</span>`;
  });

  container.innerHTML = html;
}

function renderAppFilesTable() {
  const tbody = document.getElementById("app-files-tbody");
  if (!tbody) return;

  if (currentAppFilesItems.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="4" class="cell-muted" style="text-align: center; padding: 28px;">
          This application directory is empty.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = currentAppFilesItems.map(item => {
    const itemRelPath = currentAppFilesPath ? `${currentAppFilesPath}/${item.name}` : item.name;
    const isDir = item.is_dir;
    const typeCategory = getFileTypeCategory(item.name, isDir);
    const icon = getFileIcon(item.name, isDir);
    const sizeStr = isDir ? "—" : formatBytes(item.size_bytes);
    const downloadUrl = `/api/apps/${currentAppFilesId}/files/download?path=${encodeURIComponent(itemRelPath)}`;
    const escapedPath = itemRelPath.replace(/'/g, "\\'");

    return `
      <tr class="explorer-row" ondblclick="${isDir ? `viewAppFiles('${currentAppFilesId}', '${escapedPath}')` : ''}">
        <td>
          <div class="file-name-cell">
            <span class="file-icon">${icon}</span>
            ${isDir 
              ? `<a href="javascript:void(0)" class="mono" style="font-weight: 500;" onclick="viewAppFiles('${currentAppFilesId}', '${escapedPath}')">${escapeHtml(item.name)}</a>`
              : `<span class="mono">${escapeHtml(item.name)}</span>`
            }
          </div>
        </td>
        <td class="cell-muted">${typeCategory}</td>
        <td class="mono cell-muted">${sizeStr}</td>
        <td style="text-align: right;">
          <div class="explorer-row-actions">
            ${!isDir ? `<a href="${downloadUrl}" class="btn btn-sm" download title="Download file">Download</a>` : ''}
            <button class="btn btn-sm btn-danger" onclick="deleteAppFile('${escapedPath}', ${isDir})" title="Delete">Delete</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

window.closeAppFiles = function() {
  currentAppFilesId = null;
};

window.handleAppCreateFolder = async function() {
  if (!currentAppFilesId) return;
  const name = await showCustomPrompt({
    title: "New folder",
    label: "Folder name",
    placeholder: "e.g. src",
    confirmText: "Create",
    validate: (val) => {
      if (!val) return "Folder name cannot be empty.";
      if (/[\\/:\*\?"<>\|\x00]/.test(val) || val.includes("..")) return "Invalid directory name characters.";
      return "";
    }
  });
  if (!name) return;

  try {
    await apiFetch(`/api/apps/${currentAppFilesId}/files/mkdir`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path: currentAppFilesPath })
    });
    viewAppFiles(currentAppFilesId, currentAppFilesPath);
  } catch (err) {
    showCustomAlert({ title: "Folder Creation Failed", message: err.message, isError: true });
  }
};

window.handleAppUpload = async function(fileList) {
  if (!fileList || fileList.length === 0 || !currentAppFilesId) return;
  const uploadUrl = `/api/apps/${currentAppFilesId}/files/upload?path=${encodeURIComponent(currentAppFilesPath)}`;

  for (let i = 0; i < fileList.length; i++) {
    const file = fileList[i];
    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch(uploadUrl, {
        method: "POST",
        body: formData
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || res.statusText);
      }
    } catch (err) {
      showCustomAlert({ title: "Upload Failed", message: `Upload failed for "${file.name}": ${err.message}`, isError: true });
      break;
    }
  }

  viewAppFiles(currentAppFilesId, currentAppFilesPath);
};

window.deleteAppFile = async function(itemRelPath, isDir) {
  if (!currentAppFilesId) return;
  const itemName = itemRelPath.split("/").pop();
  const confirmed = await showCustomConfirm({
    title: `Delete ${isDir ? 'Folder' : 'File'}`,
    message: `Delete "${itemName}" from application storage?\n\nThis action cannot be undone.`,
    confirmText: "Delete",
    isDanger: true
  });
  if (!confirmed) return;

  try {
    await apiFetch(`/api/apps/${currentAppFilesId}/files?path=${encodeURIComponent(itemRelPath)}`, {
      method: "DELETE"
    });
    viewAppFiles(currentAppFilesId, currentAppFilesPath);
  } catch (err) {
    showCustomAlert({ title: "Delete Failed", message: err.message, isError: true });
  }
};

document.getElementById("app-files-refresh-btn")?.addEventListener("click", () => {
  if (currentAppFilesId) viewAppFiles(currentAppFilesId, currentAppFilesPath);
});

document.getElementById("app-create-folder-btn")?.addEventListener("click", handleAppCreateFolder);

document.getElementById("app-upload-file-btn")?.addEventListener("click", () => {
  document.getElementById("app-file-upload-input")?.click();
});

document.getElementById("app-file-upload-input")?.addEventListener("change", (e) => {
  if (e.target.files && e.target.files.length > 0) {
    handleAppUpload(e.target.files);
    e.target.value = "";
  }
});

// ==========================================
// Custom PersonalServer Modal System
// ==========================================
let activeModalResolve = null;
let activeModalValidate = null;

function closeModal(result = null) {
  const backdrop = document.getElementById("ps-modal-backdrop");
  if (backdrop) backdrop.style.display = "none";
  if (activeModalResolve) {
    const res = activeModalResolve;
    activeModalResolve = null;
    activeModalValidate = null;
    res(result);
  }
}

function showCustomPrompt({ title = "Input", label = "Name", initialValue = "", placeholder = "", confirmText = "Confirm", validate = null }) {
  return new Promise((resolve) => {
    activeModalResolve = resolve;
    activeModalValidate = validate;

    const backdrop = document.getElementById("ps-modal-backdrop");
    const titleEl = document.getElementById("ps-modal-title");
    const msgEl = document.getElementById("ps-modal-message");
    const inputWrap = document.getElementById("ps-modal-input-wrap");
    const labelEl = document.getElementById("ps-modal-label");
    const inputEl = document.getElementById("ps-modal-input");
    const errorEl = document.getElementById("ps-modal-error");
    const cancelBtn = document.getElementById("ps-modal-cancel-btn");
    const confirmBtn = document.getElementById("ps-modal-confirm-btn");

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.style.display = "none";
    if (inputWrap) inputWrap.style.display = "flex";
    if (labelEl) labelEl.textContent = label;
    if (inputEl) {
      inputEl.value = initialValue;
      inputEl.placeholder = placeholder;
    }
    if (errorEl) {
      errorEl.style.display = "none";
      errorEl.textContent = "";
    }

    if (confirmBtn) {
      confirmBtn.textContent = confirmText;
      confirmBtn.className = "btn btn-sm btn-primary";
    }
    if (cancelBtn) cancelBtn.style.display = "inline-flex";

    if (backdrop) backdrop.style.display = "flex";
    setTimeout(() => {
      inputEl?.focus();
      inputEl?.select();
    }, 50);
  });
}

function showCustomConfirm({ title = "Confirm", message = "", confirmText = "Confirm", isDanger = false }) {
  return new Promise((resolve) => {
    activeModalResolve = resolve;
    activeModalValidate = null;

    const backdrop = document.getElementById("ps-modal-backdrop");
    const titleEl = document.getElementById("ps-modal-title");
    const msgEl = document.getElementById("ps-modal-message");
    const inputWrap = document.getElementById("ps-modal-input-wrap");
    const errorEl = document.getElementById("ps-modal-error");
    const cancelBtn = document.getElementById("ps-modal-cancel-btn");
    const confirmBtn = document.getElementById("ps-modal-confirm-btn");

    if (titleEl) titleEl.textContent = title;
    if (msgEl) {
      msgEl.textContent = message;
      msgEl.style.display = "block";
    }
    if (inputWrap) inputWrap.style.display = "none";
    if (errorEl) {
      errorEl.style.display = "none";
      errorEl.textContent = "";
    }

    if (confirmBtn) {
      confirmBtn.textContent = confirmText;
      confirmBtn.className = isDanger ? "btn btn-sm btn-danger" : "btn btn-sm btn-primary";
    }
    if (cancelBtn) cancelBtn.style.display = "inline-flex";

    if (backdrop) backdrop.style.display = "flex";
    setTimeout(() => confirmBtn?.focus(), 50);
  });
}

function showCustomAlert({ title = "Notice", message = "", isError = false }) {
  return new Promise((resolve) => {
    activeModalResolve = resolve;
    activeModalValidate = null;

    const backdrop = document.getElementById("ps-modal-backdrop");
    const titleEl = document.getElementById("ps-modal-title");
    const msgEl = document.getElementById("ps-modal-message");
    const inputWrap = document.getElementById("ps-modal-input-wrap");
    const errorEl = document.getElementById("ps-modal-error");
    const cancelBtn = document.getElementById("ps-modal-cancel-btn");
    const confirmBtn = document.getElementById("ps-modal-confirm-btn");

    if (titleEl) titleEl.textContent = title;
    if (msgEl) {
      msgEl.textContent = message;
      msgEl.style.display = "block";
    }
    if (inputWrap) inputWrap.style.display = "none";
    if (errorEl) {
      errorEl.style.display = "none";
      errorEl.textContent = "";
    }

    if (confirmBtn) {
      confirmBtn.textContent = "OK";
      confirmBtn.className = isError ? "btn btn-sm btn-danger" : "btn btn-sm btn-primary";
    }
    if (cancelBtn) cancelBtn.style.display = "none";

    if (backdrop) backdrop.style.display = "flex";
    setTimeout(() => confirmBtn?.focus(), 50);
  });
}

// Modal event listeners setup
document.getElementById("ps-modal-cancel-btn")?.addEventListener("click", () => closeModal(null));
document.getElementById("ps-modal-close")?.addEventListener("click", () => closeModal(null));
document.getElementById("ps-modal-backdrop")?.addEventListener("click", (e) => {
  if (e.target.id === "ps-modal-backdrop") closeModal(null);
});

document.getElementById("ps-modal-confirm-btn")?.addEventListener("click", () => {
  const inputWrap = document.getElementById("ps-modal-input-wrap");
  const isPrompt = inputWrap && inputWrap.style.display !== "none";
  if (isPrompt) {
    const inputEl = document.getElementById("ps-modal-input");
    const val = inputEl ? inputEl.value.trim() : "";
    if (activeModalValidate) {
      const err = activeModalValidate(val);
      if (err) {
        const errorEl = document.getElementById("ps-modal-error");
        if (errorEl) {
          errorEl.textContent = err;
          errorEl.style.display = "block";
        }
        inputEl?.focus();
        return;
      }
    }
    closeModal(val);
  } else {
    closeModal(true);
  }
});

document.addEventListener("keydown", (e) => {
  const backdrop = document.getElementById("ps-modal-backdrop");
  if (!backdrop || backdrop.style.display === "none") return;

  if (e.key === "Escape") {
    e.preventDefault();
    closeModal(null);
  } else if (e.key === "Enter") {
    e.preventDefault();
    document.getElementById("ps-modal-confirm-btn")?.click();
  }
});

// Initialize
navigate();

