import os, json
from flask import Flask, request, jsonify, render_template
from google.cloud import tasks_v2
from google.auth import default as google_auth_default

app = Flask(__name__, static_folder="static", template_folder="templates")

PROJECT  = os.environ["GOOGLE_CLOUD_PROJECT"]
QUEUE    = "color-changes"
LOCATION = "us-central1"
TARGET   = "https://light-worker-<your-cloud-run-url>/set-color"

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
            "url": TARGET,
            "headers": {"Content-Type": "application/json"},
            "body": payload,
            "oidc_token": {"service_account_email": f"{PROJECT}@appspot.gserviceaccount.com"},
        }
    }
    tasks.create_task(parent=parent, task=task)
    return jsonify({"queued": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
