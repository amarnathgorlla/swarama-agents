"""
auto_fix_agent.py — SWARAMA Auto Fix Agent
Applies safe automatic fixes based on root_cause_agent output.
Creates GitHub issues for anything it cannot fix automatically.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).resolve().parents[2] / "config" / ".env.agents"
load_dotenv(_ENV_PATH)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "your-org/swarama")  # owner/repo
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:3000")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

logger = logging.getLogger("swarama.auto_fix_agent")

# ---------------------------------------------------------------------------
# Pattern-to-fixer mapping
# ---------------------------------------------------------------------------
PATTERN_ID = "pattern_matched"


async def _try_restart_backend(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Ping /api/health — if it recovers fast, the server is just overloaded."""
    try:
        resp = await client.get(f"{BACKEND_URL}/api/health", timeout=5)
        if resp.status_code == 200:
            return True, "Backend health endpoint responded 200 — service recovered automatically"
    except Exception as exc:
        logger.warning("Backend health ping failed: %s", exc)

    # Try a restart via admin endpoint if available
    try:
        resp = await client.post(
            f"{BACKEND_URL}/api/admin/restart",
            headers={"Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
            timeout=10,
        )
        if resp.status_code in (200, 202):
            return True, "Backend restart triggered via /api/admin/restart"
    except Exception as exc:
        logger.warning("Backend restart endpoint failed: %s", exc)

    return False, "Could not restart backend automatically — manual intervention needed"


async def _clear_expired_sessions(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Ask Supabase to clear expired sessions via admin endpoint."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return False, "Supabase credentials not available"

    try:
        resp = await client.delete(
            f"{SUPABASE_URL}/auth/v1/admin/users/sessions",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            },
            timeout=15,
        )
        if resp.status_code in (200, 204):
            return True, "Expired Supabase sessions cleared successfully"
        return False, f"Session clear returned HTTP {resp.status_code}"
    except Exception as exc:
        return False, f"Failed to clear sessions: {exc}"


async def _check_pending_migrations(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Check for unrun migrations via backend admin endpoint."""
    try:
        resp = await client.get(
            f"{BACKEND_URL}/api/admin/migrations/pending",
            headers={"Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            pending = data.get("pending", [])
            if pending:
                # Trigger migration run
                run_resp = await client.post(
                    f"{BACKEND_URL}/api/admin/migrations/run",
                    headers={"Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
                    timeout=30,
                )
                if run_resp.status_code in (200, 202):
                    return True, f"Ran {len(pending)} pending migrations: {pending}"
                return False, f"Migration run endpoint returned {run_resp.status_code}"
            return False, "No pending migrations found"
        return False, f"Migration check returned HTTP {resp.status_code}"
    except Exception as exc:
        return False, f"Migration check failed: {exc}"


async def _create_github_issue(
    client: httpx.AsyncClient,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str | None:
    """Create a GitHub issue and return its URL, or None on failure."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning("GITHUB_TOKEN or GITHUB_REPO not set — skipping issue creation")
        return None

    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    payload = {
        "title": title,
        "body": body,
        "labels": labels or ["agent-detected", "bug"],
    }
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = await client.post(url, json=payload, headers=headers, timeout=20)
        if resp.status_code == 201:
            issue_url = resp.json().get("html_url", "")
            logger.info("GitHub issue created: %s", issue_url)
            return issue_url
        logger.error("GitHub issue creation failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.error("GitHub issue creation exception: %s", exc)

    return None


def _build_issue_body(root_cause_result: dict, failure_dict: dict) -> str:
    """Build a detailed GitHub issue body from agent results."""
    details = root_cause_result.get("details", {})
    ts = failure_dict.get("timestamp", "unknown")
    return (
        f"## 🤖 SWARAMA Agent Auto-Detected Issue\n\n"
        f"**Detected at:** {ts}\n"
        f"**Failing agent:** `{failure_dict.get('agent', 'unknown')}`\n"
        f"**Root cause:** {details.get('root_cause', 'N/A')}\n"
        f"**Confidence:** {details.get('confidence', 'N/A')}\n\n"
        f"### Suggested Fix\n"
        f"{details.get('suggested_fix', 'N/A')}\n\n"
        f"### Failure Summary\n"
        f"```\n{details.get('failure_summary', 'N/A')[:800]}\n```\n\n"
        f"### Full Failure Details\n"
        f"```json\n{json.dumps(failure_dict.get('details', {}), indent=2)[:1500]}\n```\n\n"
        f"---\n*This issue was created automatically by the SWARAMA auto_fix_agent.*"
    )


# ---------------------------------------------------------------------------
# Auto-fix dispatch table
# ---------------------------------------------------------------------------
SAFE_FIXES: dict[str, list] = {
    "service_down": [_try_restart_backend],
    "db_connection": [_try_restart_backend],
    "jwt_expired_or_rotated": [_clear_expired_sessions],
    "auth_flow_failure": [_clear_expired_sessions],
    "schema_mismatch": [_check_pending_migrations],
}


async def run(
    root_cause_result: dict,
    failure_dict: dict,
    system_state: dict | None = None,
) -> dict:
    """
    Attempt safe automatic fixes based on the root cause analysis.

    Args:
        root_cause_result: Output from root_cause_agent.run().
        failure_dict: The original failing agent's result dict.
        system_state: Optional shared orchestrator state.

    Returns:
        Standard agent result dict.
    """
    start = time.monotonic()
    agent_name = "auto_fix_agent"
    timestamp = datetime.now(timezone.utc).isoformat()

    details = root_cause_result.get("details", {})
    pattern = details.get("pattern_matched", "none")
    confidence = details.get("confidence", "LOW")

    logger.info("[%s] Starting — pattern=%s confidence=%s", agent_name, pattern, confidence)

    fix_applied = False
    fix_description = "No safe automatic fix available for this pattern"
    github_issue_url: str | None = None
    fix_steps: list[str] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # ----------------------------------------------------------------
        # 1. Attempt safe auto-fixes if pattern matches
        # ----------------------------------------------------------------
        fixers = SAFE_FIXES.get(pattern, [])
        if fixers and confidence in ("HIGH", "MEDIUM"):
            for fixer_fn in fixers:
                fn_name = fixer_fn.__name__
                logger.info("[%s] Trying fixer: %s", agent_name, fn_name)
                try:
                    ok, desc = await fixer_fn(client)
                    fix_steps.append(f"{'✅' if ok else '❌'} {fn_name}: {desc}")
                    if ok:
                        fix_applied = True
                        fix_description = desc
                        logger.info("[%s] Fix succeeded: %s", agent_name, desc)
                        break
                except Exception as exc:
                    fix_steps.append(f"❌ {fn_name}: Exception — {exc}")
                    logger.error("[%s] Fixer %s raised: %s", agent_name, fn_name, exc)
        else:
            logger.info("[%s] No safe fixers for pattern '%s' — will create GitHub issue", agent_name, pattern)

        # ----------------------------------------------------------------
        # 2. If no fix was applied, create a GitHub issue
        # ----------------------------------------------------------------
        if not fix_applied:
            failing_agent = failure_dict.get("agent", "unknown")
            root_cause_text = details.get("root_cause", "Unknown")
            issue_title = f"AGENT: {root_cause_text[:80]} — detected in {failing_agent}"
            issue_body = _build_issue_body(root_cause_result, failure_dict)

            logger.info("[%s] Creating GitHub issue: %s", agent_name, issue_title[:80])
            github_issue_url = await _create_github_issue(client, issue_title, issue_body)

            if github_issue_url:
                fix_description = f"GitHub issue created: {github_issue_url}"
            else:
                fix_description = "Fix not applied and GitHub issue creation failed — check GITHUB_TOKEN"

    duration_ms = int((time.monotonic() - start) * 1000)

    result = {
        "agent": agent_name,
        "status": "PASS",
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "details": {
            "pattern": pattern,
            "confidence": confidence,
            "fix_applied": fix_applied,
            "fix_description": fix_description,
            "fix_steps": fix_steps,
            "github_issue_url": github_issue_url,
        },
    }

    logger.info("[%s] Done — fix_applied=%s github_issue=%s", agent_name, fix_applied, github_issue_url)
    return result


# ---------------------------------------------------------------------------
# Standalone entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG)

    sample_root_cause = {
        "agent": "root_cause_agent",
        "status": "PASS",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": 12,
        "details": {
            "analysed_agent": "qa_agent",
            "pattern_matched": "service_down",
            "pattern_score": 3,
            "root_cause": "Backend service is down or unreachable",
            "suggested_fix": "Restart the backend container",
            "confidence": "HIGH",
            "failure_summary": "Connection refused on /api/bookings",
        },
    }

    sample_failure = {
        "agent": "qa_agent",
        "status": "FAIL",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": 5001,
        "details": {"failures": [{"endpoint": "/api/bookings", "error": "Connection refused"}]},
    }

    result = asyncio.run(run(sample_root_cause, sample_failure))
    print(json.dumps(result, indent=2))
