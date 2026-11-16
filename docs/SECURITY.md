# PersonalServer — Security Architecture & Threat Model

**Version:** 1.0.0  
**Status:** Active Security Policy  
**Last Updated:** November 15, 2026  

---

## 1. Threat Model & Assumptions

PersonalServer operates in a zero-trust, heterogeneous environment where nodes are connected via mobile cellular networks, public Wi-Fi, and residential ISPs across different geographic states.

### Attacker Profiles & Attack Surfaces:
1. **Public Internet Scanning & Brute-Force:**
   - *Risk:* Automated botnets scanning IPv4/IPv6 address spaces for open web interfaces, proxies, or SSH daemons.
   - *Defense:* **Zero open router ports**. PersonalServer services never bind to public WAN interfaces and never use UPnP. Public access requires passing through Cloudflare Tunnel with Cloudflare Zero Trust Access identity verification.
2. **Untrusted Local Wi-Fi Networks:**
   - *Risk:* Compromised devices on the same local residential or public Wi-Fi network attempting ARP spoofing, packet inspection, or unauthenticated HTTP requests.
   - *Defense:* All inter-node communication is end-to-end encrypted over **Tailscale WireGuard** (`100.x.y.z`). Local bridge sockets (`:8765`, `:8766`) bind exclusively to `127.0.0.1` (loopback).
3. **Unauthorized Camera / Microphone Surveillance:**
   - *Risk:* Malware or unauthorized clients attempting to remotely turn on phone cameras or record audio without the owner's knowledge.
   - *Defense:* Android Native Bridge requires an explicit `FOREGROUND_SERVICE` with an un-dismissible notification displayed on the phone whenever the service is running. Camera2 hardware is strictly closed and powered off unless an authenticated session token is active.
4. **Secrets Exposure via Logs or Git:**
   - *Risk:* Auth tokens, session cookies, or WireGuard keys accidentally committed to git or exposed in standard output.
   - *Defense:* Automatic log redaction (`redact_sensitive_text`), `.gitignore` exclusions for `config/secrets/`, and pre-commit checks.

---

## 2. Security Defense-in-Depth Layers

```text
[Operator Browser]
       │
       ▼
┌───────────────────────────────────────────────┐
│ Layer 1: Perimeter Access Control             │
│ - Cloudflare Zero Trust Access (Identity Auth)│
│ - Encrypted Cloudflare Tunnel to Controller   │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│ Layer 2: Private Mesh Network Isolation       │
│ - Tailscale WireGuard Mesh (100.x.y.z)         │
│ - Cryptographic node authentication tokens    │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│ Layer 3: Application & Gateway Authorization  │
│ - Reverse proxy path-traversal sanitization   │
│ - Single-use pairing enrollment tokens        │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│ Layer 4: Local Device Hardware Sandboxing     │
│ - Android unprivileged userspace (Termux)     │
│ - Loopback-only IPC binding (127.0.0.1:8765)  │
│ - On-demand Camera2 sensor activation/release │
│ - Persistent OS foreground notification       │
└───────────────────────────────────────────────┘
```

---

## 3. Secret Management & Storage

- **Enrollment Tokens:** Stored in `config/secrets/enrollment.token` (read-only for controller). Single-use pairing codes (`PS-XXXX-XXXX`) expire in 15 minutes.
- **Node Authentication:** Stored in `config/secrets/auth.token` generated during enrollment using `secrets.token_hex(32)`.
- **Sensitive Output Handling:** Loggers and API serializers automatically mask keys matching `*token*`, `*secret*`, `*password*`, and `*key*`.
