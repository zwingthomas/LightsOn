import os
import cv2
import time
import threading
import random
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

# Shared state
cap = None
latest_frame = None
frame_lock = threading.Lock()
open_time = 0.0

# Vars for RTSP toggle
HOMEBASE_IP   = os.environ["EUFY_HOMEBASE_IP"]
HOMEBASE_PORT = os.environ.get("EUFY_HOMEBASE_PORT", "80")
STATION_ID = os.getenv("EUFY_STATION_ID")
CAMERA_ID  = os.getenv("EUFY_CAMERA_ID")
RTSP_URL = os.environ["EUFY_RTSP_URL"]

# Simon Says
current_sequence: list[str] = []   # ["red", "green", "blue", "yellow"]
_game_lock        = asyncio.Lock() # protects current_sequence / light playback
_round_playing    = False          # True while LEDs are cycling the pattern

# -----------------------------------------------------------------------------
# Pydantic Model
# -----------------------------------------------------------------------------

class ColorPayload(BaseModel):

    color: constr(
        pattern=r'^(#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})|[a-z]+)$'
    )

class RTSPToggle(BaseModel):
    enabled: bool

class SequenceSubmission(BaseModel):
    sequence: list[str]

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

# Simon Says
async def _play_sequence():
    global _round_playing
    async with _game_lock:
        seq = list(current_sequence)   # copy so it doesn’t change mid-play
        _round_playing = True

    try:
        for color in seq:
            await hue_set_color(color)
            await asyncio.sleep(2)
    finally:
        _round_playing = False

# Quick feedback for Simon Says
async def _flash(color: str, times: int = 2, interval: float = .4):
    for _ in range(times):
        await hue_set_color(color)
        await asyncio.sleep(interval)
        await hue_set_color("yellow")   # neutral dim-yellow between flashes
        await asyncio.sleep(interval)

# Simon Says
async def hue_set_color(hex_str: str = None):

    r, g, b       = hex_to_rgb(hex_str.lstrip("#"))
    x, y          = rgb_to_xy(r, g, b)
    payload       = {"on": True, "bri": 254, "xy": [x, y]}

    async with httpx.AsyncClient(timeout=5) as client:
        await asyncio.gather(*[
            client.put(
                f"http://{os.environ['HUE_BRIDGE_IP']}/api/{os.environ['HUE_USERNAME']}/lights/{lid}/state",
                json=payload
            ) for lid in os.environ["HUE_LIGHT_IDS"].split(",")
        ])

def frame_reader(rtsp_url: str):
    global cap, latest_frame, open_time

    while True:
        now = time.time()
        # If we haven’t opened yet, or the stream died, or it's been >150s, reopen
        if cap is None or not cap.isOpened() or (now - open_time) > 30:
            if cap:
                cap.release()
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                "rtsp_transport;tcp|rtsp_flags;prefer_tcp|stimeout;5000000"
            )
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                print(f"[frame_reader] cannot open RTSP {rtsp_url}")
                time.sleep(5)
                continue
            else:
                open_time = now
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
    allow_origins=["https://lightson-460317.uc.r.appspot.com/"],
    allow_methods=["GET","POST"],
    allow_headers=["*"],
)

# Globals for the persistent capture and latest frame
cap = None
latest_frame = None
frame_lock = threading.Lock()

@app.on_event("startup")
def startup_event():
    if not RTSP_URL:
        print("EUFY_RTSP_URL not configured")
        return
    thread = threading.Thread(target=frame_reader, args=(RTSP_URL,), daemon=True)
    thread.start()

@app.post("/set-color")
async def set_color(payload: ColorPayload, request: Request):

    await verify_cloud_task(request)

    # convert hex → xy
    r, g, b = hex_to_rgb(payload.color.lstrip("#"))
    x, y = rgb_to_xy(r, g, b)

    body = {"on": True, "bri": 254, "xy": [x, y]}

    async with httpx.AsyncClient(timeout=5.0) as client:
        for lid in os.environ["HUE_LIGHT_IDS"].split(","):
            url = f"http://{os.environ['HUE_BRIDGE_IP']}/api/{os.environ['HUE_USERNAME']}/lights/{lid}/state"
            resp = await client.put(url, json=body)
            resp.raise_for_status()

    return {"status": "ok", "color": payload.color, "updated": {os.environ['HUE_LIGHT_IDS']}}

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

@app.get("/simon/round")
async def simon_round():
    """
    1) If no game yet, start with one random color.
    2) Kick off background task to play the pattern (if not already playing).
    3) Return `{"length": N}` for the front-end timer logic.
    """
    global current_sequence

    async with _game_lock:
        if not current_sequence:                      # new game
            COLOR_HEX = {
                "red":    "#ff0000",
                "blue":   "#0000ff",
                "green":  "#00ff00",
                "yellow": "#ffff00",
            }
            current_sequence = [random.choice(list(COLOR_HEX))]
        length = len(current_sequence)
        # Launch player if idle
        if not _round_playing:
            asyncio.create_task(_play_sequence())

    return {"length": length}


@app.post("/simon/check")
async def check_sequence(submission: SequenceSubmission):
    """
    Compare player’s input against the authoritative sequence.
    * On success: flash green, extend the sequence, schedule next playback.
    * On failure: flash red, reset to a new single-color sequence.
    """
    global current_sequence

    async with _game_lock:
        correct = submission.sequence == current_sequence

    if correct:
        await _flash("green")
        async with _game_lock:
            # add ONE new random color (could duplicate last – classic Simon)
            current_sequence.append(random.choice(list(COLOR_HEX)))
            # queue the next round playback
            if not _round_playing:
                asyncio.create_task(_play_sequence())
    else:
        await _flash("red")
        async with _game_lock:
            # fresh game with single random color
            current_sequence = [random.choice(list(COLOR_HEX))]
            if not _round_playing:
                asyncio.create_task(_play_sequence())

    return {"correct": correct}



    
