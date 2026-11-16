# PersonalServer — Security Rules & Trust Model

---

## 1. Perimeter & Network Boundaries

1. **Private Mesh Exclusivity:**
   - All inter-node traffic, Node API calls, heartbeats, and media streams must flow over the **Tailscale WireGuard Mesh** (`100.x.y.z`).
   - Core server daemons must bind to `127.0.0.1` (loopback) or the assigned Tailscale IP. Never bind unauthenticated services to `0.0.0.0` on public network interfaces.
2. **Zero Router Port Forwarding:**
   - No router UPnP, WAN port forwarding, or DMZ configurations are permitted.
3. **Public Ingress via Cloudflare Zero Trust:**
   - Public domains (`server.akshatsahay.space`) are tunnelled via Cloudflare Tunnel (`cloudflared`).
   - Access must pass through Cloudflare Zero Trust Access policies (identity verification) before reaching the local reverse proxy.

---

## 2. Authentication & Node Secrets

1. **Node Enrollment Tokens:**
   - Onboarding requires pre-shared tokens or single-use pairing codes (`PS-XXXX-XXXX`) expiring in 15 minutes.
   - Long-lived node tokens reside in `config/secrets/auth.token` and are strictly excluded from git via `.gitignore`.
2. **Never Commit Secrets:**
   - Never commit tokens, private keys, passwords, or personal credentials.
   - Use `.env.example` and `config/secrets/*.example` for documentation.
3. **Log Redaction:**
   - Pass all logged request payloads and terminal outputs through `redact_sensitive_text()` or `redact_sensitive_data()`.
   - Never print raw auth tokens or private WireGuard keys in standard output or telemetry streams.

---

## 3. Hardware & Privacy Protection

1. **Strict On-Demand Camera / Mic Activation:**
   - The camera and microphone must remain physically inactive (`IDLE`) until an authenticated client initiates an explicit streaming session.
   - Sessions must automatically time out or terminate if the viewing socket disconnects.
2. **Local Loopback Bridge Isolation:**
   - The Android Native Bridge HTTP server (`LocalBridgeServer`) binds exclusively to `127.0.0.1:8765`, ensuring external network devices cannot directly trigger camera or microphone hardware without passing through the authenticated Node API.
