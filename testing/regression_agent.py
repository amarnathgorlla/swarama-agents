"""
SWARAMA Regression Agent
========================
Maintains a baseline_responses.json of last-known-good API responses.
On each run compares current responses to baseline — detects schema changes.
If baseline does not exist → creates it and returns PASS.

Returns standard agent dict.
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
AGENTS_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = AGENTS_ROOT / "testing" / "regression" / "baseline_responses.json"

with open(CONFIG_DIR / "targets.yaml") as f:
    import re
    _raw = yaml.safe_load(f)
    TARGETS = {k: re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), str(v)) if isinstance(v, str) else v for k, v in _raw.items() if not isinstance(v, list)}

BACKEND = TARGETS.get("backend_url", os.getenv("BACKEND_URL", "http://localhost:3000"))

# Endpoints to baseline — only safe, read-only, no-auth endpoints
BASELINE_ENDPOINTS = {
    "GET /api/services": {
        "method": "GET",
        "url": f"{BACKEND}/api/services",
    },
    "GET /health": {
        "method": "GET",
        "url": f"{BACKEND}/health",
    },
}


def _extract_schema(data) -> dict:
    """Extract field names and types from a response — works for dicts and lists."""
    if isinstance(data, list):
        if not data:
            return {"_type": "empty_list"}
        # Schema from first item
        first = data[0]
        return {"_type": "list", "_item_schema": _extract_schema(first)}
    elif isinstance(data, dict):
        return {k: type(v).__name__ for k, v in data.items()}
    else:
        return {"_type": type(data).__name__}


def _diff_schemas(baseline: dict, current: dict) -> list[str]:
    """Return list of differences between two schemas."""
    diffs = []

    # Fields in baseline but missing from current
    for key in baseline:
        if key.startswith("_"):
            continue
        if key not in current:
            diffs.append(f"Field '{key}' was REMOVED (was: {baseline[key]})")

    # Fields in current but new
    for key in current:
        if key.startswith("_"):
            continue
        if key not in baseline:
            diffs.append(f"Field '{key}' was ADDED (type: {current[key]})")

    # Type changes
    for key in baseline:
        if key.startswith("_"):
            continue
        if key in current and baseline[key] != current[key]:
            diffs.append(
                f"Field '{key}' type changed: {baseline[key]} → {current[key]}"
            )

    return diffs


async def _fetch_response(client: httpx.AsyncClient, name: str, spec: dict) -> dict | None:
    try:
        r = await getattr(client, spec["method"].lower())(spec["url"], timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    failures = []
    details = {"endpoints_checked": [], "diffs": [], "baseline_created": False}

    # Fetch current responses
    current_responses = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for name, spec in BASELINE_ENDPOINTS.items():
            data = await _fetch_response(client, name, spec)
            if data is not None:
                current_responses[name] = {
                    "schema": _extract_schema(data),
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                }
                details["endpoints_checked"].append(name)

    if not current_responses:
        return {
            "agent": "regression_agent",
            "status": "FAIL",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "failures": ["Could not fetch any responses — backend may be down"],
            "details": details,
        }

    # Load or create baseline
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not BASELINE_FILE.exists():
        with open(BASELINE_FILE, "w") as f:
            json.dump(current_responses, f, indent=2)
        details["baseline_created"] = True
        details["note"] = f"Baseline created at {BASELINE_FILE}"
        return {
            "agent": "regression_agent",
            "status": "PASS",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "failures": [],
            "details": details,
        }

    with open(BASELINE_FILE) as f:
        baseline = json.load(f)

    # Compare schemas
    all_diffs = []
    for endpoint_name, current_data in current_responses.items():
        if endpoint_name not in baseline:
            details["diffs"].append({
                "endpoint": endpoint_name,
                "diff": ["Endpoint is NEW — not in baseline"],
            })
            continue

        diffs = _diff_schemas(
            baseline[endpoint_name]["schema"],
            current_data["schema"],
        )

        # Check nested list items too
        b_schema = baseline[endpoint_name]["schema"]
        c_schema = current_data["schema"]
        if b_schema.get("_type") == "list" and c_schema.get("_type") == "list":
            nested_diffs = _diff_schemas(
                b_schema.get("_item_schema", {}),
                c_schema.get("_item_schema", {}),
            )
            diffs.extend([f"[item] {d}" for d in nested_diffs])

        if diffs:
            all_diffs.append({"endpoint": endpoint_name, "diff": diffs})
            failures.append(f"Schema changed for {endpoint_name}: {diffs}")

    details["diffs"] = all_diffs

    # Update baseline with current (rolling baseline)
    if not failures:
        with open(BASELINE_FILE, "w") as f:
            json.dump(current_responses, f, indent=2)

    status = "PASS" if not failures else "FAIL"
    return {
        "agent": "regression_agent",
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failures": failures,
        "details": details,
    }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(asyncio.run(run()), indent=2))
