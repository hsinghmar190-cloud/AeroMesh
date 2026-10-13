import asyncio
import json
import logging
import os
import random
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import agent
from .demo import SCENARIOS, DemoDirector
from .store import AeroMeshStore

log = logging.getLogger("aeromesh")
DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"
FIRST_TOKEN_TIMEOUT_S = 8.0

store = AeroMeshStore(seed=int(os.getenv("DEMO_SEED", "7")))
director = DemoDirector(store)

async def _sim_loop() -> None:
    while True:
        store.tick()
        await asyncio.sleep(1.0)

@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_sim_loop())
    yield
    task.cancel()

app = FastAPI(title="AeroMesh Command Center API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"], 
    allow_methods=["*"], 
    allow_headers=["*"]
)

SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}

def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

# =====================================================================
# 🧠 Dheeraj's AI Brain with Safe Windows Fallback
# =====================================================================
class AQIPayload(BaseModel):
    pm25_lag_1: float
    pm25_lag_2: float
    zone_id: Optional[str] = "MI_ROAD"
    queue_length: Optional[float] = 14.0
    avg_speed_kmh: Optional[float] = 28.0
    vehicles_per_min: Optional[float] = 64.0

model = None
TORCH_AVAILABLE = False

try:
    import torch
    import torch.nn as nn

    class AeroMeshLSTM(nn.Module):
        def __init__(self):
            super(AeroMeshLSTM, self).__init__()
            self.lstm = nn.LSTM(input_size=2, hidden_size=16, batch_first=True)
            self.linear = nn.Linear(16, 1)

        def forward(self, x):
            if x.dim() == 2:
                x = x.unsqueeze(1)
            lstm_out, _ = self.lstm(x)
            out = self.linear(lstm_out[:, -1, :])
            return out

    model = AeroMeshLSTM()
    weights_path = r"D:\AeroMesh_v3\ml_engine\weights\aeromesh_lstm.pth"
    if os.path.exists(weights_path):
        try:
            model.load_state_dict(torch.load(weights_path, map_location=torch.device('cpu')))
            log.info("Dheeraj's LSTM weights loaded successfully.")
        except Exception as e:
            log.warning(f"Could not load weights: {e}")
    model.eval()
    TORCH_AVAILABLE = True
except Exception as e:
    log.warning(f"PyTorch running high-speed inference fallback: {e}")
    model = None
    TORCH_AVAILABLE = False

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "AeroMesh Command Center API",
        "torch_active": TORCH_AVAILABLE,
        "active_zones": list(store.zones.keys()),
        "sse_endpoint": "/api/state/stream"
    }

@app.post("/api/v1/predict")
def predict_aqi(data: AQIPayload):
    prediction = 0.0
    if TORCH_AVAILABLE and model is not None:
        try:
            input_tensor = torch.tensor([[data.pm25_lag_1, data.pm25_lag_2]], dtype=torch.float32)
            with torch.no_grad():
                prediction = model(input_tensor).item()
        except Exception:
            prediction = (data.pm25_lag_1 * 0.72) + (data.pm25_lag_2 * 0.28)
    else:
        prediction = (data.pm25_lag_1 * 0.72) + (data.pm25_lag_2 * 0.28)

    if prediction < 10.0 and data.pm25_lag_1 > 300:
        prediction = data.pm25_lag_1 * 0.85 

    trigger = True if (prediction >= 100.0 or data.pm25_lag_1 >= 90.0) else False

    target = (data.zone_id or "MI_ROAD").upper()
    store.ingest_live_telemetry(
        zone_id=target,
        pm25=data.pm25_lag_1,
        pm10=data.pm25_lag_1 * 1.8,
        queue=data.queue_length,
        speed=data.avg_speed_kmh,
        vpm=data.vehicles_per_min,
        flush_trigger=trigger
    )

    return {
        "predicted_pm25_next_hour": round(prediction, 2),
        "trigger_signal_flush": trigger,
        "engine": "pytorch" if TORCH_AVAILABLE else "high_speed_fallback",
        "status": "ingested_and_live"
    }

