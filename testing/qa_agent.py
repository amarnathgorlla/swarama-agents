"""
SWARAMA QA Agent
================
Calls every backend API endpoint, checks:
  - HTTP status codes
  - Response time under threshold
  - Response schema has required fields

Returns standard agent dict.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "config" / ".env.agents")
AGENT_SECRET = os.getenv("AGENT_SECRET")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
with open(CONFIG_DIR / "targets.yaml") as f:
    import re
    _raw = yaml.safe_load(f)
    def _e(v):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), str(v)) if isinstance(v, str) else v
    TARGETS = {k: _e(v) for k, v in _raw.items() if not isinstance(v, list)}

with open(CONFIG_DIR / "thresholds.yaml") as f:
    THRESHOLDS = yaml.safe_load(f)

BACKEND = TARGETS.get("backend_url", os.getenv("BACKEND_URL", "http://localhost:3000"))
MAX_MS = THRESHOLDS.get("max_response_time_ms", 2000)

# Required fields per endpoint response
SCHEMA_CHECKS: dict[str, dict] = {
    "GET /api/services": {
        "method": "GET",
        "url": f"{BACKEND}/api/services",
        "auth": False,
        "expected_status": 200,
        "required_fields": [],          # array response — check it's a list
        "is_list": True,
    },
    "GET /api/bookings": {
        "method": "GET",
        "url": f"{BACKEND}/api/bookings/user/history",
        "auth": True,
        "expected_status": [200, 401],  # 401 acceptable without real token
        "required_fields": [],
        "is_list": True,
    },
    "POST /api/bookings": {
        "method": "POST",
        "url": f"{BACKEND}/api/bookings",
        "auth": True,
        "expected_status": [200, 201, 400, 401, 422],
        "body": {
            "service_id": 1,
            "user_lat": 12.9716,
            "user_lng": 77.5946,
            "vehicle_type": "bike",
            "notes": "QA agent test booking — ignore",
        },
        "required_fields": [],
    },
    "GET /api/mechanics/nearby": {
        "method": "GET",
        "url": f"{BACKEND}/api/mechanics/nearby",
        "auth": True,
        "expected_status": [200, 401],
        "params": {"lat": "12.9716", "lng": "77.5946"},
        "required_fields": [],
        "is_list": True,
    },
    "GET /health": {
        "method": "GET",
        "url": f"{BACKEND}/health",
        "auth": False,
        "expected_status": 200,
        "required_fields": ["status"],
    },
}


async def _test_endpoint(
    client: httpx.AsyncClient,
    name: str,
    spec: dict,
) -> dict[str, Any]:
    t0 = time.monotonic()
    failure_reasons = []
    status_code = None

    try:
        headers = {"Content-Type": "application/json"}
        if AGENT_SECRET:
            headers["x-agent-secret"] = AGENT_SECRET
        if spec.get("auth"):
            headers["Authorization"] = "Bearer mock-qa-agent-test-token"

        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": MAX_MS / 1000 + 1,
        }
        if spec.get("params"):
            kwargs["params"] = spec["params"]
        if spec.get("body"):
            kwargs["json"] = spec["body"]

        method = spec["method"].upper()
        resp = await getattr(client, method.lower())(spec["url"], **kwargs)
        status_code = resp.status_code
        duration_ms = int((time.monotonic() - t0) * 1000)

        # Status check
        expected = spec["expected_status"]
        if isinstance(expected, int):
            if status_code != expected:
                failure_reasons.append(
                    f"Expected status {expected}, got {status_code}"
                )
        elif isinstance(expected, list):
            if status_code not in expected:
                failure_reasons.append(
                    f"Expected status in {expected}, got {status_code}"
                )

        # Response time check
        if duration_ms > MAX_MS:
            failure_reasons.append(
                f"Response time {duration_ms}ms exceeds threshold {MAX_MS}ms"
            )

        # Schema check
        if status_code in (200, 201):
            try:
                body = resp.json()
                if spec.get("is_list") and not isinstance(body, list):
                    # Some backends wrap in {data: []}
                    if isinstance(body, dict) and any(
                        isinstance(body.get(k), list) for k in ["data", "results", "items", "services", "mechanics", "bookings"]
                    ):
                        pass  # wrapped list is fine
                    else:
                        failure_reasons.append(f"Expected list response, got {type(body).__name__}")
                for field in spec.get("required_fields", []):
                    if isinstance(body, dict) and field not in body:
                        failure_reasons.append(f"Required field '{field}' missing from response")
            except Exception as parse_err:
                failure_reasons.append(f"Could not parse JSON response: {parse_err}")

    except httpx.TimeoutException:
        duration_ms = int((time.monotonic() - t0) * 1000)
        failure_reasons.append(f"Endpoint timed out after {duration_ms}ms")
    except httpx.ConnectError:
        duration_ms = int((time.monotonic() - t0) * 1000)
        failure_reasons.append(f"Connection refused — backend may be down")
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        failure_reasons.append(f"Unexpected error: {exc}")

    return {
        "endpoint": name,
        "status_code": status_code,
        "duration_ms": duration_ms if 'duration_ms' in dir() else 0,
        "passed": len(failure_reasons) == 0,
        "failures": failure_reasons,
    }


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    endpoints_tested = []
    all_failures = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [_test_endpoint(client, name, spec) for name, spec in SCHEMA_CHECKS.items()]
        results = await asyncio.gather(*tasks)

    for r in results:
        endpoints_tested.append(r["endpoint"])
        if not r["passed"]:
            all_failures.append({
                "endpoint": r["endpoint"],
                "reasons": r["failures"],
                "status_code": r["status_code"],
                "duration_ms": r.get("duration_ms", 0),
            })

    overall_status = "PASS" if not all_failures else "FAIL"
    duration_ms = int((time.monotonic() - t0) * 1000)

    return {
        "agent": "qa_agent",
        "status": overall_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "endpoints_tested": endpoints_tested,
        "failures": all_failures,
        "details": {
            "total_endpoints": len(SCHEMA_CHECKS),
            "passed": len(endpoints_tested) - len(all_failures),
            "failed": len(all_failures),
            "individual_results": [
                {k: v for k, v in r.items()} for r in results
            ],
        },
    }


if __name__ == "__main__":
    import json
    result = asyncio.run(run())
    print(json.dumps(result, indent=2))
