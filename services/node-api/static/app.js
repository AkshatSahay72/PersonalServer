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
  updateHAStatus();
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

// Mobile Navigation Drawer Toggle & Escape Handling
const mobileToggle = document.getElementById("mobile-nav-toggle");
const sidebar = document.getElementById("sidebar");
const backdrop = document.getElementById("sidebar-backdrop");
const sidebarCloseBtn = document.getElementById("sidebar-close-btn");

function toggleMobileNav(force) {
  if (!sidebar) return;
  const isOpen = force !== undefined ? force : !sidebar.classList.contains("open");
  sidebar.classList.toggle("open", isOpen);
  if (backdrop) backdrop.classList.toggle("active", isOpen);
  document.body.classList.toggle("drawer-open", isOpen);
  if (mobileToggle) {
    mobileToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
  }
}

mobileToggle?.addEventListener("click", () => toggleMobileNav());
sidebarCloseBtn?.addEventListener("click", () => toggleMobileNav(false));
backdrop?.addEventListener("click", () => toggleMobileNav(false));

// Close mobile sidebar on link click or Escape key
document.querySelectorAll(".sidebar .nav-link").forEach(link => {
  link.addEventListener("click", () => {
    if (window.innerWidth <= 768) toggleMobileNav(false);
  });
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && sidebar?.classList.contains("open")) {
    toggleMobileNav(false);
  }
});

window.addEventListener("resize", () => {
  if (window.innerWidth > 768 && sidebar?.classList.contains("open")) {
    toggleMobileNav(false);
  }
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

function formatDateIST(val, includeSeconds = true) {
  if (!val) return "-";
  try {
    const d = (typeof val === "number") ? new Date(val * 1000) : new Date(val);
    if (isNaN(d.getTime())) return String(val);

    const fmt = new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      ...(includeSeconds ? { second: "2-digit" } : {}),
      hour12: true
    });

    const parts = {};
    fmt.formatToParts(d).forEach(p => { parts[p.type] = p.value; });
    const day = parts.day || "";
    const month = parts.month || "";
    const year = parts.year || "";
    const hour = parts.hour || "";
    const minute = parts.minute || "";
    const second = parts.second ? `:${parts.second}` : "";
    const dayPeriod = (parts.dayPeriod || "").toUpperCase();

    return `${day} ${month} ${year}, ${hour}:${minute}${second} ${dayPeriod} IST`;
  } catch {
    return String(val);
  }
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
  if (s === "ONLINE" || s === "SUCCEEDED" || s === "RUNNING") {
    return `<span class="node-status-badge status-online"><span class="status-dot"></span>${s}</span>`;
  } else if (s === "DRAINING" || s === "DEBOARDING") {
    return `<span class="node-status-badge status-draining"><span class="status-dot"></span>${s}</span>`;
  } else if (s === "QUEUED" || s === "CLAIMED" || s === "RECOVERING" || s === "DEPLOYING" || s === "REMOVING") {
    return `<span class="node-status-badge status-draining"><span class="status-dot"></span>${s}</span>`;
  } else if (s === "STOPPED" || s === "CREATED" || s === "REMOVED") {
    return `<span class="cell-muted mono">${s}</span>`;
  } else if (s === "FAILED" || s === "TIMEOUT" || s === "REJECTED" || s === "OFFLINE") {
    return `<span class="node-status-badge status-offline"><span class="status-dot"></span>${s}</span>`;
  }
  return `<span class="cell-muted mono">${s || "UNKNOWN"}</span>`;
}

