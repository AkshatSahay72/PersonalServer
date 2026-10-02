# Personal Storage Subsystem (v1.0)

The PersonalServer Storage Subsystem turns your server node (Vivo Y31) into an accessible, secure personal file server.

---

## 1. Dedicated Storage Root

All personal files and folders are strictly confined to:
```text
~/PersonalServer/storage/
```

The storage directory is initialized with `0700` user permissions on server boot.

---

## 2. Strict Path Traversal Defenses

PersonalServer employs a multi-layer security boundary:
1. **Absolute Path Escape Prevention**: Rejects any path starting with `/`, `\`, or drive letters.
2. **Null Byte Prevention**: Rejects any path containing `\0`.
3. **Parent Traversal Prevention**: Rejects any path segment containing `..`.
4. **Canonical Resolution**: Resolves target path via `Path.resolve()` and enforces:
   $$\text{os.path.commonpath}([\text{target}, \text{STORAGE\_ROOT}]) == \text{STORAGE\_ROOT}$$
5. **System Path Protection**: Prevents access to `config/`, `config/secrets/`, `runtime/`, `~/.ssh/`, and arbitrary Termux/Android system paths.

---

## 3. Storage HTTP API

| Action | HTTP Method & Path | Query / Body | Response |
|---|---|---|---|
| **List Directory** | `GET /storage/list` | `?path=<relative_path>` | `{ path, items: [{ name, is_dir, size_bytes, modified, extension }], count }` |
| **Download File** | `GET /storage/download` | `?path=<file_path>` | Binary octet-stream with attachment filename |
| **Upload File** | `POST /storage/upload` | `?path=<dir_path>` (Multipart or raw stream) | `{ status: "uploaded", files: ["..."] }` |
| **Create Folder** | `POST /storage/mkdir` | `?path=<parent>` + `{"name": "new_folder"}` | `{ status: "created", path: "..." }` |
| **Rename Item** | `POST /storage/rename` | `{"path": "...", "new_name": "..."}` | `{ status: "renamed", from: "...", to: "..." }` |
| **Delete Item** | `DELETE /storage` | `?path=<item_path>` | `{ status: "deleted", path: "..." }` |
| **Storage Usage** | `GET /storage/usage` | None | `{ storage_root, used_bytes, files_count, folders_count, disk: {...} }` |

Uploads are capped at a maximum of 100MB per file and streamed in 64KB chunks to prevent memory spikes on mobile devices.
