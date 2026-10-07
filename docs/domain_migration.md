# PersonalServer Domain Migration Guide
========================================

PersonalServer centralizes its public domain and URL configuration in a non-secret configuration file:
[`config/platform.yaml`](file:///config/platform.yaml).

This configuration provides the single source of truth for:
- Primary public domain (e.g., `akshatsahay.space`)
- Admin console hostname (e.g., `server.akshatsahay.space`)
- API hostname (e.g., `api.akshatsahay.space`)
- Application routing scheme (`https`) and public URL generation (`https://<domain>/<app-route>/`)

---

## Important Architecture Boundary

> [!IMPORTANT]
> Modifying `config/platform.yaml` configures PersonalServer's application layer only.
> It **does not** automatically create DNS records, modify Cloudflare Tunnels, or update Cloudflare Access policies. External network configuration must be performed alongside configuration updates.

---

## Future Domain Migration Procedure

When migrating PersonalServer from the current domain (`akshatsahay.space`) to a new domain (e.g., `myprofessionalserver.com`), follow this step-by-step procedure:

### 1. Update Platform Configuration
Edit [`config/platform.yaml`](file:///config/platform.yaml):
```yaml
platform:
  domain: myprofessionalserver.com

  hosts:
    admin: server.myprofessionalserver.com
    api: api.myprofessionalserver.com

  application:
    scheme: https
    path_based: true
```

### 2. Configure DNS & Registrar
- Add DNS entries for the apex domain (`myprofessionalserver.com`), `server.myprofessionalserver.com`, and `api.myprofessionalserver.com` pointing to Cloudflare nameservers.
- Verify DNS propagation.

### 3. Update Cloudflare Tunnel Public Hostnames
- In Cloudflare Zero Trust Dashboard, navigate to **Networks > Tunnels**.
- Add Public Hostnames pointing to the appropriate local services:
  - `server.myprofessionalserver.com` -> `http://localhost:8080` (or Node 01 `:8080`)
  - `api.myprofessionalserver.com` -> `http://localhost:8080` (or Node 01 `:8080`)
  - `myprofessionalserver.com` -> Path-based routing rules or origin forwarding

### 4. Update Cloudflare Access Policies
- In Cloudflare Zero Trust Dashboard, navigate to **Access > Applications**.
- Update or add Application definitions matching the new hostnames (e.g., `server.myprofessionalserver.com`).
- Attach your required IdP authentication and zero-trust policies.

### 5. Restart / Reload PersonalServer Services
Restart PersonalServer services on the host nodes to reload the configuration:
```bash
./scripts/restart.sh
```

### 6. Verification Checklist
- [ ] Admin console loads securely at `https://server.<new-domain>/`
- [ ] Dynamic telemetry and `/api/platform` return the new domain structure
- [ ] Node API responds at `https://api.<new-domain>/health`
- [ ] Registered applications display the new canonical URLs (`https://<new-domain>/<app-route>/`)
- [ ] Public path routing forwards correctly to running containers