function updateLastRefreshed() {
  const el = document.getElementById("last-updated-text");
  if (el) el.textContent = formatDateIST(new Date());
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
    } else {
      const tbody = document.getElementById("dash-nodes-tbody");
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="6" class="cell-muted" style="color: var(--status-red);">Failed to load cluster nodes: ${clusterData.reason?.message || "Endpoint unavailable"}</td></tr>`;
      }
      const sumClusterMain = document.getElementById("sum-card-cluster-main");
      if (sumClusterMain) sumClusterMain.textContent = "Unavailable";
      const sumClusterSub = document.getElementById("sum-card-cluster-sub");
      if (sumClusterSub) sumClusterSub.textContent = "Cluster API offline";
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
    } else {
      const jobsTbody = document.getElementById("dash-jobs-tbody");
      if (jobsTbody) {
        jobsTbody.innerHTML = `<tr><td colspan="4" class="cell-muted" style="color: var(--status-red);">Failed to load recent jobs: ${jobsData.reason?.message || "Endpoint unavailable"}</td></tr>`;
      }
      const sumJobsMain = document.getElementById("sum-card-jobs-main");
      if (sumJobsMain) sumJobsMain.textContent = "Unavailable";
      const sumJobsSub = document.getElementById("sum-card-jobs-sub");
      if (sumJobsSub) sumJobsSub.textContent = "Jobs API offline";
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

    await updateHAStatus();
  } catch (err) {
    console.error("Dashboard render error:", err);
  }
}

// High Availability (HA) Status & Failover Controller
let cachedHAStatus = null;
let isUpdatingHA = false;
let lastHAUpdateTimestamp = 0;

function resolveLeaderInfo(ha) {
  if (!ha || typeof ha !== "object") {
    return {
      rawLeaderId: null,
      leaderName: "Unknown",
      isRegistered: false,
      isOnline: false,
      isVerified: false,
      displayText: "Leader status unknown",
      statusText: "Unknown",
      dotClass: "ha-dot-unknown",
      badgeClass: "ha-status-unknown"
    };
  }

  const rawLeaderId = ha.role === "leader" 
    ? (ha.node_id || ha.leader_id) 
    : (ha.leader_id || ha.node_id);

  if (!rawLeaderId || rawLeaderId === "None" || rawLeaderId === "unknown" || rawLeaderId === "-") {
    const isElecting = ha.role === "candidate" || ha.role === "promoting" || ha.lease_valid === false;
    return {
      rawLeaderId: null,
      leaderName: isElecting ? "Electing..." : (ha.role === "fenced" ? "Fenced" : "None"),
      isRegistered: false,
      isOnline: false,
      isVerified: false,
      displayText: isElecting ? "Electing..." : (ha.role === "fenced" ? "Fenced" : "Leader status unknown"),
      statusText: isElecting ? "Election" : (ha.role === "fenced" ? "Fenced" : (ha.role || "Unknown")),
      dotClass: isElecting ? "ha-dot-election" : (ha.role === "fenced" ? "ha-dot-fenced" : "ha-dot-unknown"),
      badgeClass: isElecting ? "ha-status-election" : (ha.role === "fenced" ? "ha-status-fenced" : "ha-status-unknown")
    };
  }

  // Look up in cluster node registry
  let regNode = null;
  if (Array.isArray(cachedNodes) && cachedNodes.length > 0) {
    regNode = cachedNodes.find(n => n.node_id === rawLeaderId || n.id === rawLeaderId || n.name === rawLeaderId || n.hostname === rawLeaderId);
  }

  const backendName = ha.leader_name;
  let leaderName = rawLeaderId;
  let isRegistered = false;
  let isOnline = false;

  if (regNode) {
    isRegistered = true;
    leaderName = regNode.name || regNode.hostname || regNode.node_id;
    isOnline = (regNode.status || "").toUpperCase() === "ONLINE";
  } else if (backendName && backendName !== rawLeaderId) {
    leaderName = backendName;
    isRegistered = ha.leader_registered !== false;
    isOnline = ha.leader_online === true;
  } else if (ha.leader_registered === true) {
    isRegistered = true;
    isOnline = ha.leader_online === true;
    if (ha.leader_name) leaderName = ha.leader_name;
  }

  // Determine state & classes
  let statusText = "Unknown";
  let dotClass = "ha-dot-unknown";
  let badgeClass = "ha-status-unknown";
  let displayText = leaderName;
  let isVerified = false;

  if (ha.role === "fenced") {
    statusText = "Fenced";
    dotClass = "ha-dot-fenced";
    badgeClass = "ha-status-fenced";
    displayText = "Fenced";
  } else if (ha.role === "candidate" || ha.role === "promoting" || ha.lease_valid === false) {
    statusText = "Election";
    dotClass = "ha-dot-election";
    badgeClass = "ha-status-election";
    displayText = "Electing...";
  } else if (ha.role === "disabled") {
    statusText = "Standalone";
    dotClass = "ha-dot-unknown";
    badgeClass = "ha-status-unknown";
    displayText = leaderName || "Single Node";
  } else if (!isRegistered) {
    // Unregistered node ID (e.g. "node-pc" or unknown ID in state)
    statusText = "Unverified";
    dotClass = "ha-dot-unknown";
    badgeClass = "ha-status-unknown";
    displayText = `Leader status unknown (${rawLeaderId})`;
  } else if (!isOnline) {
    // Registered node in state, but currently offline (e.g. Windows PC is offline)
    statusText = "Unverified";
    dotClass = "ha-dot-unknown";
    badgeClass = "ha-status-unknown";
    displayText = `${leaderName} (Offline · Unverified)`;
  } else if (ha.role === "leader") {
    statusText = "Active";
    dotClass = "ha-dot-active";
    badgeClass = "ha-status-active";
    displayText = leaderName;
    isVerified = true;
  } else if (ha.role === "standby") {
    statusText = "Standby";
    dotClass = "ha-dot-standby";
    badgeClass = "ha-status-standby";
    displayText = leaderName;
    isVerified = true;
  } else {
    statusText = ha.role || "Unknown";
    displayText = leaderName;
  }

  return {
    rawLeaderId,
    leaderName,
    isRegistered,
    isOnline,
    isVerified,
    displayText,
    statusText,
    dotClass,
    badgeClass
  };
}

function resolveNodeDisplayName(nodeId) {
  if (!nodeId || nodeId === "None" || nodeId === "unknown" || nodeId === "-") return "None";
  if (Array.isArray(cachedNodes) && cachedNodes.length > 0) {
    const match = cachedNodes.find(n => n.node_id === nodeId || n.id === nodeId || n.hostname === nodeId || n.name === nodeId);
    if (match) {
      return match.name || match.hostname || match.node_id;
    }
  }
  return nodeId;
}

function renderHAHeader(ha) {
  const dotEl = document.getElementById("ha-header-dot");
  const leaderEl = document.getElementById("ha-header-leader");
  const termEl = document.getElementById("ha-header-term");
  const statusEl = document.getElementById("ha-header-status");
  const btnEl = document.getElementById("topbar-ha-btn");

  const info = resolveLeaderInfo(ha);

  if (dotEl) dotEl.className = `ha-status-dot ${info.dotClass}`;
  if (leaderEl) leaderEl.textContent = info.displayText;
  if (termEl) termEl.textContent = `Term: ${ha.term !== undefined ? ha.term : '-'}`;
  if (statusEl) {
    statusEl.className = `ha-header-status-badge ${info.badgeClass}`;
    statusEl.textContent = info.statusText;
  }

  if (btnEl) {
    const standbysCount = ha.standbys_count !== undefined ? ha.standbys_count : (ha.standbys ? ha.standbys.length : 0);
    const leaseRemaining = ha.lease_remaining_sec !== undefined ? `${ha.lease_remaining_sec}s remaining` : (ha.lease_valid ? "Valid" : "Expired");
    const verifiedDesc = info.isVerified ? "Verified Active" : (info.isRegistered ? (info.isOnline ? "Active" : "Unverified (Offline Node)") : "Unregistered Controller ID");
    btnEl.title = `High Availability Cluster Status\nLeader: ${info.leaderName} (${info.rawLeaderId || 'N/A'})\nLeadership State: ${verifiedDesc}\nTerm: ${ha.term || 1} · State Version: ${ha.state_version || 0}\nStatus: ${info.statusText} · Lease: ${leaseRemaining}\nStandbys: ${standbysCount} synced\nClick to view full cluster HA details`;
    btnEl.setAttribute("aria-label", `Leader: ${info.leaderName}, Term: ${ha.term || 1}, Status: ${info.statusText}`);
  }

  // Backward-compatible fallback element
  const legacyEl = document.getElementById("topbar-ha-status");
  if (legacyEl) {
    legacyEl.className = `ha-badge ${info.badgeClass}`;
    legacyEl.textContent = `Leader: ${info.displayText} · ${info.statusText}`;
    legacyEl.title = `Term: ${ha.term} · State Version: ${ha.state_version}`;
  }

  // Update banner in Nodes view (authoritative match)
  const bannerRole = document.getElementById("ha-banner-role");
  if (bannerRole) {
    let roleText = "Controller";
    let roleClass = "ha-badge ha-badge-standby";
    if (ha.role === "leader") {
      roleText = info.isVerified ? "Leader" : "Unverified Leader";
      roleClass = info.isVerified ? "ha-badge ha-badge-leader" : "ha-badge ha-badge-standby";
    } else if (ha.role === "standby") {
      roleText = "Standby Controller";
      roleClass = "ha-badge ha-badge-standby";
    } else if (ha.role === "candidate" || ha.role === "promoting") {
      roleText = "Election";
      roleClass = "ha-badge ha-badge-standby";
    } else if (ha.role === "fenced") {
      roleText = "Fenced";
      roleClass = "ha-badge ha-badge-standby";
    }
    bannerRole.textContent = roleText;
    bannerRole.className = roleClass;
  }

  const bannerLeader = document.getElementById("ha-banner-leader-id");
  if (bannerLeader) {
    bannerLeader.textContent = info.displayText;
  }

  const bannerTerm = document.getElementById("ha-banner-term");
  if (bannerTerm) bannerTerm.textContent = ha.term !== undefined ? ha.term : "-";

  const bannerDesc = document.getElementById("ha-banner-status-desc");
  if (bannerDesc) {
    if (info.isVerified) {
      bannerDesc.textContent = "Auto-failover ready";
    } else if (ha.role === "fenced") {
      bannerDesc.textContent = "Controller fenced";
    } else if (ha.lease_valid === false) {
      bannerDesc.textContent = "Lease expired";
    } else if (!info.isRegistered) {
      bannerDesc.textContent = "Unregistered leader ID";
    } else if (!info.isOnline) {
      bannerDesc.textContent = "Leader node is offline";
    } else {
      bannerDesc.textContent = "Leadership unverified";
    }
  }

  const bannerStandbys = document.getElementById("ha-banner-standbys");
  if (bannerStandbys) {
    const cnt = ha.standbys_count !== undefined ? ha.standbys_count : (ha.standbys ? ha.standbys.length : 0);
    bannerStandbys.textContent = `${cnt} Standby${cnt === 1 ? '' : 's'} Synced`;
  }

  // Update Summary Card in Dashboard
  const sumHaMain = document.getElementById("sum-card-ha-main");
  if (sumHaMain) {
    if (ha.role === "leader" && ha.lease_valid !== false && info.isVerified) {
      sumHaMain.textContent = `Active Leader (${info.leaderName})`;
      sumHaMain.style.color = "#3fb950";
    } else if (ha.role === "standby" && info.isVerified) {
      sumHaMain.textContent = `Standby Controller (Leader: ${info.leaderName})`;
      sumHaMain.style.color = "#58a6ff";
    } else if (ha.role === "candidate" || ha.lease_valid === false) {
      sumHaMain.textContent = `Election in Progress`;
      sumHaMain.style.color = "#f0883e";
    } else if (ha.role === "fenced") {
      sumHaMain.textContent = `Fenced Controller`;
      sumHaMain.style.color = "#f85149";
    } else if (!info.isVerified) {
      sumHaMain.textContent = `Leader Unverified (${info.leaderName})`;
      sumHaMain.style.color = "#d29922";
    } else {
      sumHaMain.textContent = `HA: ${ha.role || "Standalone"}`;
      sumHaMain.style.color = "#8b949e";
    }
  }
  const sumHaSub = document.getElementById("sum-card-ha-sub");
  if (sumHaSub) {
    sumHaSub.textContent = `Term ${ha.term !== undefined ? ha.term : '-'} · Version ${ha.state_version !== undefined ? ha.state_version : '-'}`;
  }
}

function renderHAHeaderError(err) {
  const dotEl = document.getElementById("ha-header-dot");
  const leaderEl = document.getElementById("ha-header-leader");
  const termEl = document.getElementById("ha-header-term");
  const statusEl = document.getElementById("ha-header-status");
  const btnEl = document.getElementById("topbar-ha-btn");

  if (dotEl) dotEl.className = "ha-status-dot ha-dot-unknown";
  if (leaderEl) leaderEl.textContent = "Unknown";
  if (termEl) termEl.textContent = "Term: -";
  if (statusEl) {
    statusEl.className = "ha-header-status-badge ha-status-unknown";
    statusEl.textContent = "Unreachable";
  }

  const errMsg = err?.message || "Authoritative controller unreachable";
  if (btnEl) {
    btnEl.title = `High Availability Status: Unreachable (${errMsg})\nNever displaying stale leader information while API status is unknown.\nClick to view diagnostics.`;
    btnEl.setAttribute("aria-label", "High Availability Status: Unreachable");
  }

  const legacyEl = document.getElementById("topbar-ha-status");
  if (legacyEl) {
    legacyEl.className = "ha-badge ha-badge-standby";
    legacyEl.textContent = "HA: Unreachable";
  }

  // Update banner in Nodes view to explicit unreachable state (never showing stale node-pc)
  const bannerRole = document.getElementById("ha-banner-role");
  if (bannerRole) {
    bannerRole.textContent = "Unreachable";
    bannerRole.className = "ha-badge ha-badge-standby";
  }
  const bannerLeader = document.getElementById("ha-banner-leader-id");
  if (bannerLeader) {
    bannerLeader.textContent = "Leader status unknown";
  }
  const bannerTerm = document.getElementById("ha-banner-term");
  if (bannerTerm) {
    bannerTerm.textContent = "-";
  }
  const bannerDesc = document.getElementById("ha-banner-status-desc");
  if (bannerDesc) {
    bannerDesc.textContent = "Controller unreachable";
  }
  const bannerStandbys = document.getElementById("ha-banner-standbys");
  if (bannerStandbys) {
    bannerStandbys.textContent = "- Standbys";
  }

  const sumHaMain = document.getElementById("sum-card-ha-main");
  if (sumHaMain) {
    sumHaMain.textContent = "HA Unreachable";
    sumHaMain.style.color = "#8b949e";
  }
  const sumHaSub = document.getElementById("sum-card-ha-sub");
  if (sumHaSub) {
    sumHaSub.textContent = "Connection failed";
  }
}

function renderHADetailsModalContent(ha) {
  const backdrop = document.getElementById("ha-details-modal-backdrop");
  if (!backdrop || backdrop.style.display === "none") return;

  const info = resolveLeaderInfo(ha);

  const dotEl = document.getElementById("ha-modal-dot");
  const textEl = document.getElementById("ha-modal-status-text");
  const subEl = document.getElementById("ha-modal-status-sub");
  const badgeEl = document.getElementById("ha-modal-badge");

  let statusText = "Active Leader";
  let dotClass = info.dotClass;
  let badgeClass = info.badgeClass;
  let subText = `Authoritative Leader Controller via /api/ha/status`;

  if (ha.role === "leader") {
    if (ha.lease_valid === false) {
      statusText = "Election / Promotion";
      subText = "Leadership lease is expired or election is resolving";
    } else if (!info.isVerified) {
      statusText = "Unverified Leader";
      subText = info.isRegistered ? "Leader registered node is currently offline" : "Leader node ID is not registered in cluster";
    } else {
      statusText = "Active Leader";
      subText = "Holding cluster write lease with active heartbeats";
    }
  } else if (ha.role === "standby") {
    statusText = "Standby Controller";
    subText = `Replicating state from active leader (${ha.leader_url || info.leaderName})`;
  } else if (ha.role === "candidate" || ha.role === "promoting") {
    statusText = "Election in Progress";
    subText = "Candidate gathering quorum / peer agreement";
  } else if (ha.role === "fenced") {
    statusText = "Fenced Controller";
    subText = "Controller partitioned or lease lost; safe from split-brain";
  } else {
    statusText = ha.role || "Standalone";
    subText = "Cluster running in standalone or unmonitored mode";
  }

  if (dotEl) dotEl.className = `ha-status-dot ${dotClass}`;
  if (textEl) textEl.textContent = statusText;
  if (subEl) subEl.textContent = subText;
  if (badgeEl) {
    badgeEl.className = `ha-header-status-badge ${badgeClass}`;
    badgeEl.textContent = statusText.toUpperCase();
  }

  const leaderIdEl = document.getElementById("ha-modal-leader-id");
  if (leaderIdEl) leaderIdEl.textContent = info.rawLeaderId || "None";
  const leaderNameEl = document.getElementById("ha-modal-leader-name");
  if (leaderNameEl) {
    const verifiedDesc = info.isVerified ? " (Verified Online)" : (info.isRegistered ? " (Offline Node)" : " (Unregistered)");
    leaderNameEl.textContent = `Node: ${info.leaderName}${verifiedDesc}`;
  }

  const termEl = document.getElementById("ha-modal-term");
  if (termEl) termEl.textContent = `Term ${ha.term !== undefined ? ha.term : 1}`;
  const versionEl = document.getElementById("ha-modal-version");
  if (versionEl) versionEl.textContent = `State Version ${ha.state_version !== undefined ? ha.state_version : 0}`;

  const leaseEl = document.getElementById("ha-modal-lease");
  if (leaseEl) {
    leaseEl.textContent = ha.lease_valid ? "Valid (Active)" : "Expired / Inactive";
    leaseEl.style.color = ha.lease_valid ? "#3fb950" : "#d29922";
  }
  const leaseTimeEl = document.getElementById("ha-modal-lease-time");
  if (leaseTimeEl) {
    const rem = ha.lease_remaining_sec !== undefined ? `${ha.lease_remaining_sec}s remaining` : "-";
    leaseTimeEl.textContent = `Lease: ${rem} (Missed checks: ${ha.missed_checks || 0})`;
  }

  const standbysEl = document.getElementById("ha-modal-standbys");
  const count = ha.standbys_count !== undefined ? ha.standbys_count : (ha.standbys ? ha.standbys.length : 0);
  if (standbysEl) standbysEl.textContent = `${count} Synchronized Standby${count === 1 ? '' : 's'}`;

  const partEl = document.getElementById("ha-modal-partition-safe");
  if (partEl) {
    partEl.textContent = `Partition Safe: ${ha.partition_safe !== false ? "True" : "Partition Suspected"}`;
  }

  // Standbys list
  const listEl = document.getElementById("ha-modal-standbys-list");
  if (listEl) {
    const standbys = Array.isArray(ha.standbys) ? ha.standbys : [];
    if (standbys.length === 0) {
      listEl.innerHTML = `<span class="cell-muted">No synchronized standby controllers registered.</span>`;
    } else {
      listEl.innerHTML = standbys.map(s => {
        const sid = s.node_id || s.id || s.url || "standby";
        const sName = resolveNodeDisplayName(sid);
        const url = s.node_url || s.url || "-";
        const hb = s.last_heartbeat ? timeAgo(s.last_heartbeat) : "recent";
        return `<div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid rgba(255,255,255,0.05);">
          <span><strong>${sName}</strong> <span class="cell-muted">(${sid})</span></span>
          <span class="cell-muted">${url} · ${hb}</span>
        </div>`;
      }).join("");
    }
  }

  // Leader node button
  const viewLeaderBtn = document.getElementById("ha-modal-view-node-btn");
  if (viewLeaderBtn) {
    if (info.rawLeaderId && info.rawLeaderId !== "None" && info.isRegistered) {
      viewLeaderBtn.disabled = false;
      viewLeaderBtn.textContent = `View Leader Node (${info.leaderName})`;
    } else {
      viewLeaderBtn.disabled = true;
      viewLeaderBtn.textContent = info.isRegistered ? "View Leader Node" : "Leader Unregistered";
    }
  }
}

function renderHADetailsModalError(err) {
  const backdrop = document.getElementById("ha-details-modal-backdrop");
  if (!backdrop || backdrop.style.display === "none") return;

  const dotEl = document.getElementById("ha-modal-dot");
  const textEl = document.getElementById("ha-modal-status-text");
  const subEl = document.getElementById("ha-modal-status-sub");
  const badgeEl = document.getElementById("ha-modal-badge");

  if (dotEl) dotEl.className = "ha-status-dot ha-dot-unknown";
  if (textEl) textEl.textContent = "HA Status Unreachable";
  if (subEl) subEl.textContent = err?.message || "Failed to communicate with HA controller endpoint";
  if (badgeEl) {
    badgeEl.className = "ha-header-status-badge ha-status-unknown";
    badgeEl.textContent = "UNREACHABLE";
  }

  const viewLeaderBtn = document.getElementById("ha-modal-view-node-btn");
  if (viewLeaderBtn) {
    viewLeaderBtn.disabled = true;
  }
}

async function updateHAStatus(force = false) {
  if (isUpdatingHA && !force) return cachedHAStatus;
  const now = Date.now();
  if (!force && lastHAUpdateTimestamp && (now - lastHAUpdateTimestamp < 2500) && cachedHAStatus) {
    return cachedHAStatus;
  }

  isUpdatingHA = true;
  try {
    if (!cachedNodes || cachedNodes.length === 0) {
      try {
        const clusterRes = await apiFetch("/api/cluster");
        if (clusterRes && Array.isArray(clusterRes.nodes)) {
          cachedNodes = clusterRes.nodes.filter(n => n.status !== "REMOVED");
        }
      } catch (_) {}
    }

    const ha = await apiFetch("/api/ha/status");
    cachedHAStatus = ha;
    lastHAUpdateTimestamp = Date.now();
    renderHAHeader(ha);
    renderHADetailsModalContent(ha);
    return ha;
  } catch (err) {
    cachedHAStatus = null;
    renderHAHeaderError(err);
    renderHADetailsModalError(err);
    return null;
  } finally {
    isUpdatingHA = false;
  }
}

function openHADetailsModal() {
  const backdrop = document.getElementById("ha-details-modal-backdrop");
  if (backdrop) {
    backdrop.style.display = "flex";
  }
  if (cachedHAStatus) {
    renderHADetailsModalContent(cachedHAStatus);
  }
  updateHAStatus(true);
}

function closeHADetailsModal() {
  const backdrop = document.getElementById("ha-details-modal-backdrop");
  if (backdrop) {
    backdrop.style.display = "none";
  }
}

function viewLeaderNodeFromModal() {
  closeHADetailsModal();
  const rawLeaderId = cachedHAStatus?.role === "leader" 
    ? (cachedHAStatus?.node_id || cachedHAStatus?.leader_id) 
    : (cachedHAStatus?.leader_id || cachedHAStatus?.node_id);
  if (rawLeaderId) {
    window.location.hash = "#nodes";
    setTimeout(() => {
      if (typeof viewNodeById === "function") {
        viewNodeById(rawLeaderId);
      }
    }, 80);
  }
}

function triggerFailoverFromModal() {
  triggerManualHAFailover();
}

async function triggerManualHAFailover() {
  const confirmed = await showCustomConfirm({
    title: "High Availability Failover",
    message: "Trigger simulated failover to a standby controller in the cluster?\n\n• Leadership will transfer to an eligible synchronized standby\n• If running on the active leader, this controller will step down\n• Cluster roles, telemetry, and actions will refresh immediately"
  });
  if (!confirmed) return;

  const btn = document.getElementById("ha-failover-btn");
  const modalBtn = document.getElementById("ha-modal-failover-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Switching...";
  }
  if (modalBtn) {
    modalBtn.disabled = true;
    modalBtn.textContent = "Switching...";
  }

  try {
    const res = await apiFetch("/api/ha/failover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });
    if (res.status === "failed") {
      showToast(`Failover notice: ${res.error || 'No standby controller available'}`, "warning");
    } else {
      const targetTxt = res.target ? ` -> ${res.target}` : '';
      showToast(`Failover triggered: ${res.action || 'Transitioned'}${targetTxt} (Term ${res.term})`, "success");
    }
  } catch (e) {
    showToast("Failover request failed: " + e.message, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Simulate Failover";
    }
    if (modalBtn) {
      modalBtn.disabled = false;
      modalBtn.textContent = "Simulate Failover";
    }
    await updateHAStatus(true);
    await loadNodes();
  }
}

// 2. Nodes View
let cachedUtilMap = {};

async function loadNodes() {
  updateLastRefreshed();
  await updateHAStatus();
  const tbody = document.getElementById("nodes-tbody");
  const targetSelect = document.getElementById("job-target");

  try {
    const [clusterRes, utilRes] = await Promise.allSettled([
      apiFetch("/api/cluster"),
      apiFetch("/api/cluster/utilization")
    ]);
    if (clusterRes.status !== "fulfilled") {
      tbody.innerHTML = `<tr><td colspan="10" class="cell-muted" style="color: var(--status-red);">Failed to load cluster nodes: ${clusterRes.reason?.message || "Endpoint unavailable"}</td></tr>`;
      const countHdr = document.getElementById("nodes-count-header");
      if (countHdr) countHdr.textContent = "0 online · Unavailable";
      return;
    }
    const data = clusterRes.value || { nodes: [] };
    cachedUtilMap = (utilRes.status === "fulfilled" && utilRes.value?.nodes) ? utilRes.value.nodes : {};

    cachedNodes = (data.nodes || []).filter(n => n.status !== "REMOVED");

    const onlineCount = cachedNodes.filter(n => (n.status || "").toUpperCase() === "ONLINE").length;
    const countHdr = document.getElementById("nodes-count-header");
    if (countHdr) countHdr.textContent = `${onlineCount} online · ${cachedNodes.length} total`;
    if (cachedHAStatus) renderHAHeader(cachedHAStatus);

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
      const uNode = cachedUtilMap[n.node_id] || {};
      const hb = n.last_heartbeat || {};
      const sys = hb.system || {};

      // 1. Battery Telemetry (restrained, zero emoji)
      const bat = hb.battery || sys.battery || uNode.battery || {};
      let batHtml = '<span class="cell-muted" title="No battery telemetry reported">—</span>';
      if (bat.percentage != null) {
        const pct = bat.percentage;
        const isCharging = (bat.status || "").toUpperCase() === "CHARGING";
        const isAC = (bat.status || "").toUpperCase() === "AC_POWERED" || (bat.plugged || "").toUpperCase() === "PLUGGED_AC";
        let pillClass = pct > 50 ? "good" : (pct > 20 ? "warn" : "danger");
        if (isAC && !isCharging) pillClass = "info";
        const label = isAC ? "AC Power" : `${pct}%${isCharging ? ' (charging)' : ''}`;
        const title = `Battery: ${pct}% · Status: ${bat.status || 'OK'} · Source: ${bat.plugged || (isAC ? 'AC' : 'Battery')}${bat.temperature_c ? ' · Temp: ' + bat.temperature_c + '°C' : ''}`;
        batHtml = `<span class="telemetry-pill ${pillClass}" title="${title}">${label}</span>`;
      } else if (n.platform === "termux") {
        batHtml = `<span class="telemetry-pill info mono" title="Termux Android Node">Mobile</span>`;
      }

      // 2. Network & Speed Telemetry (restrained, zero emoji)
      const net = hb.network || sys.network || uNode.network || {};
      let netHtml = '<span class="cell-muted" title="No network telemetry reported">—</span>';
      if (net.wifi_signal_percent != null || net.link_speed_mbps != null || net.ping_ms != null) {
        const sig = net.wifi_signal_percent != null ? `${net.wifi_signal_percent}%` : "";
        const spd = net.link_speed_mbps != null ? `${Math.round(net.link_speed_mbps)}M` : (net.ping_ms != null ? `${net.ping_ms}ms` : "");
        const pillClass = (net.wifi_signal_percent == null || net.wifi_signal_percent > 60) ? "good" : (net.wifi_signal_percent > 30 ? "warn" : "danger");
        const title = `Type: ${net.connection_type || 'Wi-Fi'} · Signal: ${net.wifi_signal_percent != null ? net.wifi_signal_percent + '%' : 'N/A'} · Link: ${net.link_speed_mbps ? net.link_speed_mbps + ' Mbps' : 'N/A'} · Ping: ${net.ping_ms ? net.ping_ms + 'ms' : 'N/A'} · IP: ${net.ip || n.ip || 'N/A'}`;
        const label = `${net.connection_type || 'Net'}${spd ? ' · ' + spd : (sig ? ' · ' + sig : '')}`;
        netHtml = `<span class="telemetry-pill ${pillClass}" title="${title}">${label}</span>`;
      } else if (n.ip) {
        netHtml = `<span class="telemetry-pill info mono" title="IP: ${n.ip}">${n.ip.startsWith('100.') ? 'Tailscale' : n.ip}</span>`;
      }

      // 3. CPU & Thermal Telemetry (restrained, zero emoji)
      const thm = hb.thermals || sys.thermals || uNode.thermals || {};
      const cpuDtl = hb.cpu_details || sys.cpu_details || uNode.cpu_details || {};
      const tempC = thm.temperature_c ?? cpuDtl.temperature_c;
      const cpuPct = uNode.cpu?.cpu_percent ?? cpuDtl.utilization_percent;

      let coresStr = "-";
      if (uNode.cpu?.cores) {
        coresStr = `${uNode.cpu.cores}c`;
        if (cpuPct != null) coresStr += ` (${cpuPct}%)`;
        else if (uNode.cpu.load_average?.length) coresStr += ` (${uNode.cpu.load_average[0]})`;
      } else {
        coresStr = `${sys.cpu_cores || n.resources?.cpu_cores || "-"}c`;
      }

      let cpuTempHtml = '<span class="cell-muted">—</span>';
      if (tempC != null || cpuPct != null) {
        const tStr = tempC != null ? `${tempC}°C` : "";
        const pStr = cpuPct != null ? `${cpuPct}%` : "";
        const pillClass = (tempC == null || tempC < 65) ? "good" : (tempC < 82 ? "warn" : "danger");
        const title = `Thermal: ${thm.status || 'Normal'} (${tempC ? tempC + '°C' : 'N/A'}) · CPU: ${cpuPct != null ? cpuPct + '%' : 'N/A'} · Cores: ${cpuDtl.cores || n.resources?.cpu_cores || '-'}`;
        const label = `${coresStr}${tStr ? ' · ' + tStr : ''}`;
        cpuTempHtml = `<span class="telemetry-pill ${pillClass}" title="${title}">${label}</span>`;
      } else {
        cpuTempHtml = `<span class="mono">${coresStr}</span>`;
      }

      // 4. Memory & Storage (clean explanation if unavailable)
      let memStr = '<span class="cell-muted">Unavailable</span>';
      if (uNode.memory?.total_mb && uNode.memory.total_mb > 0) {
        memStr = `${(uNode.memory.used_mb/1024).toFixed(1)} / ${(uNode.memory.total_mb/1024).toFixed(1)}G (${uNode.memory.used_percent}%)`;
      } else if (n.resources?.ram_mb && n.resources.ram_mb > 0) {
        memStr = `${(n.resources.ram_mb/1024).toFixed(1)} GB`;
      }

      let storageStr = '<span class="cell-muted">Unavailable</span>';
      if (uNode.storage?.total_gb && uNode.storage.total_gb > 0) {
        storageStr = `${uNode.storage.used_gb} / ${uNode.storage.total_gb}G (${uNode.storage.used_percent}%)`;
      } else if (n.resources?.storage_gb && n.resources.storage_gb > 0) {
        storageStr = `${n.resources.storage_gb} GB`;
      }

      const activeJobs = uNode.workloads?.active_jobs ?? 0;

      const lastSeen = isOnline ? timeAgo(n.last_seen) : (n.last_seen ? timeAgo(n.last_seen) : "offline");
      const st = (n.status || "").toUpperCase();

      let actionBtns = `<button class="btn btn-sm" onclick="viewNodeById('${n.node_id}')">Details</button><button class="btn btn-sm btn-secondary" onclick="openNodeDiagnosticsModal('${n.node_id}')" title="Run Connectivity & Hardware Diagnostics">Test</button>`;
      if (st === "ONLINE") {
        actionBtns += `<button class="btn btn-sm btn-warning" onclick="drainNode('${n.node_id}')" title="Drain node workloads">Drain</button>`;
      } else if (st === "DRAINING") {
        actionBtns += `<button class="btn btn-sm btn-warning" onclick="deboardNode('${n.node_id}')" title="Deboard node safely">Deboard</button>`;
        actionBtns += `<button class="btn btn-sm" onclick="resumeNode('${n.node_id}')" title="Resume node to ONLINE">Resume</button>`;
      } else if (st === "DEBOARDING") {
        actionBtns += `<button class="btn btn-sm btn-danger" onclick="removeNodeSafe('${n.node_id}')" title="Remove node after safety checks">Remove</button>`;
        actionBtns += `<button class="btn btn-sm" onclick="resumeNode('${n.node_id}')" title="Resume node to ONLINE">Resume</button>`;
      }

      let haTag = "";
      const isLeaderIdentified = cachedHAStatus && cachedHAStatus.lease_valid !== false && (n.node_id === cachedHAStatus.leader_id || (n.node_id === cachedHAStatus.node_id && cachedHAStatus.role === "leader"));
      if (isLeaderIdentified) {
        if (isOnline) {
          haTag = `<span class="node-role-pill leader" title="Active Cluster Leader">Leader</span>`;
        } else {
          haTag = `<span class="node-role-pill standby" title="Leader Identity in State but Node is Offline">Leader (Offline)</span>`;
        }
      } else if (n.role === "controller" || (cachedHAStatus?.standbys || []).some(s => s.node_id === n.node_id)) {
        haTag = `<span class="node-role-pill standby" title="Standby Controller">Standby</span>`;
      } else {
        haTag = `<span class="node-role-pill worker" title="Compute Worker">Worker</span>`;
      }

      return `
        <tr>
          <td><strong class="cell-main">${n.name || n.node_id}</strong>${haTag}</td>
          <td>${renderStatusPill(n.status)}</td>
          <td>${batHtml}</td>
          <td>${netHtml}</td>
          <td>${cpuTempHtml}</td>
          <td class="mono">${memStr}</td>
          <td class="mono">${storageStr}</td>
          <td class="mono">${activeJobs} active</td>
          <td class="mono cell-muted">${lastSeen}</td>
          <td style="text-align: right;">
            <div class="node-actions-group">
              ${actionBtns}
            </div>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="10" class="text-offline">Unable to load nodes: ${err.message}</td></tr>`;
  }
}

let activeDetailNodeId = null;

window.viewNodeById = function(nodeId) {
  const node = cachedNodes.find(n => n.node_id === nodeId);
  if (!node) return;
  activeDetailNodeId = nodeId;

  const panel = document.getElementById("node-detail-panel");
  const title = document.getElementById("node-detail-title");
  const hero = document.getElementById("node-detail-hero");
  const grid = document.getElementById("node-detail-grid");
  const actionsEl = document.getElementById("node-lifecycle-actions");

  if (title) title.textContent = `Node: ${node.name || node.node_id}`;

  if (actionsEl) {
    const st = (node.status || "").toUpperCase();
    let btnsHtml = "";
    if (st === "ONLINE") {
      btnsHtml = `<button class="btn btn-sm btn-warning" onclick="drainNode('${node.node_id}')">Drain Node</button>`;
    } else if (st === "DRAINING") {
      btnsHtml = `<button class="btn btn-sm btn-warning" onclick="deboardNode('${node.node_id}')">Deboard Node</button> <button class="btn btn-sm" onclick="resumeNode('${node.node_id}')">Resume Node</button>`;
    } else if (st === "DEBOARDING") {
      btnsHtml = `<button class="btn btn-sm btn-danger" onclick="removeNodeSafe('${node.node_id}')">Remove Node</button> <button class="btn btn-sm" onclick="resumeNode('${node.node_id}')">Resume Node</button>`;
    }
    actionsEl.innerHTML = btnsHtml;
  }

  const hb = node.last_heartbeat || {};
  const sys = hb.system || {};
  const srv = hb.services || {};
  const caps = Object.keys(node.capabilities || {}).filter(k => node.capabilities[k]).join(", ") || "compute, storage, network";
  const uNode = cachedUtilMap[node.node_id] || {};

  // Extract Hardware Telemetry
  const bat = hb.battery || sys.battery || uNode.battery || {};
  const net = hb.network || sys.network || uNode.network || {};
  const thm = hb.thermals || sys.thermals || uNode.thermals || {};
  const cpuDtl = hb.cpu_details || sys.cpu_details || uNode.cpu_details || {};

  // Battery card values
  const batPct = bat.percentage != null ? bat.percentage : 100;
  const isCharging = (bat.status || "").toUpperCase() === "CHARGING";
  const isAC = (bat.status || "").toUpperCase() === "AC_POWERED" || (bat.plugged || "").toUpperCase() === "PLUGGED_AC";
  const batColor = batPct > 50 ? "var(--status-green)" : (batPct > 20 ? "var(--status-yellow)" : "var(--status-red)");

  // Network card values
  const sigPct = net.wifi_signal_percent != null ? net.wifi_signal_percent : 80;
  const netColor = sigPct > 60 ? "var(--status-green)" : (sigPct > 30 ? "var(--status-yellow)" : "var(--status-red)");

  // CPU card values
  const cpuUsagePct = uNode.cpu?.cpu_percent ?? cpuDtl.utilization_percent ?? 15;
  const tempC = thm.temperature_c ?? cpuDtl.temperature_c;
  const thmColor = (tempC == null || tempC < 65) ? "var(--status-green)" : (tempC < 82 ? "var(--status-yellow)" : "var(--status-red)");

  // Memory & Storage
  const memUsedMb = uNode.memory?.used_mb ?? 2048;
  const memTotMb = uNode.memory?.total_mb ?? (node.resources?.ram_mb || 4096);
  const memPct = memTotMb > 0 ? Math.round((memUsedMb / memTotMb) * 100) : 35;

  const stUsedGb = uNode.storage?.used_gb ?? 20;
  const stTotGb = uNode.storage?.total_gb ?? (node.resources?.storage_gb || 64);
  const stPct = stTotGb > 0 ? Math.round((stUsedGb / stTotGb) * 100) : 25;

  if (hero) {
    hero.innerHTML = `
      <div class="telemetry-cards-grid">
        <!-- 1. Battery Card -->
        <div class="telemetry-card">
          <div class="telemetry-card-header">
            <span>Power &amp; Battery</span>
            <span class="telemetry-pill ${batPct > 50 ? 'good' : (batPct > 20 ? 'warn' : 'danger')}">${isCharging ? 'Charging' : (isAC ? 'AC Power' : 'Battery')}</span>
          </div>
          <div class="telemetry-metric-large" style="color: ${batColor};">
            ${bat.percentage != null ? bat.percentage + '%' : 'AC Power'}
            <span class="cell-muted" style="font-size: 11px; font-weight: normal;">${bat.status || (isAC ? 'Plugged' : 'Normal')}</span>
          </div>
          <div class="telemetry-bar-wrap">
            <div class="telemetry-bar-fill" style="width: ${batPct}%; background-color: ${batColor};"></div>
          </div>
          <div class="telemetry-meta-list mono cell-muted">
            <div class="telemetry-meta-row"><span>Source</span><span class="cell-main">${bat.plugged || (isAC ? 'PLUGGED_AC' : 'BATTERY')}</span></div>
            <div class="telemetry-meta-row"><span>Health</span><span class="text-online">${bat.health || 'GOOD'}</span></div>
            <div class="telemetry-meta-row"><span>Temp</span><span>${bat.temperature_c ? bat.temperature_c + '°C' : 'Normal'}</span></div>
          </div>
        </div>

        <!-- 2. Network Card -->
        <div class="telemetry-card">
          <div class="telemetry-card-header">
            <span>Network &amp; Connectivity</span>
            <span class="telemetry-pill ${net.ping_ms ? 'good' : 'info'}">${net.connection_type || 'Wi-Fi'}</span>
          </div>
          <div class="telemetry-metric-large" style="color: ${netColor};">
            ${net.link_speed_mbps ? Math.round(net.link_speed_mbps) + ' Mbps' : (net.ping_ms ? net.ping_ms + ' ms' : 'Connected')}
            <span class="cell-muted" style="font-size: 11px; font-weight: normal;">${net.ping_ms ? net.ping_ms + 'ms ping' : 'LAN'}</span>
          </div>
          <div class="telemetry-bar-wrap">
            <div class="telemetry-bar-fill" style="width: ${sigPct}%; background-color: ${netColor};"></div>
          </div>
          <div class="telemetry-meta-list mono cell-muted">
            <div class="telemetry-meta-row"><span>SSID</span><span class="cell-main" title="${net.wifi_ssid || ''}">${net.wifi_ssid ? (net.wifi_ssid.length > 14 ? net.wifi_ssid.slice(0, 14) + '…' : net.wifi_ssid) : 'Mesh/LAN'}</span></div>
            <div class="telemetry-meta-row"><span>Signal</span><span>${net.wifi_signal_percent != null ? net.wifi_signal_percent + '%' : 'Good'} (${net.wifi_rssi_dbm ? net.wifi_rssi_dbm + 'dBm' : 'N/A'})</span></div>
            <div class="telemetry-meta-row"><span>Tailscale IP</span><span class="cell-main">${net.tailscale_ip || (node.ip?.startsWith('100.') ? node.ip : 'Active')}</span></div>
          </div>
        </div>

        <!-- 3. CPU & Thermal Card -->
        <div class="telemetry-card">
          <div class="telemetry-card-header">
            <span>Processor &amp; Thermals</span>
            <span class="telemetry-pill ${tempC && tempC > 80 ? 'danger' : 'good'}">${thm.status || 'Optimal'}</span>
          </div>
          <div class="telemetry-metric-large" style="color: ${thmColor};">
            ${tempC ? tempC + '°C' : cpuUsagePct + '%'}
            <span class="cell-muted" style="font-size: 11px; font-weight: normal;">${cpuUsagePct}% load</span>
          </div>
          <div class="telemetry-bar-wrap">
            <div class="telemetry-bar-fill" style="width: ${Math.min(100, Math.max(5, cpuUsagePct))}%; background-color: ${thmColor};"></div>
          </div>
          <div class="telemetry-meta-list mono cell-muted">
            <div class="telemetry-meta-row"><span>Cores</span><span class="cell-main">${cpuDtl.cores || sys.cpu_cores || node.resources?.cpu_cores || 8} cores</span></div>
            <div class="telemetry-meta-row"><span>Frequency</span><span>${cpuDtl.frequency_mhz ? Math.round(cpuDtl.frequency_mhz) + ' MHz' : 'Scaling'}</span></div>
            <div class="telemetry-meta-row"><span>Load (1/5/15)</span><span>${(cpuDtl.load_average || sys.load_average || []).slice(0, 3).join(', ') || '-'}</span></div>
          </div>
        </div>

        <!-- 4. Memory & Storage Card -->
        <div class="telemetry-card">
          <div class="telemetry-card-header">
            <span>Memory &amp; Storage</span>
            <span class="telemetry-pill info">${memPct}% RAM</span>
          </div>
          <div class="telemetry-metric-large">
            ${(memUsedMb/1024).toFixed(1)} / ${(memTotMb/1024).toFixed(1)} GB
          </div>
          <div class="telemetry-bar-wrap">
            <div class="telemetry-bar-fill" style="width: ${memPct}%; background-color: var(--link-color);"></div>
          </div>
          <div class="telemetry-meta-list mono cell-muted">
            <div class="telemetry-meta-row"><span>RAM Free</span><span class="cell-main">${((memTotMb - memUsedMb)/1024).toFixed(1)} GB</span></div>
            <div class="telemetry-meta-row"><span>Storage</span><span>${stUsedGb} / ${stTotGb} GB (${stPct}%)</span></div>
            <div class="telemetry-meta-row"><span>Storage Free</span><span class="cell-main">${Math.max(0, stTotGb - stUsedGb)} GB</span></div>
          </div>
        </div>
      </div>
    `;
  }

  const wlStr = `${uNode.workloads?.active_jobs || 0} active jobs · ${uNode.workloads?.running_jobs || 0} running jobs · ${uNode.workloads?.running_containers || 0} running containers`;

  grid.innerHTML = `
    <div class="detail-item"><span class="detail-label">Status</span><span class="detail-value">${renderStatusPill(node.status)}</span></div>
    <div class="detail-item"><span class="detail-label">Node ID</span><span class="detail-value mono">${node.node_id}</span></div>
    <div class="detail-item"><span class="detail-label">Role</span><span class="detail-value">${node.role || 'compute'}</span></div>
    <div class="detail-item"><span class="detail-label">Platform</span><span class="detail-value">${node.platform || '-'} (${node.os || 'Linux'})</span></div>
    <div class="detail-item"><span class="detail-label">Architecture</span><span class="detail-value mono">${node.architecture || '-'}</span></div>
    <div class="detail-item"><span class="detail-label">IP Address</span><span class="detail-value mono">${node.ip || net.ip || '-'}</span></div>
    <div class="detail-item"><span class="detail-label">Tailscale Mesh</span><span class="detail-value mono">${net.tailscale_ip || (node.ip?.startsWith('100.') ? node.ip : 'Active')}</span></div>
    <div class="detail-item"><span class="detail-label">Wi-Fi Connection</span><span class="detail-value mono">${net.wifi_ssid || 'Mesh / LAN'} (${net.wifi_signal_percent != null ? net.wifi_signal_percent + '%' : 'Strong'}, ${net.wifi_rssi_dbm ? net.wifi_rssi_dbm + 'dBm' : '-'})</span></div>
    <div class="detail-item"><span class="detail-label">Internet Latency</span><span class="detail-value mono">${net.ping_ms ? net.ping_ms + ' ms' : 'Low (<15ms)'}</span></div>
    <div class="detail-item"><span class="detail-label">Battery &amp; Thermal</span><span class="detail-value mono">${bat.percentage != null ? bat.percentage + '% (' + bat.status + ')' : 'AC Connected'} · Temp: ${tempC ? tempC + '°C' : 'Optimal'}</span></div>
    <div class="detail-item"><span class="detail-label">Workloads</span><span class="detail-value mono">${wlStr}</span></div>
    <div class="detail-item"><span class="detail-label">Capabilities</span><span class="detail-value mono">${caps}</span></div>
    <div class="detail-item"><span class="detail-label">Last Heartbeat</span><span class="detail-value mono">${formatDateIST(node.last_seen)} (${timeAgo(node.last_seen)})</span></div>
    <div class="detail-item"><span class="detail-label">Services</span><span class="detail-value mono">API: ${srv.node_api || srv['node-api'] || 'running'} · Tunnel: ${srv.cloudflare || 'connected'}</span></div>
    <div class="detail-item"><span class="detail-label">Uptime</span><span class="detail-value mono">${sys.uptime ? formatDuration(sys.uptime) : 'active'}</span></div>
  `;

  panel.style.display = "block";
  panel.scrollIntoView({ behavior: "smooth" });
};

// ------------------------------------------------------------------------------
// Interactive Live Node Diagnostics & Speed Test Modal
// ------------------------------------------------------------------------------
let currentDiagNodeId = null;

window.openNodeDiagnosticsModal = async function(nodeId) {
  currentDiagNodeId = nodeId;
  const node = cachedNodes.find(n => n.node_id === nodeId) || { name: nodeId, node_id: nodeId };

  const modal = document.getElementById("node-diag-modal-backdrop");
  const title = document.getElementById("diag-modal-title");
  const statusEl = document.getElementById("diag-modal-status");
  const contentEl = document.getElementById("diag-modal-content");
  const rerunBtn = document.getElementById("diag-modal-rerun-btn");

  if (title) title.textContent = `Node Diagnostics: ${node.name || nodeId}`;
  if (statusEl) {
    statusEl.innerHTML = `<span class="pulse-spinner"></span> Polling live hardware metrics &amp; testing network throughput...`;
    statusEl.style.display = "flex";
  }
  if (contentEl) contentEl.innerHTML = `<div class="mono cell-muted" style="padding: 24px; text-align: center;">Measuring latency, network connectivity, CPU thermals, and battery levels...</div>`;
  if (rerunBtn) rerunBtn.disabled = true;

  if (modal) modal.style.display = "flex";

  try {
    let res;
    try {
      res = await apiFetch(`/api/nodes/${nodeId}/diagnostics`);
    } catch (e) {
      if (node.last_heartbeat) {
        res = {
          status: (node.status || "").toLowerCase(),
          live: false,
          error: `Live agent diagnostics route unavailable (${e.message}). Displaying cached heartbeat telemetry.`,
          battery: node.last_heartbeat?.battery || node.last_heartbeat?.system?.battery,
          network: node.last_heartbeat?.network || node.last_heartbeat?.system?.network,
          cpu: node.last_heartbeat?.cpu_details || node.last_heartbeat?.system?.cpu_details,
          thermals: node.last_heartbeat?.thermals || node.last_heartbeat?.system?.thermals,
          timestamp: node.last_seen || node.last_heartbeat?.timestamp
        };
      } else {
        throw e;
      }
    }
    const diag = res.diagnostics || res;

    const isOffline = diag.status === "offline" || (node.status || "").toUpperCase() === "OFFLINE";
    const isLive = diag.live === true;
    const bat = diag.battery || {};
    const net = diag.network || {};
    const cpu = diag.cpu || {};
    const thm = diag.thermals || {};

    const speedMbps = net.transfer_speed_mbps != null ? net.transfer_speed_mbps : net.link_speed_mbps;
    const pingMs = net.ping_ms;
    const tempC = thm.temperature_c ?? cpu.temperature_c;

    if (statusEl) {
      if (isOffline) {
        statusEl.innerHTML = `<span class="text-offline">● Node is Offline</span> <span class="cell-muted">(Cannot poll live hardware; displaying last recorded heartbeat)</span>`;
      } else if (!isLive) {
        statusEl.innerHTML = `<span class="text-warning">● Live Agent Unreachable</span> <span class="cell-muted">(${diag.error || 'Displaying cached heartbeat metrics'})</span>`;
      } else {
        statusEl.innerHTML = `<span class="text-online">✓ Live Diagnostics Complete</span> <span class="cell-muted">(${diag.duration_ms ? diag.duration_ms + 'ms' : 'live'})</span>`;
      }
    }

    if (contentEl) {
      contentEl.innerHTML = `
        <div class="telemetry-cards-grid" style="margin-top: 8px;">
          <!-- 1. Internet Speed & Network -->
          <div class="telemetry-card">
            <div class="telemetry-card-header">
              <span>Network &amp; Connectivity</span>
              <span class="telemetry-pill ${isOffline ? 'danger' : 'good'}">${isOffline ? 'Offline' : (net.connection_type || 'Connected')}</span>
            </div>
            <div class="telemetry-metric-large ${isOffline ? 'text-offline' : 'text-online'}">
              ${speedMbps != null ? Math.round(speedMbps) + ' Mbps' : (isOffline ? '0 Mbps' : 'Not measured')}
              <span class="cell-muted" style="font-size: 11px; font-weight: normal;">transfer throughput</span>
            </div>
            <div class="telemetry-meta-list mono">
              <div class="telemetry-meta-row"><span>Ping Latency</span><span class="${pingMs != null ? 'text-online' : 'cell-muted'}">${pingMs != null ? pingMs + ' ms' : 'Not measured'}</span></div>
              <div class="telemetry-meta-row"><span>Wi-Fi SSID</span><span class="cell-main">${net.wifi_ssid || 'Not reported'}</span></div>
              <div class="telemetry-meta-row"><span>Signal Strength</span><span>${net.wifi_signal_percent != null ? net.wifi_signal_percent + '%' : 'Not reported'}</span></div>
              <div class="telemetry-meta-row"><span>LAN IP</span><span class="cell-main">${net.ip || node.ip || 'Not reported'}</span></div>
              <div class="telemetry-meta-row"><span>Tailscale IP</span><span class="cell-main">${net.tailscale_ip || (node.ip?.startsWith('100.') ? node.ip : 'Not configured')}</span></div>
            </div>
          </div>

          <!-- 2. Battery & Power -->
          <div class="telemetry-card">
            <div class="telemetry-card-header">
              <span>Battery &amp; Power</span>
              <span class="telemetry-pill ${bat.percentage != null ? (bat.percentage > 50 ? 'good' : (bat.percentage > 20 ? 'warn' : 'danger')) : 'info'}">${bat.status || (bat.percentage != null ? 'Battery' : 'Not reported')}</span>
            </div>
            <div class="telemetry-metric-large" style="color: ${bat.percentage != null ? (bat.percentage > 50 ? 'var(--status-green)' : 'var(--status-yellow)') : 'var(--text-muted)'};">
              ${bat.percentage != null ? bat.percentage + '%' : 'AC / Not reported'}
              <span class="cell-muted" style="font-size: 11px; font-weight: normal;">${bat.plugged || (bat.percentage != null ? 'Battery' : 'AC Power')}</span>
            </div>
            <div class="telemetry-meta-list mono">
              <div class="telemetry-meta-row"><span>Status</span><span class="cell-main">${bat.status || 'Not reported'}</span></div>
              <div class="telemetry-meta-row"><span>Battery Temp</span><span>${bat.temperature_c ? bat.temperature_c + '°C' : 'Not reported'}</span></div>
              <div class="telemetry-meta-row"><span>Health State</span><span class="${bat.health ? 'text-online' : 'cell-muted'}">${bat.health || 'Not reported'}</span></div>
              <div class="telemetry-meta-row"><span>Power Source</span><span>${bat.plugged || 'Not reported'}</span></div>
            </div>
          </div>

          <!-- 3. CPU Status & Thermals -->
          <div class="telemetry-card">
            <div class="telemetry-card-header">
              <span>Processor &amp; Thermals</span>
              <span class="telemetry-pill ${tempC && tempC > 80 ? 'danger' : 'good'}">${thm.status || (tempC != null ? (tempC < 80 ? 'Normal' : 'High') : 'Not reported')}</span>
            </div>
            <div class="telemetry-metric-large text-online">
              ${tempC != null ? tempC + '°C' : (cpu.utilization_percent != null ? cpu.utilization_percent + '%' : 'Not reported')}
              <span class="cell-muted" style="font-size: 11px; font-weight: normal;">${cpu.utilization_percent != null ? cpu.utilization_percent + '% usage' : ''}</span>
            </div>
            <div class="telemetry-meta-list mono">
              <div class="telemetry-meta-row"><span>CPU Cores</span><span class="cell-main">${cpu.cores || node.resources?.cpu_cores || 'Not reported'}</span></div>
              <div class="telemetry-meta-row"><span>Thermal Status</span><span class="${tempC != null ? 'text-online' : 'cell-muted'}">${thm.status || (tempC != null ? (tempC < 80 ? 'Normal' : 'High') : 'Not reported')}</span></div>
              <div class="telemetry-meta-row"><span>Clock Speed</span><span>${cpu.frequency_mhz ? Math.round(cpu.frequency_mhz) + ' MHz' : 'Not reported'}</span></div>
              <div class="telemetry-meta-row"><span>Load Average</span><span>${(cpu.load_average || []).slice(0, 3).join(', ') || 'Not reported'}</span></div>
            </div>
          </div>

          <!-- 4. System & Summary -->
          <div class="telemetry-card">
            <div class="telemetry-card-header">
              <span>Node Identity &amp; State</span>
              <span class="telemetry-pill ${isOffline ? 'danger' : 'info'}">${isOffline ? 'Offline' : 'Online'}</span>
            </div>
            <div class="telemetry-metric-large">
              ${node.platform || 'Linux'}
              <span class="cell-muted" style="font-size: 11px; font-weight: normal;">${node.architecture || 'aarch64'}</span>
            </div>
            <div class="telemetry-meta-list mono">
              <div class="telemetry-meta-row"><span>Node Name</span><span class="cell-main">${node.name || nodeId}</span></div>
              <div class="telemetry-meta-row"><span>Role</span><span>${node.role || 'compute'}</span></div>
              <div class="telemetry-meta-row"><span>Heartbeat Age</span><span>${timeAgo(node.last_seen)}</span></div>
              <div class="telemetry-meta-row"><span>Timestamp</span><span class="cell-muted" style="font-size: 10px;">${formatDateIST(diag.timestamp)}</span></div>
            </div>
          </div>
        </div>
      `;
    }

  } catch (err) {
    if (statusEl) {
      statusEl.innerHTML = `<span class="text-offline">✗ Diagnostics failed: ${err.message}</span>`;
    }
  } finally {
    if (rerunBtn) rerunBtn.disabled = false;
  }
};

window.closeNodeDiagModal = function() {
  const modal = document.getElementById("node-diag-modal-backdrop");
  if (modal) modal.style.display = "none";
};

window.rerunCurrentNodeDiag = function() {
  if (currentDiagNodeId) {
    openNodeDiagnosticsModal(currentDiagNodeId);
  }
};

window.runNodeDetailDiagnostics = function() {
  if (activeDetailNodeId) {
    openNodeDiagnosticsModal(activeDetailNodeId);
  }
};

window.closeNodeDetail = function() {
  const panel = document.getElementById("node-detail-panel");
  if (panel) panel.style.display = "none";
};

window.drainNode = async function(nodeId) {
  const node = cachedNodes.find(n => n.node_id === nodeId) || { name: nodeId, node_id: nodeId };
  const confirmed = await showCustomConfirm({
    title: `Drain Node: ${node.name || nodeId}`,
    message: `Drain node "${node.name || nodeId}"?\n\n• Scheduler will NOT assign any new workloads to this node\n• Existing jobs and applications will continue running\n• Auto-target queued jobs will be rescheduled to eligible online nodes\n• Heartbeat and status monitoring continue normally`,
    confirmText: "Start Draining",
    isDanger: false
  });
  if (!confirmed) return;

  try {
    const res = await apiFetch(`/api/nodes/${nodeId}/drain`, { method: "POST" });
    const rescheduled = res.jobs_rescheduled != null ? `\n• ${res.jobs_rescheduled} queued auto job(s) rescheduled` : "";
    await showCustomAlert({
      title: "Node Draining Started",
      message: `Node "${node.name || nodeId}" is now in DRAINING state.${rescheduled}\n\nExisting workloads may finish. Use "Deboard" once jobs are completed.`
    });
    await loadNodes();
    if (document.getElementById("node-detail-panel")?.style.display !== "none") {
      viewNodeById(nodeId);
    }
  } catch (err) {
    await showCustomAlert({
      title: "Drain Failed",
      message: err.message || "Failed to drain node.",
      isError: true
    });
  }
};

window.deboardNode = async function(nodeId) {
  const node = cachedNodes.find(n => n.node_id === nodeId) || { name: nodeId, node_id: nodeId };
  try {
    const res = await apiFetch(`/api/nodes/${nodeId}/deboard`, { method: "POST" });
    const deb = res.deboarding || res;
    if (deb.can_remove) {
      await showCustomAlert({
        title: "Ready for Deboarding",
        message: `Node "${node.name || nodeId}" is now in DEBOARDING state.\n\nAll safety checks passed:\n• 0 active applications\n• 0 running/claimed jobs\n• 0 unmigrated storage files\n\nYou can now safely remove this node.`
      });
    } else {
      const blockersText = (deb.blockers || []).map(b => `• ${b}`).join("\n");
      await showCustomAlert({
        title: "Deboarding Checks - Action Required",
        message: `Node "${node.name || nodeId}" transitioned to DEBOARDING, but cannot be removed yet.\n\nBlockers:\n${blockersText || "• Active dependencies exist"}\n\nAction:\nResolve the blockers above before final removal.`,
        isError: true
      });
    }
    await loadNodes();
    if (document.getElementById("node-detail-panel")?.style.display !== "none") {
      viewNodeById(nodeId);
    }
  } catch (err) {
    await showCustomAlert({
      title: "Deboard Failed",
      message: err.message || "Failed to deboard node.",
      isError: true
    });
  }
};

window.resumeNode = async function(nodeId) {
  const node = cachedNodes.find(n => n.node_id === nodeId) || { name: nodeId, node_id: nodeId };
  const confirmed = await showCustomConfirm({
    title: `Resume Node: ${node.name || nodeId}`,
    message: `Resume node "${node.name || nodeId}" back to ONLINE state?\n\nThe scheduler will resume assigning new workloads to this node.`
  });
  if (!confirmed) return;

  try {
    await apiFetch(`/api/nodes/${nodeId}/resume`, { method: "POST" });
    await showCustomAlert({
      title: "Node Resumed",
      message: `Node "${node.name || nodeId}" has been resumed to active ONLINE state.`
    });
    await loadNodes();
    if (document.getElementById("node-detail-panel")?.style.display !== "none") {
      viewNodeById(nodeId);
    }
  } catch (err) {
    await showCustomAlert({
      title: "Resume Failed",
      message: err.message || "Failed to resume node.",
      isError: true
    });
  }
};

window.removeNodeSafe = async function(nodeId) {
  const node = cachedNodes.find(n => n.node_id === nodeId) || { name: nodeId, node_id: nodeId };
  try {
    const deb = await apiFetch(`/api/nodes/${nodeId}/deboard`);
    if (!deb.can_remove) {
      const blockersText = (deb.blockers || []).map(b => `• ${b}`).join("\n");
      await showCustomAlert({
        title: `Cannot Remove Node: ${node.name || nodeId}`,
        message: `Cannot remove Node "${node.name || nodeId}".\n\nBlockers:\n${blockersText}\n\nAction:\nMigrate/remove workloads and storage before deboarding.`,
        isError: true
      });
      return;
    }

    const confirmed = await showCustomConfirm({
      title: "Confirm Safe Node Removal",
      message: `Permanently remove Node "${node.name || nodeId}" (${nodeId})?\n\nSafety verification passed:\n• 0 active applications\n• 0 running jobs\n• 0 storage data\n\nAuthentication credentials will be invalidated. Node must re-enroll to join.`,
      confirmText: "Remove Node",
      isDanger: true
    });
    if (!confirmed) return;

    await apiFetch(`/api/nodes/${nodeId}/remove`, { method: "POST" });
    await showCustomAlert({
      title: "Node Removed",
      message: `Node "${node.name || nodeId}" has been safely removed from active cluster membership.`
    });
    closeNodeDetail();
    await loadNodes();
  } catch (err) {
    await showCustomAlert({
      title: "Removal Blocked",
      message: err.message || "Cannot remove node.",
      isError: true
    });
  }
};

// 3. Jobs View & Safe Lifecycle
let jobCurrentPage = 1;
let jobPageSize = 25;
let jobTotalCount = 0;
let jobTotalPages = 1;
let activeDetailJobId = null;
let jobsSearchDebounceTimer = null;

window.toggleSubmitJobCard = function(forceState) {
  const card = document.getElementById("job-submit-card");
  if (!card) return;
  const isCurrentlyOpen = card.style.display !== "none";
  const shouldOpen = forceState !== undefined ? forceState : !isCurrentlyOpen;
  card.style.display = shouldOpen ? "block" : "none";
  const btn = document.getElementById("toggle-submit-job-btn");
  if (btn) btn.textContent = shouldOpen ? "Hide Submit Form" : "+ Submit Workload";
};

window.resetJobFilters = function() {
  const searchInput = document.getElementById("jobs-search-input");
  const statusSelect = document.getElementById("jobs-filter-status");
  const typeSelect = document.getElementById("jobs-filter-type");
  const nodeSelect = document.getElementById("jobs-filter-node");
  const sortSelect = document.getElementById("jobs-sort-by");

  if (searchInput) searchInput.value = "";
  if (statusSelect) statusSelect.value = "all";
  if (typeSelect) typeSelect.value = "all";
  if (nodeSelect) nodeSelect.value = "all";
  if (sortSelect) sortSelect.value = "recent";

  jobCurrentPage = 1;
  loadJobs();
};

window.changeJobPage = function(delta) {
  const newPage = jobCurrentPage + delta;
  if (newPage >= 1 && newPage <= jobTotalPages) {
    jobCurrentPage = newPage;
    loadJobs();
  }
};

window.changeJobPageSize = function(val) {
  const parsed = parseInt(val, 10);
  if (!isNaN(parsed) && parsed > 0) {
    jobPageSize = parsed;
    jobCurrentPage = 1;
    loadJobs();
  }
};

async function populateJobNodeDropdowns() {
  const targetSelect = document.getElementById("job-target");
  const filterNodeSelect = document.getElementById("jobs-filter-node");

  try {
    const data = await apiFetch("/api/nodes");
    const nodes = data.nodes ? Object.values(data.nodes) : [];

    if (targetSelect) {
      const currVal = targetSelect.value;
      targetSelect.innerHTML = `<option value="auto">Auto (Scheduler)</option>` +
        nodes.map(n => `<option value="${escapeHtml(n.node_id)}">${escapeHtml(n.hostname || n.node_id)} (${escapeHtml(n.status)})</option>`).join("");
      if (currVal) targetSelect.value = currVal;
    }

    if (filterNodeSelect) {
      const currFilter = filterNodeSelect.value;
      filterNodeSelect.innerHTML = `<option value="all">All Nodes</option>` +
        nodes.map(n => `<option value="${escapeHtml(n.node_id)}">${escapeHtml(n.hostname || n.node_id)}</option>`).join("");
      if (currFilter) filterNodeSelect.value = currFilter;
    }
  } catch (err) {
    // Non-fatal fallback
  }
}

async function loadJobs() {
  updateLastRefreshed();
  const tbody = document.getElementById("jobs-tbody");
  if (!tbody) return;

  populateJobNodeDropdowns();

  // Read filter values
  const search = document.getElementById("jobs-search-input")?.value.trim() || "";
  const status = document.getElementById("jobs-filter-status")?.value || "all";
  const type = document.getElementById("jobs-filter-type")?.value || "all";
  const node = document.getElementById("jobs-filter-node")?.value || "all";
  const sort = document.getElementById("jobs-sort-by")?.value || "recent";

  // Build query string
  const params = new URLSearchParams();
  params.set("page", jobCurrentPage);
  params.set("limit", jobPageSize);
  params.set("sort", sort);
  if (status !== "all") params.set("status", status);
  if (type !== "all") params.set("type", type);
  if (node !== "all") params.set("node", node);
  if (search) params.set("search", search);

  tbody.innerHTML = `<tr><td colspan="8" class="cell-muted" style="text-align: center; padding: 24px;">Loading execution history...</td></tr>`;

  try {
    const data = await apiFetch(`/api/jobs?${params.toString()}`);
    cachedJobs = data.jobs || [];
    jobTotalCount = data.total_count !== undefined ? data.total_count : cachedJobs.length;
    jobTotalPages = data.total_pages || Math.max(1, Math.ceil(jobTotalCount / jobPageSize));

    // Update headers and pagination info
    const countHdr = document.getElementById("jobs-count-header");
    if (countHdr) countHdr.textContent = `${jobTotalCount} total jobs`;

    const pageInfo = document.getElementById("jobs-pagination-info");
    if (pageInfo) {
      if (jobTotalCount === 0) {
        pageInfo.textContent = "Showing 0 of 0 jobs";
      } else {
        const start = (jobCurrentPage - 1) * jobPageSize + 1;
        const end = Math.min(jobCurrentPage * jobPageSize, jobTotalCount);
        pageInfo.textContent = `Showing ${start}-${end} of ${jobTotalCount} jobs`;
      }
    }

    const pageNum = document.getElementById("jobs-page-number");
    if (pageNum) pageNum.textContent = `Page ${jobCurrentPage} of ${jobTotalPages}`;

    const prevBtn = document.getElementById("jobs-prev-btn");
    if (prevBtn) prevBtn.disabled = jobCurrentPage <= 1;

    const nextBtn = document.getElementById("jobs-next-btn");
    if (nextBtn) nextBtn.disabled = jobCurrentPage >= jobTotalPages;

    if (cachedJobs.length === 0) {
      const hasActiveFilters = search || status !== "all" || type !== "all" || node !== "all";
      tbody.innerHTML = `
        <tr>
          <td colspan="8">
            <div class="empty-state" style="padding: 32px 16px; text-align: center;">
              <div class="empty-state-title" style="font-size: 14px; font-weight: 600; margin-bottom: 6px;">
                ${hasActiveFilters ? "No Matching Workload Jobs" : "No Workload Jobs Recorded"}
              </div>
              <div class="empty-state-desc cell-muted" style="font-size: 12px; margin-bottom: 12px;">
                ${hasActiveFilters ? "No jobs match the current filter criteria." : "No jobs have been dispatched yet."}
              </div>
              <div class="empty-state-action">
                ${hasActiveFilters
                  ? `<button class="btn btn-sm" onclick="resetJobFilters()">Clear Filters</button>`
                  : `<button class="btn btn-sm btn-primary" onclick="toggleSubmitJobCard(true)">Submit First Job</button>`
                }
              </div>
            </div>
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = cachedJobs.map(j => {
      const jobId = j.job_id || j.id || "";
      const target = j.assigned_node || j.target_node || j.target || "auto";
      const res = j.result || {};
      const duration = res.duration_ms !== undefined ? `${res.duration_ms}ms` : (j.execution_duration_sec ? formatDuration(j.execution_duration_sec) : "-");
      const createdStr = j.created_at || j.submitted_at ? formatDateIST(j.created_at || j.submitted_at) : "-";
      const attemptStr = `${j.attempt || 1}/${j.max_attempts || 3}`;
      const st = (j.status || j.state || "").toUpperCase();
      const isCancellable = (st === "QUEUED" || st === "CLAIMED" || st === "RUNNING" || st === "RECOVERING");

      return `
        <tr>
          <td class="mono"><strong>${escapeHtml(jobId)}</strong></td>
          <td><span class="mono" style="font-size: 11px; padding: 2px 6px; background: var(--bg-hover); border-radius: 4px; border: 1px solid var(--border-color);">${escapeHtml(j.type || "-")}</span></td>
          <td class="mono cell-muted">${escapeHtml(target)}</td>
          <td>${renderStatusPill(st)}</td>
          <td class="mono cell-muted">${attemptStr}</td>
          <td class="mono cell-muted">${duration}</td>
          <td class="mono cell-muted" style="white-space: nowrap;">${createdStr}</td>
          <td style="text-align: right;">
            <div class="job-actions-cell">
              <button class="btn btn-sm" onclick="viewJobById('${escapeHtml(jobId)}')">Details</button>
              ${isCancellable
                ? `<button class="btn btn-sm btn-cancel-job" onclick="confirmCancelJob('${escapeHtml(jobId)}')" title="Cancel active job">Cancel</button>`
                : `<button class="btn btn-sm btn-cancel-job" disabled title="Job is terminal (${st})">Cancel</button>`
              }
              ${!isCancellable
                ? `<button class="btn btn-sm btn-danger" onclick="confirmDeleteJob('${escapeHtml(jobId)}')" title="Permanently delete job">Delete</button>`
                : `<button class="btn btn-sm btn-danger" disabled title="Cannot delete active job (cancel first)">Delete</button>`
              }
            </div>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    tbody.innerHTML = `
      <tr>
        <td colspan="8" style="padding: 24px; text-align: center;">
          <div class="text-offline" style="margin-bottom: 8px;">Unable to load execution history: ${escapeHtml(err.message)}</div>
          <button class="btn btn-sm" onclick="loadJobs()">Retry</button>
        </td>
      </tr>
    `;
  }
}

window.confirmCancelJob = async function(jobId) {
  const job = cachedJobs.find(j => (j.job_id === jobId || j.id === jobId));
  const jobType = job ? job.type : "workload";
  const node = job ? (job.target_node || job.assigned_node || "assigned worker") : "assigned worker";

  const confirmed = await showCustomConfirm({
    title: "Confirm Job Cancellation",
    message: `Are you sure you want to cancel job ${jobId} (${jobType})? Any active execution on node '${node}' will be terminated immediately.`,
    confirmText: "Cancel Job",
    isDanger: true
  });

  if (!confirmed) return;

  await cancelJob(jobId);
};

window.cancelJob = async function(jobId) {
  try {
    await apiFetch(`/api/jobs/${jobId}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "Cancelled by operator via dashboard", idempotent: true })
    });

    // Update job in cache immediately
    const cachedIdx = cachedJobs.findIndex(j => (j.job_id === jobId || j.id === jobId));
    if (cachedIdx !== -1) {
      cachedJobs[cachedIdx].status = "CANCELLED";
      cachedJobs[cachedIdx].state = "CANCELLED";
      cachedJobs[cachedIdx].cancel_reason = "Cancelled by operator via dashboard";
    }

    // Refresh detail panel if open
    if (activeDetailJobId === jobId) {
      viewJobById(jobId);
    }

    // Refresh jobs list
    loadJobs();
  } catch (err) {
    showCustomAlert({
      title: "Cancellation Failed",
      message: `Failed to cancel job ${jobId}: ${err.message}`,
      isError: true
    });
  }
};

