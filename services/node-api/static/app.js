// PersonalServer v1.0 Client App Logic

let currentPath = "";
let authToken = localStorage.getItem("ps_auth_token") || "";

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

  if (targetView) {
    targetView.classList.add("active");
  }
  if (targetNav) {
    targetNav.classList.add("active");
  }

  const titles = {
    dashboard: "Dashboard Overview",
    nodes: "Cluster Nodes",
    jobs: "Workload Execution",
    storage: "Personal Storage Subsystem"
  };
  if (heading) heading.textContent = titles[hash] || "Administration";

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
    alert("Auth Token saved.");
    navigate();
  }
});

document.getElementById("refresh-btn")?.addEventListener("click", () => {
  navigate();
});

// Modal helpers
window.closeModal = function(modalId) {
  const el = document.getElementById(modalId);
  if (el) el.style.display = "none";
};

function showJobDetail(job) {
  const title = document.getElementById("modal-job-title");
  const jsonPre = document.getElementById("modal-job-json");
  const modal = document.getElementById("modal-job-detail");

  if (title) title.textContent = `Job Details: ${job.id || 'N/A'}`;
  if (jsonPre) jsonPre.textContent = JSON.stringify(job, null, 2);
  if (modal) modal.style.display = "flex";
}

// Format helpers
function formatBytes(bytes) {
  if (bytes === 0 || bytes === "0") return "0 B";
  const num = parseInt(bytes, 10);
  if (isNaN(num)) return bytes || "-";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(num) / Math.log(k));
  return parseFloat((num / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
}

function formatDuration(sec) {
  if (sec === null || sec === undefined) return "-";
  return parseFloat(sec).toFixed(2) + "s";
}

function renderStatusBadge(state) {
  const s = (state || "").toUpperCase();
  if (s === "SUCCEEDED" || s === "ONLINE" || s === "SUCCESS") {
    return `<span class="badge badge-success">${s}</span>`;
  } else if (s === "RUNNING" || s === "CLAIMED" || s === "QUEUED" || s === "RECOVERING") {
    return `<span class="badge badge-warning">${s}</span>`;
  } else if (s === "FAILED" || s === "TIMEOUT" || s === "OFFLINE" || s === "REJECTED") {
    return `<span class="badge badge-danger">${s}</span>`;
  }
  return `<span class="badge badge-neutral">${s || "UNKNOWN"}</span>`;
}

// 1. Dashboard
async function loadDashboard() {
  try {
    const [healthRes, clusterRes, jobsRes, storageRes] = await Promise.allSettled([
      fetch("/health"),
      fetch("/api/cluster", { headers: getHeaders() }),
      fetch("/api/jobs", { headers: getHeaders() }),
      fetch("/storage/usage", { headers: getHeaders() })
    ]);

    // Local Node Health
    if (healthRes.status === "fulfilled" && healthRes.value.ok) {
      const h = await healthRes.value.json();
      document.getElementById("local-node-name").textContent = h.node?.name || "vivo-y31";
      document.getElementById("dash-cpu-cores").textContent = h.system?.cpu_cores || "-";
      document.getElementById("dash-mem-usage").textContent = 
        `${h.system?.memory?.used || '-'} / ${h.system?.memory?.total || '-'}`;
      document.getElementById("dash-disk-free").textContent = 
        `${h.system?.storage?.available || '-'} (${h.system?.storage?.used_percent || '-'} used)`;
      document.getElementById("dash-uptime").textContent = 
        (h.system?.load_average || []).map(x => x.toFixed(2)).join(", ") || "Active";
    }

    // Cluster Info
    if (clusterRes.status === "fulfilled" && clusterRes.value.ok) {
      const c = await clusterRes.value.json();
      const nodes = c.nodes || [];
      const onlineNodes = nodes.filter(n => n.status === "online").length;
      document.getElementById("dash-nodes-online").textContent = `${onlineNodes} / ${nodes.length}`;
      document.getElementById("dash-cluster-status").textContent = onlineNodes > 0 ? "ONLINE" : "DEGRADED";
    }

    // Jobs Info
    if (jobsRes.status === "fulfilled" && jobsRes.value.ok) {
      const jData = await jobsRes.value.json();
      const jobs = jData.jobs || [];
      document.getElementById("dash-jobs-count").textContent = jobs.length;
      const runningCount = jobs.filter(j => j.state === "RUNNING" || j.state === "CLAIMED").length;
      document.getElementById("dash-jobs-sub").textContent = `${runningCount} running`;

      const tbody = document.getElementById("dash-jobs-table");
      if (jobs.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-muted">No workload jobs executed yet.</td></tr>`;
      } else {
        const recent = [...jobs].reverse().slice(0, 5);
        tbody.innerHTML = recent.map(j => `
          <tr>
            <td class="font-mono"><a href="#jobs" onclick="viewJobById('${j.id}')">${j.id.slice(0, 8)}...</a></td>
            <td>${j.type}</td>
            <td class="font-mono">${j.assigned_node || j.target || 'auto'}</td>
            <td>${renderStatusBadge(j.state)}</td>
            <td>${formatDuration(j.execution_duration_sec)}</td>
          </tr>
        `).join("");
      }
    }

    // Storage Info
    if (storageRes.status === "fulfilled" && storageRes.value.ok) {
      const sData = await storageRes.value.json();
      document.getElementById("dash-storage-usage").textContent = formatBytes(sData.used_bytes || 0);
      document.getElementById("dash-storage-sub").textContent = `${sData.files_count || 0} files, ${sData.folders_count || 0} dirs`;
    }

  } catch (err) {
    console.error("Failed to load dashboard data:", err);
  }
}

// 2. Nodes
async function loadNodes() {
  const tbody = document.getElementById("nodes-table-body");
  const targetSelect = document.getElementById("job-target");

  try {
    const res = await fetch("/api/cluster", { headers: getHeaders() });
    if (!res.ok) throw new Error("Cluster API responded with " + res.status);
    const data = await res.json();
    const nodes = data.nodes || [];

    if (nodes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-muted">No nodes registered.</td></tr>`;
      return;
    }

    // Update target dropdown in jobs
    if (targetSelect) {
      const currentVal = targetSelect.value;
      targetSelect.innerHTML = `<option value="auto">auto (Resource-Aware Scheduler)</option>` +
        nodes.map(n => `<option value="${n.id}">${n.name || n.id} (${n.id.slice(0, 8)})</option>`).join("");
      targetSelect.value = currentVal;
    }

    tbody.innerHTML = nodes.map(n => `
      <tr>
        <td class="font-mono">${n.id}</td>
        <td><strong>${n.name || 'node'}</strong></td>
        <td>${n.role || 'worker'}</td>
        <td>${n.platform || '-'} / ${n.arch || '-'}</td>
        <td class="font-mono">${n.cpu_cores || '-'} cores / ${n.memory_mb ? n.memory_mb + 'MB' : '-'}</td>
        <td>${(n.capabilities || []).map(c => `<span class="badge badge-neutral">${c}</span>`).join(" ")}</td>
        <td>${renderStatusBadge(n.status)}</td>
        <td class="font-mono text-muted">${n.last_heartbeat ? new Date(n.last_heartbeat * 1000).toLocaleTimeString() : '-'}</td>
      </tr>
    `).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-danger">Failed to load nodes: ${err.message}</td></tr>`;
  }
}

// 3. Jobs
let allJobsCache = [];

async function loadJobs() {
  const tbody = document.getElementById("jobs-table-body");
  try {
    const res = await fetch("/api/jobs", { headers: getHeaders() });
    if (!res.ok) throw new Error("Jobs API error: " + res.status);
    const data = await res.json();
    allJobsCache = data.jobs || [];

    if (allJobsCache.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-muted">No jobs recorded.</td></tr>`;
      return;
    }

    const sorted = [...allJobsCache].reverse();
    tbody.innerHTML = sorted.map(j => `
      <tr>
        <td class="font-mono"><strong>${j.id}</strong></td>
        <td>${j.type}</td>
        <td class="font-mono">${j.assigned_node || j.target || 'auto'}</td>
        <td>${renderStatusBadge(j.state)}</td>
        <td class="font-mono">${j.attempt || 1}/${j.max_attempts || 3}</td>
        <td class="font-mono">${j.exit_code !== undefined && j.exit_code !== null ? j.exit_code : '-'}</td>
        <td>${formatDuration(j.execution_duration_sec)}</td>
        <td class="font-mono text-muted">${j.created_at ? new Date(j.created_at * 1000).toLocaleTimeString() : '-'}</td>
        <td>
          <button class="btn btn-sm" onclick="viewJobById('${j.id}')">Inspect</button>
        </td>
      </tr>
    `).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" class="text-danger">Failed to load jobs: ${err.message}</td></tr>`;
  }
}

window.viewJobById = function(jobId) {
  const found = allJobsCache.find(j => j.id === jobId);
  if (found) {
    showJobDetail(found);
  } else {
    fetch(`/api/jobs/${jobId}`, { headers: getHeaders() })
      .then(r => r.json())
      .then(j => showJobDetail(j))
      .catch(e => alert("Could not fetch job: " + e.message));
  }
};

// Job Submission Form
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
      alert("Invalid JSON in Parameters field: " + err.message);
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
      alert("Job dispatch failed: " + (result.error || res.statusText));
      return;
    }

    alert(`Job dispatched successfully! ID: ${result.job?.id || result.id}`);
    loadJobs();
  } catch (err) {
    alert("Error dispatching job: " + err.message);
  }
});

