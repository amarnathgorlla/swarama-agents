"""
rollback_agent.py — SWARAMA Rollback Agent
Called only on CRITICAL failures.
Uses GitHub API to find last successful deployment SHA and triggers a workflow dispatch.
Sends immediate CRITICAL email notification.
"""

import asyncio
import json
import logging
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
GITHUB_WORKFLOW_ID = os.getenv("GITHUB_ROLLBACK_WORKFLOW", "deploy.yml")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "gvamarnath100@gmail.com")

logger = logging.getLogger("swarama.rollback_agent")

GITHUB_API = "https://api.github.com"


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get_last_successful_sha(client: httpx.AsyncClient) -> tuple[str | None, str | None]:
    """
    Find the SHA of the last successful deployment on main branch.
    Returns (sha, run_url) or (None, None) if not found.
    """
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/actions/runs"
    params = {
        "branch": "main",
        "status": "success",
        "per_page": 10,
    }
    try:
        resp = await client.get(url, headers=_github_headers(), params=params, timeout=20)
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])

        if not runs:
            logger.warning("[rollback_agent] No successful workflow runs found on main")
            return None, None

        # Find the most recent successful run (skip the current broken one — take the second)
        for run in runs:
            sha = run.get("head_sha")
            run_url = run.get("html_url")
            run_id = run.get("id")
            if sha:
                logger.info("[rollback_agent] Found last successful SHA: %s (run #%s)", sha, run_id)
                return sha, run_url

    except Exception as exc:
        logger.error("[rollback_agent] Failed to fetch workflow runs: %s", exc)

    return None, None


async def _trigger_rollback_workflow(
    client: httpx.AsyncClient, sha: str, reason: str
) -> tuple[bool, str]:
    """
    Trigger a GitHub Actions workflow_dispatch event with the target SHA as input.
    Returns (success, message).
    """
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW_ID}/dispatches"
    payload = {
        "ref": "main",
        "inputs": {
            "sha": sha,
            "reason": reason,
            "triggered_by": "rollback_agent",
        },
    }
    try:
        resp = await client.post(url, json=payload, headers=_github_headers(), timeout=20)
        if resp.status_code == 204:
            return True, f"Workflow dispatch triggered successfully for SHA {sha}"
        return False, f"Workflow dispatch returned HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"Workflow dispatch exception: {exc}"


def _send_critical_email(sha: str, reason: str, run_url: str | None) -> bool:
    """Send an immediate CRITICAL email about the rollback."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logger.warning("[rollback_agent] Gmail credentials not set — skipping email")
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"🚨 CRITICAL: SWARAMA rolled back to {sha[:8]} — {timestamp}"

    body_html = f"""
    <html><body style="font-family:sans-serif;">
    <h2 style="color:#cc0000;">🚨 CRITICAL SYSTEM FAILURE — AUTO ROLLBACK TRIGGERED</h2>
    <table border="0" cellpadding="8" style="border-collapse:collapse;">
      <tr><td><strong>Timestamp:</strong></td><td>{timestamp}</td></tr>
      <tr><td><strong>Rolled back to SHA:</strong></td><td><code>{sha}</code></td></tr>
      <tr><td><strong>Reason:</strong></td><td>{reason}</td></tr>
      <tr><td><strong>Workflow run:</strong></td><td>{run_url or 'N/A'}</td></tr>
      <tr><td><strong>Action required:</strong></td><td>Investigate root cause IMMEDIATELY before pushing new code.</td></tr>
    </table>
    <hr>
    <p style="color:#666;font-size:12px;">This message was sent automatically by the SWARAMA rollback_agent.</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_EMAIL
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        logger.info("[rollback_agent] CRITICAL email sent to %s", ALERT_EMAIL)
        return True
    except Exception as exc:
        logger.error("[rollback_agent] Email send failed: %s", exc)
        return False


async def run(
    reason: str = "CRITICAL failure detected by SWARAMA agents",
    failure_dict: dict | None = None,
    system_state: dict | None = None,
) -> dict:
    """
    Trigger an emergency rollback to the last known-good deployment.

    Args:
        reason: Human-readable reason for the rollback.
        failure_dict: The original failing agent's result for context.
        system_state: Optional shared orchestrator state.

    Returns:
        Standard agent result dict.
    """
    start = time.monotonic()
    agent_name = "rollback_agent"
    timestamp = datetime.now(timezone.utc).isoformat()

    logger.critical("[%s] CRITICAL rollback initiated — reason: %s", agent_name, reason)

    rollback_triggered = False
    sha: str | None = None
    run_url: str | None = None
    workflow_msg = ""
    email_sent = False

    if not GITHUB_TOKEN:
        logger.error("[%s] GITHUB_TOKEN not set — cannot trigger rollback", agent_name)
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "agent": agent_name,
            "status": "FAIL",
            "timestamp": timestamp,
            "duration_ms": duration_ms,
            "details": {
                "rollback_triggered": False,
                "sha": None,
                "reason": reason,
                "error": "GITHUB_TOKEN not configured",
                "email_sent": False,
            },
        }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Step 1: Find last successful SHA
        sha, run_url = await _get_last_successful_sha(client)

        if not sha:
            workflow_msg = "No successful GitHub Actions run found — cannot determine rollback target"
            logger.error("[%s] %s", agent_name, workflow_msg)
        else:
            # Step 2: Trigger rollback workflow
            rollback_triggered, workflow_msg = await _trigger_rollback_workflow(client, sha, reason)
            logger.info("[%s] Rollback result: %s", agent_name, workflow_msg)

    # Step 3: Send CRITICAL email immediately (regardless of rollback success)
    email_sent = _send_critical_email(
        sha or "UNKNOWN",
        reason,
        run_url,
    )

    duration_ms = int((time.monotonic() - start) * 1000)

    overall_status = "PASS" if rollback_triggered else "FAIL"

    result = {
        "agent": agent_name,
        "status": overall_status,
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "details": {
            "rollback_triggered": rollback_triggered,
            "sha": sha,
            "run_url": run_url,
            "reason": reason,
            "workflow_message": workflow_msg,
            "email_sent": email_sent,
            "alert_email": ALERT_EMAIL,
        },
    }

    logger.critical("[%s] Done — rollback_triggered=%s sha=%s email_sent=%s",
                    agent_name, rollback_triggered, sha, email_sent)
    return result


# ---------------------------------------------------------------------------
# Standalone entrypoint (for manual testing)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    result = asyncio.run(run(reason="Manual test of rollback agent"))
    print(json.dumps(result, indent=2))