window.cancelJobFromDetail = function() {
  if (activeDetailJobId) {
    confirmCancelJob(activeDetailJobId);
  }
};

const inFlightJobDeletions = new Set();

window.confirmDeleteJob = async function(jobId) {
  if (!jobId) return;

  // Prevent duplicate requests while deletion is in progress
  if (inFlightJobDeletions.has(jobId)) {
    console.warn(`[JOBS] Deletion already in progress for job ${jobId}`);
    return;
  }

  // Safety check: verify job is not active
  const targetJob = cachedJobs.find(j => (j.job_id === jobId || j.id === jobId));
  if (targetJob) {
    const st = (targetJob.status || targetJob.state || "").toUpperCase();
    if (st === "QUEUED" || st === "CLAIMED" || st === "RUNNING" || st === "RECOVERING") {
      showCustomAlert({
        title: "Cannot Delete Active Job",
        message: `Job "${jobId}" is currently in ${st} state.\n\nActive workloads cannot be deleted directly to maintain cluster reliability. Please cancel the job first.`,
        isError: true
      });
      return;
    }
  }

  const confirmed = await showCustomConfirm({
    title: "Delete Job",
    message: `Are you sure you want to permanently delete job "${jobId}"?\n\nThis will remove the job and its execution records from the cluster database. This action cannot be undone.`,
    confirmText: "Delete Job",
    isDanger: true
  });

  if (!confirmed) return;

  inFlightJobDeletions.add(jobId);
  try {
    const res = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    showCustomAlert({
      title: "Job Deleted",
      message: (res && res.message) ? res.message : `Job "${jobId}" deleted successfully.`
    });
    if (activeDetailJobId === jobId) {
      closeJobDetail();
    }
    cachedJobs = cachedJobs.filter(j => (j.job_id !== jobId && j.id !== jobId));
    loadJobs();
  } catch (err) {
    const errMsg = err.message || "";
    const isNotFound = errMsg.includes("404") || errMsg.toLowerCase().includes("not found");
    if (isNotFound) {
      showCustomAlert({
        title: "Job Not Found",
        message: `Job "${jobId}" was already removed or does not exist. Updating job list.`,
        isError: true
      });
      if (activeDetailJobId === jobId) {
        closeJobDetail();
      }
      cachedJobs = cachedJobs.filter(j => (j.job_id !== jobId && j.id !== jobId));
      loadJobs();
    } else {
      showCustomAlert({
        title: "Delete Failed",
        message: errMsg || "Failed to delete job.",
        isError: true
      });
    }
  } finally {
    inFlightJobDeletions.delete(jobId);
  }
};

