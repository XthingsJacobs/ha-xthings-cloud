"""AWS Kinesis Video Streams WebRTC signaling client.

Handles SDP offer/answer exchange and ICE candidate forwarding
via KVS signaling channels with AWS SigV4 authentication.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)


class KvsSignalingClient:
    """KVS WebRTC signaling client for SDP exchange via AWS API.

    Args:
        session: aiohttp.ClientSession instance (injected).
        region: AWS region (e.g. "us-west-2").
        channel_arn: KVS signaling channel ARN.
        credentials: Dict with AccessKeyId, SecretAccessKey, SessionToken.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        region: str,
        channel_arn: str,
        credentials: dict[str, str],
    ) -> None:
        self._session = session
        self._region = region
        self._channel_arn = channel_arn
        self._access_key = credentials["AccessKeyId"]
        self._secret_key = credentials["SecretAccessKey"]
        self._session_token = credentials.get("SessionToken", "")
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._listen_task: asyncio.Task | None = None

    async def async_get_answer_sdp(
        self,
        offer_sdp: str,
        on_ice_candidate: Callable[[dict[str, Any]], None] | None = None,
    ) -> str | None:
        """Exchange SDP offer for answer via KVS signaling.

        Args:
            offer_sdp: WebRTC SDP offer string.
            on_ice_candidate: Optional callback receiving ICE candidate dicts
                with keys: candidate, sdpMid, sdpMLineIndex.

        Returns:
            SDP answer string, or None on failure.
        """
        endpoints = await self._async_get_signaling_endpoints()
        if not endpoints:
            return None
        wss_endpoint = endpoints.get("WSS")
        if not wss_endpoint:
            _LOGGER.error("KVS: No WSS endpoint found")
            return None

        signed_url = self._build_signed_wss_url(wss_endpoint)
        _LOGGER.debug("KVS: Connecting to signaling channel")
        try:
            self._ws = await self._session.ws_connect(signed_url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("KVS: Signaling connection failed: %s", err)
            return None

        # Send SDP offer
        offer_msg = json.dumps({
            "action": "SDP_OFFER",
            "messagePayload": base64.b64encode(
                json.dumps({"type": "offer", "sdp": offer_sdp}).encode()
            ).decode(),
        })
        await self._ws.send_str(offer_msg)
        _LOGGER.debug("KVS: SDP offer sent")

        # Wait for SDP answer, forward ICE candidates
        answer_sdp = None
        try:
            async for msg in self._ws:
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raw = msg.data if isinstance(msg.data, str) else (
                        msg.data.decode() if msg.data else None
                    )
                    if not raw:
                        continue
                    _LOGGER.debug("KVS: Message: %s", raw[:200])
                    data = json.loads(raw)
                    msg_type = data.get("messageType")

                    if msg_type == "SDP_ANSWER":
                        payload = json.loads(
                            base64.b64decode(data["messagePayload"]).decode()
                        )
                        answer_sdp = payload.get("sdp")
                        _LOGGER.debug("KVS: SDP answer received")
                        # Start background ICE listener
                        self._listen_task = asyncio.create_task(
                            self._async_listen_ice(on_ice_candidate)
                        )
                        return answer_sdp

                    elif msg_type == "ICE_CANDIDATE" and on_ice_candidate:
                        self._parse_and_forward_ice(data, on_ice_candidate)

                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("KVS: SDP exchange failed: %s", err)
        return answer_sdp

    async def _async_listen_ice(
        self, on_ice_candidate: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Listen and forward ICE candidates in background."""
        if not on_ice_candidate:
            return
        try:
            async for msg in self._ws:
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raw = msg.data if isinstance(msg.data, str) else (
                        msg.data.decode() if msg.data else None
                    )
                    if not raw:
                        continue
                    data = json.loads(raw)
                    if data.get("messageType") == "ICE_CANDIDATE":
                        self._parse_and_forward_ice(data, on_ice_candidate)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.debug("KVS: ICE listener ended")

    @staticmethod
    def _parse_and_forward_ice(
        data: dict, on_ice_candidate: Callable[[dict[str, Any]], None],
    ) -> None:
        """Parse ICE candidate from signaling message and forward via callback."""
        try:
            payload = json.loads(
                base64.b64decode(data["messagePayload"]).decode()
            )
            on_ice_candidate({
                "candidate": payload.get("candidate", ""),
                "sdpMid": payload.get("sdpMid", ""),
                "sdpMLineIndex": payload.get("sdpMLineIndex", 0),
            })
        except Exception:  # noqa: BLE001
            _LOGGER.debug("KVS: Failed to parse ICE candidate")

    async def async_send_ice_candidate(
        self, candidate: str, sdp_mid: str | None, sdp_mline_index: int | None,
    ) -> None:
        """Send ICE candidate to master via signaling channel."""
        if not self._ws or self._ws.closed:
            return
        payload = json.dumps({
            "candidate": candidate,
            "sdpMid": sdp_mid or "0",
            "sdpMLineIndex": sdp_mline_index or 0,
        })
        msg = json.dumps({
            "action": "ICE_CANDIDATE",
            "messagePayload": base64.b64encode(payload.encode()).decode(),
        })
        await self._ws.send_str(msg)

    async def async_close(self) -> None:
        """Close signaling connection and cleanup."""
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        self._listen_task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    # ---- AWS KVS API ----

    async def _async_get_signaling_endpoints(self) -> dict[str, str]:
        """Get KVS signaling channel endpoints (WSS, HTTPS)."""
        url = (
            f"https://kinesisvideo.{self._region}.amazonaws.com"
            "/getSignalingChannelEndpoint"
        )
        body = json.dumps({
            "ChannelARN": self._channel_arn,
            "SingleMasterChannelEndpointConfiguration": {
                "Protocols": ["WSS", "HTTPS"],
                "Role": "VIEWER",
            },
        }, separators=(",", ":"))
        headers = self._sign_request("POST", url, body)
        try:
            resp = await self._session.post(url, data=body, headers=headers)
            data = await resp.json()
            return {
                ep["Protocol"]: ep["ResourceEndpoint"]
                for ep in data.get("ResourceEndpointList", [])
            }
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("KVS: Failed to get signaling endpoints: %s", err)
            return {}

    async def async_get_ice_server_config(self) -> list[dict[str, Any]]:
        """Get KVS ICE server config (TURN/STUN)."""
        endpoints = await self._async_get_signaling_endpoints()
        https_endpoint = endpoints.get("HTTPS")
        if not https_endpoint:
            return []
        url = f"{https_endpoint}/v1/get-ice-server-config"
        body = json.dumps(
            {"ChannelARN": self._channel_arn}, separators=(",", ":")
        )
        headers = self._sign_request("POST", url, body)
        try:
            resp = await self._session.post(url, data=body, headers=headers)
            data = await resp.json()
            return data.get("IceServerList", [])
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("KVS: Failed to get ICE servers: %s", err)
            return []

    # ---- AWS SigV4 ----

    def _sign_request(self, method: str, url: str, body: str) -> dict[str, str]:
        """AWS SigV4 sign HTTP request."""
        now = datetime.now(timezone.utc)
        datestamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        parsed = urlparse(url)
        host = parsed.hostname
        path = parsed.path or "/"
        service = "kinesisvideo"
        credential_scope = f"{datestamp}/{self._region}/{service}/aws4_request"

        headers_to_sign = {"host": host, "x-amz-date": amz_date}
        if self._session_token:
            headers_to_sign["x-amz-security-token"] = self._session_token
        signed_headers = ";".join(sorted(headers_to_sign.keys()))
        canonical_headers = "".join(
            f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items())
        )
        payload_hash = hashlib.sha256(body.encode()).hexdigest()
        canonical_request = (
            f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )
        signing_key = self._get_signature_key(datestamp, service)
        signature = hmac.new(
            signing_key, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()

        result = {
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self._access_key}/{credential_scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "x-amz-date": amz_date,
            "Content-Type": "application/json",
        }
        if self._session_token:
            result["x-amz-security-token"] = self._session_token
        return result

    def _build_signed_wss_url(self, wss_endpoint: str) -> str:
        """Build SigV4 signed WSS URL for viewer connection."""
        now = datetime.now(timezone.utc)
        datestamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        parsed = urlparse(wss_endpoint)
        host = parsed.hostname
        path = parsed.path or "/"
        service = "kinesisvideo"
        credential_scope = f"{datestamp}/{self._region}/{service}/aws4_request"

        query_params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-ChannelARN": self._channel_arn,
            "X-Amz-ClientId": "ha-viewer",
            "X-Amz-Credential": f"{self._access_key}/{credential_scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": "300",
            "X-Amz-SignedHeaders": "host",
        }
        if self._session_token:
            query_params["X-Amz-Security-Token"] = self._session_token

        canonical_qs = "&".join(
            f"{quote(k, safe='')}={quote(v, safe='')}"
            for k, v in sorted(query_params.items())
        )
        empty_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        canonical_request = (
            f"GET\n{path}\n{canonical_qs}\nhost:{host}\n\nhost\n{empty_hash}"
        )
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )
        signing_key = self._get_signature_key(datestamp, service)
        signature = hmac.new(
            signing_key, string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        return f"{wss_endpoint}?{canonical_qs}&X-Amz-Signature={signature}"

    def _get_signature_key(self, datestamp: str, service: str) -> bytes:
        """Generate AWS SigV4 signing key."""
        k_date = hmac.new(
            f"AWS4{self._secret_key}".encode(), datestamp.encode(), hashlib.sha256
        ).digest()
        k_region = hmac.new(k_date, self._region.encode(), hashlib.sha256).digest()
        k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
        return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
