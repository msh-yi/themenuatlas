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
        # Neon requires path-style addressing and SigV4
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
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
