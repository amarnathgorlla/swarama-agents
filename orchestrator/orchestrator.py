"""
SWARAMA Agent Orchestrator
==========================
Central coordinator for all 26 agents. Never imports from other app folders.
Communicates with apps only via HTTP API or Supabase read-only key.

Run modes:
  --mode=push-trigger     : Run all testing agents in parallel (CI)
  --mode=continuous       : Run scheduler loop (Docker)
  --mode=monitoring       : Run one monitoring pass then exit
"""

import asyncio
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# ─── Path setup ───────────────────────────────────────────────────────────────
AGENTS_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = AGENTS_ROOT / "config"
LOG_DIR = AGENTS_ROOT / "logs"
REPORT_DIR = AGENTS_ROOT / "reports"

# Load env before anything else
load_dotenv(CONFIG_DIR / ".env.agents")

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agent.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("orchestrator")

# ─── Config loading ───────────────────────────────────────────────────────────
with open(Path(__file__).parent / "config.yaml") as f:
    ORC_CFG = yaml.safe_load(f)

with open(CONFIG_DIR / "thresholds.yaml") as f:
    THRESHOLDS = yaml.safe_load(f)

with open(CONFIG_DIR / "targets.yaml") as f:
    raw_targets = yaml.safe_load(f)

# Expand env vars in targets
import re

def _expand(val: str) -> str:
    if isinstance(val, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), m.group(0)), val)
    return val

TARGETS = {k: _expand(v) for k, v in raw_targets.items() if not isinstance(v, list)}

# ─── Dynamic agent imports ────────────────────────────────────────────────────
sys.path.insert(0, str(AGENTS_ROOT))

def _import_agent(module_path: str, func_name: str = "run"):
    """Lazily import an agent module and return its run() coroutine."""
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, func_name)

TESTING_AGENT_MODULES = {
    "qa_agent":          "testing.qa_agent",
    "logic_agent":       "testing.logic_agent",
    "regression_agent":  "testing.regression_agent",
    "integration_agent": "testing.integration_agent",
    "load_agent":        "testing.load_agent",
    "latency_agent":     "testing.latency_agent",
    "db_query_agent":    "testing.db_query_agent",
}

MONITORING_AGENT_MODULES = {
    "health_monitor":        "monitoring.health_monitor",
    "uptime_agent":          "monitoring.uptime_agent",
    "security_agent":        "monitoring.security_agent",
    "auth_agent":            "monitoring.auth_agent",
    "analytics_agent":       "monitoring.analytics_agent",
    "data_integrity_agent":  "monitoring.data_integrity_agent",
}

ANALYSIS_AGENT_MODULES = {
    "log_analysis_agent": "analysis.log_analysis_agent",
    "root_cause_agent":   "analysis.root_cause_agent",
}

REPORTING_AGENT_MODULES = {
    "report_agent":     "reporting.report_agent",
    "email_notifier":   "reporting.email_notifier",
    "postmortem_agent": "reporting.postmortem_agent",
    "trend_agent":      "reporting.trend_agent",
}

# ─── Shared system state ──────────────────────────────────────────────────────
system_state: dict[str, Any] = {
    "run_id": None,
    "mode": None,
    "targets": TARGETS,
    "thresholds": THRESHOLDS,
    "results": {},
    "failures": [],
    "critical": False,
    "started_at": None,
}


# ─── Agent runner ─────────────────────────────────────────────────────────────
async def run_agent(name: str, module_path: str, extra_kwargs: dict | None = None) -> dict:
    """Run a single agent, catching all exceptions so one crash never stops others."""
    logger.info(f"[START] {name}")
    t0 = time.monotonic()
    try:
        run_fn = _import_agent(module_path)
        kwargs = {"system_state": system_state, **(extra_kwargs or {})}
        result = await asyncio.wait_for(
            run_fn(**kwargs),
            timeout=ORC_CFG.get("agent_timeout_s", 120),
        )
    except asyncio.TimeoutError:
        result = _make_error_result(name, "TIMEOUT", "Agent timed out")
    except Exception as exc:
        logger.exception(f"[CRASH] {name}: {exc}")
        result = _make_error_result(name, "CRASH", str(exc))

    duration = int((time.monotonic() - t0) * 1000)
    result.setdefault("duration_ms", duration)
    result.setdefault("agent", name)
    result.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

    status = result.get("status", "UNKNOWN")
    logger.info(f"[DONE]  {name} → {status} ({duration}ms)")
    system_state["results"][name] = result

    if status in ("FAIL", "CRASH", "TIMEOUT"):
        system_state["failures"].append(result)

    return result


def _make_error_result(name: str, status: str, message: str) -> dict:
    return {
        "agent": name,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": {"error": message},
        "failures": [message],
        "duration_ms": 0,
    }


