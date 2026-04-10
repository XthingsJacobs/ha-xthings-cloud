"""Constants for Xthings Cloud API."""

API_BASE_URL = "https://api.cloud.xthings.com/ha"

# Auth endpoints
API_LOGIN = f"{API_BASE_URL}/auth/login"
API_REFRESH_TOKEN = f"{API_BASE_URL}/auth/refresh"

# Device endpoints
API_DEVICES = f"{API_BASE_URL}/device"
API_DEVICE_STATUS = f"{API_BASE_URL}/device/status"

# Switch control
API_SWITCH_ON = f"{API_BASE_URL}/device/switch/command/on"
API_SWITCH_OFF = f"{API_BASE_URL}/device/switch/command/off"
API_SWITCH_BRIGHTNESS = f"{API_BASE_URL}/device/switch/command/brightness"

# Plug control
API_PLUG_ON = f"{API_BASE_URL}/device/plug/command/on"
API_PLUG_OFF = f"{API_BASE_URL}/device/plug/command/off"

# Light (Brite) control
API_BRITE_ON = f"{API_BASE_URL}/device/brite/command/on"
API_BRITE_OFF = f"{API_BASE_URL}/device/brite/command/off"
API_BRITE_BRIGHTNESS = f"{API_BASE_URL}/device/brite/command/brightness"
API_BRITE_COLOR = f"{API_BASE_URL}/device/brite/command/color"

# Lock control
API_LOCK_LOCK = f"{API_BASE_URL}/device/lock/command/lock"
API_LOCK_UNLOCK = f"{API_BASE_URL}/device/lock/command/unlock"

# Camera
API_CAMERA_WEBRTC = f"{API_BASE_URL}/device/camera/command/webrtc"

# FRP remote access
API_FRP_HTTP = f"{API_BASE_URL}/frp/get/http"

# Auth error codes that trigger re-login
AUTH_ERROR_CODES = {20001, 20011, 20012, 21022}

# WebSocket
WS_URL = "wss://api.cloud.xthings.com/api/ws"
