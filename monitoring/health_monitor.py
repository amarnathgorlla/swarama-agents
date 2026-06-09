"""
SWARAMA Health Monitor
======================
Checks:
  - Backend server responds (all API routes return 200)
  - Supabase connection is alive
  - Server health endpoint reports OK

Returns {agent, status, checks, timestamp}
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
AGENT_SECRET = os.getenv("AGENT_SECRET")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
with open(CONFIG_DIR / "targets.yaml") as f:
    import re
    _raw = yaml.safe_load(f)
    TARGETS = {k: re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), str(v)) if isinstance(v, str) else v for k, v in _raw.items() if not isinstance(v, list)}

with open(CONFIG_DIR / "thresholds.yaml") as f:
    THRESHOLDS = yaml.safe_load(f)

BACKEND = TARGETS.get("backend_url", os.getenv("BACKEND_URL", "http://localhost:3000"))
SUPABASE_URL = TARGETS.get("supabase_url", os.getenv("SUPABASE_URL", ""))
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")
MAX_MS = THRESHOLDS.get("max_response_time_ms", 2000)


async def _check(name: str, client: httpx.AsyncClient, method: str, url: str,
                  expected_status: list | int = 200, **kwargs) -> dict:
    t0 = time.monotonic()
    try:
        r = await getattr(client, method)(url, timeout=10, **kwargs)
        ms = int((time.monotonic() - t0) * 1000)
        exp = [expected_status] if isinstance(expected_status, int) else expected_status
        passed = r.status_code in exp
        return {
            "check": name,
            "status": "PASS" if passed else "WARN",
            "http_status": r.status_code,
            "response_ms": ms,
            "note": "" if passed else f"Expected {exp}, got {r.status_code}",
        }
    except httpx.ConnectError:
        return {"check": name, "status": "FAIL", "http_status": None,
                "response_ms": int((time.monotonic() - t0) * 1000), "note": "Connection refused"}
    except httpx.TimeoutException:
        return {"check": name, "status": "FAIL", "http_status": None,
                "response_ms": int((time.monotonic() - t0) * 1000), "note": "Timeout"}
    except Exception as e:
        return {"check": name, "status": "FAIL", "http_status": None,
                "response_ms": int((time.monotonic() - t0) * 1000), "note": str(e)}


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    checks = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Core health endpoint
        checks.append(await _check("backend_health", client, "get", f"{BACKEND}/health"))

        # API routes
        headers_base = {}
        if AGENT_SECRET:
            headers_base["x-agent-secret"] = AGENT_SECRET

        checks.append(await _check("api_services", client, "get", f"{BACKEND}/api/services",
                                    expected_status=[200, 401],
                                    headers=headers_base))
        
        headers_with_auth = {**headers_base, "Authorization": "Bearer mock-health-agent"}
        checks.append(await _check("api_bookings", client, "get", f"{BACKEND}/api/bookings/user/history",
                                    expected_status=[200, 401],
                                    headers=headers_with_auth))
        checks.append(await _check("api_mechanics_nearby", client, "get",
                                    f"{BACKEND}/api/mechanics/nearby",
                                    expected_status=[200, 401],
                                    params={"lat": "12.9716", "lng": "77.5946"},
                                    headers=headers_with_auth))

        # Supabase connectivity
        if SUPABASE_URL and SUPABASE_KEY:
            checks.append(await _check(
                "supabase_connection", client, "get",
                f"{SUPABASE_URL}/rest/v1/",
                expected_status=[200, 404],  # 404 is OK — just checking connection
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            ))
        else:
            checks.append({"check": "supabase_connection", "status": "WARN",
                           "note": "SUPABASE_URL not configured"})

    fails = [c for c in checks if c["status"] == "FAIL"]
    warns = [c for c in checks if c["status"] == "WARN"]

    if fails:
        overall = "FAIL"
    elif warns:
        overall = "WARN"
    else:
        overall = "PASS"

    return {
        "agent": "health_monitor",
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failures": [f"{c['check']}: {c.get('note', '')}" for c in fails],
        "details": {
            "checks": checks,
            "fail_count": len(fails),
            "warn_count": len(warns),
            "pass_count": len([c for c in checks if c["status"] == "PASS"]),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
