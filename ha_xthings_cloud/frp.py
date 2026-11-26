"""Secure FRPC manifest verification, download, and configuration helpers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import platform
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

FRP_BASE_URL = "https://xthings-deploy-package.s3.us-west-2.amazonaws.com/Frp"
FRP_MANIFEST_URL = f"{FRP_BASE_URL}/manifest.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FRPC_BYTES = 128 * 1024 * 1024
# Pinned publisher key from Frp/doc/keys/public.key. Never fetch this remotely.
FRP_MANIFEST_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDEACURUXlvNA2Pc+7nBJ92MeOz
kdc0NXOOEBgNSlnDAw1jZAXbbIGXWvl9BeZNdFFnXGImwywP2/8HqYc0BRxwgQdP
51jDev3Ptmxu2JOVByHXLWSuNakyyGq8I3uFpg/kqEWj91h5UH6yW8E6nQHtnYmt
7AG/FWEypZwvR9lKEQIDAQAB
-----END PUBLIC KEY-----
"""
PLATFORM_MAP: dict[tuple[str, str], str] = {
    ("Darwin", "arm64"): "darwin_arm64",
    ("Darwin", "x86_64"): "darwin_amd64",
    ("Darwin", "amd64"): "darwin_amd64",
    ("Linux", "aarch64"): "linux_arm64",
    ("Linux", "arm64"): "linux_arm64",
    ("Linux", "x86_64"): "linux_amd64",
    ("Linux", "amd64"): "linux_amd64",
}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PATH_RE = re.compile(r"^[^/]+/(?:darwin_arm64|darwin_amd64|linux_arm64|linux_amd64)/frpc$")


