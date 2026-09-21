from __future__ import annotations

import hashlib
import io
import re
import tempfile
from collections.abc import Iterator
from typing import BinaryIO, Any

from reqpilotx_server.ports.object_store import StoredObject


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRANSFER_CHUNK_SIZE = 1024 * 1024
_UPLOAD_MEMORY_LIMIT = 8 * 1024 * 1024


class AliyunOssObjectStore:
    """Private Aliyun OSS adapter using the official Python SDK V2."""

    def __init__(
        self,
        *,
        region: str,
        bucket: str,
        prefix: str = "reqpilotx",
        endpoint: str = "",
        client: Any | None = None,
    ) -> None:
        self.region = str(region or "").strip()
        self.bucket = str(bucket or "").strip()
        self.prefix = _validated_prefix(prefix)
        self.endpoint = str(endpoint or "").strip()
        if not self.region or not self.bucket:
            raise ValueError("Aliyun OSS region and bucket are required.")
        self._oss = None
        if client is None:
            try:
                import alibabacloud_oss_v2 as oss
            except ImportError as exc:  # pragma: no cover - dependency boundary
                raise RuntimeError(
                    "Install alibabacloud-oss-v2 to use the OSS object store."
                ) from exc
            credentials = oss.credentials.EnvironmentVariableCredentialsProvider()
            config = oss.config.load_default()
            config.credentials_provider = credentials
            config.region = self.region
            if self.endpoint:
                config.endpoint = self.endpoint
            client = oss.Client(config)
            self._oss = oss
        self.client = client

    def put(
        self,
        *,
        key: str,
        source: BinaryIO,
        expected_sha256: str,
        media_type: str,
    ) -> StoredObject:
        digest_expected = _validated_sha256(expected_sha256)
        digest = hashlib.sha256()
        size = 0
        with tempfile.SpooledTemporaryFile(
            max_size=_UPLOAD_MEMORY_LIMIT,
            mode="w+b",
        ) as upload:
            while True:
                chunk = source.read(_TRANSFER_CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise TypeError("ObjectStore sources must return bytes.")
                upload.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            if digest.hexdigest() != digest_expected:
                raise ValueError("Object content hash does not match expected_sha256.")
            upload.seek(0)
            object_key = self._object_key(key)
            request = self._request(
                "PutObjectRequest",
                bucket=self.bucket,
                key=object_key,
                body=upload,
                content_length=size,
                content_type=str(media_type or "application/octet-stream"),
                metadata={"reqpilotx-sha256": digest_expected},
            )
            result = self.client.put_object(request)
        return StoredObject(
            key=_validated_key(key),
            size=size,
            sha256=digest_expected,
            media_type=str(media_type or "application/octet-stream"),
            etag=str(getattr(result, "etag", "") or ""),
        )

    def open(self, key: str) -> BinaryIO:
        request = self._request(
            "GetObjectRequest",
            bucket=self.bucket,
            key=self._object_key(key),
        )
        result = self.client.get_object(request)
        body = getattr(result, "body", result)
        if not hasattr(body, "read") and not hasattr(body, "iter_bytes"):
            return io.BytesIO(bytes(body))
        return _OssStreamReader(body)

    def stat(self, key: str) -> StoredObject | None:
        request = self._request(
            "HeadObjectRequest",
            bucket=self.bucket,
            key=self._object_key(key),
        )
        try:
            result = self.client.head_object(request)
        except Exception as exc:
            status_code = int(getattr(exc, "status_code", 0) or 0)
            if status_code == 404:
                return None
            raise
        metadata = getattr(result, "metadata", None) or {}
        sha256 = str(
            metadata.get("reqpilotx-sha256")
            or metadata.get("x-oss-meta-reqpilotx-sha256")
            or ""
        ).strip()
        return StoredObject(
            key=_validated_key(key),
            size=int(getattr(result, "content_length", 0) or 0),
            sha256=sha256,
            media_type=str(
                getattr(result, "content_type", "application/octet-stream")
                or "application/octet-stream"
            ),
            etag=str(getattr(result, "etag", "") or ""),
        )

    def delete(self, key: str) -> bool:
        existing = self.stat(key)
        if existing is None:
            return False
        request = self._request(
            "DeleteObjectRequest",
            bucket=self.bucket,
            key=self._object_key(key),
        )
        self.client.delete_object(request)
        return True

    def _object_key(self, key: str) -> str:
        normalized = _validated_key(key)
        return f"{self.prefix}/{normalized}" if self.prefix else normalized

    def _request(self, name: str, **values: Any) -> Any:
        if self._oss is None:
            factory = getattr(self.client, f"{name[0].lower()}{name[1:]}", None)
            if factory is not None:
                return factory(**values)
            return values
        return getattr(self._oss, name)(**values)


class _OssStreamReader(io.RawIOBase):
    """Adapt the OSS SDK response stream to bounded BinaryIO reads."""

    def __init__(self, body: Any) -> None:
        super().__init__()
        self._body = body
        self._chunks = _body_chunks(body)
        self._buffer = bytearray()
        self._exhausted = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed OSS object.")
        if size is None or size < 0:
            for chunk in self._chunks:
                self._buffer.extend(chunk)
            self._exhausted = True
            payload = bytes(self._buffer)
            self._buffer.clear()
            return payload
        if size == 0:
            return b""
        while len(self._buffer) < size and not self._exhausted:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                self._exhausted = True
        payload = bytes(self._buffer[:size])
        del self._buffer[:size]
        return payload

    def close(self) -> None:
        if not self.closed:
            closer = getattr(self._body, "close", None)
            if callable(closer):
                closer()
        super().close()


def _body_chunks(body: Any) -> Iterator[bytes]:
    iterator_factory = getattr(body, "iter_bytes", None)
    if callable(iterator_factory):
        iterator = iterator_factory(block_size=_TRANSFER_CHUNK_SIZE)
    else:
        iterator = _read_chunks(body)
    for chunk in iterator:
        if not isinstance(chunk, bytes):
            raise TypeError("OSS response streams must return bytes.")
        if chunk:
            yield chunk


def _read_chunks(body: Any) -> Iterator[bytes]:
    while True:
        chunk = body.read(_TRANSFER_CHUNK_SIZE)
        if not chunk:
            return
        yield chunk


def _validated_key(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Object key must be a safe relative path.")
    return "/".join(parts)


def _validated_prefix(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").strip("/")
    if not normalized:
        return ""
    return _validated_key(normalized)


def _validated_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters.")
    return normalized


__all__ = ["AliyunOssObjectStore"]
