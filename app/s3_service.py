"""Thin wrapper around boto3's S3 client, pointed at AIC Cloud (S3-compatible,
not AWS) via `S3_ENDPOINT_URL`. This is now the fast, synchronous primary
store for every uploaded document -- Drive is written to later, in the
background, by drive_sync_worker.py; nothing in the live request path reads
from or waits on Drive anymore.
"""

import boto3

from app.config import settings


class S3ServiceError(RuntimeError):
    pass


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
    return _client


def upload_bytes(key: str, content: bytes, content_type: str) -> None:
    try:
        _get_client().put_object(Bucket=settings.s3_bucket_name, Key=key, Body=content, ContentType=content_type)
    except Exception as exc:
        raise S3ServiceError(f"S3 upload failed for {key}: {exc}") from exc


def download_bytes(key: str) -> bytes:
    try:
        response = _get_client().get_object(Bucket=settings.s3_bucket_name, Key=key)
        return response["Body"].read()
    except Exception as exc:
        raise S3ServiceError(f"S3 download failed for {key}: {exc}") from exc
