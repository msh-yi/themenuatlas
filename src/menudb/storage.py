"""Raw snapshot storage in Neon Object Storage (S3 API), content-addressed by sha256."""

from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from menudb.config import get_settings


@lru_cache
def _client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.aws_endpoint_url_s3,
        region_name=s.aws_region,
        aws_access_key_id=s.aws_access_key_id,
        aws_secret_access_key=s.aws_secret_access_key,
        # Neon requires path-style addressing and SigV4. Short timeouts and few
        # retries so an unreachable endpoint fails fast instead of hanging.
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def key_for(content_hash: str) -> str:
    return f"raw/{content_hash[:2]}/{content_hash}"


def exists(key: str, bucket: str | None = None) -> bool:
    try:
        _client().head_object(Bucket=bucket or get_settings().snapshots_bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def put_raw(content_hash: str, data: bytes, content_type: str) -> str:
    """Store bytes once per hash; returns the key."""
    key = key_for(content_hash)
    if not exists(key):
        _client().put_object(
            Bucket=get_settings().snapshots_bucket, Key=key, Body=data, ContentType=content_type
        )
    return key


def get_raw(key: str) -> bytes:
    obj = _client().get_object(Bucket=get_settings().snapshots_bucket, Key=key)
    return obj["Body"].read()


def delete(key: str) -> None:
    _client().delete_object(Bucket=get_settings().snapshots_bucket, Key=key)


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=get_settings().snapshots_bucket, Key=key, Body=data, ContentType=content_type
    )


def list_keys(prefix: str) -> list[str]:
    paginator = _client().get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=get_settings().snapshots_bucket, Prefix=prefix):
        keys += [obj["Key"] for obj in page.get("Contents", [])]
    return keys


def move(src: str, dst: str) -> None:
    bucket = get_settings().snapshots_bucket
    _client().copy_object(Bucket=bucket, Key=dst, CopySource={"Bucket": bucket, "Key": src})
    delete(src)


def last_modified(key: str):
    """When the object was stored (timezone-aware datetime)."""
    return _client().head_object(Bucket=get_settings().snapshots_bucket, Key=key)["LastModified"]


def list_modified(prefix: str) -> dict:
    """key -> when it was stored, for every object under `prefix` (one listing, no HEADs)."""
    paginator = _client().get_paginator("list_objects_v2")
    out = {}
    for page in paginator.paginate(Bucket=get_settings().snapshots_bucket, Prefix=prefix):
        out.update({obj["Key"]: obj["LastModified"] for obj in page.get("Contents", [])})
    return out
