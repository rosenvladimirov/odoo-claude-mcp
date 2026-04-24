"""MCP Contabo plugin — Customer API + S3 Object Storage.

Two classes of tools:
  - contabo_* : Customer Control Panel API (https://api.contabo.com) for
    Object Storage instance metadata, usage/quota stats. OAuth2 password grant
    (Keycloak realm `contabo`).
  - s3_* : boto3 wrappers around eu2.contabostorage.com for bucket + object
    CRUD. Endpoint overridable per call.

Env:
  CONTABO_CLIENT_ID          required — customer number (e.g. 'INT-12736076')
  CONTABO_CLIENT_SECRET      required — API secret from Contabo customer panel
  CONTABO_API_USER           required — Contabo portal email (password grant)
  CONTABO_API_PASSWORD       required — Contabo portal password
  CONTABO_AUTH_URL           optional — defaults to Contabo Keycloak
  CONTABO_API_URL            optional — defaults to https://api.contabo.com

  S3_ACCESS_KEY_ID           required for s3_* tools
  S3_SECRET_ACCESS_KEY       required for s3_* tools
  S3_ENDPOINT_URL            optional — defaults to https://eu2.contabostorage.com
  S3_REGION                  optional — defaults to 'default'
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

import boto3
import httpx
from botocore.config import Config as BotoConfig
from mcp.server.fastmcp import FastMCP

# ─── Contabo Customer API (OAuth2) ─────────────────────────────

CONTABO_CLIENT_ID = os.environ.get("CONTABO_CLIENT_ID", "")
CONTABO_CLIENT_SECRET = os.environ.get("CONTABO_CLIENT_SECRET", "")
CONTABO_API_USER = os.environ.get("CONTABO_API_USER", "")
CONTABO_API_PASSWORD = os.environ.get("CONTABO_API_PASSWORD", "")
CONTABO_AUTH_URL = os.environ.get(
    "CONTABO_AUTH_URL",
    "https://auth.contabo.com/auth/realms/contabo/protocol/openid-connect/token",
)
CONTABO_API_URL = os.environ.get("CONTABO_API_URL", "https://api.contabo.com")

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0}


async def _contabo_token() -> str:
    """Fetch / refresh Contabo OAuth2 token (password grant). Cached until expiry."""
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 30:
        return _token_cache["access_token"]
    missing = [k for k, v in {
        "CONTABO_CLIENT_ID": CONTABO_CLIENT_ID,
        "CONTABO_CLIENT_SECRET": CONTABO_CLIENT_SECRET,
        "CONTABO_API_USER": CONTABO_API_USER,
        "CONTABO_API_PASSWORD": CONTABO_API_PASSWORD,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Contabo auth env missing: {missing}")
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(CONTABO_AUTH_URL, data={
            "grant_type": "password",
            "client_id": CONTABO_CLIENT_ID,
            "client_secret": CONTABO_CLIENT_SECRET,
            "username": CONTABO_API_USER,
            "password": CONTABO_API_PASSWORD,
        })
    if r.status_code != 200:
        raise RuntimeError(f"Contabo auth failed [{r.status_code}]: {r.text[:300]}")
    data = r.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 300))
    return _token_cache["access_token"]


async def _contabo_req(method: str, path: str, json_body: Any = None,
                       params: Optional[dict] = None) -> Any:
    token = await _contabo_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    url = f"{CONTABO_API_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.request(method, url, headers=headers, json=json_body, params=params)
    if r.status_code >= 400:
        return {"error": r.text[:500], "http_status": r.status_code}
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


# ─── S3 client (boto3) ─────────────────────────────────────────

S3_KEY = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET = os.environ.get("S3_SECRET_ACCESS_KEY", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT_URL", "https://eu2.contabostorage.com")
S3_REGION = os.environ.get("S3_REGION", "default")


def _s3_client(endpoint: Optional[str] = None):
    if not (S3_KEY and S3_SECRET):
        raise RuntimeError("S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY not set")
    return boto3.client(
        "s3",
        endpoint_url=endpoint or S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        config=BotoConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        region_name=S3_REGION,
    )


# ─── FastMCP ───────────────────────────────────────────────────

mcp = FastMCP("contabo")


# ---------- Contabo Customer API ----------
@mcp.tool()
async def contabo_account_info() -> Any:
    """Return the authenticated customer's profile (name, contact, address, tenants)."""
    return await _contabo_req("GET", "/v1/users/client")


@mcp.tool()
async def contabo_object_storages_list(page: int = 1, size: int = 50) -> Any:
    """List Object Storage instances (subscriptions) on the account.

    Returns per-instance: region, quota (GB), used GB, created date, status.
    """
    return await _contabo_req("GET", "/v1/object-storages", params={"page": page, "size": size})


@mcp.tool()
async def contabo_object_storage_get(object_storage_id: str) -> Any:
    """Get one Object Storage instance in full detail."""
    return await _contabo_req("GET", f"/v1/object-storages/{object_storage_id}")


@mcp.tool()
async def contabo_object_storage_stats(object_storage_id: str) -> Any:
    """Get usage stats (GB used, object count, etc) for an Object Storage instance."""
    return await _contabo_req("GET", f"/v1/object-storages/{object_storage_id}/stats")


