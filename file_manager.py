"""
file_manager.py — All storage operations, backed entirely by S3.

Every path coming from a client is treated as an S3 key (or key *prefix*,
since S3 has no real folders — a "folder" is modeled the conventional way,
as a zero-byte object whose key ends in "/", plus every object that shares
its prefix). This module never touches the local filesystem.
"""
from pathlib import PurePosixPath
from typing import Any, Dict, Iterator, List, Optional

from botocore.exceptions import ClientError
from fastapi import HTTPException

from s3 import BUCKET, s3
from config import CHUNK_SIZE

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


# ── Key helpers ───────────────────────────────────────────────────────────
def normalize_key(relative_path: str) -> str:
    """
    Turn a client-supplied path into a safe S3 key/prefix ('' = bucket root).
    Strips leading/trailing slashes and drops '.' / '..' segments so a
    request can never smuggle in a confusing or unexpected key.
    """
    relative_path = (relative_path or "").strip().replace("\\", "/").strip("/")
    if not relative_path:
        return ""
    parts = [p for p in relative_path.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def _folder_prefix(relative_path: str) -> str:
    key = normalize_key(relative_path)
    return f"{key}/" if key else ""


def _kind(key: str) -> str:
    if key.endswith("/"):
        return "folder"
    ext = PurePosixPath(key).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return "file"


def object_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _list_all_keys(prefix: str) -> List[str]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


# ── Listing / search ─────────────────────────────────────────────────────
def list_directory(relative_path: str = "") -> List[Dict[str, Any]]:
    prefix = _folder_prefix(relative_path)

    entries: List[Dict[str, Any]] = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix, Delimiter="/"):
            for common in page.get("CommonPrefixes", []):
                folder_key = common["Prefix"]
                entries.append({
                    "name": folder_key.rstrip("/").split("/")[-1],
                    "path": folder_key.rstrip("/"),
                    "type": "folder",
                    "size": None,
                    "modified": None,
                })

            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key == prefix:
                    continue  # the empty folder-marker object for this dir itself
                entries.append({
                    "name": key.rstrip("/").split("/")[-1],
                    "path": key,
                    "type": _kind(key),
                    "size": obj["Size"],
                    "modified": obj["LastModified"].timestamp(),
                })
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))

    entries.sort(key=lambda e: (e["type"] != "folder", e["name"].lower()))
    return entries


def search_files(query: str, relative_path: str = "") -> List[Dict[str, Any]]:
    prefix = _folder_prefix(relative_path)
    query_lower = query.lower()

    results: List[Dict[str, Any]] = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key == prefix:
                    continue
                name = key.rstrip("/").split("/")[-1]
                if query_lower in name.lower():
                    results.append({
                        "name": name,
                        "path": key,
                        "type": _kind(key),
                        "size": obj["Size"],
                    })
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return results


# ── Folder operations ─────────────────────────────────────────────────────
def create_folder(relative_path: str) -> Dict[str, Any]:
    prefix = _folder_prefix(relative_path)
    if not prefix:
        raise HTTPException(status_code=400, detail="Invalid folder path.")
    if object_exists(prefix):
        raise HTTPException(status_code=409, detail="Folder already exists.")

    try:
        s3.put_object(Bucket=BUCKET, Key=prefix, Body=b"")
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"path": prefix.rstrip("/"), "created": True}


def delete_path(relative_path: str) -> Dict[str, Any]:
    key = normalize_key(relative_path)
    if not key:
        raise HTTPException(status_code=400, detail="Cannot delete the storage root.")

    folder_prefix = f"{key}/"
    child_keys = _list_all_keys(folder_prefix)
    is_folder = object_exists(folder_prefix) or bool(child_keys)

    try:
        if is_folder:
            keys_to_delete = child_keys + [folder_prefix]
            # S3 batch delete accepts at most 1000 keys per call.
            for i in range(0, len(keys_to_delete), 1000):
                batch = keys_to_delete[i:i + 1000]
                s3.delete_objects(
                    Bucket=BUCKET,
                    Delete={"Objects": [{"Key": k} for k in batch]},
                )
        else:
            if not object_exists(key):
                raise HTTPException(status_code=404, detail="Path not found.")
            s3.delete_object(Bucket=BUCKET, Key=key)
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"path": key, "deleted": True}


def rename_path(old_relative: str, new_relative: str) -> Dict[str, Any]:
    old_key = normalize_key(old_relative)
    new_key = normalize_key(new_relative)
    if not old_key or not new_key:
        raise HTTPException(status_code=400, detail="Invalid source or destination path.")

    old_folder_prefix = f"{old_key}/"
    old_children = _list_all_keys(old_folder_prefix)
    is_folder = object_exists(old_folder_prefix) or bool(old_children)

    try:
        if is_folder:
            new_folder_prefix = f"{new_key}/"
            if object_exists(new_folder_prefix):
                raise HTTPException(status_code=409, detail="Destination already exists.")

            source_keys = old_children + [old_folder_prefix]
            for src_key in source_keys:
                dst_key = new_folder_prefix + src_key[len(old_folder_prefix):]
                s3.copy_object(
                    Bucket=BUCKET,
                    CopySource={"Bucket": BUCKET, "Key": src_key},
                    Key=dst_key,
                )
            for i in range(0, len(source_keys), 1000):
                batch = source_keys[i:i + 1000]
                s3.delete_objects(
                    Bucket=BUCKET,
                    Delete={"Objects": [{"Key": k} for k in batch]},
                )
            result_path = new_key
        else:
            if not object_exists(old_key):
                raise HTTPException(status_code=404, detail="Source path not found.")
            if object_exists(new_key):
                raise HTTPException(status_code=409, detail="Destination already exists.")

            s3.copy_object(
                Bucket=BUCKET,
                CopySource={"Bucket": BUCKET, "Key": old_key},
                Key=new_key,
            )
            s3.delete_object(Bucket=BUCKET, Key=old_key)
            result_path = new_key
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"old_path": old_key, "new_path": result_path}


# ── Reading (download / preview / range-based video streaming) ───────────
def get_object_stream(key: str, range_header: Optional[str] = None):
    """
    Fetch an object (optionally a single byte range) straight from S3.

    Returns a tuple of:
        body_iterator, content_type, content_length, content_range, status_code
    `content_range` is only set (and status_code is 206) when a Range
    header was honored by S3.
    """
    params = {"Bucket": BUCKET, "Key": key}
    if range_header:
        params["Range"] = range_header

    try:
        response = s3.get_object(**params)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise HTTPException(status_code=404, detail="File not found.")
        if code == "InvalidRange":
            raise HTTPException(status_code=416, detail="Requested range not satisfiable.")
        raise HTTPException(status_code=500, detail=str(e))

    body = response["Body"]

    def iterator() -> Iterator[bytes]:
        for chunk in body.iter_chunks(chunk_size=CHUNK_SIZE):
            yield chunk

    content_type = response.get("ContentType") or "application/octet-stream"
    content_length = response.get("ContentLength")
    content_range = response.get("ContentRange")
    status_code = 206 if range_header and content_range else 200

    return iterator(), content_type, content_length, content_range, status_code