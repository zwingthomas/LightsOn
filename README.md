[LightsOn](https://lightson-460317.uc.r.appspot.com/)

LightsOn is a simple web app that lets you pick colors for your Philips Hue lights and view a live webcam snapshot, all in real time. The frontend is built with Flask + JavaScript, and the backend is a FastAPI service running on uvicorn inside Docker. Cloudflare Tunnel (Argo Tunnel) forwards requests from your public domain to your local machine.


⸻

Table of Contents
	•	Features
	•	Architecture
	•	Prerequisites
	•	Installation
	•	Configuration
	•	Running Locally
	•	Backend
	•	Frontend
	•	Deployment
	•	Environment Variables
	•	Contributing
	•	License

⸻

Features
	•	🔆 Pick any color via an interactive color wheel
	•	⏱️ Color change commands are enqueued via Google Cloud Tasks and dispatched securely to Cloudflare Edge
	•	💡 Send color changes to your Philips Hue bridge via FastAPI
	•	📸 View live webcam snapshots polled every 2 s
	•	⚡ Fast, asynchronous backend with FastAPI & uvicorn
	•	🐳 Containerized backend for easy deployment

⸻

Architecture

[Browser]                         
   │                              
   │ 1. User clicks “Enter”       
   ▼                              
Flask Frontend (App Engine)      
   │                              
   │ 2. Enqueue color task or request camera still frame        
   ▼                              
Google Cloud Tasks Queue (if /set-color, else skip to 4.)        
   │ (configured dispatch rate)   
   │                              
   ▼                              
Cloudflare Edge → Argo Tunnel    
   │                              
   │ 3. POST /set-color            
   ▼                              
FastAPI (uvicorn @ localhost:8080)
   │                              
   ├─▶ /set-color → Hue Bridge    
   │                              
   └─▶ /camera/snapshot → OpenCV   
      camera reader thread        

	•	1. Flask frontend enqueues color-change tasks.
	•	2. Cloud Tasks reliably delivers HTTP requests (rate-limited and centralized so no color is skipped) to the backend via Cloudflare Tunnel.
	•	3. FastAPI updates Hue lights or returns a JPEG snapshot.

⸻

Prerequisites
	•	Python 3.9+
	•	Docker & Docker Compose (for backend)
	•	cloudflared (Cloudflare Tunnel)
	•	Google Cloud SDK (for Cloud Tasks & App Engine)
	•	A Philips Hue Bridge on your LAN
	•	A webcam at /dev/video0 (or another device)

⸻

Installation
	1.	Clone the repo

git clone https://github.com/zwingthomas/LightsOn.git
cd LightsOn


	2.	Backend dependencies

cd backend
pip install --upgrade pip
pip install -r requirements.txt


	3.	Frontend dependencies

cd ../LightsOn
pip install Flask requests google-cloud-tasks



⸻

Configuration

Create a .env file in backend/ with:

WEBCAM=</dev/videoN-index-for-OpenCV-(default-0)>
HUE_BRIDGE_IP=<On-your-local-network>
HUE_USERNAME=<your-hue-username>
HUE_LIGHT_IDS=<steps-below>
SERVICE_URL=<cloudflare-dns>
TASK_SERVICE_ACCOUNT_EMAIL=<your-service-account-email-for-gcp-queue>

In LightsOn/main.py, set your Google Cloud project:

export GOOGLE_CLOUD_PROJECT=<your-project-id>

____

Getting your Hue light IDs, Bridge IP, and Username

1. Get your Hue Bridge IP
```
curl https://discovery.meethue.com
```

2. Get or create a Hue API Username (need to press the button on top of the Hue Bridge)
```
curl -X POST -d '{"devicetype":"my_script#terminal"}' http://192.168.1.42/api
```

3. Get the list of lights
```
curl http://<BRIDGE_IP>/api/<USERNAME>/lights | jq
```

Optional Shell Script:
```
BRIDGE_IP="192.168.X.X"
USERNAME="your-bridge-username"

curl -s http://$BRIDGE_IP/api/$USERNAME/lights | jq 'to_entries[] | "\(.key): \(.value.name)"'
```
Returns something like this:
```
"1: Living Room Lamp"
"2: Kitchen Light"
```

⸻


# Build & run backend with Docker Compose (exposes :8080)
docker-compose up --build

# Stand up cloudflared and the following command after creating a tunnel [[GUIDE]](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/)
Run the tunnel with
```
cloudflared tunnel --loglevel debug --config ~/.cloudflared/config.yml run <tunnel-id>
```

~/.cloudflared/config.yml
```
tunnel: 17223237-3bc2-4bdb-b4be-0d5b027ba881
credentials-file: ~/.cloudflared/17223237-3bc2-4bdb-b4be-0d5b027ba881.json
ingress:
  - hostname: zwingerbackend.com
    path: /camera/snapshot
    service: http://localhost:8080
  - hostname: zwingerbackend.com
    path: /set-color
    service: http://localhost:8080
  - hostname: zwingerbackend.com
    path: /health
    service: http://localhost:8080
  - service: http_status:404
originRequest:
  keepAliveTimeout: 35s
  tcpKeepAlive: 60s
no-quic: true
protocol: http2
```

# Get running with Google App Engine
Create project
```
gcloud projects create YOUR_PROJECT_ID --name="Your Project Name"
gcloud config set project YOUR_PROJECT_ID
```

Enabled App Engine in desired region as well as Task Queue API
```
gcloud app create --region=us-central
gcloud services enable cloudtasks.googleapis.com
```

Create Task Queue
```
gcloud tasks queues create color-changes \
  --max-dispatches-per-second=0.5 \
  --max-concurrent-dispatches=1 \
  --location=us-central1
```

Deploy and view
```
gcloud app deploy
gcloud app browse
```



⸻

Contributing
	1.	Fork it
	2.	Create a feature branch
	3.	Submit a pull request
    4.  Leave a Star

Please open an issue first if you’re planning a major change.