from __future__ import annotations
import asyncio
import time
from typing import Callable

Step = tuple[float, Callable[[], None]]

def _dust(s, hold: float) -> list[Step]:
    z = "MI_ROAD"
    return [(0, lambda: s.set_drivers(z, pm10=300, pm25=50)), (hold, lambda: s.clear_drivers(z))]

def _traffic(s, hold: float) -> list[Step]:
    z = "PANCH_BATTI"
    return [(0, lambda: s.set_drivers(z, pm25=80, pm10=96, queue=46, speed=6)), (hold, lambda: s.clear_drivers(z))]

def _combined(s, hold: float) -> list[Step]:
    return _dust(s, hold) + [(t + 6, fn) for t, fn in _traffic(s, hold)]

SCENARIOS = {
    "dust_spike":    (_dust,     30, "PM10 dust spike at MI Road"),
    "traffic_spike": (_traffic,  25, "PM2.5 traffic spike at Panch Batti"),
    "combined":      (_combined, 30, "Both at once, 6 s apart."),
}

def build(name: str, store, hold_s: float | None = None) -> list[Step]:
    fn, default_hold, _ = SCENARIOS[name]
    return sorted(fn(store, hold_s or default_hold), key=lambda step: step[0])

class DemoDirector:
    def __init__(self, store):
        self.store = store
        self.running: str | None = None
        self._task: asyncio.Task | None = None
    def start(self, name: str, hold_s: float | None = None, fresh: bool = True) -> dict:
        if name not in SCENARIOS: raise KeyError(name)
        self.cancel()
        if fresh: self.store.reset()
        steps = build(name, self.store, hold_s)
        self._task = asyncio.create_task(self._play(name, steps))
        return {"scenario": name, "steps": len(steps), "fresh": fresh}
    def reset(self) -> None:
        self.cancel()
        self.store.reset()
    def cancel(self) -> None:
        if self._task and not self._task.done(): self._task.cancel()
        self._task, self.running = None, None
    async def _play(self, name: str, steps: list[Step]) -> None:
        self.running = name
        try:
            t0 = time.monotonic()
            for at, action in steps:
                await asyncio.sleep(max(0.0, at - (time.monotonic() - t0)))
                action()
        finally:
            self.running = None