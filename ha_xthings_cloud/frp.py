"""FRPC binary download and update support."""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import platform
import stat
from pathlib import Path
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)
FRP_S3_BASE_URL = "https://xthings-deploy-package.s3.us-west-2.amazonaws.com/Frp"
FRP_VERSION_FILENAME = "version.txt"
PLATFORM_MAP: dict[tuple[str, str], str] = {
    ("Darwin", "arm64"): "darwin_arm64",
    ("Darwin", "x86_64"): "darwin_amd64",
    ("Linux", "aarch64"): "linux_arm64",
    ("Linux", "x86_64"): "linux_amd64",
    ("Linux", "armv7l"): "linux_arm64",
}


def _get_platform_key() -> str | None:
    """Return the FRPC artifact key for the current platform."""
    key = PLATFORM_MAP.get((platform.system(), platform.machine()))
    if not key:
        _LOGGER.error("Unsupported platform: %s %s", platform.system(), platform.machine())
    return key


def _get_local_version(version_path: Path) -> str | None:
    """Read the locally stored FRPC version."""
    if version_path.exists():
        return version_path.read_text(encoding="utf-8").strip()
    return None


async def _async_fetch_upgrade_info(session: aiohttp.ClientSession) -> dict[str, str] | None:
    """Fetch and parse the FRP upgrade manifest."""
    try:
        async with session.get(f"{FRP_S3_BASE_URL}/upgrade.ini") as response:
            response.raise_for_status()
            text = await response.text()
    except aiohttp.ClientError as err:
        _LOGGER.error("Failed to fetch frpc upgrade info: %s", err)
        return None

    parser = configparser.ConfigParser()
    parser.read_string("[frp]\n" + text)
    return {
        key: value.strip('"').strip("'")
        for key, value in parser.items("frp")
    }


async def _async_download_frpc(
    session: aiohttp.ClientSession,
    download_path: str,
    destination: Path,
) -> bool:
    """Download a platform-specific FRPC binary."""
    try:
        async with session.get(f"{FRP_S3_BASE_URL}/{download_path}") as response:
            response.raise_for_status()
            content = await response.read()
    except aiohttp.ClientError as err:
        _LOGGER.error("Failed to download frpc: %s", err)
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    destination.chmod(
        destination.stat().st_mode
        | stat.S_IXUSR
        | stat.S_IXGRP
        | stat.S_IXOTH
    )
    _LOGGER.info("Downloaded frpc to %s (%d bytes)", destination, len(content))
    return True


def build_frpc_toml(
    data: dict[str, Any], ha_port: int = 8123, client_id: str = ""
) -> str:
    """Build an FRPC TOML configuration from cloud settings."""
    http_config = data.get("http", {})
    lines = [
        f'clientID = "{client_id}"',
        f'serverAddr = "{data.get("serverAddr", "")}"',
        f"serverPort = {data.get('serverPort', 7002)}",
        "loginFailExit = false",
        'auth.method = "token"',
        f'auth.token = "{data.get("auth_token", "")}"',
        f'metadatas.jwt = "{data.get("metadatas_jwt", "")}"',
        "",
        "[transport]",
        f'protocol = "{data.get("transport_protocol", "kcp")}"',
        "",
        "[[proxies]]",
        f'name = "{http_config.get("name", "")}"',
        f'type = "{http_config.get("type", "http")}"',
        'localIP = "127.0.0.1"',
        f"localPort = {ha_port}",
        f'subdomain = "{http_config.get("subdomain", "")}"',
    ]
    request_headers = http_config.get("requestHeaders", {})
    if isinstance(request_headers, dict):
        header_values = request_headers.get("set", {})
        if isinstance(header_values, dict):
            for header_name, header_value in header_values.items():
                lines.append(
                    f"requestHeaders.set.{header_name} = {json.dumps(str(header_value))}"
                )
    return "\n".join(lines) + "\n"


async def async_ensure_frpc(
    session: aiohttp.ClientSession,
    save_dir: str | Path,
) -> Path | None:
    """Ensure the current platform's FRPC binary exists and is up to date."""
    platform_key = _get_platform_key()
    if not platform_key:
        return None

    frp_dir = Path(save_dir)
    frpc_path = frp_dir / platform_key / "frpc"
    version_path = frp_dir / FRP_VERSION_FILENAME
    frp_dir.mkdir(parents=True, exist_ok=True)

    info = await _async_fetch_upgrade_info(session)
    if not info:
        if frpc_path.exists():
            _LOGGER.warning("Cannot check frpc update; using existing binary")
            return frpc_path
        _LOGGER.error("Cannot download frpc: upgrade info unavailable")
        return None

    remote_version = info.get("version", "")
    remote_path = info.get(platform_key)
    if not remote_path:
        _LOGGER.error("No download path for platform %s", platform_key)
        return frpc_path if frpc_path.exists() else None

    local_version = _get_local_version(version_path)
    if not frpc_path.exists() or local_version != remote_version:
        if not await _async_download_frpc(session, remote_path, frpc_path):
            return frpc_path if frpc_path.exists() else None
        version_path.write_text(remote_version, encoding="utf-8")

    return frpc_path