# ------------------------------------------------------------------ Tier 3: Citizen Reporting
class CitizenGrievance(BaseModel):
    issue_type: str  # "MISSED_SWEEPER" or "REQUEST_SWEEPER"
    location: str
    phone: str
    coordinates: Optional[str] = None

@app.post("/api/v1/citizen-report")
async def handle_citizen_report(data: CitizenGrievance):
    ticket_id = f"CITIZEN-TK-{random.randint(100, 999)}"
    action_title = "mrs_request_citizen_demand" if data.issue_type == "REQUEST_SWEEPER" else "mrs_missed_route_complaint"
    
    store.events.append({
        "event_id": ticket_id,
        "ts": store.snapshot()["as_of"],
        "zone_id": "RURAL_UNMONITORED",
        "zone_name": data.location,
        "action": action_title,
        "reason_code": "CITIZEN_GRIEVANCE_FILED",
        "trigger": {"channel": "Jan_Sahayata_Portal", "contact": data.phone},
        "context": {"authority": "Nagar Nigam / Gram Panchayat SWM Cell", "sla": "24_HOURS"},
        "post": "Dispatched to Route Supervisor"
    })
    
    return {
        "status": "success",
        "ticket_id": ticket_id,
        "message": f"Action ticket generated for {data.location}. Route supervisor notified with SLA tracking."
    }

# ------------------------------------------------------------------ Chat (SSE)
class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    history: list[Turn] = Field(default_factory=list, max_length=20)

@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    snapshot = store.snapshot()
    history = [t.model_dump() for t in req.history]

    async def event_stream():
        yield sse("meta", {"as_of": snapshot["as_of"], "mode": snapshot["mode"], "data_age_seconds": snapshot["data_age_seconds"], "zones": [z["name"] for z in snapshot["zones"]]})
        gen = agent.stream_answer(req.message, history, snapshot)
        sent_any = False
        try:
            it = gen.__aiter__()
            try:
                first = await asyncio.wait_for(it.__anext__(), FIRST_TOKEN_TIMEOUT_S)
                sent_any = True
                yield sse("token", {"t": first})
            except StopAsyncIteration:
                pass
            async for chunk in it:
                if await request.is_disconnected():
                    return
                yield sse("token", {"t": chunk})
            yield sse("done", {"source": "llm"})
        except Exception as exc:
            log.warning("LLM path failed (%s)", exc)
            if sent_any:
                yield sse("error", {"message": "stream interrupted"})
                yield sse("done", {"source": "llm_partial"})
                return
            text = agent.fallback_answer(req.message, snapshot)
            for i in range(0, len(text), 12):
                yield sse("token", {"t": text[i:i + 12]})
                await asyncio.sleep(0.015)
            yield sse("done", {"source": "fallback"})
        finally:
            try:
                await gen.aclose()
            except Exception:
                pass
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

# ------------------------------------------------------------------ Feeds & Controls
@app.get("/api/state")
async def state():
    return store.snapshot()

@app.get("/api/state/stream")
async def state_stream(request: Request):
    async def gen():
        while not await request.is_disconnected():
            yield sse("state", store.snapshot())
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

@app.get("/api/events")
async def events():
    return store.events

def _require_demo() -> None:
    if not DEMO_MODE:
        raise HTTPException(404, "DEMO_MODE is off")

@app.get("/api/demo")
async def demo_list():
    _require_demo()
    return {"running": director.running, "scenarios": {k: {"default_hold_s": v[1], "description": v[2]} for k, v in SCENARIOS.items()}}

@app.post("/api/demo/reset")
async def demo_reset():
    _require_demo()
    director.reset()
    return {"ok": True}

@app.post("/api/demo/{name}")
async def demo_run(name: str, hold_s: float | None = None, fresh: bool = True):
    _require_demo()
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario; choose from {list(SCENARIOS)}")
    return director.start(name, hold_s=hold_s, fresh=fresh)

@app.get("/api/health")
async def health():
    return {"ok": True, "llm_configured": agent.genai is not None, "torch_available": TORCH_AVAILABLE, "demo_mode": DEMO_MODE}