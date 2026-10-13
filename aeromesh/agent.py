from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import AsyncIterator
import asyncio
import logging

try:
    import google.generativeai as genai
except ImportError:
    genai = None

log = logging.getLogger("aeromesh")

# 👇 Yahan tumhari Gemini API key set kar di gayi hai
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6IFvU_150bcJxsD3mIJdD270TVjrnIG2W8Ak4XJSZQfPA")
if genai:
    genai.configure(api_key=GEMINI_API_KEY)

MODEL_NAME = "gemini-1.5-pro-latest"  # Flash model fast response deta hai

try:
    SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "aci_system.txt").read_text()
except:
    SYSTEM_PROMPT = "You are AeroMesh Central Intelligence (ACI)."

MAX_HISTORY_TURNS = 8
_TAG = re.compile(r"</?\s*live_state[^>]*>", re.I)

def _clean(text: str) -> str:
    return _TAG.sub("", text)[:2000]

def build_messages(user_msg: str, history: list[dict], snapshot: dict) -> list[dict]:
    msgs = []
    for h in history[-MAX_HISTORY_TURNS:]:
        role = "model" if h["role"] == "assistant" else "user"
        msgs.append({"role": role, "parts": [_clean(h["content"])]})
    
    state = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False)
    final_prompt = f"<live_state>\n{state}\n</live_state>\n\n{_clean(user_msg)}"
    msgs.append({"role": "user", "parts": [final_prompt]})
    return msgs

async def stream_answer(user_msg: str, history: list[dict], snapshot: dict) -> AsyncIterator[str]:
    # Agar SDK nahi hai ya Key fail ho jaye, toh ye direct error throw karega (jo main.py me fallback ko chalu kar dega)
    if not genai:
        raise RuntimeError("google-generativeai not installed")

    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=SYSTEM_PROMPT
    )
    
    formatted_history = build_messages(user_msg, history, snapshot)
    
    try:
        response = await model.generate_content_async(formatted_history, stream=True)
        async for chunk in response:
            if chunk.text:
                yield chunk.text
    except Exception as e:
        log.error(f"Gemini API Error: {e}")
        raise e  # Error aate hi ye main.py ko bolega -> Fallback answer do!

def fallback_answer(user_msg: str, snapshot: dict) -> str:
    """The Dummy/Offline Answer Generator"""
    m = user_msg.lower()
    zone = next((z for z in snapshot["zones"] if z["name"].lower() in m or z["zone_id"].lower().replace("_", " ") in m), None)
    if zone is None:
        names = ", ".join(z["name"] for z in snapshot["zones"])
        return f"**Status:** Please name a zone. Valid zones: {names}. (snapshot {snapshot['as_of']}, simulated feed)"
    lines = [f"**Status:** {zone['name']} is classified {zone['source_class']} (snapshot {snapshot['as_of']}).", "**Evidence:**", f"- PM2.5 {zone['pm25']} µg/m³ (AQI {zone['pm25_aqi']}, {zone['pm25_category']}), trend {zone['trend_5m']['pm25']}", f"- PM10 {zone['pm10']} µg/m³ vs threshold {zone['pm10_threshold']}, trend {zone['trend_5m']['pm10']}", f"- PM2.5/PM10 ratio {zone['pm25_pm10_ratio']}"]
    evs = [e for e in snapshot["recent_events"] if e["zone_id"] == zone["zone_id"]]
    if evs:
        e = evs[-1]
        t = e["trigger"]
        lines.append(f"- Last action {e['action']} ({e['event_id']}, {e['reason_code']}) at {e['ts']}: {t['metric']} {t['value']} vs {t['threshold']}")
        lines.append(f"- Post-action reading: {e['post']}" if e["post"] else "- Post-action effect not yet measurable.")
    return "\n".join(lines)