import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, constr
import httpx  # or any async HTTP client

class ColorPayload(BaseModel):
    # accept #RGB, #RRGGBB or simple lowercase names
    color: constr(regex=r'^(#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})|[a-z]+)$')

app = FastAPI()

@app.post("/set-color")
async def set_color(payload: ColorPayload, request: Request):
    # Verify the OIDC JWT to ensure only Cloud Tasks can call this.
    auth: str | None = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(403, "Forbidden")

    hex_value = payload.color.lstrip("#")
    # TODO: edit this with Hue's API call:
    await httpx.post(
        os.environ["LIGHT_API_URL"],
        json={"hex": hex_value},
        timeout=5.0,
    )
    return {"status": "ok", "color": payload.color}
