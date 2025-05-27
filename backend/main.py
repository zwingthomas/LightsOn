import os
import cv2
import time
import threading
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, constr
from google.auth.transport import requests as grequests
from google.oauth2 import id_token
import httpx
import asyncio
from dotenv import load_dotenv
import threading

load_dotenv()

import logging
logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
logger.warning("Logger is running")

# Shared state
open_time = 0.0

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
    # Check for Authorization header
    header = request.headers.get("Authorization")
    if not header:
        raise HTTPException(403, "Missing Authorization header")

    # Check that Bearer token exists
    parts = header.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer":
        raise HTTPException(403, "Malformed Authorization header")

    # Verify token
    jwt = parts[1]
    try:
        id_info = id_token.verify_oauth2_token(
            jwt,
            grequests.Request(),
            audience=os.environ["SERVICE_URL"]
        )
    except ValueError:
        raise HTTPException(403, "Invalid OIDC token")

    # Check that only the allowed task service account made this request
    if id_info.get("email") != os.environ["TASK_SERVICE_ACCOUNT_EMAIL"]:
        raise HTTPException(403, "Unauthorized caller")

def hex_to_rgb(hexstr: str) -> tuple[float,float,float]:
    # Convert “#RRGGBB” → (r, g, b) numbers betweeen 0 and 1
    h = hexstr.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return r, g, b

def rgb_to_xy(r: float, g: float, b: float) -> tuple[float,float]:
    """
    Convert linearized sRGB → CIE 1931 xy
    (per Philips Hue color math documentation).
    https://stackoverflow.com/questions/20854825/how-do-i-convert-an-rgb-value-to-a-xy-value-for-the-phillips-hue-bulb
    https://github.com/benknight/hue-python-rgb-converter/blob/master/rgbxy/__init__.py
    """
    # Gamma correction
    def gamma(v):
        return ((v + 0.055) / 1.055) ** 2.4 if v > 0.04045 else v / 12.92

    r_lin = gamma(r)
    g_lin = gamma(g)
    b_lin = gamma(b)

    # Convert to XYZ (changed to help with camera saturation)
    X = r_lin * 0.4124 + g_lin * 0.3576 + b_lin * 0.1805
    Y = r_lin * 0.2126 + g_lin * 0.7152 + b_lin * 0.0722
    Z = r_lin * 0.0193 + g_lin * 0.1192 + b_lin * 0.9505

    # Normalize to XY
    total = X + Y + Z
    if total == 0:
        return 0.0, 0.0
    return X / total, Y / total


def frame_reader(source: int):
    global cap, latest_frame

    while True:
        # Open camera feed if feed is closed or broken
        if cap is None or not cap.isOpened():
            # Begin feed
            cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
            if not cap.isOpened():
                print("[frame_reader] cannot open /dev/video0")
                time.sleep(2)
                continue
        # Read one frame
        ret, frame = cap.read()
        if ret and frame is not None:
            with frame_lock:
                # Update global (accessed in endpoint)
                latest_frame = frame.copy()
        else:
            print("[frame_reader] read failed, will retry reopen")
            cap.release()
            cap = None
            time.sleep(0.5)

        # Throttle loop (~1 FPS max)
        time.sleep(1)



# -----------------------------------------------------------------------------
# App & handler
# -----------------------------------------------------------------------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://lightson-460317.uc.r.appspot.com",
                   "https://zwingerbackend.com"],
    allow_methods=["GET","POST"],
    allow_headers=["*"],
)

# Globals for the persistent capture and latest frame
cap = None
latest_frame = None
frame_lock = threading.Lock()

@app.on_event("startup")
def startup_event():
    device = int(os.getenv("WEBCAM", "0"))
    thread = threading.Thread(target=frame_reader, args=(device,), daemon=True)
    thread.start()

# Update both lights simultaneously
async_client = httpx.AsyncClient(timeout=2.0)

@app.post("/set-color")
async def set_color(payload: ColorPayload, request: Request):
    await verify_cloud_task(request)

    # hex → xy
    r, g, b = hex_to_rgb(payload.color.lstrip("#"))
    x, y     = rgb_to_xy(r, g, b)
    body     = {"on": True, "bri": 254, "xy": [x, y]} # Full brightness

    # create tasks for changing lights
    tasks = []
    for lid in os.environ["HUE_LIGHT_IDS"].split(","):
        url = f"http://{os.environ['HUE_BRIDGE_IP']}/api/{os.environ['HUE_USERNAME']}/lights/{lid}/state"
        tasks.append(async_client.put(url, json=body))

    # send requests at the same time
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            logger.error("Hue error: %s", res)
            raise HTTPException(502, "Failed to update one or more lights")
        res.raise_for_status()

    return {"status":"ok","color":payload.color}

@app.get("/camera/snapshot")
def camera_snapshot():
    # Grab latest frame
    with frame_lock:
        frame = latest_frame.copy() if latest_frame is not None else None
    if frame is None:
        logger.warning("503: No frame available yet")    
        raise HTTPException(503, "No frame available yet")
    
    # Saturate it to show color better
    ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    
    if not ok:
        logger.error("[snapshot] JPEG encoding failed")
        raise HTTPException(500, "Encoding error")

    # Return latest frame
    return Response(content=jpeg.tobytes(), media_type="image/jpeg", headers={"Connection": "close"})

# Testing and SRE endpoint
@app.get("/health")
def health():
    return {"ok": True}
