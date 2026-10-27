# PersonalServer V2.4 — Multi-Application Deployment Foundation

## 1. Architectural Overview & Discovered Reality

PersonalServer hosts heterogeneous workloads across an authoritative mobile controller (Vivo Y31), a compute worker (Realme 5 Pro), and an on-demand Windows build worker.

When exposing multiple web applications concurrently under path-based public URLs on `https://akshatsahay.space` (e.g. `https://akshatsahay.space/RecallFlow/`), traffic encounters the following routing layers:

```
[Public Client]
       │
       ▼
[Edge Layer: akshatsahay.space]
       │
       ├─────────────────────────────────┬─────────────────────────────────┐
       ▼                                 ▼                                 ▼
Portfolio & Root Pages            RecallFlow Frontend               RecallFlow Backend
(Vercel Origin)                   (Vercel Deployment)               (Render Web Service)
https://akshatsahay.space/*       https://...vercel.app             https://...onrender.com
                                         ▲                                 ▲
                                         │                                 │
                         ┌───────────────┴─────────────────────────────────┴───────────────┐
                         │ PersonalServer Application Gateway (router/app_router.py)       │
                         │ Data-plane reverse proxy with API precedence & error isolation  │
                         └─────────────────────────────────────────────────────────────────┘
```

### Edge Routing Reality
1. The root domain `akshatsahay.space` is hosted externally on **Vercel** for the portfolio website.
2. PersonalServer services are tunneled via **Cloudflare Zero Trust** (`server.akshatsahay.space`, `api.akshatsahay.space`).
3. For public traffic arriving at `https://akshatsahay.space/RecallFlow/*` to reach the appropriate backends without moving the root portfolio away from Vercel, **Vercel Rewrites** or **Cloudflare Ingress** serves as the public ingress layer, while the PersonalServer **Application Router** acts as the internal controller gateway and data-plane reverse proxy.

---

## 2. Reusable Application Manifest Schema

The manifest schema (`common/app_manifest.py`) provides a single, declarative contract for multi-component applications. It separates frontend and backend deployments, establishes path routing rules, and strictly excludes secrets.

### RecallFlow Specification Example

```json
{
  "schema_version": "2.4",
  "name": "RecallFlow",
  "app_id": "app-recallflow",
  "public_path": "/RecallFlow",
  "display_name": "RecallFlow Knowledge Platform",
  "status": "active",
  "components": {
    "frontend": {
      "type": "frontend",
      "provider": "vercel",
      "destination": "https://recallflow-client.vercel.app",
      "routing": {
        "path_prefix": "/",
        "strip_mode": "strip_app",
        "spa_fallback": true,
        "assets_prefix": "/assets"
      },
      "build": {
        "builder": "windows-on-demand",
        "command": "npm run build",
        "base_path": "/RecallFlow",
        "output_dir": "dist",
        "artifact_id": "art-0fa243a82034",
        "artifact_sha256": "7d4cc60804efe0601f59697152cbc6c8b6484c64247568e90e382a288607e7ef"
      }
    },
    "backend": {
      "type": "backend",
      "provider": "render",
      "destination": "https://recallflow-api.onrender.com",
      "runtime": "python",
      "health_endpoint": "/api/health",
      "routing": {
        "path_prefix": "/api",
        "strip_mode": "strip_app",
        "api_priority": true
      }
    }
  },
  "health_check": {
    "path": "/api/health",
    "interval_seconds": 30,
    "timeout_seconds": 5
  },
  "release": {
    "version": "1.0.0",
    "release_id": "rel-20261026-001",
    "last_result": "SUCCESS"
  }
}
```

---

## 3. Path-Based Routing & URL Contract

The gateway enforces the following routing behavior:

| Request Path | Matching Component | Upstream Target | Behavior |
| :--- | :--- | :--- | :--- |
| `GET /RecallFlow` | — | HTTP 308 Redirect | Normalizes to `/RecallFlow/` |
| `GET /RecallFlow/` | `frontend` | `https://recallflow-client.vercel.app/` | Serves frontend index |
| `GET /RecallFlow/dashboard` | `frontend` | `https://recallflow-client.vercel.app/dashboard` | Client-side SPA navigation |
| `GET /RecallFlow/assets/main.js` | `frontend` | `https://recallflow-client.vercel.app/assets/main.js` | Direct asset serving (no SPA fallback) |
| `GET /RecallFlow/api/items` | `backend` | `https://recallflow-api.onrender.com/api/items` | Evaluated before frontend |
| `GET /RecallFlow/api/missing` | `backend` | `https://recallflow-api.onrender.com/api/missing` | Returns HTTP 404 JSON, **not** HTML |

### Key Invariants
1. **API Route Precedence:** Routes matching `routing.path_prefix` (e.g. `/api`) are evaluated before frontend fallback routes.
2. **Failure Isolation:** Any HTTP error returned by the backend (400, 401, 404, 500) is returned directly as JSON to the client. The gateway never falls back to frontend `index.html` for API requests.
3. **Prefix Stripping Modes:**
   - `strip_app`: Strips application prefix only (`/RecallFlow/api/notes` -> `/api/notes`). Default for backend APIs that mount under `/api`.
   - `strip_all`: Strips application and component prefixes (`/RecallFlow/api/notes` -> `/notes`). Used for APIs mounted at root.
   - `preserve`: Forwards the full original request path without modification.
4. **Header & Redirect Rewriting:**
   - Location headers from upstream redirects are rewritten with the public prefix (`Location: /api/login` -> `Location: /RecallFlow/api/login`).
   - Set-Cookie `Path=/` headers are rewritten to `Path=/RecallFlow` to prevent cookie collision across applications.

---

## 4. Deployment Adapter Boundaries

Components are decoupled across three supported execution models:

### A. Vercel Frontend Adapter (`VercelDeploymentAdapter`)
- **Responsibility:** Manages frontend deployment artifacts on Vercel.
- **External Dependencies:** `VERCEL_TOKEN`, `VERCEL_PROJECT_ID`, `VERCEL_ORG_ID`.
- **External Edge Rewrites:** When using Vercel at the domain root, the portfolio `vercel.json` must declare:
  ```json
  {
    "rewrites": [
      {
        "source": "/RecallFlow/api/:path*",
        "destination": "https://recallflow-api.onrender.com/api/:path*"
      },
      {
        "source": "/RecallFlow/:path*",
        "destination": "https://recallflow-client.vercel.app/:path*"
      }
    ]
  }
  ```

### B. Render Backend Adapter (`RenderDeploymentAdapter`)
- **Responsibility:** Manages backend API service deployments on Render.
- **External Dependencies:** `RENDER_API_KEY`, `RENDER_SERVICE_ID`, or Render Deploy Webhook URL.
- **Service Configuration:** CORS on Render backend must permit `https://akshatsahay.space`.

### C. PersonalServer Self-Hosted Adapter (`PersonalServerSelfHostedAdapter`)
- **Responsibility:** Local container or script execution on PersonalServer nodes.
- **Build Worker:** Reuses the Windows on-demand build worker (`ps-worker run`) targeting `cap:build`.
- **Storage Boundary:** The Windows laptop produces the ZIP artifact, uploads it with SHA-256 to the Vivo Y31 controller, and shuts down (`ps-worker stop`). The laptop is strictly excluded from authoritative cluster storage.

---

## 5. Security & Isolation Constraints

- **Zero Secret Exposure:** Credentials, API keys, passwords, and tokens are prohibited in manifests. Validation raises `ManifestValidationError` if secret keys are present.
- **Path Traversal Rejection:** Any request containing `..`, `%2e%2e`, `\`, or null bytes is rejected at the gateway boundary with HTTP 400 Bad Request.
- **Application Isolation:** Updating, pausing, or removing one application (e.g. `MemoVault`) has zero effect on the configuration or uptime of adjacent applications (`RecallFlow`).