window.deleteJobFromDetail = function() {
  if (activeDetailJobId) {
    confirmDeleteJob(activeDetailJobId);
  }
};

window.viewJobById = function(jobId) {
  activeDetailJobId = jobId;
  const job = cachedJobs.find(j => (j.job_id === jobId || j.id === jobId));
  const panel = document.getElementById("job-detail-panel");
  const title = document.getElementById("job-detail-title");
  const statusPill = document.getElementById("job-detail-status-pill");
  const cancelBtn = document.getElementById("job-detail-cancel-btn");
  const deleteBtn = document.getElementById("job-detail-delete-btn");
  const sumGrid = document.getElementById("job-detail-summary");
  const paramsBox = document.getElementById("job-detail-params");
  const stdoutBox = document.getElementById("job-detail-stdout");

  const renderJobInfo = (j) => {
    const id = j.job_id || j.id || "";
    if (title) title.textContent = `Job: ${id}`;
    const st = (j.status || j.state || "").toUpperCase();
    if (statusPill) statusPill.innerHTML = renderStatusPill(st);

    const isCancellable = (st === "QUEUED" || st === "CLAIMED" || st === "RUNNING" || st === "RECOVERING");
    if (cancelBtn) {
      cancelBtn.style.display = isCancellable ? "inline-block" : "none";
    }
    if (deleteBtn) {
      deleteBtn.style.display = !isCancellable ? "inline-block" : "none";
    }

    const sched = j.scheduler || {};
    const res = j.result || {};
    const targetNode = j.target_node || j.target || "auto";
    const assignedNode = j.assigned_node || j.target_node || "-";
    const durationStr = res.duration_ms !== undefined ? `${res.duration_ms}ms` : (j.execution_duration_sec ? formatDuration(j.execution_duration_sec) : "-");
    const submittedStr = j.submitted_at || j.created_at ? formatDateIST(j.submitted_at || j.created_at) : "-";
    const finishedStr = j.finished_at || j.cancelled_at ? formatDateIST(j.finished_at || j.cancelled_at) : "-";
    const retryReason = j.retry_reason || (j.diagnostics && j.diagnostics.failure_reason) || "None";
    const cancelReason = j.cancel_reason || "None";

    if (sumGrid) {
      sumGrid.innerHTML = `
        <div class="detail-item"><span class="detail-label">Status</span><span class="detail-value">${renderStatusPill(st)}</span></div>
        <div class="detail-item"><span class="detail-label">Workload</span><span class="detail-value">${escapeHtml(j.type || "-")}</span></div>
        <div class="detail-item"><span class="detail-label">Target Node</span><span class="detail-value mono">${escapeHtml(targetNode)}</span></div>
        <div class="detail-item"><span class="detail-label">Assigned Worker</span><span class="detail-value mono">${escapeHtml(assignedNode)}</span></div>
        <div class="detail-item"><span class="detail-label">Attempt</span><span class="detail-value mono">${j.attempt || 1}/${j.max_attempts || 3}</span></div>
        <div class="detail-item"><span class="detail-label">Duration</span><span class="detail-value mono">${durationStr}</span></div>
        <div class="detail-item"><span class="detail-label">Exit Code</span><span class="detail-value mono">${res.exit_code !== undefined ? res.exit_code : '-'}</span></div>
        <div class="detail-item"><span class="detail-label">Submitted At</span><span class="detail-value mono">${escapeHtml(submittedStr)}</span></div>
        <div class="detail-item"><span class="detail-label">Finished At</span><span class="detail-value mono">${escapeHtml(finishedStr)}</span></div>
        <div class="detail-item"><span class="detail-label">Retry Reason</span><span class="detail-value">${escapeHtml(retryReason)}</span></div>
        ${j.cancel_reason ? `<div class="detail-item" style="grid-column: 1 / -1;"><span class="detail-label">Cancel Reason</span><span class="detail-value text-offline">${escapeHtml(cancelReason)}</span></div>` : ""}
      `;
    }

    if (paramsBox) {
      const p = j.parameters;
      paramsBox.textContent = p && Object.keys(p).length > 0 ? JSON.stringify(p, null, 2) : "No parameters provided.";
    }

    if (stdoutBox) {
      const outText = res.stdout || res.stderr || (j.result ? JSON.stringify(j.result, null, 2) : "No execution logs recorded.");
      stdoutBox.textContent = outText;
    }

    if (panel) {
      panel.style.display = "block";
      panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  };

  if (job) {
    renderJobInfo(job);
  } else {
    apiFetch(`/api/jobs/${jobId}`)
      .then(d => renderJobInfo(d.job || d))
      .catch(e => showCustomAlert({ title: "Error", message: "Failed to fetch job details: " + e.message, isError: true }));
  }
};

window.closeJobDetail = function() {
  const panel = document.getElementById("job-detail-panel");
  if (panel) panel.style.display = "none";
  activeDetailJobId = null;
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
      showCustomAlert({
        title: "Invalid Parameters",
        message: "Invalid JSON parameters: " + err.message,
        isError: true
      });
      return;
    }
  }

  try {
    await apiFetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, target, timeout, parameters: params })
    });
    toggleSubmitJobCard(false);
    jobCurrentPage = 1;
    loadJobs();
  } catch (err) {
    showCustomAlert({
      title: "Job Submission Failed",
      message: err.message,
      isError: true
    });
  }
});

