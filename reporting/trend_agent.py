"""
trend_agent.py — SWARAMA Trend Analysis Agent
Reads last 7 days of reports to detect recurring failures,
degrading latency trends, and increasing error rates week-over-week.
Sends email warning if worsening trends are detected.
"""

import asyncio
import json
import logging
import os
import smtplib
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
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
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("swarama.trend_agent")

RECURRING_FAILURE_THRESHOLD = 3  # Same agent failing more than N times in 7 days


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_reports_last_n_days(days: int = 7) -> list[dict]:
    """Load all JSON report files from the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    reports = []
    for path in sorted(REPORTS_DIR.glob("*.json")):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                    data["_mtime"] = mtime.isoformat()
                    data["_filename"] = path.name
                    reports.append(data)
        except Exception as exc:
            logger.warning("Could not read %s: %s", path, exc)
    return reports


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def _detect_recurring_failures(reports: list[dict]) -> list[dict]:
    """
    Find agents that failed more than RECURRING_FAILURE_THRESHOLD times.
    Returns list of {agent, failure_count, occurrences}.
    """
    failure_counts: Counter = Counter()
    failure_times: dict = defaultdict(list)

    for r in reports:
        for failure in r.get("failures_detail", []):
            agent = failure.get("agent", "unknown")
            failure_counts[agent] += 1
            failure_times[agent].append(r.get("timestamp", ""))

    recurring = []
    for agent, count in failure_counts.items():
        if count > RECURRING_FAILURE_THRESHOLD:
            recurring.append({
                "agent": agent,
                "failure_count": count,
                "occurrences": failure_times[agent],
            })

    return sorted(recurring, key=lambda x: x["failure_count"], reverse=True)


def _detect_latency_trend(reports: list[dict]) -> dict:
    """
    Detect if average total run duration is increasing over time.
    Returns {trending_up: bool, early_avg_ms: float, recent_avg_ms: float, change_pct: float}.
    """
    if len(reports) < 4:
        return {"trending_up": False, "early_avg_ms": 0, "recent_avg_ms": 0, "change_pct": 0.0}

    mid = len(reports) // 2
    early = reports[:mid]
    recent = reports[mid:]

    def avg_duration(rs: list) -> float:
        durations = [r.get("duration_total_ms", 0) for r in rs if r.get("duration_total_ms")]
        return sum(durations) / len(durations) if durations else 0.0

    early_avg = avg_duration(early)
    recent_avg = avg_duration(recent)

    if early_avg == 0:
        return {"trending_up": False, "early_avg_ms": 0, "recent_avg_ms": recent_avg, "change_pct": 0.0}

    change_pct = ((recent_avg - early_avg) / early_avg) * 100
    trending_up = change_pct > 15  # More than 15% increase is a warning

    return {
        "trending_up": trending_up,
        "early_avg_ms": round(early_avg, 1),
        "recent_avg_ms": round(recent_avg, 1),
        "change_pct": round(change_pct, 1),
    }


def _detect_error_rate_trend(reports: list[dict]) -> dict:
    """
    Detect week-over-week error rate increase.
    Returns {worsening: bool, early_error_rate: float, recent_error_rate: float}.
    """
    if len(reports) < 4:
        return {"worsening": False, "early_error_rate": 0.0, "recent_error_rate": 0.0}

    mid = len(reports) // 2
    early = reports[:mid]
    recent = reports[mid:]

    def error_rate(rs: list) -> float:
        total = len(rs)
        if total == 0:
            return 0.0
        failed = sum(1 for r in rs if r.get("overall_status") in ("FAIL", "CRITICAL"))
        return (failed / total) * 100

    early_rate = error_rate(early)
    recent_rate = error_rate(recent)
    worsening = recent_rate > early_rate + 10  # More than 10% points worse

    return {
        "worsening": worsening,
        "early_error_rate": round(early_rate, 1),
        "recent_error_rate": round(recent_rate, 1),
        "change_points": round(recent_rate - early_rate, 1),
    }


def _detect_most_common_failure_agents(reports: list[dict], top_n: int = 5) -> list[dict]:
    """Return the top N agents by total failure count."""
    counts: Counter = Counter()
    for r in reports:
        for f in r.get("failures_detail", []):
            counts[f.get("agent", "unknown")] += 1
    return [{"agent": a, "count": c} for a, c in counts.most_common(top_n)]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _send_trend_warning_email(trend_report: dict) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logger.warning("[trend_agent] Gmail credentials not set — skipping email")
        return False

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"📈 SWARAMA — Worsening trend detected [{date_str}]"

    recurring = trend_report.get("recurring_failures", [])
    latency = trend_report.get("latency_trend", {})
    error_rate = trend_report.get("error_rate_trend", {})

    warnings = []
    for r in recurring:
        warnings.append(f"<li><strong>{r['agent']}</strong> failed {r['failure_count']} times in 7 days</li>")
    if latency.get("trending_up"):
        warnings.append(
            f"<li>Average run duration increased by <strong>{latency['change_pct']}%</strong> "
            f"({latency['early_avg_ms']}ms → {latency['recent_avg_ms']}ms)</li>"
        )
    if error_rate.get("worsening"):
        warnings.append(
            f"<li>Error rate worsened by <strong>{error_rate['change_points']} percentage points</strong> "
            f"({error_rate['early_error_rate']}% → {error_rate['recent_error_rate']}%)</li>"
        )

    warnings_html = "\n".join(warnings) if warnings else "<li>None</li>"

    html_body = f"""
    <html><body style="font-family:sans-serif;max-width:640px;margin:auto;">
    <div style="background:linear-gradient(135deg,#f59e0b,#d97706);padding:24px;border-radius:12px 12px 0 0;">
      <h2 style="color:#fff;margin:0;">📈 Worsening Trend Detected</h2>
      <p style="color:#fff;opacity:.85;margin:6px 0 0;">SWARAMA Weekly Trend Report — {date_str}</p>
    </div>
    <div style="background:#fff;padding:24px;border-radius:0 0 12px 12px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
      <h3 style="color:#dc2626;">⚠️ Detected Issues</h3>
      <ul style="color:#555;line-height:1.8;">{warnings_html}</ul>
      <hr>
      <p style="color:#888;font-size:12px;">Full trend data saved to reports/weekly-trend-{date_str}.json</p>
      <p style="color:#888;font-size:12px;">Generated by SWARAMA trend_agent</p>
    </div>
    </body></html>
    """

    plain_body = (
        f"SWARAMA Trend Warning — {date_str}\n\n"
        f"Worsening trends detected in the last 7 days:\n"
        + "\n".join(f"- {w.replace('<li>', '').replace('</li>', '').replace('<strong>', '').replace('</strong>', '')}" for w in warnings)
        + "\n\nSee reports/weekly-trend-{date_str}.json for details."
    )

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
        logger.info("[trend_agent] Warning email sent to %s", ALERT_EMAIL)
        return True
    except Exception as exc:
        logger.error("[trend_agent] Email send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------

async def run(system_state: dict | None = None) -> dict:
    """
    Analyse 7-day trends and generate weekly trend report.

    Returns:
        Standard agent result dict.
    """
    start = time.monotonic()
    agent_name = "trend_agent"
    timestamp = datetime.now(timezone.utc).isoformat()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    logger.info("[%s] Starting 7-day trend analysis", agent_name)

    reports = _load_reports_last_n_days(days=7)
    logger.info("[%s] Loaded %d reports from last 7 days", agent_name, len(reports))

    # Run all analyses
    recurring_failures = _detect_recurring_failures(reports)
    latency_trend = _detect_latency_trend(reports)
    error_rate_trend = _detect_error_rate_trend(reports)
    top_failing_agents = _detect_most_common_failure_agents(reports)

    worsening = (
        bool(recurring_failures)
        or latency_trend.get("trending_up", False)
        or error_rate_trend.get("worsening", False)
    )

    trend_report = {
        "date": date_str,
        "timestamp": timestamp,
        "reports_analysed": len(reports),
        "worsening_trend_detected": worsening,
        "recurring_failures": recurring_failures,
        "latency_trend": latency_trend,
        "error_rate_trend": error_rate_trend,
        "top_failing_agents": top_failing_agents,
    }

    # Save weekly trend report
    trend_filename = f"weekly-trend-{date_str}.json"
    trend_path = REPORTS_DIR / trend_filename
    try:
        with open(trend_path, "w", encoding="utf-8") as fh:
            json.dump(trend_report, fh, indent=2, default=str)
        logger.info("[%s] Trend report saved: %s", agent_name, trend_path)
    except Exception as exc:
        logger.error("[%s] Failed to save trend report: %s", agent_name, exc)
        trend_path = None

    # Send email warning if worsening
    email_sent = False
    if worsening:
        logger.warning("[%s] Worsening trends detected — sending email", agent_name)
        email_sent = _send_trend_warning_email(trend_report)

    duration_ms = int((time.monotonic() - start) * 1000)

    result = {
        "agent": agent_name,
        "status": "WARN" if worsening else "PASS",
        "timestamp": timestamp,
        "duration_ms": duration_ms,
        "details": {
            "reports_analysed": len(reports),
            "worsening_trend_detected": worsening,
            "recurring_failures": recurring_failures,
            "latency_trend": latency_trend,
            "error_rate_trend": error_rate_trend,
            "top_failing_agents": top_failing_agents,
            "trend_report_path": str(trend_path) if trend_path else None,
            "email_sent": email_sent,
        },
    }

    logger.info("[%s] Done — worsening=%s email_sent=%s", agent_name, worsening, email_sent)
    return result


# ---------------------------------------------------------------------------
# Standalone entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    result = asyncio.run(run())
    print(json.dumps(result, indent=2))
