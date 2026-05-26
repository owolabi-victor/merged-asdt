# service_layer/twin_api.py
"""
Twin API — lightweight Ditto-compatible REST service.
Stores Things in MongoDB. Exposes the same endpoints as Eclipse Ditto /api/2.

Extended with ASDT UI endpoints:
  - /api/v1/auth/*        — user registration, login, JWT
  - /api/v1/ui/scientist/* — full research dashboard
  - /api/v1/ui/farmer/*    — simplified advisory dashboard
  - /ws/{role}/{user_id}   — WebSocket live updates
"""
import json
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pymongo import MongoClient

from shared.config import MONGO_URI, MONGO_DB

app = FastAPI(title="ASDT Twin API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# ── Mount UI API extensions ──────────────────────────────────────────────────
try:
    from ui.api_extensions import router as ui_router
    app.include_router(ui_router)
    print("[twin_api] ✅ UI API extensions mounted successfully")
except Exception as e:
    import traceback
    print(f"[twin_api] ⚠️ Failed to mount UI API extensions: {type(e).__name__}: {e}")
    traceback.print_exc()


@app.get("/", response_class=HTMLResponse)
def root():
    """Status page — shows when you visit http://localhost:8080 in a browser."""
    things = list(db.things.find({}, {"_id": 0, "thingId": 1}))
    thing_links = "".join(
        f'<li><a href="/api/2/things/{t["thingId"]}">{t["thingId"]}</a></li>'
        for t in things
    )
    return f"""
    <html><head><title>ASDT Twin API</title>
    <style>body{{font-family:monospace;padding:40px;background:#0d1117;color:#c9d1d9}}
    h1{{color:#58a6ff}}a{{color:#79c0ff}}li{{margin:6px 0}}
    .badge{{background:#238636;color:#fff;padding:3px 10px;border-radius:4px;font-size:12px}}</style>
    </head><body>
    <h1>🌱 Agentic Soil Digital Twin — Twin API</h1>
    <p><span class="badge">RUNNING</span></p>
    <h2>Registered Digital Twins</h2>
    <ul>{thing_links or "<li>No twins registered yet — run python main.py --setup</li>"}</ul>
    <h2>Quick Links</h2>
    <ul>
      <li><a href="/api/2/things">GET /api/2/things</a> — list all twins</li>
      <li><a href="/health">GET /health</a> — health check</li>
      <li><a href="/docs">GET /docs</a> — API documentation (Swagger UI)</li>
    </ul>
    <h2>UI Dashboards</h2>
    <ul>
      <li><a href="http://localhost:8501" target="_blank">🔬 Scientist Dashboard</a> (Streamlit, port 8501)</li>
      <li><a href="http://localhost:8502" target="_blank">🌱 Farmer Dashboard</a> (Streamlit, port 8502)</li>
    </ul>
    <h2>Other Services</h2>
    <ul>
      <li><a href="http://localhost:3000" target="_blank">Grafana Dashboard</a></li>
      <li><a href="http://localhost:8086" target="_blank">InfluxDB</a> — time-series data</li>
      <li><a href="http://localhost:7474" target="_blank">Neo4j Browser</a> — knowledge graph</li>
    </ul>
    </body></html>
    """


@app.get("/health")
def health():
    return {"status": "ok", "service": "ASDT Twin API"}


@app.get("/api/2/things")
def list_things():
    return list(db.things.find({}, {"_id": 0}))


@app.get("/api/2/things/{thing_id:path}")
def get_thing(thing_id: str):
    doc = db.things.find_one({"thingId": thing_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, f"Thing '{thing_id}' not found")
    return doc


@app.put("/api/2/things/{thing_id:path}")
async def put_thing(thing_id: str, request: Request):
    body = await request.json()
    body["thingId"] = thing_id
    db.things.replace_one({"thingId": thing_id}, body, upsert=True)
    return body


@app.put("/api/2/policies/{policy_id:path}")
async def put_policy(policy_id: str, request: Request):
    body = await request.json()
    body["policyId"] = policy_id
    db.policies.replace_one({"policyId": policy_id}, body, upsert=True)
    return body


@app.patch("/api/2/things/{thing_id:path}/features/{feature}/properties")
async def patch_feature(thing_id: str, feature: str, request: Request):
    body = await request.json()
    update = {f"features.{feature}.properties.{k}": v for k, v in body.items()}
    db.things.update_one({"thingId": thing_id}, {"$set": update}, upsert=True)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)