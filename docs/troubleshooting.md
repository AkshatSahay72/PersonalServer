# Troubleshooting & Runbook (v1.0)

This runbook covers diagnosis and resolution steps for common operational issues.

---

## 1. Node Shows as OFFLINE on Controller

### Symptoms
`python controller/controller.py nodes` shows node in state `OFFLINE`.

### Diagnosis & Fix
1. Verify Tailscale connectivity:
   ```bash
   ping 100.85.108.5
   ```
2. Check if Node Agent heartbeat is reporting:
   ```bash
   ssh -p 8022 u0_a244@100.85.108.5 "cd ~/PersonalServer && python agent/node-agent.py heartbeat"
   ```
3. Check Controller logs to ensure heartbeat timeout has not elapsed.

---

## 2. Web Interface Fails to Load via Cloudflare

### Symptoms
Browser returns error 502 Bad Gateway or 403 Forbidden.

### Diagnosis & Fix
1. If receiving Cloudflare Error 1010, ensure request contains a valid browser `User-Agent`.
2. Check if `cloudflared` is running on the Vivo Y31:
   ```bash
   ssh -p 8022 u0_a244@100.85.108.5 "cd ~/PersonalServer && bash scripts/status.sh"
   ```
3. Restart services if needed:
   ```bash
   bash scripts/restart.sh
   ```

---

## 3. Storage Upload or Download Errors

### Symptoms
`HTTP 403 Forbidden` or `HTTP 413 Payload Too Large`.

### Diagnosis & Fix
1. Ensure the file name does not contain illegal characters (`\`, `/`, `:`, `*`, `?`, `"`, `<`, `>`, `|`, null bytes).
2. Verify upload size is under the 100MB limit.
3. Check storage free space via `python agent/node-agent.py health`.
