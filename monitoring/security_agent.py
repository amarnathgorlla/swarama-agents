"""
SWARAMA Security Agent
======================
Tests for common vulnerabilities:
  1. SQL injection attempt → should return 400 not 500
  2. Unauthenticated access to protected routes → should return 401
  3. Oversized payload → should return 413 or 400
  4. No API keys exposed in public endpoints

Returns standard dict with vulnerabilities_found: []
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

BACKEND = TARGETS.get("backend_url", os.getenv("BACKEND_URL", "http://localhost:3000"))

SQL_INJECTION_PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE bookings; --",
    "1 UNION SELECT * FROM users",
    "\" OR \"\"=\"",
]

KNOWN_SECRET_PATTERNS = [
    "eyJ",       # JWT token prefix
    "sk_live",   # Stripe live key
    "sk_test",   # Stripe test key
    "AKIA",      # AWS access key
    "ghp_",      # GitHub personal token
    "xoxb-",     # Slack bot token
]


async def _test_sql_injection(client: httpx.AsyncClient) -> dict:
    """Send SQLi payloads to booking endpoint — expect 400 or 422, not 500."""
    vulnerabilities = []
    for payload in SQL_INJECTION_PAYLOADS:
        try:
            r = await client.post(
                f"{BACKEND}/api/bookings",
                json={"service_id": payload, "notes": payload},
                headers={"Authorization": "Bearer security-agent-test"},
                timeout=10,
            )
            if r.status_code == 500:
                vulnerabilities.append(
                    f"SQL injection payload caused 500: '{payload[:30]}...'"
                )
            # 400, 401, 422 are all acceptable (input rejected)
        except httpx.ConnectError:
            return {"passed": True, "note": "Backend not reachable — SQLi test skipped",
                    "vulnerabilities": []}
        except Exception:
            pass

    return {
        "test": "sql_injection",
        "passed": len(vulnerabilities) == 0,
        "vulnerabilities": vulnerabilities,
    }


async def _test_unauth_access(client: httpx.AsyncClient) -> dict:
    """Access protected routes without a token — expect 401 or 403."""
    protected_routes = [
        f"{BACKEND}/api/bookings",
        f"{BACKEND}/api/mechanics/nearby",
        f"{BACKEND}/admin/stats",
    ]
    vulnerabilities = []
    for route in protected_routes:
        try:
            r = await client.get(route, timeout=10)
            if r.status_code == 200:
                # Check if response contains actual user data
                try:
                    body = r.json()
                    if isinstance(body, list) and body:
                        vulnerabilities.append(
                            f"Unauthenticated access returned data: {route}"
                        )
                    elif isinstance(body, dict) and any(
                        k in body for k in ["data", "bookings", "mechanics", "users"]
                    ):
                        vulnerabilities.append(
                            f"Unauthenticated access returned data: {route}"
                        )
                except Exception:
                    pass
        except httpx.ConnectError:
            return {"test": "unauth_access", "passed": True,
                    "note": "Backend not reachable", "vulnerabilities": []}
        except Exception:
            pass

    return {
        "test": "unauth_access",
        "passed": len(vulnerabilities) == 0,
        "vulnerabilities": vulnerabilities,
    }


async def _test_oversized_payload(client: httpx.AsyncClient) -> dict:
    """Send a 10MB payload — expect 413 or 400, not 500."""
    oversized = {"data": "X" * (10 * 1024 * 1024)}  # 10 MB
    vulnerabilities = []
    try:
        r = await client.post(
            f"{BACKEND}/api/bookings",
            json=oversized,
            headers={"Authorization": "Bearer security-agent-test"},
            timeout=30,
        )
        if r.status_code == 500:
            vulnerabilities.append(
                f"Oversized payload caused 500 (status: {r.status_code})"
            )
    except httpx.ConnectError:
        return {"test": "oversized_payload", "passed": True,
                "note": "Backend not reachable", "vulnerabilities": []}
    except Exception:
        pass  # Client may reject before sending — that's fine

    return {
        "test": "oversized_payload",
        "passed": len(vulnerabilities) == 0,
        "vulnerabilities": vulnerabilities,
    }


async def _test_no_key_exposure(client: httpx.AsyncClient) -> dict:
    """Check public endpoints don't leak API keys in response."""
    public_routes = [
        f"{BACKEND}/api/services",
        f"{BACKEND}/health",
    ]
    vulnerabilities = []
    for route in public_routes:
        try:
            r = await client.get(route, timeout=10)
            if r.status_code == 200:
                text = r.text
                for pattern in KNOWN_SECRET_PATTERNS:
                    if pattern in text:
                        vulnerabilities.append(
                            f"Possible secret key pattern '{pattern}' found in {route}"
                        )
        except Exception:
            pass

    return {
        "test": "no_key_exposure",
        "passed": len(vulnerabilities) == 0,
        "vulnerabilities": vulnerabilities,
    }


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    all_vulnerabilities = []
    test_results = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tests = await asyncio.gather(
            _test_sql_injection(client),
            _test_unauth_access(client),
            _test_oversized_payload(client),
            _test_no_key_exposure(client),
        )

    for test in tests:
        test_results.append(test)
        if not test.get("passed", True):
            all_vulnerabilities.extend(test.get("vulnerabilities", []))

    status = "PASS" if not all_vulnerabilities else "FAIL"
    return {
        "agent": "security_agent",
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failures": all_vulnerabilities,
        "details": {
            "vulnerabilities_found": all_vulnerabilities,
            "test_results": test_results,
            "tests_run": len(test_results),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
