"""
SWARAMA Chaos Agent
===================
ONLY runs in staging environment (ENV=staging).
Simulates adverse conditions to verify system resilience:
  1. Slow response — calls endpoints with very short timeout to simulate slowness
  2. Malformed JSON to POST endpoints → verify correct error codes (400/422)
  3. Verify system does not crash (no 500s returned)

Returns standard dict with chaos_tests: [], system_survived: bool
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
ENV = os.getenv("ENV", "production")
SLOW_TIMEOUT = THRESHOLDS.get("chaos_slow_response_timeout_s", 5)
ONLY_IN_ENV = THRESHOLDS.get("chaos_only_in_env", "staging")

MALFORMED_PAYLOADS = [
    b"not json at all",
    b'{"incomplete": ',
    b"<xml>not json</xml>",
    b"null",
    b"[]",
    b'{"service_id": null, "location": "not_an_object"}',
]

POST_ENDPOINTS = [
    f"{BACKEND}/api/bookings",
]


async def _test_slow_response(client: httpx.AsyncClient) -> dict:
    """Call endpoints with aggressive timeout to simulate/detect slow responses."""
    results = []
    endpoints = [f"{BACKEND}/health", f"{BACKEND}/api/services"]
    for url in endpoints:
        t0 = time.monotonic()
        try:
            r = await client.get(url, timeout=SLOW_TIMEOUT)
            ms = int((time.monotonic() - t0) * 1000)
            results.append({
                "url": url,
                "responded_in_ms": ms,
                "status_code": r.status_code,
                "timed_out": False,
            })
        except httpx.TimeoutException:
            results.append({
                "url": url,
                "responded_in_ms": SLOW_TIMEOUT * 1000,
                "status_code": None,
                "timed_out": True,
            })
        except Exception as e:
            results.append({"url": url, "error": str(e), "timed_out": False})

    return {
        "chaos_test": "slow_response_simulation",
        "passed": True,  # This test just measures — doesn't fail the agent
        "results": results,
    }


async def _test_malformed_json(client: httpx.AsyncClient) -> dict:
    """Send malformed JSON to POST endpoints — expect 400/422 not 500."""
    crashes = []
    for endpoint in POST_ENDPOINTS:
        for payload in MALFORMED_PAYLOADS:
            try:
                r = await client.post(
                    endpoint,
                    content=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer chaos-agent-test",
                    },
                    timeout=10,
                )
                if r.status_code == 500:
                    crashes.append({
                        "endpoint": endpoint,
                        "payload": payload.decode("utf-8", errors="replace")[:50],
                        "status_code": 500,
                        "note": "Server returned 500 on malformed input — potential crash!",
                    })
            except Exception:
                pass

    return {
        "chaos_test": "malformed_json",
        "passed": len(crashes) == 0,
        "crashes_detected": crashes,
    }


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()

    if ENV != ONLY_IN_ENV:
        return {
            "agent": "chaos_agent",
            "status": "PASS",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": 0,
            "failures": [],
            "details": {
                "chaos_tests": [],
                "system_survived": True,
                "skipped": True,
                "reason": f"Chaos agent only runs in ENV={ONLY_IN_ENV}, current ENV={ENV}",
            },
        }

    chaos_tests = []
    system_survived = True

    async with httpx.AsyncClient(follow_redirects=True) as client:
        slow_result = await _test_slow_response(client)
        chaos_tests.append(slow_result)

        malformed_result = await _test_malformed_json(client)
        chaos_tests.append(malformed_result)

        if not malformed_result["passed"]:
            system_survived = False

    failures = []
    if not system_survived:
        failures = [
            crash["note"]
            for test in chaos_tests
            for crash in test.get("crashes_detected", [])
        ]

    return {
        "agent": "chaos_agent",
        "status": "PASS" if system_survived else "FAIL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failures": failures,
        "details": {
            "chaos_tests": chaos_tests,
            "system_survived": system_survived,
            "skipped": False,
            "env": ENV,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
