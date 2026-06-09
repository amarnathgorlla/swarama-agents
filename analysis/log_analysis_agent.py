"""
SWARAMA Log Analysis Agent
===========================
Reads backend server logs from /api/admin/logs endpoint or local file.
Counts: error count, warning count, most frequent error messages.
Detects: error rate spike (>10 errors in last hour).

Returns standard dict with error_count, warning_count, top_errors, spike_detected.
"""

import asyncio
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "config" / ".env.agents")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
with open(CONFIG_DIR / "targets.yaml") as f:
    _raw = yaml.safe_load(f)
    TARGETS = {k: re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), str(v)) if isinstance(v, str) else v for k, v in _raw.items() if not isinstance(v, list)}

with open(CONFIG_DIR / "thresholds.yaml") as f:
    THRESHOLDS = yaml.safe_load(f)

BACKEND = TARGETS.get("backend_url", os.getenv("BACKEND_URL", "http://localhost:3000"))
ERROR_SPIKE_THRESHOLD = THRESHOLDS.get("error_spike_threshold_per_hour", 10)

# Patterns to classify log lines
ERROR_PATTERN = re.compile(r"\b(error|ERROR|Error|exception|EXCEPTION|Exception|FATAL|fatal)\b")
WARN_PATTERN = re.compile(r"\b(warn|WARN|warning|WARNING|Warning)\b")
TIMESTAMP_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})")


def _parse_log_lines(lines: list[str]) -> dict:
    """Parse raw log lines — count errors/warnings, find top messages, detect spikes."""
    errors = []
    warnings = []
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

    for line in lines:
        is_error = bool(ERROR_PATTERN.search(line))
        is_warn = bool(WARN_PATTERN.search(line))

        if is_error:
            errors.append(line.strip())
        elif is_warn:
            warnings.append(line.strip())

    # Top 5 most frequent error patterns (first 80 chars of each)
    error_snippets = [e[:80] for e in errors]
    top_errors = [
        {"message": msg, "count": count}
        for msg, count in Counter(error_snippets).most_common(5)
    ]

    # Spike detection — count errors that have recent timestamps
    recent_errors = 0
    for line in errors:
        ts_match = TIMESTAMP_PATTERN.search(line)
        if ts_match:
            try:
                ts = datetime.fromisoformat(ts_match.group(1).replace(" ", "T"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= one_hour_ago:
                    recent_errors += 1
            except Exception:
                pass

    spike_detected = recent_errors >= ERROR_SPIKE_THRESHOLD

    return {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "recent_error_count": recent_errors,
        "spike_detected": spike_detected,
        "top_errors": top_errors,
    }


async def _fetch_logs_from_api(client: httpx.AsyncClient) -> list[str] | None:
    """Try to fetch logs from backend admin endpoint."""
    endpoints = [
        f"{BACKEND}/api/admin/logs",
        f"{BACKEND}/admin/logs",
        f"{BACKEND}/logs",
    ]
    for url in endpoints:
        try:
            r = await client.get(
                url,
                headers={"Authorization": "Bearer log-agent-admin"},
                timeout=15,
            )
            if r.status_code == 200:
                body = r.json()
                if isinstance(body, list):
                    return [str(item) for item in body]
                if isinstance(body, dict) and "logs" in body:
                    return [str(item) for item in body["logs"]]
                # Try treating response as plain text log
                return r.text.split("\n")
        except Exception:
            continue
    return None


async def run(system_state: dict | None = None, **kwargs) -> dict:
    t0 = time.monotonic()
    failures = []
    log_source = "none"
    log_lines = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        api_logs = await _fetch_logs_from_api(client)
        if api_logs:
            log_lines = api_logs
            log_source = "api"

    analysis = _parse_log_lines(log_lines)

    if analysis["spike_detected"]:
        failures.append(
            f"Error spike detected: {analysis['recent_error_count']} errors in the last hour "
            f"(threshold: {ERROR_SPIKE_THRESHOLD})"
        )

    return {
        "agent": "log_analysis_agent",
        "status": "PASS" if not failures else "FAIL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "failures": failures,
        "details": {
            "log_source": log_source,
            "lines_analyzed": len(log_lines),
            "error_count": analysis["error_count"],
            "warning_count": analysis["warning_count"],
            "recent_error_count": analysis["recent_error_count"],
            "spike_detected": analysis["spike_detected"],
            "top_errors": analysis["top_errors"],
            "spike_threshold": ERROR_SPIKE_THRESHOLD,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(asyncio.run(run()), indent=2))