async def run_agents_parallel(agents: dict[str, str]) -> list[dict]:
    """Run a dict of {name: module_path} agents fully in parallel."""
    tasks = [run_agent(name, path) for name, path in agents.items()]
    return await asyncio.gather(*tasks, return_exceptions=False)


async def run_agents_sequential(agents: dict[str, str]) -> list[dict]:
    """Run agents one at a time (for analysis/fix chain)."""
    results = []
    for name, path in agents.items():
        r = await run_agent(name, path)
        results.append(r)
    return results


# ─── Failure handling pipeline ────────────────────────────────────────────────
async def handle_failures(failures: list[dict]):
    """Called when any agent returns FAIL. Runs root cause → report."""
    if not failures:
        return

    logger.warning(f"Handling {len(failures)} failure(s): {[f['agent'] for f in failures]}")

    # Root cause analysis
    rca = await run_agent(
        "root_cause_agent",
        ANALYSIS_AGENT_MODULES["root_cause_agent"],
        extra_kwargs={"failures": failures},
    )

    # Generate report
    await run_agent("report_agent", REPORTING_AGENT_MODULES["report_agent"])

    # Email notification
    await run_agent("email_notifier", REPORTING_AGENT_MODULES["email_notifier"])


async def handle_critical():
    """Called on CRITICAL failure. Logs warning and triggers notification email."""
    logger.critical("CRITICAL failure detected!")
    system_state["critical"] = True

    # Forced critical email
    await run_agent(
        "email_notifier",
        REPORTING_AGENT_MODULES["email_notifier"],
        extra_kwargs={"force_subject": "🚨 CRITICAL — SWARAMA system failure"},
    )


# ─── Run modes ────────────────────────────────────────────────────────────────
async def run_push_trigger():
    """Run all testing agents in parallel — called by GitHub Actions on push."""
    logger.info("=== PUSH TRIGGER: Running all testing agents ===")
    system_state["mode"] = "push-trigger"
    system_state["run_id"] = f"push-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    system_state["started_at"] = datetime.now(timezone.utc).isoformat()

    await run_agents_parallel(TESTING_AGENT_MODULES)

    failures = system_state["failures"]
    critical_count = ORC_CFG.get("critical_fail_threshold", 3)

    if len(failures) >= critical_count:
        await handle_critical()
    elif failures:
        await handle_failures(failures)
    else:
        await run_agent("report_agent", REPORTING_AGENT_MODULES["report_agent"])
        await run_agent("email_notifier", REPORTING_AGENT_MODULES["email_notifier"])

    return system_state["results"]


async def run_monitoring_pass():
    """Run all monitoring agents — called every 2 hours by scheduler."""
    logger.info("=== MONITORING PASS: Running all monitoring agents ===")
    system_state["mode"] = "monitoring"
    system_state["run_id"] = f"mon-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    system_state["started_at"] = datetime.now(timezone.utc).isoformat()
    system_state["failures"] = []

    await run_agents_parallel(MONITORING_AGENT_MODULES)

    failures = system_state["failures"]
    critical_count = ORC_CFG.get("critical_fail_threshold", 3)

    # Log analysis always runs with monitoring
    await run_agent("log_analysis_agent", ANALYSIS_AGENT_MODULES["log_analysis_agent"])

    if len(failures) >= critical_count:
        await handle_critical()
    elif failures:
        await handle_failures(failures)
    else:
        await run_agent("report_agent", REPORTING_AGENT_MODULES["report_agent"])
        await run_agent("email_notifier", REPORTING_AGENT_MODULES["email_notifier"])

    return system_state["results"]


async def run_daily_pass():
    """Run trend + postmortem — called every 24 hours by scheduler."""
    logger.info("=== DAILY PASS: Running trend + postmortem agents ===")
    await run_agent("trend_agent", REPORTING_AGENT_MODULES["trend_agent"])
    await run_agent("postmortem_agent", REPORTING_AGENT_MODULES["postmortem_agent"])


# ─── Entry point ──────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="SWARAMA Agent Orchestrator")
    parser.add_argument(
        "--mode",
        choices=["push-trigger", "continuous", "monitoring", "daily"],
        default="push-trigger",
    )
    args = parser.parse_args()

    logger.info(f"Orchestrator starting in mode: {args.mode}")

    if args.mode == "push-trigger":
        await run_push_trigger()

    elif args.mode == "monitoring":
        await run_monitoring_pass()

    elif args.mode == "daily":
        await run_daily_pass()

    elif args.mode == "continuous":
        # In continuous mode, start the APScheduler-based runner
        try:
            from orchestrator.scheduler import start_scheduler
        except ImportError:
            from scheduler import start_scheduler
        start_scheduler()


if __name__ == "__main__":
    asyncio.run(main())
