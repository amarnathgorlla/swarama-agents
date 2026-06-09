"""
email_notifier.py — SWARAMA Email Notification Agent
Reads the latest report and sends status emails via Gmail SMTP.
Handles PASS, FAIL, and CRITICAL status levels with distinct subjects and bodies.
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

logger = logging.getLogger("swarama.email_notifier")

# ---------------------------------------------------------------------------
# HTML email templates
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f4f6f9;margin:0;padding:20px;}
.container{max-width:680px;margin:0 auto;background:#fff;border-radius:12px;
           overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.1);}
.header{padding:28px 32px;color:#fff;text-align:center;}
.header.pass{background:linear-gradient(135deg,#1db954,#17a844);}
.header.fail{background:linear-gradient(135deg,#f59e0b,#d97706);}
.header.critical{background:linear-gradient(135deg,#ef4444,#b91c1c);}
.header h1{margin:0;font-size:26px;font-weight:700;}
.header p{margin:6px 0 0;opacity:.85;font-size:14px;}
.body{padding:28px 32px;}
.stat-row{display:flex;gap:16px;margin-bottom:20px;}
.stat{flex:1;background:#f8f9fa;border-radius:8px;padding:16px;text-align:center;}
.stat .num{font-size:28px;font-weight:700;color:#1a1a2e;}
.stat .label{font-size:12px;color:#666;margin-top:4px;text-transform:uppercase;}
.failure-card{background:#fff5f5;border:1px solid #fecaca;border-radius:8px;
              padding:16px;margin-bottom:12px;}
.failure-card h3{margin:0 0 6px;color:#dc2626;font-size:15px;}
.failure-card p{margin:4px 0;font-size:13px;color:#555;}
.agent-table{width:100%;border-collapse:collapse;font-size:13px;}
.agent-table th{text-align:left;padding:8px 12px;background:#f1f5f9;color:#475569;}
.agent-table td{padding:8px 12px;border-bottom:1px solid #f1f5f9;}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:600;}
.badge.pass{background:#dcfce7;color:#166534;}
.badge.fail{background:#fee2e2;color:#991b1b;}
.badge.warn{background:#fef3c7;color:#92400e;}
.footer{padding:16px 32px;background:#f8f9fa;font-size:12px;color:#999;text-align:center;}
"""


def _agent_rows(all_results: list[dict]) -> str:
    rows = []
    for r in all_results:
        status = r.get("status", "UNKNOWN")
        badge_class = status.lower() if status.lower() in ("pass", "fail", "warn") else "fail"
        rows.append(
            f"<tr><td>{r.get('agent','')}</td>"
            f"<td><span class='badge {badge_class}'>{status}</span></td>"
            f"<td>{r.get('duration_ms',0)}ms</td></tr>"
        )
    return "\n".join(rows)


def _failure_cards(failures: list[dict]) -> str:
    if not failures:
        return "<p style='color:#555'>No failures recorded.</p>"
    cards = []
    for f in failures:
        details = f.get("details", {})
        root_cause = details.get("root_cause", "")
        suggested_fix = details.get("suggested_fix", "")
        rc_html = f"<p><strong>Root Cause:</strong> {root_cause}</p>" if root_cause else ""
        fix_html = f"<p><strong>Suggested Fix:</strong> {suggested_fix}</p>" if suggested_fix else ""
        cards.append(
            f"<div class='failure-card'>"
            f"<h3>❌ {f.get('agent','unknown')} — {f.get('status')}</h3>"
            f"{rc_html}{fix_html}"
            f"</div>"
        )
    return "\n".join(cards)


def _build_pass_html(report: dict, ts: str) -> str:
    total = report.get("agents_total", 0)
    dur = report.get("duration_total_ms", 0) / 1000
    rows = _agent_rows(report.get("all_results", []))
    return f"""<html><head><style>{_CSS}</style></head><body>
<div class='container'>
  <div class='header pass'>
    <h1>✅ All Systems Operational</h1>
    <p>SWARAMA — {ts}</p>
  </div>
  <div class='body'>
    <div class='stat-row'>
      <div class='stat'><div class='num'>{total}</div><div class='label'>Agents Run</div></div>
      <div class='stat'><div class='num'>{total}</div><div class='label'>Passed</div></div>
      <div class='stat'><div class='num'>{dur:.1f}s</div><div class='label'>Duration</div></div>
    </div>
    <p style='color:#555'>All {total} agents passed. No issues found. System is healthy.</p>
    <table class='agent-table'>
      <thead><tr><th>Agent</th><th>Status</th><th>Duration</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <div class='footer'>SWARAMA Agent System • Auto-generated report</div>
</div>
</body></html>"""