def canonical_json_bytes(manifest: dict[str, Any]) -> bytes:
    """Serialize the unsigned manifest using the publisher's canonical JSON rules."""
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_signature(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("manifest signature is missing")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("manifest signature is not valid base64") from err


def verify_manifest_signature(
    manifest: dict[str, Any], public_key_pem: bytes | None = None
) -> None:
    """Verify a signed manifest using the built-in publisher public key."""
    if not isinstance(manifest, dict):
        raise ValueError("FRPC manifest must be a JSON object")
    algorithm = manifest.get("signatureAlgorithm")
    if algorithm not in {"ed25519", "rsa-pss-sha256"}:
        raise ValueError("unsupported manifest signature algorithm")
    signature = _decode_signature(manifest.get("signature"))
    public_key_pem = public_key_pem or FRP_MANIFEST_PUBLIC_KEY_PEM
    if not public_key_pem:
        raise ValueError("FRPC manifest public key is not configured")
    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
        if algorithm == "rsa-pss-sha256":
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise ValueError("manifest key is not an RSA public key")
            public_key.verify(
                signature,
                canonical_json_bytes(manifest),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        else:
            if not isinstance(public_key, ed25519.Ed25519PublicKey):
                raise ValueError("manifest key is not an Ed25519 public key")
            public_key.verify(signature, canonical_json_bytes(manifest))
    except (InvalidSignature, ValueError, TypeError) as err:
        raise ValueError("FRPC manifest signature verification failed") from err


def _get_platform_key() -> str:
    key = PLATFORM_MAP.get((platform.system(), platform.machine()))
    if not key:
        raise ValueError(f"unsupported FRPC platform: {platform.system()} {platform.machine()}")
    return key


def _validate_manifest(manifest: Any, platform_key: str) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("FRPC manifest must be a JSON object")
    version = manifest.get("version")
    if not isinstance(version, str) or not version or not VERSION_RE.fullmatch(version):
        raise ValueError("FRPC manifest version is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get(platform_key), dict):
        raise ValueError(f"FRPC artifact is missing for {platform_key}")
    artifact = artifacts[platform_key]
    path = artifact.get("path")
    checksum = artifact.get("sha256")
    if not isinstance(path, str) or not path or not PATH_RE.fullmatch(path):
        raise ValueError("FRPC artifact path is invalid")
    parts = path.split("/")
    if (
        path.startswith("/")
        or "\\" in path
        or ".." in parts
        or parts[0] != version
        or parts[1] != platform_key
        or urlsplit(path).scheme
    ):
        raise ValueError("FRPC artifact path is unsafe")
    if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
        raise ValueError("FRPC artifact sha256 is invalid")
    return {"version": version, "path": path, "sha256": checksum.lower()}


async def _read_response_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    length = response.content_length
    if length is not None and length > limit:
        raise ValueError("FRPC response exceeds size limit")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise ValueError("FRPC response exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_manifest(session: aiohttp.ClientSession) -> dict[str, Any]:
    parsed = urlsplit(FRP_MANIFEST_URL)
    if parsed.scheme != "https" or parsed.netloc != urlsplit(FRP_BASE_URL).netloc:
        raise ValueError("FRPC manifest URL is not an allowed HTTPS URL")
    try:
        async with session.get(FRP_MANIFEST_URL, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise ValueError("FRPC manifest redirects are not allowed")
            response.raise_for_status()
            raw = await _read_response_limited(response, MAX_MANIFEST_BYTES)
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise RuntimeError("failed to download FRPC manifest") from err
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError("FRPC manifest is not valid UTF-8 JSON") from err
    return manifest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _download_and_install(
    session: aiohttp.ClientSession,
    url: str,
    destination: Path,
    expected_sha256: str,
) -> None:
    temp_path = destination.with_name(f".{destination.name}.download")
    try:
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        async with session.get(url, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise ValueError("FRPC download redirects are not allowed")
            response.raise_for_status()
            if response.content_length is not None and response.content_length > MAX_FRPC_BYTES:
                raise ValueError("FRPC binary exceeds size limit")
            digest = hashlib.sha256()
            size = 0
            file = await asyncio.to_thread(temp_path.open, "wb")
            try:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FRPC_BYTES:
                        raise ValueError("FRPC binary exceeds size limit")
                    digest.update(chunk)
                    await asyncio.to_thread(file.write, chunk)
                await asyncio.to_thread(file.flush)
                await asyncio.to_thread(os.fsync, file.fileno())
            finally:
                await asyncio.to_thread(file.close)
        actual = digest.hexdigest()
        if not hmac.compare_digest(actual, expected_sha256):
            raise ValueError("FRPC checksum verification failed")
        await asyncio.to_thread(temp_path.chmod, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        await asyncio.to_thread(os.replace, temp_path, destination)
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise RuntimeError("failed to download FRPC binary") from err
    finally:
        await asyncio.to_thread(temp_path.unlink, missing_ok=True)


def build_frpc_toml(data: dict[str, Any], ha_port: int = 8123, client_id: str = "") -> str:
    """Build an FRPC TOML configuration from cloud settings."""
    http_config = data.get("http", {})
    lines = [
        f'clientID = "{client_id}"', f'serverAddr = "{data.get("serverAddr", "")}"',
        f"serverPort = {data.get('serverPort', 7002)}", "loginFailExit = false",
        'auth.method = "token"', f'auth.token = "{data.get("auth_token", "")}"',
        f'metadatas.jwt = "{data.get("metadatas_jwt", "")}"', "", "[transport]",
        f'protocol = "{data.get("transport_protocol", "kcp")}"', "", "[[proxies]]",
        f'name = "{http_config.get("name", "")}"', f'type = "{http_config.get("type", "http")}"',
        'localIP = "127.0.0.1"', f"localPort = {ha_port}",
        f'subdomain = "{http_config.get("subdomain", "")}"',
    ]
    headers = http_config.get("requestHeaders", {})
    if isinstance(headers, dict) and isinstance(headers.get("set"), dict):
        for name, value in headers["set"].items():
            lines.append(f"requestHeaders.set.{name} = {json.dumps(str(value))}")
    return "\n".join(lines) + "\n"


async def async_ensure_frpc(session: aiohttp.ClientSession, save_dir: str | Path) -> Path:
    """Verify the signed manifest and atomically install the current FRPC binary."""
    platform_key = _get_platform_key()
    manifest = await _fetch_manifest(session)
    verify_manifest_signature(manifest)
    artifact = _validate_manifest(manifest, platform_key)
    destination = Path(save_dir) / platform_key / "frpc"
    destination_exists = await asyncio.to_thread(destination.exists)
    local_checksum = (
        await asyncio.to_thread(_sha256_file, destination)
        if destination_exists
        else None
    )
    if not destination_exists or not local_checksum or not hmac.compare_digest(
        local_checksum, artifact["sha256"]
    ):
        await _download_and_install(
            session, f"{FRP_BASE_URL}/{artifact['path']}", destination, artifact["sha256"]
        )
    return destination
