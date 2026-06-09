"""
postmortem_agent.py — SWARAMA Postmortem Agent
Runs every 24 hours. Reads all reports from the last 24 hours,
generates a postmortem Markdown file, and emails it as an attachment.
"""

import asyncio
import json
import logging
import os
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).resolve().parents[1] / "config" / ".env.agents"
load_dotenv(_ENV_PATH)

GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "gvamarnath100@gmail.com")

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("swarama.postmortem_agent")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_reports_last_n_hours(hours: int = 24) -> list[dict]:
    """Load all JSON report files written in the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    reports = []
    for path in sorted(REPORTS_DIR.glob("*.json")):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                    data["_filename"] = path.name
                    reports.append(data)
        except Exception as exc:
            logger.warning("Could not read %s: %s", path, exc)
    return reports


def _extract_incidents(reports: list[dict]) -> list[dict]:
    """Extract all non-PASS events from reports."""
    incidents = []
    for r in reports:
        if r.get("overall_status") in ("FAIL", "CRITICAL", "WARN"):
            incidents.append({
                "timestamp": r.get("timestamp"),
                "status": r.get("overall_status"),
                "agents_failed": r.get("agents_failed", 0),
                "failures": r.get("failures_detail", []),
                "filename": r.get("_filename"),
            })
    return incidents


def _collect_root_causes(incidents: list[dict]) -> list[str]:
    """Collect all unique root causes mentioned across incidents."""
    seen = set()
    causes = []
    for inc in incidents:
        for failure in inc.get("failures", []):
            rc = failure.get("details", {}).get("root_cause")
            if rc and rc not in seen:
                seen.add(rc)
                causes.append(rc)
    return causes


def _collect_fixes_applied(incidents: list[dict]) -> list[str]:
    """Collect all fix descriptions from auto_fix_agent results."""
    fixes = []
    for inc in incidents:
        for failure in inc.get("failures", []):
            fix = failure.get("details", {}).get("fix_description")
            if fix:
                fixes.append(fix)
    return fixes


def _generate_postmortem_md(
    reports: list[dict],
    incidents: list[dict],
    date_str: str,
) -> str:
    """Generate a postmortem Markdown document."""
    total_runs = len(reports)
    total_pass = sum(1 for r in reports if r.get("overall_status") == "PASS")
    total_fail = sum(1 for r in reports if r.get("overall_status") in ("FAIL", "CRITICAL", "WARN"))
    uptime_pct = (total_pass / total_runs * 100) if total_runs else 0

    root_causes = _collect_root_causes(incidents)
    fixes = _collect_fixes_applied(incidents)

    # Time metrics
    first_ts = min((r.get("timestamp", "") for r in reports), default="N/A")
    last_ts = max((r.get("timestamp", "") for r in reports), default="N/A")

    # Build incidents section
    incident_lines = []
    for i, inc in enumerate(incidents, 1):
        incident_lines.append(f"### Incident {i}")
        incident_lines.append(f"- **Time:** {inc['timestamp']}")
        incident_lines.append(f"- **Severity:** {inc['status']}")
        incident_lines.append(f"- **Agents failed:** {inc['agents_failed']}")
        for failure in inc.get("failures", []):
            agent = failure.get("agent", "unknown")
            rc = failure.get("details", {}).get("root_cause", "")
            incident_lines.append(f"  - `{agent}`: {rc}")
        incident_lines.append("")

    incidents_md = "\n".join(incident_lines) if incident_lines else "_No incidents recorded._"

    root_causes_md = "\n".join(f"- {c}" for c in root_causes) if root_causes else "- None identified"
    fixes_md = "\n".join(f"- {f}" for f in fixes) if fixes else "- No automatic fixes were applied"

    return f"""# SWARAMA Daily Postmortem — {date_str}

> Auto-generated by the SWARAMA postmortem_agent

---

## Summary

| Metric | Value |
|--------|-------|
| Date | {date_str} |
| Total agent runs | {total_runs} |
| Passed runs | {total_pass} |
| Failed/Warn runs | {total_fail} |
| Uptime (run-based) | {uptime_pct:.1f}% |
| First check | {first_ts} |
| Last check | {last_ts} |

---

## Incidents ({len(incidents)} total)

{incidents_md}

---

## Root Causes Identified

{root_causes_md}

---

## Fixes Applied

{fixes_md}

---

## Time Metrics