@mcp.tool()
async def contabo_instances_list(page: int = 1, size: int = 50) -> Any:
    """List VPS instances on the account (name, IP, status, plan)."""
    return await _contabo_req("GET", "/v1/compute/instances", params={"page": page, "size": size})


# ---------- S3 Buckets (via boto3) ----------
@mcp.tool()
def s3_buckets_list(endpoint: Optional[str] = None) -> Any:
    """List all S3 buckets for the configured account on the target endpoint."""
    s3 = _s3_client(endpoint)
    resp = s3.list_buckets()
    return [{"name": b["Name"], "created": b["CreationDate"].isoformat()}
            for b in resp.get("Buckets", [])]


@mcp.tool()
def s3_bucket_create(bucket: str, endpoint: Optional[str] = None) -> Any:
    """Create a new bucket on the configured S3 endpoint."""
    s3 = _s3_client(endpoint)
    try:
        s3.create_bucket(Bucket=bucket)
        return {"created": bucket}
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
        return {"error": f"{code}: {e}"}


@mcp.tool()
def s3_bucket_delete(bucket: str, force: bool = False,
                     endpoint: Optional[str] = None) -> Any:
    """Delete an S3 bucket. Set force=true to delete all objects first."""
    s3 = _s3_client(endpoint)
    if force:
        paginator = s3.get_paginator("list_objects_v2")
        deleted = 0
        for page in paginator.paginate(Bucket=bucket):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
                deleted += len(keys)
        s3.delete_bucket(Bucket=bucket)
        return {"deleted_bucket": bucket, "deleted_objects": deleted}
    try:
        s3.delete_bucket(Bucket=bucket)
        return {"deleted_bucket": bucket}
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
        return {"error": f"{code}: {e}",
                "hint": "Pass force=true to purge objects first."}


@mcp.tool()
def s3_bucket_stats(bucket: str, endpoint: Optional[str] = None) -> Any:
    """Return object_count + total_bytes + oldest/newest object key for a bucket."""
    s3 = _s3_client(endpoint)
    paginator = s3.get_paginator("list_objects_v2")
    count, total, oldest, newest = 0, 0, None, None
    for page in paginator.paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            count += 1
            total += o["Size"]
            if oldest is None or o["LastModified"] < oldest["LastModified"]:
                oldest = o
            if newest is None or o["LastModified"] > newest["LastModified"]:
                newest = o
    return {
        "bucket": bucket,
        "object_count": count,
        "total_bytes": total,
        "oldest_key": oldest["Key"] if oldest else None,
        "oldest_modified": oldest["LastModified"].isoformat() if oldest else None,
        "newest_key": newest["Key"] if newest else None,
        "newest_modified": newest["LastModified"].isoformat() if newest else None,
    }


# ---------- S3 Objects ----------
@mcp.tool()
def s3_objects_list(bucket: str, prefix: str = "", max_keys: int = 100,
                    endpoint: Optional[str] = None) -> Any:
    """List objects in a bucket (prefix-filtered, page size-capped)."""
    s3 = _s3_client(endpoint)
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
    return [{"key": o["Key"], "size": o["Size"],
             "modified": o["LastModified"].isoformat(), "etag": o.get("ETag")}
            for o in resp.get("Contents", [])]


@mcp.tool()
def s3_object_get(bucket: str, key: str, endpoint: Optional[str] = None,
                  max_bytes: int = 2_000_000) -> Any:
    """Download an object (text). Binary or >max_bytes objects return metadata only."""
    s3 = _s3_client(endpoint)
    head = s3.head_object(Bucket=bucket, Key=key)
    size = head["ContentLength"]
    result = {
        "key": key, "size": size, "etag": head.get("ETag"),
        "content_type": head.get("ContentType"),
        "modified": head["LastModified"].isoformat(),
    }
    if size > max_bytes:
        result["truncated"] = True
        result["note"] = f"Size {size} > max_bytes {max_bytes}; metadata only"
        return result
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        result["content"] = body.decode("utf-8")
        try:
            result["json"] = json.loads(result["content"])
        except Exception:
            pass
    except UnicodeDecodeError:
        result["content_bytes_base64"] = __import__("base64").b64encode(body).decode()
        result["note"] = "Binary content — base64 encoded"
    return result


@mcp.tool()
def s3_object_delete(bucket: str, key: str, endpoint: Optional[str] = None) -> Any:
    """Delete a single object from a bucket."""
    s3 = _s3_client(endpoint)
    s3.delete_object(Bucket=bucket, Key=key)
    return {"deleted": {"bucket": bucket, "key": key}}


@mcp.tool()
def s3_objects_delete_prefix(bucket: str, prefix: str,
                             endpoint: Optional[str] = None) -> Any:
    """Delete all objects under a prefix in one bucket. Returns count."""
    if not prefix:
        return {"error": "Refusing to delete with empty prefix. Use s3_bucket_delete(force=true) instead."}
    s3 = _s3_client(endpoint)
    paginator = s3.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            deleted += len(keys)
    return {"bucket": bucket, "prefix": prefix, "deleted_objects": deleted}


if __name__ == "__main__":
    mcp.run()
