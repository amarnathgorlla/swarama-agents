"""
SWARAMA Uptime Agent
====================
Pings backend URL, Supabase URL every run.
Records response time and status.
Calculates rolling uptime percentage from last 24 readings stored in
  agents/reports/uptime_history.json

Returns standard dict with uptime percentage per service.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "config" / ".env.agents")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
HISTORY_FILE = REPORT_DIR / "uptime_history.json"

with open(CONFIG_DIR / "targets.yaml") as f:
    import re
    _raw = yaml.safe_load(f)
    TARGETS = {k: re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), str(v)) if isinstance(v, str) else v for k, v in _raw.items() if not isinstance(v, list)}

with open(CONFIG_DIR / "thresholds.yaml") as f:
    THRESHOLDS = yaml.safe_load(f)

BACKEND = TARGETS.get("backend_url", os.getenv("BACKEND_URL", "http://localhost:3000"))
SUPABASE_URL = TARGETS.get("supabase_url", os.getenv("SUPABASE_URL", ""))
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")
MIN_UPTIME = THRESHOLDS.get("min_uptime_percent", 99)
HISTORY_WINDOW = 24  # readings

SERVICES_TO_PING = {
    "backend": {"url": f"{BACKEND}/health", "headers": {}},
    "supabase": {
        "url": f"{SUPABASE_URL}/rest/v1/" if SUPABASE_URL else "",
        "headers": {"apikey": SUPABASE_KEY} if SUPABASE_KEY else {},
    },
}


def _load_history() -> dict:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {name: [] for name in SERVICES_TO_PING}


def _save_history(history: dict):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


async def _ping_service(client: httpx.AsyncClient, name: str, cfg: dict) -> dict:
    if not cfg["url"]:
        return {"service": name, "up": False, "response_ms": 0,
                "status_code": None, "note": "URL not configured"}
    t0 = time.monotonic()
    try:
        r = await client.get(cfg["url"], headers=cfg["headers"], timeout=10)
        ms = int((time.monotonic() - t0) * 1000)
        up = r.status_code < 500
        return {"service": name, "up": up, "response_ms": ms,
                "status_code": r.status_code, "note": ""}
    except httpx.ConnectError:
        return {"service": name, "up": False, "response_ms": int((time.monotonic() - t0) * 1000),
                "status_code": None, "note": "Connection refused"}
    except Exception as e:
        return {"service": name, "up": False, "response_ms": int((time.monotonic() - t0) * 1000),
                "status_code": None, "note": str(e)}


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    failures = []
    history = _load_history()
    current_ping = {}
    timestamp = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [_ping_service(client, name, cfg) for name, cfg in SERVICES_TO_PING.items()]
        pings = await asyncio.gather(*tasks)

    for ping in pings:
        name = ping["service"]
        current_ping[name] = ping

        # Append to history
        if name not in history:
            history[name] = []
        history[name].append({
            "timestamp": timestamp,
            "up": ping["up"],
            "response_ms": ping["response_ms"],
        })

        # Keep only last HISTORY_WINDOW readings
        history[name] = history[name][-HISTORY_WINDOW:]

    # Calculate rolling uptime per service
    uptime_stats = {}
    for name, readings in history.items():
        if not readings:
            uptime_stats[name] = {"uptime_percent": 100.0, "readings": 0}
            continue
        up_count = sum(1 for r in readings if r["up"])
        uptime_pct = (up_count / len(readings)) * 100
        uptime_stats[name] = {
            "uptime_percent": round(uptime_pct, 2),
            "readings": len(readings),
            "up_count": up_count,
            "down_count": len(readings) - up_count,
        }
        if uptime_pct < MIN_UPTIME:
            failures.append(
                f"{name}: uptime {uptime_pct:.1f}% below threshold {MIN_UPTIME}%"
            )

    _save_history(history)

    return {
        "agent": "uptime_agent",
        "status": "PASS" if not failures else "FAIL",
        "timestamp": timestamp,
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failures": failures,
        "details": {
            "current_ping": current_ping,
            "uptime_stats": uptime_stats,
            "history_window": HISTORY_WINDOW,
            "history_file": str(HISTORY_FILE),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
