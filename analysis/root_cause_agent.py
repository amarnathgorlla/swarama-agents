"""
root_cause_agent.py — SWARAMA Root Cause Analysis Agent
Maps agent failures to known root causes and suggests fixes.
Called by orchestrator whenever any agent returns FAIL status.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("swarama.root_cause_agent")


# ---------------------------------------------------------------------------
# Known failure patterns → root cause mappings
# ---------------------------------------------------------------------------
FAILURE_PATTERNS: list[dict[str, Any]] = [
    {
        "pattern_id": "db_connection",
        "keywords": ["500", "internal server error", "database", "connection refused", "db"],
        "endpoint_pattern": None,
        "root_cause": "Database connection failure — backend cannot reach Supabase or the DB pool is exhausted",
        "suggested_fix": "1. Check Supabase project status at supabase.com/dashboard. "
                         "2. Verify SUPABASE_URL and SUPABASE_ANON_KEY in .env. "
                         "3. Restart the backend service. "
                         "4. Check connection pool settings in the backend config.",
        "confidence": "HIGH",
    },
    {
        "pattern_id": "jwt_expired_or_rotated",
        "keywords": ["401", "unauthorized", "jwt", "token", "invalid signature", "forbidden"],
        "endpoint_pattern": None,
        "root_cause": "JWT secret may have been rotated or Supabase project keys were regenerated",
        "suggested_fix": "1. Confirm SUPABASE_ANON_KEY and SUPABASE_SERVICE_KEY in .env match dashboard. "
                         "2. Redeploy backend with updated keys. "
                         "3. Force all clients to re-authenticate.",
        "confidence": "HIGH",
    },
    {
        "pattern_id": "slow_queries",
        "keywords": ["slow", "timeout", "query_time", "db_query", "latency", "p95", "p99"],
        "endpoint_pattern": None,
        "root_cause": "Database query performance degradation — likely missing index or table bloat",
        "suggested_fix": "1. Run EXPLAIN ANALYZE on the slowest queries identified. "
                         "2. Add indexes on: bookings(user_id), bookings(mechanic_id), bookings(created_at). "
                         "3. Run VACUUM ANALYZE on bookings and mechanics tables. "
                         "4. Check for N+1 query patterns in recent code changes.",
        "confidence": "MEDIUM",
    },
    {
        "pattern_id": "schema_mismatch",
        "keywords": ["schema", "field", "missing field", "key error", "column", "regression", "baseline"],
        "endpoint_pattern": None,
        "root_cause": "API schema mismatch — a recent migration or code change removed or renamed fields",
        "suggested_fix": "1. Review the latest git diff for schema changes. "
                         "2. Check if any pending Supabase migrations were not applied. "
                         "3. Update regression baseline after confirming the new schema is intentional. "
                         "4. Bump API version if this is a breaking change.",
        "confidence": "HIGH",
    },
    {
        "pattern_id": "rate_limiting",
        "keywords": ["429", "rate limit", "too many requests", "throttle"],
        "endpoint_pattern": None,
        "root_cause": "API rate limit exceeded — either from Supabase or the backend itself",
        "suggested_fix": "1. Check Supabase rate limit logs in the dashboard. "
                         "2. Implement exponential backoff in the backend HTTP client. "
                         "3. Add request queuing for high-volume endpoints. "
                         "4. Consider upgrading the Supabase plan if legitimate traffic.",
        "confidence": "HIGH",
    },
    {
        "pattern_id": "service_down",
        "keywords": ["connection error", "refused", "unreachable", "timeout", "host", "dns", "network"],
        "endpoint_pattern": None,
        "root_cause": "Backend service is down or unreachable — process crash or network issue",
        "suggested_fix": "1. SSH to the server and check if the backend process is running. "
                         "2. Check docker-compose logs for crash stack traces. "
                         "3. Restart the backend container: docker-compose restart backend. "
                         "4. Verify firewall/security group rules allow traffic on port 3000.",
        "confidence": "HIGH",
    },
    {
        "pattern_id": "load_failure",
        "keywords": ["load", "concurrent", "success rate", "capacity", "503", "overload"],
        "endpoint_pattern": None,
        "root_cause": "System cannot handle concurrent load — insufficient resources or lack of horizontal scaling",
        "suggested_fix": "1. Check server CPU and memory during load tests. "
                         "2. Enable connection pooling with PgBouncer for Supabase. "
                         "3. Add caching layer (Redis) for repeated reads. "
                         "4. Consider horizontal scaling or upgrading server tier.",
        "confidence": "MEDIUM",
    },
    {
        "pattern_id": "auth_flow_failure",
        "keywords": ["signup", "login", "session", "auth", "rls", "row level security"],
        "endpoint_pattern": None,
        "root_cause": "Supabase Auth or Row Level Security misconfiguration",
        "suggested_fix": "1. Check Supabase Auth settings in dashboard (email confirmations, JWT expiry). "
                         "2. Review RLS policies on the bookings table. "
                         "3. Verify service role key is used for admin operations. "
                         "4. Test auth flow manually via Supabase dashboard.",
        "confidence": "MEDIUM",
    },
    {
        "pattern_id": "data_integrity",
        "keywords": ["integrity", "duplicate", "null", "missing mechanic", "unassigned", "orphan"],
        "endpoint_pattern": None,
        "root_cause": "Data integrity violation — business logic gap allowing invalid data states",
        "suggested_fix": "1. Add database constraints (NOT NULL, UNIQUE) to prevent bad data. "
                         "2. Review booking assignment logic for race conditions. "
                         "3. Run a one-time cleanup query to fix existing bad rows. "
                         "4. Add server-side validation before INSERT/UPDATE operations.",
        "confidence": "MEDIUM",
    },
    {
        "pattern_id": "security_vulnerability",
        "keywords": ["sql injection", "vulnerability", "exposed", "unauthorized", "413", "payload"],
        "endpoint_pattern": None,
        "root_cause": "Security vulnerability detected in API endpoints",
        "suggested_fix": "1. Add input sanitization and parameterized queries throughout the backend. "
                         "2. Enforce payload size limits (body-parser maxSize). "
                         "3. Review all endpoints for missing auth middleware. "
                         "4. Schedule immediate security audit.",
        "confidence": "HIGH",
    },
]

DEFAULT_CAUSE = {
    "root_cause": "Unknown failure — no matching pattern found in knowledge base",
    "suggested_fix": "1. Review the full agent failure output in the logs. "
                     "2. Check recent git commits for related changes. "
                     "3. Manually reproduce the failure to gather more context. "
                     "4. Escalate to the development team.",
    "confidence": "LOW",
}


def _score_pattern(pattern: dict, failure_text: str) -> int:
    """Return the number of keywords from the pattern found in the failure text."""
    lower_text = failure_text.lower()
    return sum(1 for kw in pattern["keywords"] if kw.lower() in lower_text)


def _build_failure_text(failure_dict: dict) -> str:
    """Flatten a failure dict into a searchable string."""
    parts: list[str] = []

    def _flatten(obj: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(obj, dict):
            for v in obj.values():
                _flatten(v, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _flatten(item, depth + 1)
        else:
            parts.append(str(obj))

    _flatten(failure_dict)
    return " ".join(parts)


async def run(
    failure_dict: dict | None = None,
    failures: list[dict] | None = None,
    system_state: dict | None = None,
) -> dict:
    """
    Analyse a failure dict and return the most likely root cause.

    Args:
        failure_dict: The dict returned by the failing agent.
        failures: Optional list of failure dicts from orchestrator.
        system_state: Optional shared orchestrator state for additional context.

    Returns:
        Standard agent result dict containing root_cause, suggested_fix, confidence.
    """
    start = time.monotonic()
    agent_name = "root_cause_agent"
    timestamp = datetime.now(timezone.utc).isoformat()

    if failure_dict is None:
        if failures and len(failures) > 0:
            failure_dict = failures[0]
        else:
            failure_dict = {}

    logger.info("[%s] Starting root cause analysis for failure from agent: %s",
                agent_name, failure_dict.get("agent", "unknown"))

    failure_text = _build_failure_text(failure_dict)
    logger.debug("[%s] Failure text for pattern matching:\n%s", agent_name, failure_text[:500])

    # Score every pattern against the failure text
    best_pattern: dict | None = None
    best_score = 0

    for pattern in FAILURE_PATTERNS:
        score = _score_pattern(pattern, failure_text)
        if score > best_score:
            best_score = score
            best_pattern = pattern

    if best_pattern and best_score >= 1:
        matched = best_pattern
        logger.info("[%s] Matched pattern '%s' with score %d (confidence=%s)",
                    agent_name, matched["pattern_id"], best_score, matched["confidence"])
    else:
        matched = DEFAULT_CAUSE
        logger.warning("[%s] No pattern matched — using default cause", agent_name)

    duration_ms = int((time.monotonic() - start) * 1000)

    result = {
        "agent": agent_name,
        "status": "PASS",
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "details": {
            "analysed_agent": failure_dict.get("agent", "unknown"),
            "pattern_matched": matched.get("pattern_id", "none"),
            "pattern_score": best_score,
            "root_cause": matched["root_cause"],
            "suggested_fix": matched["suggested_fix"],
            "confidence": matched["confidence"],
            "failure_summary": failure_text[:300],
        },
    }

    logger.info("[%s] Root cause: %s (confidence=%s)", agent_name,
                matched["root_cause"][:80], matched["confidence"])
    return result


# ---------------------------------------------------------------------------
# Standalone entrypoint for manual testing
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.DEBUG)

    sample_failure = {
        "agent": "qa_agent",
        "status": "FAIL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": 342,
        "details": {
            "failures": [
                {"endpoint": "/api/bookings", "status_code": 500,
                 "error": "Internal Server Error — database connection refused"}
            ],
        },
    }

    result = asyncio.run(run(sample_failure))
    print(json.dumps(result, indent=2))
