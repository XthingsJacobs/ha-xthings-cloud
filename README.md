# ha-xthings-cloud

Async Python client for the [Xthings Cloud](https://cloud.xthings.com) API, designed for [Home Assistant](https://www.home-assistant.io/) integration.

## Installation

Requires Python 3.10 or later.

```bash
pip install ha-xthings-cloud
```

To use the current source checkout, run this from the repository root:

```bash
pip install -e ./pypi/ha-xthings-cloud
```

New methods in this checkout are not available in an already published package until a release containing them is installed.

## Usage

### API Client

The caller owns the `aiohttp.ClientSession` and is responsible for closing it. Login stores the returned token on the client; subsequent authenticated requests send it in the `x-token` header.

```python
import asyncio
import os

import aiohttp

from ha_xthings_cloud import XthingsCloudApiClient


async def main():
    async with aiohttp.ClientSession() as session:
        client = XthingsCloudApiClient(session)
        result = await client.async_login(
            os.environ["XTHINGS_EMAIL"],
            os.environ["XTHINGS_PASSWORD"],
            client_id="your-persistent-instance-id",
        )
        devices = await client.async_get_devices()
        print(f"Found {len(devices)} devices")

        # Use actual device UUIDs when controlling devices.
        # await client.async_switch_on("device-uuid")
        # await client.async_brite_brightness("device-uuid", 80)
        # await client.async_lock_lock("device-uuid")

        if client.is_token_expired() and result["refresh_token"]:
            result = await client.async_refresh_token(result["refresh_token"])


asyncio.run(main())
```

An existing token can also be supplied with `XthingsCloudApiClient(session, token=token)`. Token refresh updates `client.token`, but is not automatic: the caller must retain the refresh token and decide when to refresh or log in again. The current login method accepts email, password, and an optional client ID; it does not expose a 2FA verification flow.

### Home Assistant Binding and Remote Access

These methods require an authenticated client. In the Home Assistant integration:

- `uuid` is the persistent HA instance ID, also used as `client_id` when requesting FRP configuration. It is not an individual device UUID.
- `version` is the HA Core version, not the integration or FRP version.
- `model` is the fixed string `Home-Assistant`.

The following helper shows the cloud requests used when enabling access. Supply the real instance ID and HA version from the application:

```python
async def bind_and_get_frp_config(client, instance_id, ha_version):
    await client.async_bind(
        uuid=instance_id,
        version=ha_version,
        model="Home-Assistant",
    )
    return await client.async_get_frp_config(client_id=instance_id)
```

When disabling access, the application should stop its local FRP process first, then call `await client.async_unbind(instance_id)`. A failed cloud request must not prevent the local tunnel from being stopped.

The client currently uses these routes:

| Method | Endpoint | JSON Body |
|--------|----------|-----------|
| `async_bind(uuid, version, model, sso_ha_user_id)` | `POST https://api.cloud.xthings.com/ha/sso/bind` | `{"instance_id": "<instance-id>", "sso_ha_user_id": "<ha-user-id>"}` |
| `async_unbind(uuid)` | `POST https://api.cloud.xthings.com/ha/unbind` | `{"uuid": "<instance-id>"}` |
| `async_get_frp_config(client_id)` | `POST https://api.cloud.xthings.com/ha/frp/get/http` | `{"uuid": "<instance-id>"}` |

Each method returns the response envelope's `data` value on success. The server must expose these routes; an HTTP 404 may indicate an incorrect endpoint or an unavailable route. Check the endpoint, gateway routing, and deployment rather than treating a 404 alone as proof of invalid credentials.

This package only handles the cloud API calls. Downloading FRP binaries, generating TOML configuration, and starting or stopping the FRP process belong to the Home Assistant integration, not this client library. In the current integration, binding succeeds before FRP setup proceeds; a normal restart or reload with the option enabled repeats binding and FRP setup, while an unload only stops the local tunnel and does not unbind the instance.

### WebSocket (Real-time Push)

`async_start()` starts a background connection loop and returns immediately. Keep the session open until `async_stop()` completes. The status callback is synchronous; the token-expired callback is awaited.

```python
from ha_xthings_cloud import XthingsCloudWebSocket


async def watch_devices(session, client, refresh_token, stop_event):
    def on_status(device_uuid, status):
        print(f"Device {device_uuid}: {status}")

    async def on_token_expired():
        nonlocal refresh_token
        result = await client.async_refresh_token(refresh_token)
        refresh_token = result["refresh_token"]
        ws.token = result["token"]

    ws = XthingsCloudWebSocket(
        session, client.token, on_status, on_token_expired
    )
    try:
        await ws.async_start()
        await stop_event.wait()
    finally:
        await ws.async_stop()
```

Pass an authenticated client, its refresh token, and an `asyncio.Event` that the application sets when it is ready to stop. Updating `ws.token` supplies the token used on the next WebSocket login; it does not itself force a reconnect or reauthenticate an existing connection.

### KVS Signaling (Camera WebRTC)

Create `KvsSignalingClient(session, region, channel_arn, credentials)` using the camera's signaling details. The credentials mapping requires `AccessKeyId` and `SecretAccessKey`, and accepts `SessionToken` for temporary credentials. Keep these values private.

Within the application's async WebRTC lifecycle:

```python
from ha_xthings_cloud import KvsSignalingClient


async def run_signaling(
    session, region, channel_arn, credentials, offer_sdp, callback, stop_event
):
    kvs = KvsSignalingClient(session, region, channel_arn, credentials)
    try:
        answer_sdp = await kvs.async_get_answer_sdp(
            offer_sdp, on_ice_candidate=callback
        )
        if answer_sdp is None:
            return
        # Apply answer_sdp to the application's WebRTC peer connection.
        # Send local ICE candidates with kvs.async_send_ice_candidate(...).
        await stop_event.wait()
    finally:
        await kvs.async_close()
```

The caller supplies the offer, synchronous ICE-candidate callback, and stop event, and manages the WebRTC peer connection itself; this example only demonstrates signaling-client lifetime.

## Features

- Async HTTP client with `aiohttp.ClientSession` injection
- Authentication: email/password login, token refresh, and existing-token reuse
- Device control: Switch, Plug, Light (brightness/HS color/color temp), Lock, Camera
- WebSocket: real-time device status push with auto-reconnect
- KVS Signaling: AWS Kinesis Video Streams WebRTC SDP exchange with SigV4 auth
- Home Assistant instance binding and unbinding
- FRP remote access configuration retrieval (no local process management)

## API Reference

### XthingsCloudApiClient

| Method | Description |
|--------|-------------|
| `async_login(email, password, client_id=None)` | Log in and store the returned token |
| `async_refresh_token(refresh_token)` | Refresh and store the auth token |
| `is_token_expired()` | Check local JWT expiry, including a 60-second margin; does not validate the token with the server |
| `async_get_devices()` | Get device list |
| `async_get_device_status(device_id)` | Get single device status |
| `async_switch_on/off(device_id)` | Switch control |
| `async_switch_brightness(device_id, brightness)` | Switch brightness (0-100) |
| `async_plug_on/off(device_id)` | Plug control |
| `async_brite_on/off(device_id)` | Light on/off |
| `async_brite_brightness(device_id, brightness)` | Light brightness (0-100) |
| `async_brite_color(device_id, color)` | Light color (HS/color temp) |
| `async_lock_lock/unlock(device_id)` | Lock control |
| `async_get_camera_webrtc(device_id)` | Get camera KVS credentials |
| `async_bind(uuid, version, model)` | Bind a Home Assistant instance to the cloud account |
| `async_unbind(uuid)` | Unbind a Home Assistant instance |
| `async_get_frp_config(client_id)` | Get FRP remote access config using the instance ID |
| `async_get_snapshot(url)` | Get image bytes, or `None` on failure |

`on/off` and `lock/unlock` in the table are shorthand for separate methods, such as `async_switch_on()` and `async_switch_off()`.

### XthingsCloudWebSocket

| Method | Description |
|--------|-------------|
| `async_start()` | Start WebSocket with auto-reconnect |
| `async_stop()` | Stop WebSocket connection |

### KvsSignalingClient

| Method | Description |
|--------|-------------|
| `async_get_answer_sdp(offer_sdp, on_ice_candidate)` | SDP offer/answer exchange |
| `async_send_ice_candidate(candidate, sdp_mid, sdp_mline_index)` | Send ICE candidate |
| `async_get_ice_server_config()` | Get TURN/STUN servers |
| `async_close()` | Close signaling connection |

## Error Handling

Cloud API operations use POST requests with JSON bodies and expect a response envelope such as `{"code": 200, "data": {...}}`. HTTP success alone is not sufficient: the JSON `code` must also equal `200`. Snapshot downloads and KVS signaling use their own request flows.

- `XthingsCloudAuthError` is raised for cloud authentication codes `20001`, `20011`, `20012`, and `21022`.
- `XthingsCloudApiError` is raised for other cloud error codes and wrapped `aiohttp.ClientError` failures, including HTTP 404 responses.
- `XthingsCloudAuthError` inherits from `XthingsCloudApiError`, so catch authentication failures first if they need separate handling.
- The exception's `code` contains the cloud response code when available; transport and HTTP failures use the default `0`.
- Request timeouts can propagate as `TimeoutError`. The library does not automatically retry, refresh tokens, or roll back binding after a later FRP failure.

```python
from ha_xthings_cloud import XthingsCloudApiError, XthingsCloudAuthError


async def get_devices_safely(client):
    try:
        return await client.async_get_devices()
    except XthingsCloudAuthError:
        # Refresh the token or request a new login in the application.
        raise
    except (XthingsCloudApiError, TimeoutError):
        # Let the application report the error or schedule a retry.
        raise
```

## Debug Logging

Enable the `ha_xthings_cloud` logger to inspect HTTP requests, responses, and WebSocket activity. For example, inside a Home Assistant `logger` configuration:

```yaml
logger:
  logs:
    ha_xthings_cloud: debug
```

HTTP debug logging masks fields named `password`, `token`, `refresh_token`, and `x-token`. This is not comprehensive sanitization: FRP responses may contain `auth_token` or `metadatas_jwt`, and incoming WebSocket messages are logged as received. Review and redact logs before sharing them, and disable debug logging when troubleshooting is complete.

## License

Apache-2.0
