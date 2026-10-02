# Networking & Ingress Architecture (v1.0)

PersonalServer separates network traffic into two isolated planes: **Private Control Network** and **Public Application Ingress**.

---

## 1. Network Planes

```text
+-------------------------------------------------------------------------+
|                    PLANE 1: PUBLIC USER ACCESS                          |
|  Internet -> Cloudflare Edge -> Cloudflare Tunnel -> Node API (:8080)   |
|  Purpose: Operations Web UI & Personal File Storage                     |
+-------------------------------------------------------------------------+

+-------------------------------------------------------------------------+
|                    PLANE 2: PRIVATE CLUSTER MESH                        |
|  Tailscale Encrypted WireGuard Mesh (100.x.y.z)                         |
|  - Laptop Controller (:8000)                                            |
|  - Node Heartbeats & Job Polling                                        |
|  - SSH Administration (:8022)                                           |
+-------------------------------------------------------------------------+
```

---

## 2. Port Allocation & Security Matrix

| Service | Host | Port | Network Scope | Public Exposure |
|---|---|---|---|---|
| **SSH** | Vivo Y31 | `8022` | Tailscale Only | **NO** (Strictly Private) |
| **Node API & Storage** | Vivo Y31 | `8080` | Localhost & Cloudflare Tunnel | **Proxied via Tunnel** |
| **Controller** | Laptop | `8000` | Tailscale Only | **NO** (Strictly Private) |
| **Cloudflare Tunnel** | Vivo Y31 | Outbound | Cloudflare Edge | **Ingress only** |

---

## 3. Remote Browser Access Without Tailscale

Users can open `https://api.akshatsahay.space` on any public machine (such as a library PC or mobile phone) without installing Tailscale. All sensitive cluster internals (SSH, Node API raw sockets, Controller database) remain securely tucked away behind Tailscale.
