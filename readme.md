# My Drive API

A private personal-cloud backend (FastAPI) — tested end-to-end and working.

## Setup

```bash
pip install -r requirements.txt

# Point at your real drive (Windows example):
export MYDRIVE_BASE_DIR="D:/MyDrive"          # PowerShell: $env:MYDRIVE_BASE_DIR="D:/MyDrive"
export MYDRIVE_SECRET_KEY="$(openssl rand -hex 32)"

uvicorn main:app --host 0.0.0.0 --port 8000
```

Docs: `http://localhost:8000/docs` (interactive Swagger UI — try every endpoint from the browser).

## First login

On first startup a default account is seeded: `admin` / `changeme123`.
**Change it immediately** via `POST /change-password` once logged in.

## Endpoints

| Method | Path                        | Purpose                              |
|--------|-----------------------------|---------------------------------------|
| POST   | `/login`                    | Get a JWT (form fields: username, password) |
| GET    | `/me`                       | Current user info |
| POST   | `/change-password`          | Change your password |
| GET    | `/files?path=`              | List a folder's contents |
| GET    | `/search?q=&path=`          | Recursive filename search |
| POST   | `/create-folder`            | `{"path": "Photos/2026"}` |
| POST   | `/rename`                   | `{"old_path": "...", "new_path": "..."}` |
| DELETE | `/delete/{path}`            | Delete a file or folder |
| POST   | `/upload?path=`             | Multipart upload (`file` field) |
| GET    | `/download/{path}`          | Force-download a file |
| GET    | `/preview/{path}`           | Inline view (images etc.) |
| GET    | `/stream/{path}`            | Range-aware video streaming (seek/scrub works) |
| GET    | `/health`                   | Liveness check |

Every endpoint except `/login` and `/health` requires:
`Authorization: Bearer <token>`

## Security notes (read before exposing this to the internet)

1. **Change `MYDRIVE_SECRET_KEY`** — never ship the default.
2. **Change the seeded admin password** on first login.
3. Path traversal is blocked server-side (`file_manager.safe_resolve`) — every
   client-supplied path is resolved and checked against the storage root.
4. Prefer a VPN (e.g. Tailscale/WireGuard) or a tunnel over raw port-forwarding
   for remote access — this API has no rate-limiting or brute-force lockout
   built in yet.
5. Put this behind HTTPS (a reverse proxy like Caddy/nginx, or your tunnel
   provider's TLS) before it ever leaves your LAN — JWTs sent over plain HTTP
   can be intercepted.
6. `MAX_UPLOAD_SIZE_BYTES` in `config.py` caps individual uploads at 5 GB —
   tune to taste.

## Project layout

```
mydrive_api/
├── main.py           # All API routes
├── auth.py           # JWT + password hashing
├── database.py       # SQLite user table (files themselves stay on disk)
├── file_manager.py   # Safe filesystem operations, path-traversal guard
├── config.py         # All settings (env-var overridable)
└── requirements.txt
```

## Next steps (from the original plan)

- Flutter client hitting these endpoints (matches the `/login`, `/files`,
  `/upload`, `/download`, `/delete`, `/create-folder`, `/rename` contract
  exactly, plus `/search`, `/preview`, `/stream` for extra features).
- Photo auto-backup: have the Flutter app POST to `/upload?path=Photos` on a
  background schedule when it detects new camera images.
- Recycle bin: instead of deleting in `file_manager.delete_path`, move to a
  hidden `.trash/` folder and add a `/restore` endpoint.