"""Object storage for remittance documents, behind one interface.

Production is any S3-compatible store (SeaweedFS's S3 gateway in the
compose topology). The S3 client here is deliberately small — PUT,
GET, HEAD, ensure-bucket — signed with AWS Signature Version 4 using
only the standard library. Tests use the in-memory double.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import urllib.request
from datetime import datetime, timezone
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "text/plain") -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key, data, content_type="text/plain"):
        self._objects[key] = data

    def get(self, key):
        return self._objects[key]

    def exists(self, key):
        return key in self._objects


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signature_key(secret: str, day: str, region: str) -> bytes:
    k = _sign(("AWS4" + secret).encode(), day)
    k = _sign(k, region)
    k = _sign(k, "s3")
    return _sign(k, "aws4_request")


class S3ObjectStore:
    def __init__(self, bucket: str, endpoint: str, access_key: str, secret_key: str, region: str = "us-east-1") -> None:
        self.bucket = bucket
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region

    def _request(self, method: str, key: str, data: bytes | None = None, content_type: str = "text/plain") -> bytes:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(data or b"").hexdigest()
        host = self.endpoint.split("://", 1)[1]
        path = f"/{self.bucket}/{key}" if key else f"/{self.bucket}"
        canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical = "\n".join([method, path, "", canonical_headers, signed_headers, payload_hash])
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        signature = hmac.new(_signature_key(self.secret_key, day, self.region), to_sign.encode(), hashlib.sha256).hexdigest()
        auth = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        req = urllib.request.Request(
            self.endpoint + path,
            data=data,
            method=method,
            headers={
                "Authorization": auth,
                "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_hash,
                "Content-Type": content_type,
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()

    def ensure_bucket(self) -> None:
        try:
            self._request("HEAD", "")
        except Exception:
            self._request("PUT", "")

    def put(self, key, data, content_type="text/plain"):
        self._request("PUT", key, data=data, content_type=content_type)

    def get(self, key):
        return self._request("GET", key)

    def exists(self, key):
        try:
            self._request("HEAD", key)
            return True
        except Exception:
            return False


def make_object_store() -> ObjectStore:
    bucket = os.environ.get("S3_BUCKET")
    endpoint = os.environ.get("S3_ENDPOINT_URL")
    if bucket and endpoint:
        store = S3ObjectStore(
            bucket=bucket,
            endpoint=endpoint,
            access_key=os.environ.get("S3_ACCESS_KEY_ID", ""),
            secret_key=os.environ.get("S3_SECRET_ACCESS_KEY", ""),
            region=os.environ.get("S3_REGION", "us-east-1"),
        )
        store.ensure_bucket()
        return store
    return InMemoryObjectStore()
