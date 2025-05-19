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

load_dotenv()

# Shared state
cap = None
latest_frame = None
frame_lock = threading.Lock()
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

def frame_reader(rtsp_url: str):
    global cap, latest_frame, open_time

    while True:
        now = time.time()
        # If we haven’t opened yet, or the stream died, or it's been >150s, reopen
        if cap is None or not cap.isOpened() or (now - open_time) > 150:
            if cap:
                cap.release()
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            open_time = now
            if not cap.isOpened():
                print(f"[frame_reader] cannot open RTSP {rtsp_url}")
                time.sleep(5)
                continue
            else:
                print(f"[frame_reader] opened RTSP stream at {rtsp_url}")

        # Read one frame
        ret, frame = cap.read()
        if ret and frame is not None:
            with frame_lock:
                latest_frame = frame.copy()
        else:
            print("[frame_reader] read failed, will retry reopen")
            cap.release()
            cap = None

        # Throttle loop (~30 FPS max)
        time.sleep(0.03)



# -----------------------------------------------------------------------------
# App & handler
# -----------------------------------------------------------------------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://lightson-460317.uc.r.appspot.com"],
    allow_methods=["GET","POST"],
    allow_headers=["*"],
)

# Globals for the persistent capture and latest frame
cap = None
latest_frame = None
frame_lock = threading.Lock()

@app.on_event("startup")
def startup_event():
    rtsp_url = os.environ.get("EUFY_RTSP_URL")
    if not rtsp_url:
        print("EUFY_RTSP_URL not configured")
        return
    thread = threading.Thread(target=frame_reader, args=(rtsp_url,), daemon=True)
    thread.start()
    print("Started RTSP frame reader thread")

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

@app.get("/camera/snapshot")
def camera_snapshot():
    with frame_lock:
        frame = latest_frame.copy() if latest_frame is not None else None

    if frame is None:
        raise HTTPException(503, "No frame available yet")

    ok, jpeg = cv2.imencode(".jpg", frame)
    if not ok:
        print("[snapshot] JPEG encoding failed")
        raise HTTPException(500, "Encoding error")

    return Response(content=jpeg.tobytes(), media_type="image/jpeg")
