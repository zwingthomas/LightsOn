import os, json
import requests
from flask import Flask, request, Response, jsonify, render_template, abort
from google.cloud import tasks_v2
from google.auth import default as google_auth_default

app = Flask(__name__, static_folder="static", template_folder="templates")

PROJECT  = os.environ["GOOGLE_CLOUD_PROJECT"]
QUEUE    = "color-changes"
LOCATION = "us-central1"
TARGET   = "https://zwingerbackend.com"

# set up Cloud Tasks client
client, _ = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
tasks = tasks_v2.CloudTasksClient(credentials=client)

@app.route("/")
def index():
    return render_template("index.html")

@app.post("/enqueue-color")
def enqueue_color():
    data = request.json or {}
    color = data.get("color")
    if not color:
        return {"error": "color required"}, 400

    payload = json.dumps({"color": color}).encode()
    parent  = tasks.queue_path(PROJECT, LOCATION, QUEUE)
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": TARGET + "/set-color",
            "headers": {"Content-Type": "application/json"},
            "body": payload,
            "oidc_token": {
                "service_account_email": f"{PROJECT}@appspot.gserviceaccount.com",
                "audience": "https://zwingerbackend.com"
            },
        }
    }
    tasks.create_task(parent=parent, task=task)
    return jsonify({"queued": True})

@app.route("/camera/snapshot")
def camera_snapshot():
    """
    Proxy a JPEG snapshot from the FastAPI worker.
    """
    try:
        # hit the FastAPI /camera/snapshot endpoint
        resp = requests.get(TARGET + "/camera/snapshot", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        app.logger.error(f"Error fetching camera snapshot: {e}")
        # return 502 Bad Gateway so the frontend knows it wasn't your app’s fault
        return ("", 502)

    # mirror the content-type header (should be image/jpeg)
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return Response(resp.content, mimetype=content_type)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


# SIMON SAYS ROUTES

@app.post("/simon/submit")
def simon_submit():
    """
    Proxy the player's color sequence to the FastAPI worker
    and return whether it matches the current round.
    """
    data = request.get_json(force=True, silent=True) or {}
    seq  = data.get("sequence")
    if not isinstance(seq, list):
        return {"error": "sequence must be a list"}, 400

    try:
        resp = requests.post(TARGET + "/simon/check",
                             json={"sequence": seq},
                             timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        app.logger.error(f"Error validating sequence: {e}")
        return {"error": "backend unreachable"}, 502

    return resp.json(), resp.status_code


@app.get("/simon/round")
def simon_round():
    """
    Ask the FastAPI worker how many colors are in the current round.
    Returns: {"length": <int>}
    """
    try:
        resp = requests.get(TARGET + "/simon/round", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        app.logger.error(f"Error fetching round info: {e}")
        return {"error": "backend unreachable"}, 502

    return resp.json(), resp.status_code