def _build_fail_html(report: dict, ts: str) -> str:
    total = report.get("agents_total", 0)
    passed = report.get("agents_passed", 0)
    failed = report.get("agents_failed", 0)
    dur = report.get("duration_total_ms", 0) / 1000
    failures = report.get("failures_detail", [])
    rows = _agent_rows(report.get("all_results", []))
    failure_html = _failure_cards(failures)
    return f"""<html><head><style>{_CSS}</style></head><body>
<div class='container'>
  <div class='header fail'>
    <h1>⚠️ Issues Detected</h1>
    <p>SWARAMA — {ts}</p>
  </div>
  <div class='body'>
    <div class='stat-row'>
      <div class='stat'><div class='num'>{total}</div><div class='label'>Agents Run</div></div>
      <div class='stat'><div class='num' style='color:#166534'>{passed}</div><div class='label'>Passed</div></div>
      <div class='stat'><div class='num' style='color:#dc2626'>{failed}</div><div class='label'>Failed</div></div>
      <div class='stat'><div class='num'>{dur:.1f}s</div><div class='label'>Duration</div></div>
    </div>
    <h3 style='color:#dc2626'>Failures</h3>
    {failure_html}
    <h3>All Agents</h3>
    <table class='agent-table'>
      <thead><tr><th>Agent</th><th>Status</th><th>Duration</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <div class='footer'>SWARAMA Agent System • Auto-generated report</div>
</div>
</body></html>"""


def _build_critical_html(report: dict, ts: str) -> str:
    failures = report.get("failures_detail", [])
    failure_html = _failure_cards(failures)
    return f"""<html><head><style>{_CSS}</style></head><body>
<div class='container'>
  <div class='header critical'>
    <h1>🚨 CRITICAL SYSTEM FAILURE</h1>
    <p>SWARAMA — {ts} — IMMEDIATE ACTION REQUIRED</p>
  </div>
  <div class='body'>
    <p style='color:#dc2626;font-weight:600;font-size:16px;'>
      A CRITICAL failure has been detected. The rollback agent may have been triggered.
      Please investigate immediately.
    </p>
    <h3 style='color:#dc2626'>Critical Failures</h3>
    {failure_html}
  </div>
  <div class='footer'>SWARAMA Agent System • Auto-generated critical alert</div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Core send function
# ---------------------------------------------------------------------------

def _send_email(subject: str, html_body: str, plain_body: str) -> bool:
    """Send an email via Gmail SMTP. Returns True on success."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logger.warning("[email_notifier] Gmail credentials not configured — skipping email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_EMAIL
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        logger.info("[email_notifier] Email sent: %s → %s", subject[:60], ALERT_EMAIL)
        return True
    except Exception as exc:
        logger.error("[email_notifier] Failed to send email: %s", exc)
        return False


def _load_latest_report() -> dict | None:
    """Load the most recent JSON report from the reports directory."""
    try:
        reports = sorted(REPORTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not reports:
            logger.warning("[email_notifier] No report files found in %s", REPORTS_DIR)
            return None
        with open(reports[0], encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.error("[email_notifier] Failed to load latest report: %s", exc)
        return None


async def run(
    report: dict | None = None,
    plain_text_summary: str | None = None,
    system_state: dict | None = None,
    force_subject: str | None = None,
) -> dict:
    """
    Send an email notification based on the current system report.

    Args:
        report: Pre-built report dict (from report_agent). If None, loads latest from disk.
        plain_text_summary: Plain-text summary for the email body fallback.
        system_state: Optional shared orchestrator state.

    Returns:
        Standard agent result dict.
    """
    start = time.monotonic()
    agent_name = "email_notifier"
    timestamp = datetime.now(timezone.utc).isoformat()
    ts_display = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Resolve report
    if report is None:
        logger.info("[%s] No report passed — loading from disk", agent_name)
        report = _load_latest_report()

    if report is None:
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "agent": agent_name,
            "status": "FAIL",
            "timestamp": timestamp,
            "duration_ms": duration_ms,
            "details": {
                "email_sent": False,
                "subject": None,
                "error": "No report available to send",
            },
        }

    overall_status = report.get("overall_status", "UNKNOWN")
    plain_body = plain_text_summary or f"SWARAMA system report — status: {overall_status}"

    # Build subject and HTML based on status
    if overall_status == "PASS":
        subject = f"✅ SWARAMA — All systems good [{ts_display}]"
        html_body = _build_pass_html(report, ts_display)
    elif overall_status == "CRITICAL":
        subject = f"🚨 CRITICAL — SWARAMA system failure [{ts_display}]"
        html_body = _build_critical_html(report, ts_display)
    else:  # FAIL or WARN
        subject = f"⚠️ SWARAMA — Issues detected [{ts_display}]"
        html_body = _build_fail_html(report, ts_display)

    if force_subject:
        subject = force_subject

    email_sent = _send_email(subject, html_body, plain_body)

    duration_ms = int((time.monotonic() - start) * 1000)

    result = {
        "agent": agent_name,
        "status": "PASS" if email_sent else "WARN",
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "details": {
            "email_sent": email_sent,
            "subject": subject,
            "to": ALERT_EMAIL,
            "overall_status_reported": overall_status,
        },
    }

    logger.info("[%s] Done — email_sent=%s subject=%s", agent_name, email_sent, subject[:60])
    return result


# ---------------------------------------------------------------------------
# Standalone entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    result = asyncio.run(run())
    print(json.dumps(result, indent=2))
