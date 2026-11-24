"""FRPC process lifecycle helpers."""

from __future__ import annotations

import asyncio
import logging
import stat
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def _ensure_executable(path: Path) -> None:
    """Ensure the FRPC binary is executable."""
    mode = path.stat().st_mode
    if not mode & stat.S_IXUSR:
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


async def async_verify_frpc_config(frpc_path: str | Path, config_path: str | Path) -> bool:
    """Validate an FRPC configuration using the FRPC binary."""
    binary = Path(frpc_path)
    config = Path(config_path)
    try:
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "verify",
            "-c",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
    except OSError as err:
        _LOGGER.error("Failed to verify frpc config: %s", err)
        return False
    if process.returncode:
        details = output.decode(errors="replace").strip()
        _LOGGER.error("Invalid frpc config (exit %s): %s", process.returncode, details)
        return False
    return True


async def async_start_frpc(
    frpc_path: str | Path, config_path: str | Path
) -> asyncio.subprocess.Process | None:
    """Validate and start FRPC without forwarding its output to the caller."""
    binary = Path(frpc_path)
    config = Path(config_path)
    if not config.exists():
        _LOGGER.error("frpc config not found: %s", config)
        return None
    _ensure_executable(binary)
    if not await async_verify_frpc_config(binary, config):
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "-c",
            str(config),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as err:
        _LOGGER.error("Failed to start frpc: %s", err)
        return None
    _LOGGER.info("frpc started (pid=%s)", process.pid)
    return process


async def async_stop_frpc(process: asyncio.subprocess.Process | None) -> None:
    """Stop a running FRPC process."""
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


def remove_frpc_config(config_path: str | Path) -> None:
    """Remove a generated FRPC configuration if it exists."""
    path = Path(config_path)
    if path.exists():
        path.unlink()
