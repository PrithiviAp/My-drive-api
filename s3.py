"""
s3.py — Shared boto3 S3 client used by every storage operation in file_manager.py.
"""
import boto3
from config import (
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    AWS_REGION,
    AWS_BUCKET_NAME,
)

if not AWS_BUCKET_NAME:
    raise RuntimeError(
        "AWS_BUCKET_NAME is not set. Configure AWS_ACCESS_KEY_ID, "
        "AWS_SECRET_ACCESS_KEY, AWS_REGION, and AWS_BUCKET_NAME (e.g. in a .env file)."
    )

s3 = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION,
)

BUCKET = AWS_BUCKET_NAME