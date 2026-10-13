from __future__ import annotations
import random
import time
from collections import deque
from datetime import datetime
from typing import Callable

AQI_STANDARD = "India NAQI (CPCB)"
PM25_AQI_TRIGGER = 100
PM10_THRESHOLD = 150.0
DUST_RATIO_MAX = 0.40
TRAFFIC_RATIO_MIN = 0.60
RAMP = 0.15
FLUSH_EFFECT_S = 40
FLUSH_COOLDOWN_S = 30
FLUSH_PM25_FACTOR = 0.55
FLUSH_QUEUE_FACTOR = 0.40
FLUSH_SPEED_KMH = 22.0
SPRINKLER_MIN_RUN_S = 45
SPRINKLER_COOLDOWN_S = 20
SPRINKLER_PM10_FACTOR = 0.40
POST_MEASURE_S = 20

BASELINES = {
    "MI_ROAD":     dict(name="MI Road",      pm25=38, pm10=92, queue=14, speed=28),
    "PANCH_BATTI": dict(name="Panch Batti",  pm25=35, pm10=85, queue=12, speed=30),
    "SINDHI_CAMP": dict(name="Sindhi Camp",  pm25=33, pm10=75, queue=10, speed=32),
}

_PM25_BP = [(0, 30, 0, 50), (30, 60, 51, 100), (60, 90, 101, 200), (90, 120, 201, 300), (120, 250, 301, 400), (250, 500, 401, 500)]
_PM10_BP = [(0, 50, 0, 50), (50, 100, 51, 100), (100, 250, 101, 200), (250, 350, 201, 300), (350, 430, 301, 400), (430, 800, 401, 500)]

def _aqi(c: float, bp) -> int:
    for lo, hi, ilo, ihi in bp:
        if c <= hi: return round(ilo + (c - lo) * (ihi - ilo) / (hi - lo))
    return 500

def aqi_category(aqi: int) -> str:
    for limit, name in [(50, "Good"), (100, "Satisfactory"), (200, "Moderate"), (300, "Poor"), (400, "Very Poor")]:
        if aqi <= limit: return name
    return "Severe"

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")

class Zone:
    def __init__(self, zone_id: str, cfg: dict):
        self.zone_id, self.name = zone_id, cfg["name"]
        self.base = {k: float(cfg[k]) for k in ("pm25", "pm10", "queue", "speed")}
        self.drivers = dict(self.base)
        self.vals = dict(self.base)
        self.vpm = 64.0
        self.history: deque = deque(maxlen=400)
        self.signal = "NORMAL_CYCLE"
        self.flush_until = 0.0
        self.last_flush = -1e9
        self.flush_event_id: str | None = None
        self.sprinkler_since: float | None = None
        self.sprinkler_event_id: str | None = None
        self.last_sprinkler_end = -1e9
        self.idling_count = 5
        self.density_level = "MODERATE"
        
        # 🚜 Mechanized Road Sweeping (MRS) & SLA State
        self.sweeper_status = "STANDBY"
        self.sweeper_ticket: dict | None = None
        self.sweeper_dispatched_at: float | None = None