// 4. Storage Subsystem
async function loadStorage(path = "") {
  currentPath = path;
  renderBreadcrumbs(path);
  const tbody = document.getElementById("storage-table-body");

  try {
    const res = await fetch(`/storage/list?path=${encodeURIComponent(path)}`, { headers: getHeaders() });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.error || ("HTTP " + res.status));
    }
    const data = await res.json();
    const items = data.items || [];

    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-muted">Directory is empty.</td></tr>`;
      return;
    }

    tbody.innerHTML = items.map(item => {
      const itemRelPath = path ? `${path}/${item.name}` : item.name;
      const isDir = item.is_dir;

      return `
        <tr>
          <td>
            ${isDir 
              ? `<a href="javascript:void(0)" onclick="loadStorage('${itemRelPath}')" style="font-weight:600">📁 ${item.name}</a>`
              : `<span class="font-mono">📄 ${item.name}</span>`
            }
          </td>
          <td><span class="badge badge-neutral">${isDir ? 'directory' : (item.extension || 'file')}</span></td>
          <td class="font-mono">${isDir ? '-' : formatBytes(item.size_bytes)}</td>
          <td class="font-mono text-muted">${item.modified ? new Date(item.modified * 1000).toLocaleString() : '-'}</td>
          <td>
            <div style="display:flex; gap:4px;">
              ${!isDir ? `<a href="/storage/download?path=${encodeURIComponent(itemRelPath)}" class="btn btn-sm btn-primary" download>Download</a>` : ''}
              <button class="btn btn-sm btn-secondary" onclick="renameItem('${itemRelPath}')">Rename</button>
              <button class="btn btn-sm btn-danger" onclick="deleteItem('${itemRelPath}', ${isDir})">Delete</button>
            </div>
          </td>
        </tr>
      `;
    }).join("");

  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-danger">Failed to list directory: ${err.message}</td></tr>`;
  }
}

