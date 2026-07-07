"""
config.py — Central configuration for My Drive API.

All file storage lives in S3 (or an S3-compatible service). Nothing is
read from or written to local disk — only SQLite (user accounts) stays local.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Auth / JWT ────────────────────────────────────────────────────────────
# IMPORTANT: override via environment variable in production:
#   export MYDRIVE_SECRET_KEY="$(openssl rand -hex 32)"
SECRET_KEY = os.environ.get("MYDRIVE_SECRET_KEY", "CHANGE_ME_DEV_ONLY_INSECURE")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12  # 12 hours

# ── Database (stores users only — files live in S3) ─────────────────────
DATABASE_URL = os.environ.get("MYDRIVE_DB_URL", "sqlite:///./mydrive.db")

# ── Uploads / streaming ───────────────────────────────────────────────────
MAX_UPLOAD_SIZE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB per file, tune as needed
CHUNK_SIZE = 1024 * 1024  # 1 MB chunk size used when streaming S3 object bodies

# ── S3 ────────────────────────────────────────────────────────────────────
AWS_ACCESS_KEY_ID = os.getenv("R2_AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("R2_AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("R2_AWS_REGION")
AWS_BUCKET_NAME = os.getenv("R2_AWS_BUCKET_NAME")

# ── CORS ──────────────────────────────────────────────────────────────────
# Restrict this to your actual app's origin in production.
ALLOWED_ORIGINS = os.environ.get("MYDRIVE_ALLOWED_ORIGINS", "*").split(",")