class AeroMeshStore:
    def __init__(self, seed: int = 7, clock: Callable[[], float] = time.time):
        self.seed, self.clock = seed, clock
        self.reset()

    def reset(self) -> None:
        self.rng = random.Random(self.seed)
        self.zones = {zid: Zone(zid, cfg) for zid, cfg in BASELINES.items()}
        self.events: list[dict] = []
        self._pending: list[tuple] = []
        self._evt_n = 0
        now = self.clock()
        for z in self.zones.values():
            for i in range(300, 0, -1):
                z.history.append((now - i, z.vals["pm25"], z.vals["pm10"]))
        self.last_update_ts = now

    def set_drivers(self, zone_id: str, **kw: float) -> None:
        if zone_id in self.zones:
            z = self.zones[zone_id]
            for k, v in kw.items(): z.drivers[k] = float(v)

    def clear_drivers(self, zone_id: str) -> None:
        if zone_id in self.zones:
            z = self.zones[zone_id]
            z.drivers = dict(z.base)

    def ingest_live_telemetry(self, zone_id: str, pm25: float | None = None, pm10: float | None = None, 
                              queue: float | None = None, speed: float | None = None, vpm: float | None = None, 
                              flush_trigger: bool | None = None) -> None:
        if zone_id not in self.zones:
            zone_id = "MI_ROAD"
        z = self.zones[zone_id]
        now = self.clock()
        if pm25 is not None:
            z.vals["pm25"] = float(pm25)
            z.drivers["pm25"] = float(pm25)
        if pm10 is not None:
            z.vals["pm10"] = float(pm10)
            z.drivers["pm10"] = float(pm10)
        elif pm25 is not None:
            z.vals["pm10"] = float(pm25) * 1.8
            z.drivers["pm10"] = float(pm25) * 1.8
        if queue is not None:
            z.vals["queue"] = float(queue)
            z.drivers["queue"] = float(queue)
        if speed is not None:
            z.vals["speed"] = float(speed)
            z.drivers["speed"] = float(speed)
        if vpm is not None:
            z.vpm = float(vpm)
        if flush_trigger:
            z.signal = "GREEN_FLUSH"
            z.flush_until = now + FLUSH_EFFECT_S
        self.last_update_ts = now

    def tick(self) -> None:
        now = self.clock()
        for z in self.zones.values():
            flushing = now < z.flush_until
            eff = dict(z.drivers)
            if flushing:
                eff["pm25"] *= FLUSH_PM25_FACTOR
                eff["queue"] *= FLUSH_QUEUE_FACTOR
                eff["speed"] = max(eff["speed"], FLUSH_SPEED_KMH)
            if z.sprinkler_since is not None:
                eff["pm10"] *= SPRINKLER_PM10_FACTOR
            for k in z.vals:
                noise = self.rng.gauss(0, 0.008) * max(z.vals[k], 1.0)
                z.vals[k] = max(0.0, z.vals[k] + RAMP * (eff[k] - z.vals[k]) + noise)
            if z.signal == "GREEN_FLUSH" and not flushing:
                z.signal = "NORMAL_CYCLE"
            z.history.append((now, z.vals["pm25"], z.vals["pm10"]))
        self._evaluate_rules(now)
        self._resolve_pending(now)
        self.last_update_ts = now

    def derive(self, z: Zone, now: float) -> dict:
        pm25, pm10 = round(z.vals["pm25"], 1), round(z.vals["pm10"], 1)
        ratio = round(pm25 / pm10, 2) if pm10 else None
        aqi25, aqi10 = _aqi(pm25, _PM25_BP), _aqi(pm10, _PM10_BP)
        if pm10 > PM10_THRESHOLD and ratio is not None and ratio < DUST_RATIO_MAX: source = "ROAD_DUST"
        elif aqi25 >= PM25_AQI_TRIGGER: source = ("TRAFFIC_COMBUSTION" if ratio is not None and ratio >= TRAFFIC_RATIO_MIN else "MIXED")
        elif z.sprinkler_since is not None: source = "DUST_UNDER_MITIGATION"
        elif now < z.flush_until: source = "TRAFFIC_UNDER_MITIGATION"
        else: source = "WITHIN_LIMITS"

        return {
            "zone_id": z.zone_id,
            "name": z.name,
            "pm25": pm25,
            "baseline_pm25": round(z.base["pm25"] * 1.15, 1),
            "pm25_aqi": aqi25,
            "pm25_category": aqi_category(aqi25),
            "pm10": pm10,
            "pm10_aqi": aqi10,
            "pm10_threshold": PM10_THRESHOLD,
            "pm25_pm10_ratio": ratio,
            "source_class": source,
            "trend_5m": {"pm25": self._trend(z, 1, now), "pm10": self._trend(z, 2, now)},
            "traffic": {
                "queue_length_veh": round(z.vals["queue"]),
                "avg_speed_kmh": round(z.vals["speed"]),
                "vehicles_per_min": round(z.vpm),
                "signal_state": z.signal
            },
            "weather": {
                "wind_ms": 2.8
            },
            "sprinkler_active": z.sprinkler_since is not None
        }

    @staticmethod
    def _trend(z: Zone, idx: int, now: float, window: float = 300.0) -> str:
        cur = z.history[-1][idx]
        old = next((h[idx] for h in z.history if now - h[0] <= window), cur)
        pct = (cur - old) / old * 100 if old else 0.0
        label = "RISING" if pct > 5 else "FALLING" if pct < -5 else "STABLE"
        return f"{label} {pct:+.0f}%"

    def _evaluate_rules(self, now: float) -> None:
        for z in self.zones.values():
            d = self.derive(z, now)
            # Signal Flush Rule
            if (d["pm25_aqi"] >= PM25_AQI_TRIGGER and now >= z.flush_until and now - z.last_flush >= FLUSH_COOLDOWN_S):
                ev = self._event(now, z, "trigger_signal_flush", "PM25_AQI_THRESHOLD_EXCEEDED", {"metric": "pm25_aqi", "value": d["pm25_aqi"], "threshold": PM25_AQI_TRIGGER}, {"pm25_pm10_ratio": d["pm25_pm10_ratio"], "source_class": d["source_class"], "queue_length_veh_before": d["traffic"]["queue_length_veh"], "avg_speed_kmh_before": d["traffic"]["avg_speed_kmh"]})
                z.flush_until, z.last_flush = now + FLUSH_EFFECT_S, now
                z.signal, z.flush_event_id = "GREEN_FLUSH", ev["event_id"]

            # Tier 1: Fixed Median Sprinklers
            if z.sprinkler_since is None:
                if (d["source_class"] == "ROAD_DUST" and now - z.last_sprinkler_end >= SPRINKLER_COOLDOWN_S):
                    ev = self._event(now, z, "water_sprinkler_active", "PM10_SPIKE_LOW_PM25_RATIO", {"metric": "pm10", "value": d["pm10"], "threshold": PM10_THRESHOLD}, {"pm25_pm10_ratio": d["pm25_pm10_ratio"], "ratio_max": DUST_RATIO_MAX, "pm25": d["pm25"], "source_class": d["source_class"]})
                    z.sprinkler_since, z.sprinkler_event_id = now, ev["event_id"]
            elif (now - z.sprinkler_since >= SPRINKLER_MIN_RUN_S and d["pm10"] < 0.9 * PM10_THRESHOLD):
                self._event(now, z, "water_sprinkler_deactivated", "PM10_BELOW_RECOVERY_LEVEL", {"metric": "pm10", "value": d["pm10"], "threshold": round(0.9 * PM10_THRESHOLD, 1)}, {"ran_for_s": round(now - z.sprinkler_since)}, measure_post=False)
                z.sprinkler_since, z.sprinkler_event_id = None, None
                z.last_sprinkler_end = now

            # 🚜 Tier 2: Mechanized Road Sweeper (MRS) Dispatch + SLA Escalation Engine
            if d["source_class"] == "ROAD_DUST" or d["pm10"] >= 180.0:
                if z.sweeper_status == "STANDBY":
                    z.sweeper_status = "DISPATCHED"
                    z.sweeper_dispatched_at = now
                    ticket_id = f"MRS-TK-{self._evt_n + 1:03d}"
                    z.sweeper_ticket = {
                        "ticket_id": ticket_id,
                        "unit": "MRS-Vacuum-Truck-04",
                        "target_corridor": z.name,
                        "eta_minutes": 15,
                        "sla_breached": False
                    }
                    self._event(
                        now, z, "mrs_sweeper_dispatched", "PM10_ROAD_DUST_EXCEEDED",
                        {"metric": "pm10", "value": d["pm10"], "threshold": 180.0},
                        {"ticket": z.sweeper_ticket, "action": "Wet-Mist + Mechanical Broom Vacuum Sweep"}
                    )
                elif z.sweeper_status == "DISPATCHED" and (now - z.sweeper_dispatched_at > 20.0): # Simulation SLA breach
                    z.sweeper_status = "SLA_BREACHED"
                    z.sweeper_ticket["sla_breached"] = True
                    self._event(
                        now, z, "mrs_sla_breached_escalated", "CLEANING_CREW_SLA_TIMEOUT",
                        {"metric": "response_time", "value": "SLA_EXCEEDED", "threshold": "15_MIN"},
                        {"authority": "Jaipur Nagar Nigam SWM Control Room", "non_compliance_flag": "SEVERITY_HIGH"}
                    )
            elif z.sweeper_status in ["DISPATCHED", "SLA_BREACHED"] and d["pm10"] < PM10_THRESHOLD:
                z.sweeper_status = "STANDBY"
                z.sweeper_ticket = None

    def _event(self, now, z, action, reason, trigger, context, measure_post=True) -> dict:
        self._evt_n += 1
        ev = {"event_id": f"EVT-{self._evt_n:04d}", "ts": _iso(now), "zone_id": z.zone_id, "zone_name": z.name, "action": action, "reason_code": reason, "trigger": trigger, "context": context, "post": None}
        self.events.append(ev)
        if measure_post: self._pending.append((now + POST_MEASURE_S, ev, z, now))
        return ev

    def _resolve_pending(self, now: float) -> None:
        still = []
        for due, ev, z, ev_ts in self._pending:
            if now < due:
                still.append((due, ev, z, ev_ts))
                continue
            d = self.derive(z, now)
            post = {"measured_after_s": round(now - ev_ts)}
            if ev["action"] == "trigger_signal_flush":
                post.update(pm25_aqi=d["pm25_aqi"], pm25=d["pm25"], queue_length_veh=d["traffic"]["queue_length_veh"], avg_speed_kmh=d["traffic"]["avg_speed_kmh"])
            else: post.update(pm10=d["pm10"], pm25_pm10_ratio=d["pm25_pm10_ratio"])
            ev["post"] = post
        self._pending = still

    def active_mitigations(self) -> list[dict]:
        now, out = self.clock(), []
        for z in self.zones.values():
            if now < z.flush_until:
                s = now - z.last_flush
                out.append({"zone_id": z.zone_id, "action": "trigger_signal_flush", "event_id": z.flush_event_id, "seconds_active": round(s), "minutes_active": round(s / 60, 1)})
            if z.sprinkler_since is not None:
                s = now - z.sprinkler_since
                out.append({"zone_id": z.zone_id, "action": "water_sprinkler_active", "event_id": z.sprinkler_event_id, "seconds_active": round(s), "minutes_active": round(s / 60, 1)})
            if z.sweeper_status in ["DISPATCHED", "SLA_BREACHED"]:
                out.append({"zone_id": z.zone_id, "action": f"mrs_sweeper_{z.sweeper_status.lower()}", "event_id": z.sweeper_ticket["ticket_id"] if z.sweeper_ticket else "MRS-TK", "seconds_active": 30, "minutes_active": 0.5})
        return out

    def snapshot(self) -> dict:
        now = self.clock()
        target_z = self.zones.get("MI_ROAD") or list(self.zones.values())[0]
        is_flush = (now < target_z.flush_until) or (target_z.signal == "GREEN_FLUSH")
        is_spr = target_z.sprinkler_since is not None

        return {
            "timestamp": _iso(now),
            "as_of": _iso(now),
            "data_age_seconds": max(0, round(now - self.last_update_ts)),
            "mode": "LIVE_TELEMETRY" if now - self.last_update_ts <= 5.0 else "SIMULATION",
            "aqi_standard": AQI_STANDARD,
            "thresholds": {"pm25_aqi_trigger": PM25_AQI_TRIGGER, "pm10_ug_m3": PM10_THRESHOLD, "dust_ratio_max": DUST_RATIO_MAX, "traffic_ratio_min": TRAFFIC_RATIO_MIN},
            "zones": [self.derive(z, now) for z in self.zones.values()],
            "controller": {
                "zone": target_z.name,
                "flush_active": is_flush,
                "sprinklers_active": is_spr,
                "phase": "GREEN" if is_flush else "CYCLE",
                "confidence": 98.4 if is_flush else 95.0
            },
            "mrs_sweeper": {
                "status": target_z.sweeper_status,
                "ticket": target_z.sweeper_ticket
            },
            "active_mitigations": self.active_mitigations(),
            "recent_events": self.events[-10:]
        }