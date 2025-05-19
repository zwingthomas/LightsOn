import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, constr
from google.auth.transport import requests as grequests
from google.oauth2 import id_token
import httpx
from dotenv import load_dotenv
load_dotenv()

# -----------------------------------------------------------------------------
# Pydantic Model
# -----------------------------------------------------------------------------

class ColorPayload(BaseModel):

    color: constr(
        pattern=r'^(#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})|[a-z]+)$'
    )

# -----------------------------------------------------------------------------
# Helpers: token verification & color conversion
# -----------------------------------------------------------------------------

async def verify_cloud_task(request: Request) -> None:
    """
    Verifies the incoming OIDC JWT came from our Cloud Tasks queue/SA.
    Raises HTTPException(403) on failure.
    """
    header = request.headers.get("Authorization")
    if not header:
        raise HTTPException(403, "Missing Authorization header")

    parts = header.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer":
        raise HTTPException(403, "Malformed Authorization header")

    jwt = parts[1]
    try:
        id_info = id_token.verify_oauth2_token(
            jwt,
            grequests.Request(),
            audience=os.environ["CLOUD_RUN_SERVICE_URL"]
        )
    except ValueError:
        raise HTTPException(403, "Invalid OIDC token")

    if id_info.get("email") != os.environ["TASK_SERVICE_ACCOUNT_EMAIL"]:
        raise HTTPException(403, "Unauthorized caller")

def hex_to_rgb(hexstr: str) -> tuple[float,float,float]:
    """Convert “#RRGGBB” → (r, g, b) in [0,1]"""
    h = hexstr.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return r, g, b

def rgb_to_xy(r: float, g: float, b: float) -> tuple[float,float]:
    """
    Convert linearized sRGB → CIE 1931 xy
    (per Philips Hue color math documentation).
    """
    # Gamma correction
    def gamma(v):
        return ((v + 0.055) / 1.055) ** 2.4 if v > 0.04045 else v / 12.92

    r_lin = gamma(r)
    g_lin = gamma(g)
    b_lin = gamma(b)

    # Convert to XYZ
    X = r_lin * 0.4124 + g_lin * 0.3576 + b_lin * 0.1805
    Y = r_lin * 0.2126 + g_lin * 0.7152 + b_lin * 0.0722
    Z = r_lin * 0.0193 + g_lin * 0.1192 + b_lin * 0.9505

    total = X + Y + Z
    if total == 0:
        return 0.0, 0.0
    return X / total, Y / total

# -----------------------------------------------------------------------------
# App & handler
# -----------------------------------------------------------------------------

app = FastAPI()

@app.post("/set-color")
async def set_color(payload: ColorPayload, request: Request):
    await verify_cloud_task(request)

    # convert hex → xy
    r, g, b = hex_to_rgb(payload.color.lstrip("#"))
    x, y = rgb_to_xy(r, g, b)

    bridge = os.environ["HUE_BRIDGE_IP"]
    user   = os.environ["HUE_USERNAME"]
    # split the comma-delimited list of IDs
    light_ids = os.environ["HUE_LIGHT_IDS"].split(",")

    body = {"on": True, "bri": 254, "xy": [x, y]}

    async with httpx.AsyncClient(timeout=5.0) as client:
        for lid in light_ids:
            url = f"http://{bridge}/api/{user}/lights/{lid}/state"
            resp = await client.put(url, json=body)
            resp.raise_for_status()

    return {"status": "ok", "color": payload.color, "updated": light_ids}