function renderBreadcrumbs(path) {
  const container = document.getElementById("storage-breadcrumbs");
  if (!container) return;

  const parts = path ? path.split("/").filter(Boolean) : [];
  let html = `<span class="crumb ${parts.length === 0 ? 'active' : ''}" onclick="loadStorage('')">/ storage</span>`;

  let accumulated = "";
  parts.forEach((p, index) => {
    accumulated += (accumulated ? "/" : "") + p;
    const isLast = index === parts.length - 1;
    const clickPath = accumulated;
    html += ` <span class="text-muted">/</span> <span class="crumb ${isLast ? 'active' : ''}" ${!isLast ? `onclick="loadStorage('${clickPath}')"` : ''}>${p}</span>`;
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
    const uploadUrl = `/storage/upload?path=${encodeURIComponent(currentPath)}`;
    const res = await fetch(uploadUrl, {
      method: "POST",
      headers: getHeaders(),
      body: formData
    });

    const result = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert("Upload failed: " + (result.error || res.statusText));
      return;
    }

    alert(`Uploaded "${file.name}" successfully!`);
    e.target.value = "";
    loadStorage(currentPath);
  } catch (err) {
    alert("Error uploading file: " + err.message);
  }
});

// Create folder
document.getElementById("create-folder-btn")?.addEventListener("click", async () => {
  const name = prompt("Enter new folder name:");
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
      alert("Failed to create folder: " + (result.error || res.statusText));
      return;
    }

    loadStorage(currentPath);
  } catch (err) {
    alert("Error creating folder: " + err.message);
  }
});

// Rename
window.renameItem = async function(itemRelPath) {
  const oldName = itemRelPath.split("/").pop();
  const newName = prompt("Rename item to:", oldName);
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
    alert("Error renaming: " + err.message);
  }
};

// Delete
window.deleteItem = async function(itemRelPath, isDir) {
  const itemName = itemRelPath.split("/").pop();
  if (!confirm(`Are you sure you want to delete ${isDir ? 'folder' : 'file'} "${itemName}"?`)) {
    return;
  }

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
    alert("Error deleting item: " + err.message);
  }
};

// Initial navigation
navigate();
