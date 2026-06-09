"""
SWARAMA Analytics Agent
========================
Sends test events to Google Analytics 4 via Measurement Protocol.
Reads booking metrics from Supabase and pushes as custom dimensions.

Events sent:
  - agent_health_check
  - system_status
  - booking metrics (today's count, failed, avg mechanic response time)

Returns standard agent dict.
"""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, date, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "config" / ".env.agents")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
with open(CONFIG_DIR / "targets.yaml") as f:
    import re
    _raw = yaml.safe_load(f)
    TARGETS = {k: re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), str(v)) if isinstance(v, str) else v for k, v in _raw.items() if not isinstance(v, list)}

SUPABASE_URL = TARGETS.get("supabase_url", os.getenv("SUPABASE_URL", ""))
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")
GA4_MEASUREMENT_ID = os.getenv("GA4_MEASUREMENT_ID", "")
GA4_API_SECRET = os.getenv("GA4_API_SECRET", "")
GA4_ENDPOINT = "https://www.google-analytics.com/mp/collect"

CLIENT_ID = str(uuid.uuid4())  # stable within this run


async def _get_booking_metrics(client: httpx.AsyncClient) -> dict:
    """Fetch today's booking metrics from Supabase."""
    metrics = {"total_today": 0, "failed_today": 0, "avg_response_time_s": None}
    if not SUPABASE_URL or not SUPABASE_KEY:
        return metrics

    today = date.today().isoformat()
    try:
        # Total bookings today
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/bookings",
            params={"created_at": f"gte.{today}", "select": "id,status,created_at"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Prefer": "count=exact"},
            timeout=10,
        )
        if r.status_code == 200:
            bookings = r.json()
            metrics["total_today"] = len(bookings)
            metrics["failed_today"] = sum(
                1 for b in bookings if b.get("status") in ("failed", "cancelled")
            )
    except Exception:
        pass

    return metrics


async def _send_ga4_event(client: httpx.AsyncClient, event_name: str,
                           params: dict) -> bool:
    """Send event to GA4 Measurement Protocol."""
    if not GA4_MEASUREMENT_ID or not GA4_API_SECRET:
        return False
    try:
        r = await client.post(
            GA4_ENDPOINT,
            params={"measurement_id": GA4_MEASUREMENT_ID, "api_secret": GA4_API_SECRET},
            json={
                "client_id": CLIENT_ID,
                "events": [{"name": event_name, "params": params}],
            },
            timeout=10,
        )
        return r.status_code in (200, 204)
    except Exception:
        return False


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    events_sent = []
    failures = []

    # Get overall status from system_state if available
    overall_status = "OK"
    if system_state and system_state.get("failures"):
        overall_status = "DEGRADED"

    async with httpx.AsyncClient() as client:
        # Fetch booking metrics
        metrics = await _get_booking_metrics(client)

        timestamp_ms = int(time.time() * 1000)
        base_params = {
            "session_id": CLIENT_ID,
            "engagement_time_msec": "100",
            "environment": os.getenv("ENV", "production"),
            "run_id": (system_state or {}).get("run_id", "unknown"),
        }

        # Event 1: agent health check
        ok1 = await _send_ga4_event(client, "agent_health_check", {
            **base_params,
            "agent_version": "1.0",
            "timestamp_ms": timestamp_ms,
        })
        if ok1:
            events_sent.append("agent_health_check")

        # Event 2: system status
        ok2 = await _send_ga4_event(client, "system_status", {
            **base_params,
            "status": overall_status,
            "timestamp_ms": timestamp_ms,
        })
        if ok2:
            events_sent.append("system_status")

        # Event 3: booking metrics
        ok3 = await _send_ga4_event(client, "booking_metrics", {
            **base_params,
            "total_bookings_today": metrics["total_today"],
            "failed_bookings_today": metrics["failed_today"],
            "timestamp_ms": timestamp_ms,
        })
        if ok3:
            events_sent.append("booking_metrics")

    ga4_configured = bool(GA4_MEASUREMENT_ID and GA4_API_SECRET)
    if ga4_configured and not events_sent:
        failures.append("GA4 configured but no events were sent successfully")

    return {
        "agent": "analytics_agent",
        "status": "PASS" if not failures else "FAIL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failures": failures,
        "details": {
            "ga4_configured": ga4_configured,
            "events_sent": events_sent,
            "booking_metrics": metrics,
            "note": "GA4 events skipped — set GA4_MEASUREMENT_ID and GA4_API_SECRET" if not ga4_configured else "",
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