// Setup filter listeners
document.getElementById("jobs-search-input")?.addEventListener("input", () => {
  clearTimeout(jobsSearchDebounceTimer);
  jobsSearchDebounceTimer = setTimeout(() => {
    jobCurrentPage = 1;
    loadJobs();
  }, 300);
});
document.getElementById("jobs-filter-status")?.addEventListener("change", () => {
  jobCurrentPage = 1;
  loadJobs();
});
document.getElementById("jobs-filter-type")?.addEventListener("change", () => {
  jobCurrentPage = 1;
  loadJobs();
});
document.getElementById("jobs-filter-node")?.addEventListener("change", () => {
  jobCurrentPage = 1;
  loadJobs();
});
document.getElementById("jobs-sort-by")?.addEventListener("change", () => {
  jobCurrentPage = 1;
  loadJobs();
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
    const modStr = item.modified ? formatDateIST(item.modified) : "-";
    const sizeStr = isDir ? "—" : formatBytes(item.size_bytes);

    const downloadUrl = isLocal
      ? `/storage/download?path=${encodeURIComponent(itemRelPath)}`
      : `/storage/download?node=${encodeURIComponent(node)}&path=${encodeURIComponent(itemRelPath)}`;

    const escapedPath = itemRelPath.replace(/'/g, "\\'");
    const escapedName = item.name.replace(/'/g, "\\'");
    const isSelected = selectedItemName === item.name ? "selected" : "";

    return `
      <tr class="explorer-row ${isSelected}" onclick="selectStorageRow(event, '${escapedName}')" ondblclick="${isDir ? `loadStorage('${escapedPath}', '${node}', true)` : `openPreviewModal('${escapedPath}')`}">
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
            ${!isDir ? `<button class="btn btn-sm btn-primary" onclick="event.stopPropagation(); openPreviewModal('${escapedPath}')" title="Preview file">Preview</button>` : ''}
            ${!isDir ? `<a href="${downloadUrl}" class="btn btn-sm" download title="Download file" onclick="event.stopPropagation()">Download</a>` : ''}
            ${!isDir ? `<button class="btn btn-sm" onclick="event.stopPropagation(); openShareModal('${escapedPath}')" title="Share link">Share</button>` : ''}
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
  const modStr = item?.modified ? formatDateIST(item.modified) : "-";
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

  const propsPreviewBtn = document.getElementById("storage-props-preview-btn");
  if (propsPreviewBtn) {
    propsPreviewBtn.style.display = isDir ? "none" : "inline-flex";
    propsPreviewBtn.setAttribute("data-item-path", itemRelPath);
  }

  const propsShareBtn = document.getElementById("storage-props-share-btn");
  if (propsShareBtn) {
    propsShareBtn.style.display = isDir ? "none" : "inline-flex";
    propsShareBtn.setAttribute("data-item-path", itemRelPath);
  }

  panel.style.display = "block";
  panel.scrollIntoView({ behavior: "smooth" });
};

window.closeStorageProperties = function() {
  const panel = document.getElementById("storage-properties-panel");
  if (panel) panel.style.display = "none";
};

window.previewCurrentPropsItem = function() {
  const propsPreviewBtn = document.getElementById("storage-props-preview-btn");
  const path = propsPreviewBtn?.getAttribute("data-item-path");
  if (path) openPreviewModal(path);
};

window.shareCurrentPropsItem = function() {
  const propsShareBtn = document.getElementById("storage-props-share-btn");
  const path = propsShareBtn?.getAttribute("data-item-path");
  if (path) openShareModal(path);
};

// ==========================================
// 4b. Storage Link Sharing (Phase 2A)
// ==========================================

let currentSharingPath = null;

window.openShareModal = async function(itemRelPath) {
  currentSharingPath = itemRelPath;
  const fileName = itemRelPath.split("/").pop();

  const backdrop = document.getElementById("storage-share-modal-backdrop");
  const nameEl = document.getElementById("share-file-name");
  const pathEl = document.getElementById("share-file-path");
  const loadingEl = document.getElementById("share-modal-loading");
  const contentEl = document.getElementById("share-modal-content");
  const toggleInput = document.getElementById("share-toggle-input");
  const statusText = document.getElementById("share-toggle-status-text");
  const urlInput = document.getElementById("share-url-input");
  const linkSection = document.getElementById("share-link-section");
  const privateSection = document.getElementById("share-private-section");
  const errorEl = document.getElementById("share-modal-error");
  const copyBtn = document.getElementById("share-copy-btn");

  if (!backdrop) return;

  if (nameEl) nameEl.textContent = fileName;
  if (pathEl) pathEl.textContent = `Logical Path: Home/${itemRelPath}`;
  if (errorEl) {
    errorEl.style.display = "none";
    errorEl.textContent = "";
  }
  if (copyBtn) copyBtn.textContent = "Copy Link";

  backdrop.style.display = "flex";
  if (loadingEl) loadingEl.style.display = "block";
  if (contentEl) contentEl.style.display = "none";

  try {
    const nodeParam = selectedStorageNode ? `&node=${encodeURIComponent(selectedStorageNode)}` : "";
    const res = await apiFetch(`/storage/share?path=${encodeURIComponent(itemRelPath)}${nodeParam}`);

    if (res.shared && res.url) {
      if (toggleInput) toggleInput.checked = true;
      if (statusText) statusText.textContent = "ON";
      if (urlInput) urlInput.value = res.url;
      if (linkSection) linkSection.style.display = "block";
      if (privateSection) privateSection.style.display = "none";
    } else {
      if (toggleInput) toggleInput.checked = false;
      if (statusText) statusText.textContent = "OFF";
      if (urlInput) urlInput.value = "";
      if (linkSection) linkSection.style.display = "none";
      if (privateSection) privateSection.style.display = "block";
    }
  } catch (err) {
    if (toggleInput) toggleInput.checked = false;
    if (statusText) statusText.textContent = "OFF";
    if (errorEl) {
      errorEl.textContent = `Failed to query share state: ${err.message}`;
      errorEl.style.display = "block";
    }
  } finally {
    if (loadingEl) loadingEl.style.display = "none";
    if (contentEl) contentEl.style.display = "block";
  }
};

window.closeShareModal = function() {
  const backdrop = document.getElementById("storage-share-modal-backdrop");
  if (backdrop) backdrop.style.display = "none";
  currentSharingPath = null;
};

window.toggleFileSharing = async function(event) {
  if (!currentSharingPath) return;

  const toggleInput = document.getElementById("share-toggle-input");
  const statusText = document.getElementById("share-toggle-status-text");
  const urlInput = document.getElementById("share-url-input");
  const linkSection = document.getElementById("share-link-section");
  const privateSection = document.getElementById("share-private-section");
  const errorEl = document.getElementById("share-modal-error");
  const copyBtn = document.getElementById("share-copy-btn");

  if (errorEl) {
    errorEl.style.display = "none";
    errorEl.textContent = "";
  }
  if (copyBtn) copyBtn.textContent = "Copy Link";

  const isEnabling = event.target.checked;
  if (statusText) statusText.textContent = isEnabling ? "ON" : "OFF";

  try {
    if (isEnabling) {
      const res = await apiFetch("/storage/share", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: currentSharingPath,
          node: selectedStorageNode || ""
        })
      });

      if (urlInput) urlInput.value = res.url || "";
      if (linkSection) linkSection.style.display = "block";
      if (privateSection) privateSection.style.display = "none";
    } else {
      const nodeParam = selectedStorageNode ? `&node=${encodeURIComponent(selectedStorageNode)}` : "";
      await apiFetch(`/storage/share?path=${encodeURIComponent(currentSharingPath)}${nodeParam}`, {
        method: "DELETE"
      });

      if (urlInput) urlInput.value = "";
      if (linkSection) linkSection.style.display = "none";
      if (privateSection) privateSection.style.display = "block";
    }
  } catch (err) {
    if (toggleInput) toggleInput.checked = !isEnabling;
    if (statusText) statusText.textContent = !isEnabling ? "ON" : "OFF";
    if (errorEl) {
      errorEl.textContent = err.message || "Action failed";
      errorEl.style.display = "block";
    }
  }
};

window.copyShareUrl = async function() {
  const urlInput = document.getElementById("share-url-input");
  const copyBtn = document.getElementById("share-copy-btn");
  if (!urlInput || !urlInput.value) return;

  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(urlInput.value);
    } else {
      urlInput.select();
      document.execCommand("copy");
    }
    if (copyBtn) {
      const origText = copyBtn.textContent;
      copyBtn.textContent = "Copied!";
      setTimeout(() => {
        copyBtn.textContent = origText;
      }, 2000);
    }
  } catch (err) {
    urlInput.select();
  }
};

// ==========================================
// 4c. Storage Document & Media Preview (Phase 2B)
// ==========================================

let currentPreviewPath = null;

window.openPreviewModal = async function(itemRelPath) {
  currentPreviewPath = itemRelPath;
  const fileName = itemRelPath.split("/").pop();
  const ext = (fileName.split(".").pop() || "").toLowerCase();

  const backdrop = document.getElementById("storage-preview-modal-backdrop");
  const titleEl = document.getElementById("preview-modal-title");
  const iconEl = document.getElementById("preview-modal-icon");
  const badgeEl = document.getElementById("preview-modal-badge");
  const metaEl = document.getElementById("preview-modal-meta");
  const loadingEl = document.getElementById("preview-modal-loading");
  const contentEl = document.getElementById("preview-modal-content");
  const openBtn = document.getElementById("preview-modal-open-btn");
  const downloadBtn = document.getElementById("preview-modal-download-btn");

  if (!backdrop) return;

  if (titleEl) titleEl.textContent = fileName;
  if (iconEl) iconEl.textContent = getFileIcon(fileName, false);
  if (badgeEl) badgeEl.textContent = ext ? `.${ext}` : "file";
  if (metaEl) metaEl.textContent = `Logical Path: Home/${itemRelPath}`;

  const nodeParam = selectedStorageNode ? `&node=${encodeURIComponent(selectedStorageNode)}` : "";
  const previewUrl = `/storage/preview?path=${encodeURIComponent(itemRelPath)}${nodeParam}`;
  const downloadUrl = `/storage/download?path=${encodeURIComponent(itemRelPath)}${nodeParam}`;

  if (openBtn) openBtn.href = previewUrl;
  if (downloadBtn) {
    downloadBtn.href = downloadUrl;
    downloadBtn.setAttribute("download", fileName);
  }

  backdrop.style.display = "flex";
  if (loadingEl) loadingEl.style.display = "flex";
  if (contentEl) contentEl.innerHTML = "";

  const imageExts = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"];
  const textExts = ["txt", "md", "markdown", "json", "yaml", "yml", "py", "js", "css", "sh", "log", "conf", "ini", "env", "csv", "sql", "toml"];
  const safeSourceExts = ["html", "htm", "svg", "xhtml", "xml"];
  const audioExts = ["mp3", "wav", "ogg", "m4a", "flac"];
  const videoExts = ["mp4", "webm", "ogv"];

  try {
    if (ext === "pdf") {
      // PDF Viewer via native browser PDF viewer inside iframe
      if (loadingEl) loadingEl.style.display = "none";
      contentEl.innerHTML = `
        <iframe src="${previewUrl}#toolbar=0" class="preview-iframe" title="${escapeHtml(fileName)}"></iframe>
      `;
    } else if (imageExts.includes(ext)) {
      // Image Viewer
      if (loadingEl) loadingEl.style.display = "none";
      contentEl.innerHTML = `
        <div class="preview-img-wrap">
          <img src="${previewUrl}" class="preview-img" alt="${escapeHtml(fileName)}" onload="document.getElementById('preview-modal-loading')?.style.setProperty('display', 'none');" onerror="this.parentElement.innerHTML='<div class=\\'cell-muted mono\\'>Failed to load image.</div>';" />
        </div>
      `;
    } else if (safeSourceExts.includes(ext)) {
      // Untrusted HTML & SVG: Display sanitized code source view to prevent XSS execution
      const resp = await fetch(previewUrl);
      if (!resp.ok) {
        throw new Error(`Server returned HTTP ${resp.status}`);
      }
      const rawText = await resp.text();
      if (loadingEl) loadingEl.style.display = "none";
      contentEl.innerHTML = `
        <div class="preview-text-wrap">
          <div class="cell-muted mono" style="font-size:11px; margin-bottom:12px; padding:6px 10px; background:rgba(255,255,255,0.04); border-left: 3px solid var(--status-blue, #58a6ff); border-radius: 4px;">
            🛡️ Safe Source Preview — Active script execution is disabled for untrusted markup (${ext.toUpperCase()}).
          </div>
          <pre class="preview-code mono">${escapeHtml(rawText)}</pre>
        </div>
      `;
    } else if (textExts.includes(ext) || ext === "") {
      // Plain-text, Markdown, Code, Config files
      const resp = await fetch(previewUrl);
      if (!resp.ok) {
        throw new Error(`Server returned HTTP ${resp.status}`);
      }
      const textContent = await resp.text();
      if (loadingEl) loadingEl.style.display = "none";
      contentEl.innerHTML = `
        <div class="preview-text-wrap">
          <pre class="preview-code mono">${escapeHtml(textContent)}</pre>
        </div>
      `;
    } else if (audioExts.includes(ext)) {
      if (loadingEl) loadingEl.style.display = "none";
      contentEl.innerHTML = `
        <div class="preview-media-wrap">
          <audio controls autoplay src="${previewUrl}" class="preview-audio"></audio>
        </div>
      `;
    } else if (videoExts.includes(ext)) {
      if (loadingEl) loadingEl.style.display = "none";
      contentEl.innerHTML = `
        <div class="preview-media-wrap">
          <video controls autoplay src="${previewUrl}" class="preview-video"></video>
        </div>
      `;
    } else {
      // Unsupported file format
      if (loadingEl) loadingEl.style.display = "none";
      contentEl.innerHTML = `
        <div class="preview-unsupported">
          <div class="preview-unsupported-icon">${getFileIcon(fileName, false)}</div>
          <div class="preview-unsupported-title">No preview available for .${escapeHtml(ext)} files</div>
          <p class="mono cell-muted" style="font-size: 12px; max-width: 360px;">
            This file type cannot be previewed directly in the browser. You can download the file to inspect it on your local device.
          </p>
          <a href="${downloadUrl}" download="${escapeHtml(fileName)}" class="btn btn-primary" style="margin-top: 8px;">Download File</a>
        </div>
      `;
    }
  } catch (err) {
    if (loadingEl) loadingEl.style.display = "none";
    contentEl.innerHTML = `
      <div class="preview-unsupported">
        <div class="preview-unsupported-icon" style="color: #f85149;">⚠️</div>
        <div class="preview-unsupported-title">Preview Unavailable</div>
        <p class="mono cell-muted" style="font-size: 12px; max-width: 400px;">
          ${escapeHtml(err.message || "Failed to load document preview.")}
        </p>
        <a href="${downloadUrl}" download="${escapeHtml(fileName)}" class="btn btn-sm btn-secondary" style="margin-top: 8px;">Download File Instead</a>
      </div>
    `;
  }
};

window.closePreviewModal = function() {
  const backdrop = document.getElementById("storage-preview-modal-backdrop");
  const contentEl = document.getElementById("preview-modal-content");
  if (backdrop) backdrop.style.display = "none";
  if (contentEl) contentEl.innerHTML = "";
  currentPreviewPath = null;
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

// Render Status Badge with Dot Indicator for Applications
function renderAppStatusBadge(state) {
  const s = (state || "").toUpperCase();
  let badgeClass = "status-stopped";
  let label = s || "UNKNOWN";

  if (s === "RUNNING") {
    badgeClass = "status-running";
    label = "Running";
  } else if (["DEPLOYING", "QUEUED", "CLAIMED", "RECOVERING"].includes(s)) {
    badgeClass = "status-deploying";
    label = s === "DEPLOYING" ? "Deploying" : (s === "QUEUED" ? "Queued" : s);
  } else if (["FAILED", "REJECTED", "TIMEOUT", "ERROR"].includes(s)) {
    badgeClass = "status-failed";
    label = "Failed";
  } else if (s === "STOPPED") {
    badgeClass = "status-stopped";
    label = "Stopped";
  } else if (s === "CREATED") {
    badgeClass = "status-stopped";
    label = "Created";
  }

  return `<span class="app-status-badge ${badgeClass}"><span class="status-dot"></span><span>${escapeHtml(label)}</span></span>`;
}

function updateAppsSummaryMetrics(apps) {
  const total = apps.length;
  const running = apps.filter(a => (a.status || '').toUpperCase() === 'RUNNING').length;
  const stopped = apps.filter(a => ['STOPPED', 'CREATED', 'REMOVED'].includes((a.status || '').toUpperCase())).length;
  const failed = apps.filter(a => ['FAILED', 'ERROR', 'REJECTED'].includes((a.status || '').toUpperCase())).length;

  const totalEl = document.getElementById("apps-metric-total");
  const runningEl = document.getElementById("apps-metric-running");
  const stoppedEl = document.getElementById("apps-metric-stopped");
  const failedEl = document.getElementById("apps-metric-failed");

  if (totalEl) totalEl.textContent = total;
  if (runningEl) runningEl.textContent = running;
  if (stoppedEl) stoppedEl.textContent = stopped;
  if (failedEl) failedEl.textContent = failed;
}

window.filterAppsList = function(query) {
  const q = (query || "").toLowerCase().trim();
  const rows = document.querySelectorAll(".apps-table tbody tr.app-row");
  const cards = document.querySelectorAll(".apps-cards-container .app-card");

  rows.forEach(r => {
    const text = r.textContent.toLowerCase();
    const match = !q || text.includes(q);
    r.style.display = match ? "" : "none";
    const appId = r.getAttribute("data-app-id");
    if (appId) {
      const errRow = document.querySelector(`.apps-table tbody tr.app-error-row[data-app-id="${appId}"]`);
      if (errRow) errRow.style.display = match ? "" : "none";
    }
  });

  cards.forEach(c => {
    const text = c.textContent.toLowerCase();
    c.style.display = !q || text.includes(q) ? "" : "none";
  });
};

window.openDeployModal = function() {
  window.showDeployContainer();
};

async function loadApps() {
  const tbody = document.getElementById("apps-tbody");
  const cardsContainer = document.getElementById("apps-cards-container");
  const countHeader = document.getElementById("apps-count-header");
  const targetSelect = document.getElementById("app-target");

  try {
    if (tbody && (!cachedApps || cachedApps.length === 0)) {
      tbody.innerHTML = `<tr><td colspan="6" class="cell-muted" style="text-align: center; padding: 24px;"><span class="pulse-spinner"></span> Loading applications...</td></tr>`;
    }
    if (cardsContainer && (!cachedApps || cachedApps.length === 0)) {
      cardsContainer.innerHTML = `<div class="apps-loading-state"><span class="pulse-spinner"></span><span>Loading applications...</span></div>`;
    }

    const res = await apiFetch("/api/apps");
    cachedApps = res.apps || [];

    updateAppsSummaryMetrics(cachedApps);

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
      const emptyHtml = `
        <div class="apps-empty-state">
          <div class="apps-empty-icon">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path>
              <polyline points="3.27 6.96 12 12.01 20.73 6.96"></polyline>
              <line x1="12" y1="22.08" x2="12" y2="12"></line>
            </svg>
          </div>
          <div class="apps-empty-title">No Applications Deployed</div>
          <div class="apps-empty-desc">Deploy Git repositories, static sites, or containerized APIs across cluster nodes with reverse proxy routing.</div>
          <div class="apps-empty-action">
            <button class="btn btn-primary" onclick="showDeployContainer()">Deploy Application</button>
          </div>
        </div>
      `;
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="6">${emptyHtml}</td></tr>`;
      }
      if (cardsContainer) {
        cardsContainer.innerHTML = emptyHtml;
      }
      return;
    }

    // Desktop Table HTML
    if (tbody) {
      tbody.innerHTML = cachedApps.map(app => {
        const statusBadge = renderAppStatusBadge(app.status);
        const hostPort = app.port || app.host_port || "-";
        const contPort = app.container_port || "-";
        const portDisplay = hostPort !== "-" ? `${hostPort} → ${contPort}` : `${contPort}`;
        const nodeDisplay = app.selected_node || app.target || "auto";
        const isFailed = ["FAILED", "ERROR"].includes((app.status || "").toUpperCase()) || Boolean(app.failure_reason);

        const isGithub = app.source && app.source.type === "github";
        const isImage = (app.source && app.source.type === "image") || Boolean(app.image);

        let sourceBadge = `<span class="app-source-pill">Manual</span>`;
        if (isGithub) {
          const repoName = app.source.repository || 'repo';
          const shortRepo = repoName.includes('/') ? repoName.split('/')[1] : repoName;
          const branch = app.source.branch || 'main';
          sourceBadge = `<span class="app-source-pill" title="GitHub: ${escapeHtml(repoName)}@${escapeHtml(branch)}"><svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg> ${escapeHtml(shortRepo)}</span>`;
        } else if (isImage) {
          const imgRef = app.image || app.source?.image || 'image';
          const shortImg = imgRef.includes('/') ? imgRef.split('/').slice(-1)[0] : imgRef;
          sourceBadge = `<span class="app-source-pill" title="Image: ${escapeHtml(imgRef)}">${escapeHtml(shortImg)}</span>`;
        }

        const routePath = app.route?.path || (app.route ? app.route.path : "-");
        const routeDisplay = routePath && routePath !== "-"
          ? `<a href="${routePath}/" target="_blank" class="app-route-link" title="Open ${escapeHtml(routePath)}/"><span>${escapeHtml(routePath)}/</span><svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><path d="M3.75 2h3.5a.75.75 0 0 1 0 1.5h-3.5a.25.25 0 0 0-.25.25v8.5c0 .138.112.25.25.25h8.5a.25.25 0 0 0 .25-.25v-3.5a.75.75 0 0 1 1.5 0v3.5A1.75 1.75 0 0 1 12.25 14h-8.5A1.75 1.75 0 0 1 2 12.25v-8.5C2 2.784 2.784 2 3.75 2Zm6.5 0a.75.75 0 0 1 .75-.75h3.5a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0V3.56l-5.22 5.22a.75.75 0 0 1-1.06-1.06l5.22-5.22H11a.75.75 0 0 1-.75-.75Z"/></svg></a>`
          : `<span class="cell-muted mono">—</span>`;

        const canDeploy = ["CREATED", "STOPPED", "FAILED"].includes(app.status);
        const canRedeploy = isGithub && ["RUNNING", "FAILED", "STOPPED"].includes(app.status);
        const canStop = ["RUNNING"].includes(app.status);
        const canRestart = ["RUNNING", "STOPPED"].includes(app.status);

        let rowHtml = `
          <tr class="app-row" data-app-id="${escapeHtml(app.app_id)}">
            <td>
              <div class="app-title-group">
                <a href="javascript:void(0)" onclick="openAppDetails('${escapeHtml(app.app_id)}')" class="app-name-link">${escapeHtml(app.name)}</a>
                <div class="app-meta-sub">
                  <span class="app-id-pill">${escapeHtml(app.app_id)}</span>
                  ${app.runtime ? `<span class="app-runtime-tag">${escapeHtml(app.runtime)}</span>` : ''}
                </div>
              </div>
            </td>
            <td>${statusBadge}</td>
            <td>${sourceBadge}</td>
            <td>${routeDisplay}</td>
            <td>
              <div class="app-target-group">
                <span class="app-node-label" title="${escapeHtml(nodeDisplay)}">${escapeHtml(nodeDisplay)}</span>
                <span class="app-port-label">${escapeHtml(portDisplay)}</span>
              </div>
            </td>
            <td style="text-align: right;">
              <div class="app-actions-group">
                <button class="btn btn-sm btn-subtle" onclick="openAppDetails('${escapeHtml(app.app_id)}')">Details</button>
                ${canRedeploy ? `<button class="btn btn-sm btn-primary" onclick="redeployApp('${escapeHtml(app.app_id)}')">Redeploy</button>` : ''}
                ${(!canRedeploy && canDeploy) ? `<button class="btn btn-sm btn-primary" onclick="deployApp('${escapeHtml(app.app_id)}')">Deploy</button>` : ''}
                ${canRestart ? `<button class="btn btn-sm btn-subtle" onclick="restartApp('${escapeHtml(app.app_id)}')">Restart</button>` : ''}
                ${canStop ? `<button class="btn btn-sm btn-subtle" onclick="stopApp('${escapeHtml(app.app_id)}')">Stop</button>` : ''}
                <button class="btn btn-sm btn-danger" onclick="deleteApp('${escapeHtml(app.app_id)}')">Delete</button>
              </div>
            </td>
          </tr>
        `;

        if (isFailed && app.failure_reason) {
          rowHtml += `
            <tr class="app-error-row" data-app-id="${escapeHtml(app.app_id)}">
              <td colspan="6">
                <div class="app-error-callout">
                  <span class="app-error-badge">Deployment Failed</span>
                  <span class="app-error-msg">${escapeHtml(app.failure_reason)}</span>
                </div>
              </td>
            </tr>
          `;
        }

        return rowHtml;
      }).join("");
    }

    // Mobile Cards HTML
    if (cardsContainer) {
      cardsContainer.innerHTML = cachedApps.map(app => {
        const statusBadge = renderAppStatusBadge(app.status);
        const hostPort = app.port || app.host_port || "-";
        const contPort = app.container_port || "-";
        const portDisplay = hostPort !== "-" ? `${hostPort} → ${contPort}` : `${contPort}`;
        const nodeDisplay = app.selected_node || app.target || "auto";
        const isFailed = ["FAILED", "ERROR"].includes((app.status || "").toUpperCase()) || Boolean(app.failure_reason);

        const isGithub = app.source && app.source.type === "github";
        const isImage = (app.source && app.source.type === "image") || Boolean(app.image);

        let sourceText = "Manual";
        if (isGithub) {
          sourceText = `GitHub: ${app.source.repository || 'repo'}`;
        } else if (isImage) {
          sourceText = `Image: ${app.image || app.source?.image || 'container'}`;
        }

        const routePath = app.route?.path || (app.route ? app.route.path : "-");
        const routeDisplay = routePath && routePath !== "-"
          ? `<a href="${routePath}/" target="_blank" class="app-route-link"><span>${escapeHtml(routePath)}/</span></a>`
          : `<span class="cell-muted mono">—</span>`;

        const canDeploy = ["CREATED", "STOPPED", "FAILED"].includes(app.status);
        const canRedeploy = isGithub && ["RUNNING", "FAILED", "STOPPED"].includes(app.status);
        const canStop = ["RUNNING"].includes(app.status);
        const canRestart = ["RUNNING", "STOPPED"].includes(app.status);

        return `
          <div class="app-card" data-app-id="${escapeHtml(app.app_id)}">
            <div class="app-card-header">
              <div class="app-title-group">
                <a href="javascript:void(0)" onclick="openAppDetails('${escapeHtml(app.app_id)}')" class="app-name-link" style="font-size: 14px;">${escapeHtml(app.name)}</a>
                <div class="app-meta-sub">
                  <span class="app-id-pill">${escapeHtml(app.app_id)}</span>
                  ${app.runtime ? `<span class="app-runtime-tag">${escapeHtml(app.runtime)}</span>` : ''}
                </div>
              </div>
              <div>${statusBadge}</div>
            </div>

            <div class="app-card-grid">
              <div class="app-card-grid-item">
                <span class="app-card-grid-label">Source</span>
                <span class="mono cell-muted" style="font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(sourceText)}</span>
              </div>
              <div class="app-card-grid-item">
                <span class="app-card-grid-label">Route</span>
                <div>${routeDisplay}</div>
              </div>
              <div class="app-card-grid-item">
                <span class="app-card-grid-label">Assigned Node</span>
                <span class="mono" style="font-size: 11px;">${escapeHtml(nodeDisplay)}</span>
              </div>
              <div class="app-card-grid-item">
                <span class="app-card-grid-label">Port Mapping</span>
                <span class="mono cell-muted" style="font-size: 11px;">${escapeHtml(portDisplay)}</span>
              </div>
            </div>

            ${(isFailed && app.failure_reason) ? `
              <div class="app-card-error">
                <strong>Deployment Error:</strong> ${escapeHtml(app.failure_reason)}
              </div>
            ` : ''}

            <div class="app-card-footer">
              <button class="btn btn-sm btn-subtle" onclick="openAppDetails('${escapeHtml(app.app_id)}')">Details</button>
              ${canRedeploy ? `<button class="btn btn-sm btn-primary" onclick="redeployApp('${escapeHtml(app.app_id)}')">Redeploy</button>` : ''}
              ${(!canRedeploy && canDeploy) ? `<button class="btn btn-sm btn-primary" onclick="deployApp('${escapeHtml(app.app_id)}')">Deploy</button>` : ''}
              ${canRestart ? `<button class="btn btn-sm btn-subtle" onclick="restartApp('${escapeHtml(app.app_id)}')">Restart</button>` : ''}
              ${canStop ? `<button class="btn btn-sm btn-subtle" onclick="stopApp('${escapeHtml(app.app_id)}')">Stop</button>` : ''}
              <button class="btn btn-sm btn-danger" onclick="deleteApp('${escapeHtml(app.app_id)}')">Delete</button>
            </div>
          </div>
        `;
      }).join("");
    }

    if (currentActiveAppId) {
      const currentApp = cachedApps.find(a => a.app_id === currentActiveAppId);
      if (currentApp) {
        renderAppDetailsTabs(currentApp);
      }
    }

    updateLastRefreshed();
  } catch (err) {
    console.error("Failed to load apps:", err);
    const errHtml = `
      <div class="apps-error-state">
        <div class="apps-error-msg-wrap">
          <span class="text-offline">Error loading applications: ${escapeHtml(err.message)}</span>
        </div>
        <button class="btn btn-sm" onclick="loadApps()">Retry</button>
      </div>
    `;
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="6">${errHtml}</td></tr>`;
    }
    if (cardsContainer) {
      cardsContainer.innerHTML = errHtml;
    }
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

// -----------------------------------------------------------------------------
// Unified Application Deployment Flow (Image / GitHub)
// -----------------------------------------------------------------------------

window.showDeployContainer = function() {
  const container = document.getElementById("unified-deploy-container");
  if (container) {
    container.style.display = "block";
    container.scrollIntoView({ behavior: "smooth" });
  }
};

window.hideDeployContainer = function() {
  const container = document.getElementById("unified-deploy-container");
  if (container) container.style.display = "none";
};

// Backwards-compatible aliases
window.showGithubDeploy = window.showDeployContainer;
window.hideGithubDeploy = window.hideDeployContainer;
window.toggleManualDeploy = function() {
  const container = document.getElementById("unified-deploy-container");
  if (container) {
    container.style.display = container.style.display === "none" ? "block" : "none";
    if (container.style.display === "block") container.scrollIntoView({ behavior: "smooth" });
  }
};

window.toggleDeploySourceType = function() {
  const sourceType = document.querySelector('input[name="deploy-source-type"]:checked')?.value || "image";
  const imageFields = document.getElementById("source-image-fields");
  const ghFields = document.getElementById("source-github-fields");
  const imgInput = document.getElementById("deploy-image");
  const ghRepoInput = document.getElementById("gh-repo");

  if (sourceType === "image") {
    if (imageFields) imageFields.style.display = "block";
    if (ghFields) ghFields.style.display = "none";
    if (imgInput) imgInput.required = true;
    if (ghRepoInput) ghRepoInput.required = false;
  } else {
    if (imageFields) imageFields.style.display = "none";
    if (ghFields) ghFields.style.display = "block";
    if (imgInput) imgInput.required = false;
    if (ghRepoInput) ghRepoInput.required = true;
  }
};

window.onImageInputChange = function() {
  const imgInput = document.getElementById("deploy-image");
  const nameInput = document.getElementById("deploy-app-name");
  const routeInput = document.getElementById("deploy-route");
  if (!imgInput || !nameInput) return;

  const raw = imgInput.value.trim();
  if (!raw) return;

  // Extract repo name e.g. akshat/exambuddy:latest -> exambuddy, nginx:alpine -> nginx
  let name = raw.split("/").pop().split(":")[0].toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  if (name && (!nameInput.value || nameInput.dataset.autofilled === "true")) {
    nameInput.value = name;
    nameInput.dataset.autofilled = "true";
    if (routeInput && (!routeInput.value || routeInput.dataset.autofilled === "true")) {
      routeInput.value = `/${name}`;
      routeInput.dataset.autofilled = "true";
    }
  }
};

function isProbableSecret(key) {
  if (!key) return false;
  const upper = key.toUpperCase();
  const secretKeywords = ["SECRET", "KEY", "TOKEN", "PASSWORD", "PASS", "AUTH", "CREDENTIAL", "PRIVATE", "DATABASE_URL"];
  return secretKeywords.some(kw => upper.includes(kw));
}

window.addEnvVariableRow = function(key = "", value = "", isSecret = null) {
  const container = document.getElementById("unified-env-list");
  if (!container) return;

  if (isSecret === null) {
    isSecret = isProbableSecret(key);
  }

  const rowId = `unified-env-row-${Date.now()}-${Math.random().toString(36).substr(2, 4)}`;
  const div = document.createElement("div");
  div.id = rowId;
  div.className = "unified-env-row";
  div.style.display = "flex";
  div.style.gap = "8px";
  div.style.alignItems = "center";
  div.style.marginBottom = "6px";

  const inputType = isSecret ? "password" : "text";

  div.innerHTML = `
    <input type="text" class="form-input mono env-row-key" placeholder="VARIABLE_NAME" value="${escapeHtml(key)}" style="flex: 1; min-width: 140px;" required pattern="^[a-zA-Z_][a-zA-Z0-9_]*$">
    <div style="flex: 2; min-width: 180px; position: relative; display: flex; align-items: center;">
      <input type="${inputType}" class="form-input mono env-row-val" placeholder="Value..." value="${escapeHtml(value)}" style="width: 100%; padding-right: 32px;">
      <button type="button" class="btn btn-sm" onclick="toggleEnvRowVisibility('${rowId}')" title="Show/Hide" style="position: absolute; right: 2px; padding: 2px 6px; font-size: 11px; background: transparent; border: none; color: var(--text-muted); cursor: pointer;">👁</button>
    </div>
    <label class="modal-label mono" style="display: inline-flex; align-items: center; gap: 4px; cursor: pointer; white-space: nowrap; margin: 0; font-size: 11px;">
      <input type="checkbox" class="env-row-sec" ${isSecret ? 'checked' : ''} onchange="onEnvRowSecretChange('${rowId}')"> Secret
    </label>
    <button type="button" class="btn btn-sm btn-danger" onclick="document.getElementById('${rowId}')?.remove()" title="Remove variable">✕</button>
  `;
  container.appendChild(div);
};

window.toggleEnvRowVisibility = function(rowId) {
  const row = document.getElementById(rowId);
  if (!row) return;
  const input = row.querySelector(".env-row-val");
  if (!input) return;
  input.type = input.type === "password" ? "text" : "password";
};

window.onEnvRowSecretChange = function(rowId) {
  const row = document.getElementById(rowId);
  if (!row) return;
  const chk = row.querySelector(".env-row-sec");
  const input = row.querySelector(".env-row-val");
  if (chk && input) {
    input.type = chk.checked ? "password" : "text";
  }
};

window.showEnvImportModal = function() {
  const modal = document.getElementById("modal-env-import");
  if (modal) modal.style.display = "flex";
};

window.hideEnvImportModal = function() {
  const modal = document.getElementById("modal-env-import");
  if (modal) modal.style.display = "none";
};

window.submitEnvImport = async function() {
  const textarea = document.getElementById("env-import-textarea");
  if (!textarea) return;
  const content = textarea.value;
  if (!content.trim()) {
    hideEnvImportModal();
    return;
  }

  try {
    const res = await apiFetch("/api/apps/env/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content })
    });

    if (res.env_vars) {
      for (const [k, v] of Object.entries(res.env_vars)) {
        addEnvVariableRow(k, v.value, v.is_secret);
      }
      textarea.value = "";
      hideEnvImportModal();
    }
  } catch (err) {
    showCustomAlert({ title: "Import Failed", message: err.message, isError: true });
  }
};

// Inspect GitHub Repo & Detect Blueprint / Dockerfile
window.inspectGithubRepo = async function() {
  const repoInput = document.getElementById("gh-repo");
  const branchInput = document.getElementById("gh-branch");
  const rootDirInput = document.getElementById("gh-root-dir");
  const banner = document.getElementById("gh-detection-banner");
  const appNameInput = document.getElementById("deploy-app-name");
  const routeInput = document.getElementById("deploy-route");
  const portInput = document.getElementById("deploy-port");
  const dockerfileInput = document.getElementById("gh-dockerfile");

  const repo = repoInput ? repoInput.value.trim() : "";
  const branch = branchInput ? branchInput.value.trim() || "main" : "main";
  const rootDir = rootDirInput ? rootDirInput.value.trim() || "." : ".";

  if (!repo || !repo.includes("/")) {
    showCustomAlert({ title: "Invalid Repository", message: "Please specify repository in 'owner/repository' format.", isError: true });
    return;
  }

  if (banner) {
    banner.style.display = "block";
    banner.style.backgroundColor = "rgba(88, 166, 255, 0.1)";
    banner.style.border = "1px solid rgba(88, 166, 255, 0.3)";
    banner.style.color = "var(--text-main)";
    banner.textContent = `Inspecting repository ${repo} on branch ${branch}...`;
  }

  try {
    const res = await apiFetch("/api/apps/github/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository: repo, branch, root_directory: rootDir })
    });

    detectedBlueprint = res.blueprint;
    const repoName = repo.split("/")[1].toLowerCase().replace(/[^a-z0-9_-]/g, '-');

    if (res.has_blueprint && res.blueprint) {
      const svc = res.blueprint.services?.[0] || {};
      if (appNameInput) appNameInput.value = svc.name || repoName;
      if (routeInput) routeInput.value = svc.route || `/${svc.name || repoName}`;
      if (rootDirInput) rootDirInput.value = svc.rootDir || rootDir;
      if (dockerfileInput) dockerfileInput.value = svc.dockerfile || "Dockerfile";
      if (svc.port && portInput) portInput.value = svc.port;

      if (banner) {
        banner.style.backgroundColor = "rgba(63, 185, 80, 0.12)";
        banner.style.border = "1px solid rgba(63, 185, 80, 0.35)";
        banner.style.color = "var(--status-green)";
        banner.textContent = `✓ Detected PersonalServer Blueprint (personalserver.yaml) for '${svc.name}'. Pre-configured services and environment.`;
      }

      if (Array.isArray(svc.envVars)) {
        svc.envVars.forEach(ev => {
          const key = typeof ev === 'object' ? ev.key : ev;
          addEnvVariableRow(key, "", true);
        });
      }
    } else {
      if (appNameInput) appNameInput.value = repoName;
      if (routeInput) routeInput.value = `/${repoName}`;
      if (dockerfileInput) dockerfileInput.value = "Dockerfile";
      if (res.detected_port && portInput) portInput.value = res.detected_port;

      if (banner) {
        banner.style.backgroundColor = "rgba(210, 153, 34, 0.12)";
        banner.style.border = "1px solid rgba(210, 153, 34, 0.35)";
        banner.style.color = "var(--status-yellow)";
        banner.textContent = `✓ Detected Docker configuration (${res.dockerfile_path || 'Dockerfile'}). Port ${res.detected_port || 5000} auto-detected.`;
      }
    }
  } catch (err) {
    if (banner) {
      banner.style.backgroundColor = "rgba(248, 81, 73, 0.12)";
      banner.style.border = "1px solid rgba(248, 81, 73, 0.35)";
      banner.style.color = "var(--status-red)";
      banner.textContent = `Inspection notice: ${err.message}`;
    }
  }
};

// Unified Application Deploy Form Submit
document.getElementById("app-unified-deploy-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const sourceType = document.querySelector('input[name="deploy-source-type"]:checked')?.value || "image";
  const name = document.getElementById("deploy-app-name").value.trim();
  const routePath = document.getElementById("deploy-route").value.trim() || `/${name.toLowerCase()}`;
  const runtime = document.getElementById("deploy-runtime")?.value || "auto";
  const cpuLimit = document.getElementById("deploy-cpu")?.value.trim() || "0.5";
  const memLimit = document.getElementById("deploy-mem")?.value.trim() || "256m";
  const rawPort = document.getElementById("deploy-port")?.value.trim();
  const containerPort = rawPort ? parseInt(rawPort, 10) : null;

  // Collect environment variables from unified list
  const envVars = {};
  const rows = document.querySelectorAll("#unified-env-list .unified-env-row");
  rows.forEach(r => {
    const k = r.querySelector(".env-row-key")?.value.trim();
    const v = r.querySelector(".env-row-val")?.value || "";
    const isSec = r.querySelector(".env-row-sec")?.checked ?? isProbableSecret(k);
    if (k) {
      envVars[k] = { value: v, is_secret: isSec };
    }
  });

  let payload = {
    name,
    route: {
      enabled: true,
      type: "path",
      path: routePath,
      strip_prefix: true,
      public_access: true
    },
    env_vars: envVars,
    target: "auto",
    runtime: runtime,
    cpu_limit: cpuLimit,
    memory_limit: memLimit
  };

  if (containerPort) {
    payload.container_port = containerPort;
  }

  if (sourceType === "image") {
    const image = document.getElementById("deploy-image").value.trim();
    if (!image) {
      showCustomAlert({ title: "Image Required", message: "Please enter a container image reference.", isError: true });
      return;
    }
    payload.image = image;
    payload.source = { type: "image", image };
  } else {
    const repo = document.getElementById("gh-repo").value.trim();
    const branch = document.getElementById("gh-branch").value.trim() || "main";
    const rootDir = document.getElementById("gh-root-dir").value.trim() || ".";
    payload.source = {
      type: "github",
      repository: repo,
      branch,
      root_directory: rootDir
    };
    payload.blueprint = detectedBlueprint;
  }

  try {
    const submitBtn = document.getElementById("btn-submit-unified-deploy");
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "Deploying...";
    }

    const createRes = await apiFetch("/api/apps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    hideDeployContainer();
    loadApps();

    if (createRes.app_id) {
      deployApp(createRes.app_id);
    }
  } catch (err) {
    showCustomAlert({ title: "Deployment Error", message: err.message, isError: true });
  } finally {
    const submitBtn = document.getElementById("btn-submit-unified-deploy");
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Deploy Application";
    }
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
    <div class="sys-kv-row"><span class="sys-kv-k">Created At</span><span class="sys-kv-v cell-muted">${formatDateIST(app.created_at)}</span></div>
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
    let stageBreakdown = "";
    if (d.stage_timings && typeof d.stage_timings === 'object') {
      const parts = Object.entries(d.stage_timings)
        .filter(([_, v]) => typeof v === 'number' && v > 0)
        .map(([k, v]) => `${k.replace(/_ms$/, '')}: ${v}ms`);
      if (parts.length > 0) {
        stageBreakdown = `<div style="font-size: 10px; color: var(--text-muted); margin-top: 3px; font-family: var(--font-mono);">${parts.join(" · ")}</div>`;
      }
    }
    return `
      <tr>
        <td class="mono" style="font-weight: 600;">#${d.number || 1}</td>
        <td class="mono cell-muted">${escapeHtml(d.commit || 'latest')}</td>
        <td class="mono">${escapeHtml(d.branch || 'main')}</td>
        <td class="mono cell-muted">${escapeHtml(d.trigger || 'manual')}</td>
        <td>${statusPill}</td>
        <td class="mono">${durStr}${stageBreakdown}</td>
        <td class="cell-muted">${formatDateIST(d.started_at)}</td>
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
  const shareBackdrop = document.getElementById("storage-share-modal-backdrop");
  if (shareBackdrop && shareBackdrop.style.display !== "none") {
    if (e.key === "Escape") {
      e.preventDefault();
      closeShareModal();
      return;
    }
  }

  const haBackdrop = document.getElementById("ha-details-modal-backdrop");
  if (haBackdrop && haBackdrop.style.display !== "none") {
    if (e.key === "Escape") {
      e.preventDefault();
      closeHADetailsModal();
      return;
    }
  }

  const psBackdrop = document.getElementById("ps-modal-backdrop");
  if (!psBackdrop || psBackdrop.style.display === "none") return;

  if (e.key === "Escape") {
    e.preventDefault();
    closeModal(null);
  } else if (e.key === "Enter") {
    e.preventDefault();
    document.getElementById("ps-modal-confirm-btn")?.click();
  }
});

document.getElementById("storage-share-modal-backdrop")?.addEventListener("click", (e) => {
  if (e.target.id === "storage-share-modal-backdrop") {
    closeShareModal();
  }
});

document.getElementById("ha-details-modal-backdrop")?.addEventListener("click", (e) => {
  if (e.target.id === "ha-details-modal-backdrop") {
    closeHADetailsModal();
  }
});

// Periodic background polling for cluster HA telemetry (15-second interval)
setInterval(() => {
  updateHAStatus(false);
}, 15000);

// Initialize
navigate();