| Metric | Value |
|--------|-------|
| Time to detect (avg) | ≤ 2 hours (check interval) |
| Incidents requiring manual review | {sum(1 for i in incidents if i['status'] == 'CRITICAL')} |

---

## What To Improve

{"- **High incident rate** — review recent deployments and add more test coverage." if total_fail > 3 else "- System performed well today. Continue monitoring."}
{"- **CRITICAL incidents detected** — implement additional circuit breakers and alerting." if any(i['status'] == 'CRITICAL' for i in incidents) else ""}
- Ensure all agents have retry logic for transient network failures.
- Review and update `regression/baseline_responses.json` if API schema changed intentionally.
- Verify alert email delivery is working (check spam folder if emails not received).

---

## Agent Coverage

All 26 SWARAMA agents monitored the following:
- **Testing (7):** qa, logic, regression, integration, load, latency, db_query
- **Monitoring (7):** health, uptime, security, auth, analytics, data_integrity, chaos
- **Analysis (4):** log_analysis, root_cause, auto_fix, rollback
- **Reporting (4):** report, email_notifier, postmortem, trend

---

*Generated at {datetime.now(timezone.utc).isoformat()} by SWARAMA postmortem_agent*
"""


def _send_postmortem_email(postmortem_path: Path, date_str: str) -> bool:
    """Send the postmortem Markdown as an email attachment."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logger.warning("[postmortem_agent] Gmail credentials not set — skipping email")
        return False

    subject = f"📋 SWARAMA Daily Postmortem — {date_str}"
    body_text = (
        f"Please find attached the SWARAMA daily postmortem report for {date_str}.\n\n"
        "This report includes all incidents, root causes, and fixes applied in the last 24 hours.\n\n"
        "Generated automatically by the SWARAMA postmortem_agent."
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_EMAIL
    msg.attach(MIMEText(body_text, "plain"))

    # Attach postmortem file
    try:
        with open(postmortem_path, "rb") as fh:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(fh.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename={postmortem_path.name}",
        )
        msg.attach(part)
    except Exception as exc:
        logger.error("[postmortem_agent] Could not attach file: %s", exc)
        return False

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        logger.info("[postmortem_agent] Postmortem email sent to %s", ALERT_EMAIL)
        return True
    except Exception as exc:
        logger.error("[postmortem_agent] Email send failed: %s", exc)
        return False


async def run(system_state: dict | None = None) -> dict:
    """
    Generate and email the daily postmortem report.

    Returns:
        Standard agent result dict.
    """
    start = time.monotonic()
    agent_name = "postmortem_agent"
    timestamp = datetime.now(timezone.utc).isoformat()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    logger.info("[%s] Starting daily postmortem for %s", agent_name, date_str)

    # Load last 24 hours of reports
    reports = _load_reports_last_n_hours(hours=24)
    logger.info("[%s] Loaded %d reports", agent_name, len(reports))

    incidents = _extract_incidents(reports)
    logger.info("[%s] Found %d incidents", agent_name, len(incidents))

    # Generate Markdown
    postmortem_md = _generate_postmortem_md(reports, incidents, date_str)

    # Save postmortem file
    postmortem_filename = f"postmortem-{date_str}.md"
    postmortem_path = REPORTS_DIR / postmortem_filename
    try:
        with open(postmortem_path, "w", encoding="utf-8") as fh:
            fh.write(postmortem_md)
        logger.info("[%s] Postmortem saved: %s", agent_name, postmortem_path)
    except Exception as exc:
        logger.error("[%s] Failed to save postmortem: %s", agent_name, exc)
        postmortem_path = None

    # Email it
    email_sent = False
    if postmortem_path:
        email_sent = _send_postmortem_email(postmortem_path, date_str)

    duration_ms = int((time.monotonic() - start) * 1000)

    result = {
        "agent": agent_name,
        "status": "PASS",
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "details": {
            "date": date_str,
            "reports_analysed": len(reports),
            "incidents_found": len(incidents),
            "postmortem_path": str(postmortem_path) if postmortem_path else None,
            "email_sent": email_sent,
        },
    }

    logger.info("[%s] Done — incidents=%d email_sent=%s path=%s",
                agent_name, len(incidents), email_sent, postmortem_path)
    return result


# ---------------------------------------------------------------------------
# Standalone entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    result = asyncio.run(run())
    print(json.dumps(result, indent=2))
