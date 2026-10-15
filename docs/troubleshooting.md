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

## 2. Web Interface Fails or Cloudflare Reports Node 1 is Down

### Symptoms
- Cloudflare Zero Trust Dashboard shows Tunnel / Connector status as **DOWN** or **INACTIVE**.
- Web browser visiting `https://server.akshatsahay.space` or `https://api.akshatsahay.space` returns:
  - **Error 521: Web server is down**
  - **Error 502: Bad Gateway**
  - **Error 1033: Argo Tunnel error**
  - **Error 1010: Access Denied**

### Common Causes & Diagnosis

#### Cause A: Android Killed the `cloudflared` Background Process
Android's battery management (especially Vivo Funtouch OS) and Android 12+ Phantom Process Killer automatically terminate background processes in Termux when the screen turns off.
1. Check process status on Node 1:
   ```bash
   ssh -p 8022 u0_a244@100.85.108.5 "cd ~/PersonalServer && bash scripts/status.sh"
   ```
2. If `cloudflared` is stopped, acquire a wake lock and restart:
   ```bash
   termux-wake-lock
   bash scripts/start.sh
   ```
   *(Ensure Termux has "Allow background activity / Unrestricted" in Android Battery Settings).*

#### Cause B: QUIC (UDP 7844) Blocked by Mobile Carrier or Router
`cloudflared` defaults to QUIC protocol over UDP port 7844. Many mobile carriers (Jio, Airtel, Vi) and Wi-Fi networks block or throttle outbound UDP 7844, causing the tunnel to constantly disconnect and report DOWN.
1. Check tunnel logs:
   ```bash
   tail -n 30 logs/cloudflared.log
   ```
   If you see `Failed to create new quic connection` or repeated `retrying in ...`:
2. Force HTTP/2 over standard TCP port 443 (which is never blocked):
   ```bash
   cloudflared tunnel run --protocol http2 --token-file config/secrets/cloudflared.token
   ```
   *(Note: `scripts/start.sh` automatically defaults to `--protocol http2`)*.

#### Cause C: Cloudflare Ingress Set to `localhost:8080` (IPv6 Loopback Refusal)
In Cloudflare Zero Trust Dashboard (`Networks > Tunnels > [Your Tunnel] > Public Hostname`):
- If the service URL is `http://localhost:8080`, `cloudflared` may resolve `localhost` to IPv6 `::1`.
- Node API is bound to IPv4 (`0.0.0.0:8080`), so attempts to connect to `[::1]:8080` get connection refused, triggering **Cloudflare Error 521 (Web Server Down)**.
- **Fix**: In the Cloudflare Zero Trust dashboard, set Public Hostname service URL strictly to:
  ```
  HTTP://127.0.0.1:8080
  ```

#### Cause D: Cloudflare Tunnel Token Mismatch or Expiration
If the tunnel was recreated in the dashboard, the token file on Node 1 must be refreshed:
1. Verify token file exists:
   ```bash
   ls -la config/secrets/cloudflared.token
   ```
2. Test authentication directly:
   ```bash
   cloudflared tunnel run --token-file config/secrets/cloudflared.token
   ```

#### Cause E: Browser Integrity Check (Error 1010)
If accessing via automated CLI or curl, Cloudflare's Bot Management requires a browser User-Agent:
```bash
curl -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" https://server.akshatsahay.space/health
```

---

## 3. Storage Upload or Download Errors

### Symptoms
`HTTP 403 Forbidden` or `HTTP 413 Payload Too Large`.

### Diagnosis & Fix
1. Ensure the file name does not contain illegal characters (`\`, `/`, `:`, `*`, `?`, `"`, `<`, `>`, `|`, null bytes).
2. Verify upload size is under the 100MB limit.
3. Check storage free space via `python agent/node-agent.py health`.

---

## 4. High Availability (HA) & Master Failover Issues

### 4.1. Inspect HA Cluster Status
Run the HA inspection tool to verify current leader, term, and connected standbys:
```bash
./scripts/ha-status.sh http://127.0.0.1:8000
```
Or query the API directly:
```bash
curl -s http://127.0.0.1:8000/ha/status
```

### 4.2. Standby Fails to Promote After Master Goes Down
#### Symptoms:
Primary master crashes or powers off, but standby node does not promote to `LEADER`.
#### Diagnosis & Fix:
1. **Candidate URL List**: Verify that `PERSONALSERVER_CONTROLLER_URLS` includes the standby's own IP/port as well as the master's:
   ```bash
   echo $PERSONALSERVER_CONTROLLER_URLS
   ```
2. **Lease Duration & Timing**: Standby promotes after `PERSONALSERVER_HA_MAX_MISSED` (default: 3) consecutive checks (at `PERSONALSERVER_HA_CHECK_SEC=2.5s` intervals) OR after `PERSONALSERVER_HA_LEASE_SEC` (default: 10s) expires. Allow up to 10 seconds for failover to trigger.
3. **Check Standby Logs**:
   Look for `[HA_FAILOVER]` and `[HA_ELECTION]` events:
   ```bash
   curl -s http://<STANDBY_IP>:8000/diagnostics
   ```

### 4.3. Two Nodes Claim to Be Master (Split-Brain)
#### Symptoms:
Network partition occurs and both nodes believe they are `LEADER`.
#### Diagnosis & Fix:
1. PersonalServer enforces **term supremacy**: when the network heals, the node with the higher `term` wins.
2. The node with the lower term will automatically execute `step_down()` upon discovering the higher term peer.
3. To manually force a node to yield leadership and demote to standby:
   ```bash
   curl -X POST http://<NODE_IP>:8000/ha/failover -d '{"target_node_url": "http://<OTHER_NODE>:8000"}'
   ```

### 4.4. Workers Fail to Reconnect to Promoted Master
#### Symptoms:
Workers report connection errors after failover.
#### Diagnosis & Fix:
Ensure worker processes are launched with `PERSONALSERVER_CONTROLLER_URLS` pointing to both primary and secondary controllers:
```bash
export PERSONALSERVER_CONTROLLER_URLS="http://192.168.1.100:8000,http://192.168.1.101:8000"
python agent/local_worker.py
```
Workers will automatically probe all candidate URLs in order when the active master becomes unreachable.

