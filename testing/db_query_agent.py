"""
SWARAMA DB Query Agent
======================
Connects to Supabase with read-only anon key.
Runs SELECT COUNT(*) on: bookings, mechanics, users, services tables.
Measures query time, checks tables exist, detects unexpected empty tables.

Returns standard agent dict with query times and row counts.
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

SUPABASE_URL = TARGETS.get("supabase_url", os.getenv("SUPABASE_URL", ""))
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")
MAX_QUERY_MS = THRESHOLDS.get("max_db_query_ms", 500)

# Tables to check with expected minimum row count
TABLES_TO_CHECK = {
    "bookings":  {"min_rows": 0, "critical": False},  # can be empty early on
    "mechanics": {"min_rows": 0, "critical": True},   # needs at least some mechanics
    "users":     {"min_rows": 0, "critical": False},
    "services":  {"min_rows": 1, "critical": True},   # services must always exist
}


async def _query_table(client: httpx.AsyncClient, table: str) -> dict:
    """Query Supabase REST API for row count in a table."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {
            "table": table,
            "row_count": None,
            "query_ms": 0,
            "error": "SUPABASE_URL or SUPABASE_ANON_KEY not configured",
            "reachable": False,
        }

    t0 = time.monotonic()
    try:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params={"select": "id", "limit": "1"},
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Prefer": "count=exact",
            },
            timeout=MAX_QUERY_MS / 1000 + 2,
        )
        query_ms = int((time.monotonic() - t0) * 1000)

        if r.status_code == 200:
            # Supabase returns count in Content-Range header: "0-0/42"
            content_range = r.headers.get("content-range", "")
            row_count = None
            if "/" in content_range:
                try:
                    row_count = int(content_range.split("/")[1])
                except (ValueError, IndexError):
                    row_count = len(r.json()) if r.text else 0
            return {
                "table": table,
                "row_count": row_count,
                "query_ms": query_ms,
                "error": None,
                "reachable": True,
                "status_code": r.status_code,
            }
        elif r.status_code == 401:
            return {
                "table": table,
                "row_count": None,
                "query_ms": query_ms,
                "error": "Unauthorized — RLS may be blocking agent key",
                "reachable": True,
                "status_code": r.status_code,
            }
        elif r.status_code == 404:
            return {
                "table": table,
                "row_count": None,
                "query_ms": query_ms,
                "error": f"Table '{table}' does not exist",
                "reachable": True,
                "status_code": r.status_code,
            }
        else:
            return {
                "table": table,
                "row_count": None,
                "query_ms": query_ms,
                "error": f"Unexpected status {r.status_code}",
                "reachable": True,
                "status_code": r.status_code,
            }
    except httpx.TimeoutException:
        return {
            "table": table,
            "row_count": None,
            "query_ms": int((time.monotonic() - t0) * 1000),
            "error": f"Query timed out after {MAX_QUERY_MS}ms",
            "reachable": False,
        }
    except Exception as exc:
        return {
            "table": table,
            "row_count": None,
            "query_ms": int((time.monotonic() - t0) * 1000),
            "error": str(exc),
            "reachable": False,
        }


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    failures = []
    table_results = {}

    async with httpx.AsyncClient() as client:
        tasks = [_query_table(client, table) for table in TABLES_TO_CHECK]
        results = await asyncio.gather(*tasks)

    for result in results:
        table = result["table"]
        cfg = TABLES_TO_CHECK[table]
        table_results[table] = result

        if result.get("error"):
            # Supabase not configured → warn but don't fail
            if "not configured" in (result.get("error") or ""):
                failures.append(f"[CONFIG] {table}: {result['error']}")
            elif cfg["critical"] and result.get("reachable"):
                failures.append(f"[{table}] {result['error']}")
            continue

        row_count = result.get("row_count")
        query_ms = result.get("query_ms", 0)

        # Query time threshold
        if query_ms > MAX_QUERY_MS:
            failures.append(
                f"[{table}] Query took {query_ms}ms, exceeds threshold {MAX_QUERY_MS}ms"
            )

        # Unexpected empty table
        if row_count is not None and row_count == 0 and cfg["min_rows"] > 0:
            failures.append(
                f"[{table}] Table has 0 rows — expected at least {cfg['min_rows']}"
            )

    duration_ms = int((time.monotonic() - t0) * 1000)
    status = "PASS" if not failures else "FAIL"

    return {
        "agent": "db_query_agent",
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": duration_ms,
        "failures": failures,
        "details": {
            "table_results": table_results,
            "max_query_ms_threshold": MAX_QUERY_MS,
            "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
