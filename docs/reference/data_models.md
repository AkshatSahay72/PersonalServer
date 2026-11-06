# Multi-Node Decentralized Storage Subsystem

PersonalServer provides a secure, sandboxed multi-node storage subsystem that exposes storage across all active cluster nodes through a unified API and web interface.

---

## 1. Storage Sandbox & Path Traversal Defenses

All file operations on every node are strictly confined to the local storage sandbox:
```text
~/PersonalServer/storage/
```

### Multi-Layer Security Boundary
1. **Absolute Path Escape Prevention**: Rejects any path starting with `/`, `\`, or drive letters.
2. **Null Byte Prevention**: Rejects any path containing `\0`.
3. **Parent Traversal Prevention**: Rejects any path segment containing `..`.
4. **Canonical Resolution**: Resolves target paths via `Path.resolve()` and enforces:
   $$\text{os.path.commonpath}([\text{target}, \text{STORAGE\_ROOT}]) == \text{STORAGE\_ROOT}$$
5. **System Path Protection**: Restricts access strictly inside `storage/` and blocks access to `config/`, `config/secrets/`, `runtime/`, `~/.ssh/`, or root system files.

---

## 2. Multi-Node Storage Topology

Every storage node exposes local read and write operations. The primary Node API dynamically discovers cluster storage capabilities and proxies operations to remote nodes transparently:

```text
[ Browser / Client ]
        │
        ▼
[ Primary Node API (:8080) ]
        │
   ┌────┴───────────────────────────┐
   │ (Local Node)                   │ (Remote Node: node-02)
   ▼                                ▼
[ Local Storage Root ]       [ Node 02 API (:8080) via Tailscale ]
~/PersonalServer/storage/           │
                                    ▼
                             [ Node 02 Storage Root ]
                             ~/PersonalServer/storage/
```

### Storage Node Discovery (`GET /storage/nodes`)
Returns active storage nodes across the cluster:
```json
{
  "nodes": [
    {
      "node_id": "server-5387a86bf36116b1",
      "name": "vivo-y31",
      "status": "ONLINE",
      "is_local": true,
      "storage": { "total": "107G", "used": "11G", "available": "96G", "used_percent": "10%" }
    },
    {
      "node_id": "server-95bad5ff01424d4c8d184330d6d2e394",
      "name": "node-02",
      "status": "ONLINE",
      "is_local": false,
      "storage": { "total": "50G", "used": "9.3G", "available": "41G", "used_percent": "19%" }
    }
  ],
  "count": 2
}
```

---

## 3. Storage HTTP API Reference

To target a specific node, pass the `?node=<NODE_NAME_OR_ID>` query parameter (defaults to local node if omitted):

| Action | HTTP Method & Path | Parameters / Body | Description |
|---|---|---|---|
| **List Directory** | `GET /storage/list` | `?node=<node>&path=<rel_path>` | List files and folders in directory |
| **Download File** | `GET /storage/download` | `?node=<node>&path=<file_path>` | Stream file download |
| **Upload File** | `POST /storage/upload` | `?node=<node>&path=<dir>` (multipart or stream) | Upload file to target node |
| **Create Folder** | `POST /storage/mkdir` | `?node=<node>&path=<dir>` + `{"name": "...", "path": "..."}` | Create a new folder |
| **Rename Item** | `POST /storage/rename` | `?node=<node>` + `{"path": "...", "new_name": "..."}` | Rename file or folder |
| **Delete Item** | `DELETE /storage` | `?node=<node>&path=<item_path>` | Delete file or folder recursively |
| **Storage Usage** | `GET /storage/usage` | `?node=<node>` | Query disk usage and file counts |

---

## 4. Offline Node Protection

If a remote storage node goes `OFFLINE` or unreachable:
* The web interface disables write actions (upload, rename, mkdir, delete) and displays an explicit `Node Offline (Read Only)` indicator.
* API write requests directed to an offline remote node are rejected with `HTTP 503 Service Unavailable`.
