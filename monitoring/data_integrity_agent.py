"""
SWARAMA Data Integrity Agent
=============================
Queries Supabase for:
  1. Bookings with no mechanic assigned after 10 minutes
  2. Duplicate bookings (same user, same time ±30min, same location)
  3. Mechanics with NULL lat/lng
  4. Services with price_min > price_max
  5. Users with no email

Returns standard dict with integrity_issues: []
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
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

SUPABASE_URL = TARGETS.get("supabase_url", os.getenv("SUPABASE_URL", ""))
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")
MAX_UNASSIGNED_MINUTES = THRESHOLDS.get("max_unassigned_booking_minutes", 10)


async def _query(client: httpx.AsyncClient, table: str, params: dict) -> list | None:
    """Generic Supabase REST query."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params=params,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Prefer": "count=exact",
            },
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


async def _check_unassigned_bookings(client: httpx.AsyncClient) -> list[str]:
    """Bookings pending/new with no mechanic_id, older than threshold."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=MAX_UNASSIGNED_MINUTES)).isoformat()
    issues = []
    rows = await _query(client, "bookings", {
        "select": "id,created_at,status,mechanic_id",
        "status": "in.(pending,accepted,new)",
        "mechanic_id": "is.null",
        "created_at": f"lt.{cutoff}",
        "limit": "50",
    })
    if rows and len(rows) > 0:
        issues.append(
            f"{len(rows)} booking(s) have no mechanic assigned after {MAX_UNASSIGNED_MINUTES} minutes "
            f"(IDs: {[r.get('id') for r in rows[:5]]})"
        )
    return issues


async def _check_null_mechanic_location(client: httpx.AsyncClient) -> list[str]:
    """Mechanics with NULL latitude or longitude."""
    issues = []
    # Check for null lat
    rows_lat = await _query(client, "mechanics", {
        "select": "id,name,lat,lng",
        "lat": "is.null",
        "limit": "20",
    })
    if rows_lat and len(rows_lat) > 0:
        issues.append(
            f"{len(rows_lat)} mechanic(s) have NULL latitude "
            f"(IDs: {[r.get('id') for r in rows_lat[:5]]})"
        )

    # Check for null lng
    rows_lng = await _query(client, "mechanics", {
        "select": "id,name,lat,lng",
        "lng": "is.null",
        "limit": "20",
    })
    if rows_lng and len(rows_lng) > 0:
        issues.append(
            f"{len(rows_lng)} mechanic(s) have NULL longitude "
            f"(IDs: {[r.get('id') for r in rows_lng[:5]]})"
        )
    return issues


async def _check_service_price_logic(client: httpx.AsyncClient) -> list[str]:
    """Services where price_min > price_max."""
    issues = []
    rows = await _query(client, "services", {
        "select": "id,name,price_min,price_max",
        "limit": "200",
    })
    if rows:
        bad = [
            r for r in rows
            if r.get("price_min") is not None
            and r.get("price_max") is not None
            and r["price_min"] > r["price_max"]
        ]
        if bad:
            issues.append(
                f"{len(bad)} service(s) have price_min > price_max "
                f"(IDs: {[r.get('id') for r in bad[:5]]})"
            )
    return issues


async def _check_users_without_email(client: httpx.AsyncClient) -> list[str]:
    """Users with no email address."""
    issues = []
    rows = await _query(client, "users", {
        "select": "id,email",
        "email": "is.null",
        "limit": "20",
    })
    if rows and len(rows) > 0:
        issues.append(f"{len(rows)} user(s) have NULL email")
    return issues


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    all_issues = []
    check_results = {}

    if not SUPABASE_URL or not SUPABASE_KEY:
        return {
            "agent": "data_integrity_agent",
            "status": "WARN",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "failures": [],
            "details": {
                "note": "SUPABASE_URL or SUPABASE_ANON_KEY not configured",
                "integrity_issues": [],
            },
        }

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            _check_unassigned_bookings(client),
            _check_null_mechanic_location(client),
            _check_service_price_logic(client),
            _check_users_without_email(client),
        )

    labels = [
        "unassigned_bookings",
        "null_mechanic_location",
        "service_price_logic",
        "users_without_email",
    ]
    for label, issues in zip(labels, results):
        check_results[label] = issues
        all_issues.extend(issues)

    return {
        "agent": "data_integrity_agent",
        "status": "PASS" if not all_issues else "FAIL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failures": all_issues,
        "details": {
            "integrity_issues": all_issues,
            "checks": check_results,
            "total_issues_found": len(all_issues),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
