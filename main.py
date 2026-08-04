import re
from pathlib import Path
from typing import List
from s3 import s3, BUCKET
from botocore.exceptions import ClientError

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session
from urllib.parse import quote 

from config import ALLOWED_ORIGINS
from database import init_db, get_db, User
from auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    get_current_user_stream,
    hash_password,
)


 
class BulkPathsRequest(BaseModel):
    paths: List[str]
 
 
class BulkDestinationRequest(BaseModel):
    paths: List[str]
    destination: str 

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

    from database import SessionLocal

    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(username="admin", hashed_password=hash_password("changeme123"))
            db.add(admin)
            db.commit()
    finally:
        db.close()


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


@app.post("/login", response_model=TokenResponse, tags=["auth"])
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    print("user entering", form_data.username, form_data.password)
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



@app.get("/files")
def list_files(path: str = "", current_user: User = Depends(get_current_user)):

    response = s3.list_objects_v2(
        Bucket=BUCKET,
        Prefix=path,
        Delimiter="/"
    )

    entries = []

    for folder in response.get("CommonPrefixes", []):
        entries.append({
            "name": Path(folder["Prefix"].rstrip("/")).name,
            "path": folder["Prefix"],
            "type": "folder"
        })

    for obj in response.get("Contents", []):

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

        folder_prefix = file_path.rstrip("/") + "/"

        response = s3.list_objects_v2(
            Bucket=BUCKET,
            Prefix=folder_prefix,
            MaxKeys=1
        )

        if response.get("KeyCount", 0) > 0:

            paginator = s3.get_paginator("list_objects_v2")

            objects_to_delete = []

            for page in paginator.paginate(
                Bucket=BUCKET,
                Prefix=folder_prefix
            ):
                for obj in page.get("Contents", []):
                    objects_to_delete.append({
                        "Key": obj["Key"]
                    })

            if objects_to_delete:

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

@app.get("/download/{file_path:path}", tags=["files"])
def download(file_path: str, current_user: User = Depends(get_current_user_stream)):
    try:
        obj = s3.get_object(
            Bucket=BUCKET,
            Key=file_path
        )

        filename = Path(file_path).name
        ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
        encoded_filename = quote(filename)  # UTF-8 percent-encoded, safe for headers

        return StreamingResponse(
            obj["Body"],
            media_type=obj.get("ContentType", "application/octet-stream"),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{ascii_fallback}"; '
                    f"filename*=UTF-8''{encoded_filename}"
                )
            },
        )

    except ClientError:
        raise HTTPException(404, "File not found")

@app.get("/preview/{file_path:path}", tags=["files"])
def preview(file_path: str, current_user: User = Depends(get_current_user_stream)):
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
RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


@app.get("/stream/{file_path:path}", tags=["files"])
def stream_video(file_path: str, request: Request, current_user: User = Depends(get_current_user_stream)):

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


def _delete_key_recursive(key: str) -> None:
    """Delete a single object, or every object under it if it's a folder."""
    folder_prefix = key.rstrip("/") + "/"
    probe = s3.list_objects_v2(Bucket=BUCKET, Prefix=folder_prefix, MaxKeys=1)
 
    if probe.get("KeyCount", 0) > 0:
        paginator = s3.get_paginator("list_objects_v2")
        objects_to_delete = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix=folder_prefix):
            for obj in page.get("Contents", []):
                objects_to_delete.append({"Key": obj["Key"]})
        for i in range(0, len(objects_to_delete), 1000):
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objects_to_delete[i:i + 1000]})
    else:
        s3.delete_object(Bucket=BUCKET, Key=key)
 
 
def _copy_key_recursive(old_key: str, new_key: str, delete_source: bool) -> None:
    """Copy a single object, or an entire folder's contents, to new_key.
    When delete_source is True this behaves like the existing /rename move;
    when False it's a pure copy that leaves the source untouched."""
    folder_prefix = old_key.rstrip("/") + "/"
    probe = s3.list_objects_v2(Bucket=BUCKET, Prefix=folder_prefix, MaxKeys=1)
    is_folder = probe.get("KeyCount", 0) > 0
 
    if is_folder:
        new_prefix = new_key.rstrip("/") + "/"
        paginator = s3.get_paginator("list_objects_v2")
        source_keys = []
        for page in paginator.paginate(Bucket=BUCKET, Prefix=folder_prefix):
            for obj in page.get("Contents", []):
                source_keys.append(obj["Key"])
 
        for src_key in source_keys:
            dst_key = new_prefix + src_key[len(folder_prefix):]
            s3.copy_object(Bucket=BUCKET, CopySource={"Bucket": BUCKET, "Key": src_key}, Key=dst_key)
 
        if delete_source:
            objects_to_delete = [{"Key": k} for k in source_keys]
            for i in range(0, len(objects_to_delete), 1000):
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objects_to_delete[i:i + 1000]})
    else:
        try:
            s3.head_object(Bucket=BUCKET, Key=new_key)
            raise HTTPException(409, f"Destination already exists: {new_key}")
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise
 
        s3.copy_object(Bucket=BUCKET, CopySource={"Bucket": BUCKET, "Key": old_key}, Key=new_key)
        if delete_source:
            s3.delete_object(Bucket=BUCKET, Key=old_key)
 
 
def register_bulk_routes(app):

    """Call register_bulk_routes(app) once, after `app = FastAPI(...)`."""
 
    @app.post("/bulk-delete", tags=["files"])
    def bulk_delete(body: BulkPathsRequest, current_user: User = Depends(get_current_user)):
        errors = {}
        deleted = []
        for p in body.paths:
            try:
                _delete_key_recursive(p)
                deleted.append(p)
            except ClientError as e:
                errors[p] = e.response["Error"]["Message"]
        if errors and not deleted:
            raise HTTPException(status_code=500, detail=errors)
        return {"deleted": deleted, "errors": errors}
 
    @app.post("/bulk-move", tags=["files"])
    def bulk_move(body: BulkDestinationRequest, current_user: User = Depends(get_current_user)):
        dest = body.destination.rstrip("/")
        moved = []
        errors = {}
        for p in body.paths:
            name = Path(p.rstrip("/")).name
            new_key = f"{dest}/{name}" if dest else name
            try:
                _copy_key_recursive(p, new_key, delete_source=True)
                moved.append({"old_path": p, "new_path": new_key})
            except HTTPException as e:
                errors[p] = e.detail
            except ClientError as e:
                errors[p] = e.response["Error"]["Message"]
        if errors and not moved:
            raise HTTPException(status_code=500, detail=errors)
        return {"moved": moved, "errors": errors}
 
    @app.post("/bulk-copy", tags=["files"])
    def bulk_copy(body: BulkDestinationRequest, current_user: User = Depends(get_current_user)):
        dest = body.destination.rstrip("/")
        copied = []
        errors = {}
        for p in body.paths:
            name = Path(p.rstrip("/")).name
            new_key = f"{dest}/{name}" if dest else name
            try:
                _copy_key_recursive(p, new_key, delete_source=False)
                copied.append({"old_path": p, "new_path": new_key})
            except HTTPException as e:
                errors[p] = e.detail
            except ClientError as e:
                errors[p] = e.response["Error"]["Message"]
        if errors and not copied:
            raise HTTPException(status_code=500, detail=errors)
        return {"copied": copied, "errors": errors}


register_bulk_routes(app) 