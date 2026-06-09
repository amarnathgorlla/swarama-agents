"""
SWARAMA Latency Agent
=====================
Pings all API endpoints 10 times each.
Calculates P50, P95, P99 response times per endpoint.
Compares against thresholds.yaml.

Returns standard agent dict with latency breakdown per endpoint.
"""

import asyncio
import os
import statistics
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
P95_LIMIT = THRESHOLDS.get("latency_p95_ms", 1500)
P99_LIMIT = THRESHOLDS.get("latency_p99_ms", 2500)
PING_COUNT = THRESHOLDS.get("latency_ping_count", 10)

ENDPOINTS_TO_PING = [
    {"name": "health", "url": f"{BACKEND}/health", "method": "get", "auth": False},
    {"name": "services", "url": f"{BACKEND}/api/services", "method": "get", "auth": False},
    {"name": "bookings", "url": f"{BACKEND}/api/bookings", "method": "get", "auth": True},
    {"name": "mechanics_nearby", "url": f"{BACKEND}/api/mechanics/nearby", "method": "get", "auth": True, "params": {"lat": "12.9716", "lng": "77.5946"}},
]


def _percentile(sorted_data: list[float], p: int) -> float:
    if not sorted_data:
        return 0.0
    idx = int((p / 100) * len(sorted_data))
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


async def _ping_endpoint(client: httpx.AsyncClient, endpoint: dict) -> list[float]:
    """Ping an endpoint PING_COUNT times, return list of response times in ms."""
    times = []
    headers = {}
    if endpoint.get("auth"):
        headers["Authorization"] = "Bearer latency-agent-test"

    for _ in range(PING_COUNT):
        t0 = time.monotonic()
        try:
            r = await getattr(client, endpoint["method"])(
                endpoint["url"],
                headers=headers,
                params=endpoint.get("params"),
                timeout=MAX_MS / 1000 + 2,
            )
            elapsed = (time.monotonic() - t0) * 1000
            # Count any response (even 4xx) as received
            times.append(elapsed)
        except (httpx.TimeoutException, httpx.ConnectError):
            times.append(MAX_MS + 1000)  # penalize failed pings heavily
        except Exception:
            times.append(MAX_MS + 1000)
        # Small gap between pings
        await asyncio.sleep(0.1)

    return times


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    failures = []
    endpoint_results = {}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for ep in ENDPOINTS_TO_PING:
            raw_times = await _ping_endpoint(client, ep)
            sorted_times = sorted(raw_times)
            p50 = _percentile(sorted_times, 50)
            p95 = _percentile(sorted_times, 95)
            p99 = _percentile(sorted_times, 99)
            avg = statistics.mean(raw_times)

            ep_failures = []
            if p95 > P95_LIMIT:
                ep_failures.append(f"P95 {p95:.0f}ms exceeds limit {P95_LIMIT}ms")
            if p99 > P99_LIMIT:
                ep_failures.append(f"P99 {p99:.0f}ms exceeds limit {P99_LIMIT}ms")
            if avg > MAX_MS:
                ep_failures.append(f"Average {avg:.0f}ms exceeds limit {MAX_MS}ms")

            endpoint_results[ep["name"]] = {
                "p50_ms": round(p50, 1),
                "p95_ms": round(p95, 1),
                "p99_ms": round(p99, 1),
                "avg_ms": round(avg, 1),
                "min_ms": round(min(raw_times), 1),
                "max_ms": round(max(raw_times), 1),
                "samples": PING_COUNT,
                "passed": len(ep_failures) == 0,
                "failures": ep_failures,
            }

            if ep_failures:
                failures.extend([f"{ep['name']}: {f}" for f in ep_failures])

    duration_ms = int((time.monotonic() - t0) * 1000)
    return {
        "agent": "latency_agent",
        "status": "PASS" if not failures else "FAIL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "failures": failures,
        "details": {
            "endpoints": endpoint_results,
            "thresholds": {"p95_ms": P95_LIMIT, "p99_ms": P99_LIMIT, "avg_ms": MAX_MS},
            "ping_count_per_endpoint": PING_COUNT,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
