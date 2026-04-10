"""Exceptions for Xthings Cloud API."""

from __future__ import annotations


class XthingsCloudApiError(Exception):
    """API call error."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class XthingsCloudAuthError(XthingsCloudApiError):
    """Authentication error (token invalid/expired)."""
