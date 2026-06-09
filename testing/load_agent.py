"""
SWARAMA Load Agent
==================
Fires 50 concurrent POST /api/bookings requests via asyncio + httpx.
Measures: success rate, average response time, max response time, failures.
If success rate < 95% or avg response > threshold → FAIL.

Returns standard agent dict with load metrics.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
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

with open(CONFIG_DIR / "thresholds.yaml") as f:
    THRESHOLDS = yaml.safe_load(f)

BACKEND = TARGETS.get("backend_url", os.getenv("BACKEND_URL", "http://localhost:3000"))
MAX_MS = THRESHOLDS.get("max_response_time_ms", 2000)
MIN_SUCCESS_RATE = THRESHOLDS.get("min_load_success_rate_percent", 95)
CONCURRENT = THRESHOLDS.get("load_concurrent_requests", 50)

LOAD_PAYLOAD = {
    "service_id": "load-test-service",
    "location": {"lat": 12.9716, "lng": 77.5946},
    "notes": "Load agent test — auto-generated, please ignore",
}
LOAD_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer load-agent-test",
    "X-Agent": "swarama-load-agent",
}


async def _single_request(client: httpx.AsyncClient, req_id: int) -> dict:
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{BACKEND}/api/bookings",
            json={**LOAD_PAYLOAD, "load_test_id": req_id},
            headers=LOAD_HEADERS,
            timeout=MAX_MS / 1000 + 3,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        # Accept any non-5xx as "success" in load testing context
        success = r.status_code < 500
        return {
            "id": req_id,
            "status_code": r.status_code,
            "duration_ms": duration_ms,
            "success": success,
            "error": None,
        }
    except httpx.TimeoutException:
        return {
            "id": req_id,
            "status_code": None,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "success": False,
            "error": "timeout",
        }
    except httpx.ConnectError:
        return {
            "id": req_id,
            "status_code": None,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "success": False,
            "error": "connection_refused",
        }
    except Exception as exc:
        return {
            "id": req_id,
            "status_code": None,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "success": False,
            "error": str(exc),
        }


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    failures = []

    # Use a connection pool sized for concurrent load
    limits = httpx.Limits(max_connections=CONCURRENT + 10, max_keepalive_connections=CONCURRENT)
    async with httpx.AsyncClient(follow_redirects=True, limits=limits) as client:
        tasks = [_single_request(client, i) for i in range(CONCURRENT)]
        results = await asyncio.gather(*tasks)

    total = len(results)
    successes = [r for r in results if r["success"]]
    errors = [r for r in results if not r["success"]]

    success_count = len(successes)
    success_rate = (success_count / total) * 100 if total > 0 else 0

    durations = [r["duration_ms"] for r in successes]
    avg_ms = int(sum(durations) / len(durations)) if durations else 0
    max_ms = max(durations) if durations else 0
    min_ms = min(durations) if durations else 0

    error_summary: dict[str, int] = {}
    for e in errors:
        key = e.get("error") or f"http_{e.get('status_code')}"
        error_summary[key] = error_summary.get(key, 0) + 1

    # Threshold checks
    if success_rate < MIN_SUCCESS_RATE:
        failures.append(
            f"Success rate {success_rate:.1f}% below threshold {MIN_SUCCESS_RATE}%"
        )
    if avg_ms > MAX_MS:
        failures.append(
            f"Average response time {avg_ms}ms exceeds threshold {MAX_MS}ms"
        )

    # All connection refused = backend is down, not a load failure
    if error_summary.get("connection_refused", 0) == total:
        failures = ["Backend connection refused — load test could not run"]

    status = "PASS" if not failures else "FAIL"
    duration_ms = int((time.monotonic() - t0) * 1000)

    return {
        "agent": "load_agent",
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "failures": failures,
        "details": {
            "concurrent_requests": CONCURRENT,
            "total_requests": total,
            "success_count": success_count,
            "failure_count": len(errors),
            "success_rate_percent": round(success_rate, 2),
            "avg_response_ms": avg_ms,
            "max_response_ms": max_ms,
            "min_response_ms": min_ms,
            "error_breakdown": error_summary,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
