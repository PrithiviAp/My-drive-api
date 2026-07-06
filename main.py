"""
main.py — My Drive API
A private cloud storage backend: login, browse, upload, download,
delete, rename, create-folder, search, and range-aware video streaming.

Run locally:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Interactive docs:
    http://localhost:8000/docs
"""
import mimetypes
import re
from pathlib import Path
from typing import Optional
from s3 import s3, BUCKET
from botocore.exceptions import ClientError

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import ALLOWED_ORIGINS, MAX_UPLOAD_SIZE_BYTES, CHUNK_SIZE
from database import init_db, get_db, User
from auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    hash_password,
)

app = FastAPI(
    title="My Drive API",
    description="Private personal cloud storage — like Google Drive, hosted on your own PC.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()
    # Seed a default admin user ONLY if the users table is empty.
    # Change this password immediately after first login.
    from database import SessionLocal

    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(username="admin", hashed_password=hash_password("changeme123"))
            db.add(admin)
            db.commit()
            print("⚠️  Seeded default user admin/changeme123 — change this immediately.")
    finally:
        db.close()


# ── Schemas ──────────────────────────────────────────────────────────────
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class FolderCreate(BaseModel):
    path: str


class RenameRequest(BaseModel):
    old_path: str
    new_path: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ── Auth endpoints ───────────────────────────────────────────────────────
@app.post("/login", response_model=TokenResponse, tags=["auth"])
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(data={"sub": user.username})
    return TokenResponse(access_token=token)


@app.get("/me", tags=["auth"])
def read_current_user(current_user: User = Depends(get_current_user)):
    return {"username": current_user.username, "is_active": current_user.is_active}


@app.post("/change-password", tags=["auth"])
def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from auth import verify_password

    if not verify_password(body.old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Old password is incorrect.")
    current_user.hashed_password = hash_password(body.new_password)
    db.commit()
    return {"detail": "Password updated."}


# ── File browsing ────────────────────────────────────────────────────────


@app.get("/files")
def list_files(path: str = "", current_user: User = Depends(get_current_user)):

    response = s3.list_objects_v2(
        Bucket=BUCKET,
        Prefix=path,
        Delimiter="/"
    )

    entries = []

    # Folders
    for folder in response.get("CommonPrefixes", []):
        entries.append({
            "name": Path(folder["Prefix"].rstrip("/")).name,
            "path": folder["Prefix"],
            "type": "folder"
        })

    # Files
    for obj in response.get("Contents", []):

        # Skip the folder marker object
        if obj["Key"] == path:
            continue

        entries.append({
            "name": Path(obj["Key"]).name,
            "path": obj["Key"],
            "size": obj["Size"],
            "lastModified": obj["LastModified"],
            "type": "file"
        })

    return {
        "path": path,
        "entries": entries
    }

@app.get("/search")
def search(q: str, current_user: User = Depends(get_current_user)):

    results = []

    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=BUCKET):

        for obj in page.get("Contents", []):

            if q.lower() in obj["Key"].lower():

                results.append({
                    "name": Path(obj["Key"]).name,
                    "path": obj["Key"],
                    "size": obj["Size"],
                    "type": "folder" if obj["Key"].endswith("/") else "file"
                })

    return {
        "query": q,
        "results": results
    }

# ── Folder operations ────────────────────────────────────────────────────
@app.post("/create-folder")
def create_folder(body: FolderCreate, current_user: User = Depends(get_current_user)):

    folder = body.path.rstrip("/") + "/"

    s3.put_object(
        Bucket=BUCKET,
        Key=folder,
        Body=b"",
    )

    return {
        "folder": folder
    }

@app.post("/rename")
def rename(body: RenameRequest, current_user: User = Depends(get_current_user)):

    
    old_path = body.old_path
    new_path = body.new_path

    if old_path == new_path:
        return {
            "renamed": False,
            "message": "No changes."
        }

    # ---------- Rename Folder ----------
    if old_path.endswith("/"):

        paginator = s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(
            Bucket=BUCKET,
            Prefix=old_path
        ):

            for obj in page.get("Contents", []):

                old_key = obj["Key"]

                if old_key == old_path:
                    continue
                new_key = new_path.rstrip("/") + "/" + old_key[len(old_path):]

                s3.copy_object(
                    Bucket=BUCKET,
                    CopySource={
                        "Bucket": BUCKET,
                        "Key": old_key
                    },
                    Key=new_key
                )

                s3.delete_object(
                    Bucket=BUCKET,
                    Key=old_key
                )

        return {
            "renamed": True,
            "type": "folder"
        }

    # ---------- Rename File ----------

    try:
        s3.head_object(
            Bucket=BUCKET,
            Key=new_path
        )

        raise HTTPException(409, "Destination already exists")

    except ClientError:
        pass

    s3.copy_object(
        Bucket=BUCKET,
        CopySource={
            "Bucket": BUCKET,
            "Key": old_path
        },
        Key=new_path
    )

    s3.delete_object(
        Bucket=BUCKET,
        Key=old_path
    )

    return {
        "renamed": True,
        "type": "file"
    }

@app.delete("/delete/{file_path:path}", tags=["files"])
def delete(file_path: str, current_user: User = Depends(get_current_user)):

    try:

        # ---------- Delete Folder ----------
        if file_path.endswith("/"):

            paginator = s3.get_paginator("list_objects_v2")

            objects_to_delete = []

            for page in paginator.paginate(
                Bucket=BUCKET,
                Prefix=file_path
            ):

                for obj in page.get("Contents", []):

                    objects_to_delete.append({
                        "Key": obj["Key"]
                    })

            if objects_to_delete:

                # Delete in batches of 1000
                for i in range(0, len(objects_to_delete), 1000):

                    s3.delete_objects(
                        Bucket=BUCKET,
                        Delete={
                            "Objects": objects_to_delete[i:i + 1000]
                        }
                    )

            return {
                "deleted": True,
                "type": "folder"
            }

        # ---------- Delete File ----------

        s3.delete_object(
            Bucket=BUCKET,
            Key=file_path
        )

        return {
            "deleted": True,
            "type": "file"
        }

    except ClientError as e:

        raise HTTPException(
            status_code=500,
            detail=e.response["Error"]["Message"]
        )
    
# ── Upload ────────────────────────────────────────────────────────────────
@app.post("/upload", tags=["files"])
async def upload_file(
    path: str = "",
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Upload a file into folder `path` (relative to storage root, "" = root).
    Enforces MAX_UPLOAD_SIZE_BYTES to avoid filling the disk from one request.
    """
    safe_name = Path(file.filename).name

    key = f"{path.strip('/')}/{safe_name}" if path else safe_name

    await file.seek(0)

    s3.upload_fileobj(
        Fileobj=file.file,
        Bucket=BUCKET,
        Key=key,
        ExtraArgs={
            "ContentType": file.content_type or "application/octet-stream"
        }
    )

    await file.close()

    return {
        "name": safe_name,
        "path": key,
    }

# ── Download ─────────────────────────────────────────────────────────────

@app.get("/download/{file_path:path}", tags=["files"])
def download(file_path: str, current_user: User = Depends(get_current_user)):
    try:
        obj = s3.get_object(
            Bucket=BUCKET,
            Key=file_path
        )

        return StreamingResponse(
            obj["Body"],
            media_type=obj.get("ContentType", "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{Path(file_path).name}"'
            },
        )

    except ClientError:
        raise HTTPException(404, "File not found")

# ── Preview (inline, e.g. images) ────────────────────────────────────────
@app.get("/preview/{file_path:path}", tags=["files"])
def preview(file_path: str, current_user: User = Depends(get_current_user)):
    try:
        obj = s3.get_object(
            Bucket=BUCKET,
            Key=file_path
        )

        return StreamingResponse(
            obj["Body"],
            media_type=obj.get("ContentType", "application/octet-stream"),
        )

    except ClientError:
        raise HTTPException(404, "File not found")
# ── Video streaming with HTTP Range support (needed for seek/scrub) ─────
RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


@app.get("/stream/{file_path:path}", tags=["files"])
def stream_video(file_path: str, request: Request, current_user: User = Depends(get_current_user)):

    try:
        range_header = request.headers.get("range")

        kwargs = {
            "Bucket": BUCKET,
            "Key": file_path,
        }

        if range_header:
            kwargs["Range"] = range_header

        obj = s3.get_object(**kwargs)

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(obj["ContentLength"]),
        }

        if "ContentRange" in obj:
            headers["Content-Range"] = obj["ContentRange"]

        return StreamingResponse(
            obj["Body"],
            media_type=obj.get("ContentType", "video/mp4"),
            status_code=206 if range_header else 200,
            headers=headers,
        )

    except ClientError:
        raise HTTPException(404, "Video not found")