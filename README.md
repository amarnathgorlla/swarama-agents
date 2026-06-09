# SWARAMA Agent System

> **Production-grade AI agent monitoring, testing, and self-healing system for the SWARAMA roadside vehicle assistance platform.**

---

## Overview

The SWARAMA agent system is a fully autonomous, 26-agent infrastructure that continuously tests, monitors, analyses, and reports on the SWARAMA backend API. It runs every 2 hours via APScheduler (or Docker) and fires immediately on every GitHub push via CI/CD.

```
agents/
├── orchestrator/       ← Master controller + scheduler
├── testing/            ← 7 agents: fire on every code push
├── monitoring/         ← 7 agents: run every 2 hours
├── analysis/           ← 4 agents: triggered on failures
├── reporting/          ← 4 agents: email + postmortem + trends
├── config/             ← .env.agents, targets.yaml, thresholds.yaml
├── logs/               ← agent.log
├── reports/            ← JSON reports + postmortem .md files
├── requirements.txt
├── Dockerfile.agents
└── README.md (this file)
```

---

## The 26 Agents

### 🎯 Orchestrator (2 files)
| File | Role |
|------|------|
| `orchestrator/orchestrator.py` | Runs all agents, handles failures, triggers CRITICAL rollback |
| `orchestrator/scheduler.py` | APScheduler jobs: 2h monitoring, 24h postmortem, startup testing |

### 🧪 Testing Agents — fire on every push (7)
| Agent | What it tests |
|-------|--------------|
| `qa_agent` | All API endpoint status codes, response times, schemas |
| `logic_agent` | Booking flow, price calculation, mechanic dispatch logic |
| `regression_agent` | Compares responses to baseline; detects schema drift |
| `integration_agent` | End-to-end: user → booking → mechanic → completion |
| `load_agent` | 50 concurrent requests; measures success rate & latency |
| `latency_agent` | P50/P95/P99 per endpoint against thresholds |
| `db_query_agent` | Supabase table counts, query times, missing tables |

### 📡 Monitoring Agents — every 2 hours (7)
| Agent | What it monitors |
|-------|-----------------|
| `health_monitor` | Backend + Supabase liveness; all routes return 200 |
| `uptime_agent` | Rolling 24-reading uptime % per service |
| `security_agent` | SQL injection, auth bypass, oversized payloads |
| `auth_agent` | Full Supabase auth flow + RLS row isolation |
| `analytics_agent` | GA4 Measurement Protocol events + booking metrics |
| `data_integrity_agent` | Orphan bookings, duplicates, NULL coordinates |
| `chaos_agent` | Malformed JSON, timeout simulation (staging only) |

### 🔍 Analysis Agents — triggered on failure (4)
| Agent | What it does |
|-------|-------------|
| `log_analysis_agent` | Reads server logs; detects error spikes |
| `root_cause_agent` | Maps failures to known causes with confidence scores |
| `auto_fix_agent` | Applies safe fixes; creates GitHub issues for the rest |
| `rollback_agent` | On CRITICAL: finds last good SHA → triggers GitHub Actions redeploy |

### 📊 Reporting Agents — scheduled (4)
| Agent | What it produces |
|-------|----------------|
| `report_agent` | JSON report `reports/YYYY-MM-DD-HH.json` + plain text summary |
| `email_notifier` | Sends ✅/⚠️/🚨 HTML email via Gmail SMTP |
| `postmortem_agent` | Daily `reports/postmortem-YYYY-MM-DD.md` emailed at midnight |
| `trend_agent` | `reports/weekly-trend-YYYY-MM-DD.json`; emails on worsening trends |

---

## Quick Start

### 1. Configure secrets

```bash
cp agents/config/.env.agents.example agents/config/.env.agents
# Edit .env.agents with your real values
```

Required values in `.env.agents`:
```
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
GMAIL_USER=gvamarnath100@gmail.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx   # Gmail App Password (not your login password)
GITHUB_TOKEN=ghp_...
GITHUB_REPO=your-org/swarama
BACKEND_URL=http://localhost:3000
ALERT_EMAIL=gvamarnath100@gmail.com
```

### 2. Run locally

```bash
# Install dependencies
pip install -r agents/requirements.txt

# One-shot run (testing mode)
python agents/orchestrator/orchestrator.py --mode=push-trigger

# Continuous mode (scheduler, runs indefinitely)
python agents/orchestrator/orchestrator.py --mode=continuous

# Monitoring only
python agents/orchestrator/orchestrator.py --mode=monitoring-only
```

### 3. Run with Docker

```bash
# Build
docker build -f agents/Dockerfile.agents -t swarama-agents .

# Run (continuous mode)
docker run -d \
  --env-file agents/config/.env.agents \
  -v $(pwd)/agents/logs:/app/agents/logs \
  -v $(pwd)/agents/reports:/app/agents/reports \
  --name swarama-agents \
  swarama-agents

# Or with docker-compose (adds to existing services)
docker-compose up -d agents
```

### 4. GitHub Actions (automatic)

- **On push to `main`** → runs all 7 testing agents via `--mode=push-trigger`
- **Every 2 hours** → runs monitoring agents + email report
- Reports are uploaded as GitHub Actions artifacts (30 day retention)

Set these **GitHub Secrets** in your repo settings:
```
SUPABASE_URL
SUPABASE_ANON_KEY
GMAIL_USER
GMAIL_APP_PASSWORD
AGENT_GITHUB_TOKEN   (fine-grained PAT with repo write access)
BACKEND_URL
```

---

## Configuration

### `config/targets.yaml`
URLs the agents hit. Update `BACKEND_URL` to point to your deployed backend.

### `config/thresholds.yaml`
Pass/fail thresholds:
```yaml
max_response_time_ms: 2000     # API must respond within 2s
max_error_rate_percent: 5      # Max 5% error rate under load
min_uptime_percent: 99         # 99% uptime required
max_db_query_ms: 500           # DB queries must complete in 500ms
check_interval_hours: 2        # How often monitoring runs
```

---

## Standard Agent Result Format

Every agent returns this dict:

```python
{
    "agent": "agent_name",
    "status": "PASS" | "FAIL" | "WARN" | "CRITICAL",
    "timestamp": "2024-01-01T00:00:00+00:00",
    "duration_ms": 342,
    "details": { ... }   # Agent-specific payload
}
```

---

## Orchestrator Decision Tree

```
Agent returns FAIL
  → root_cause_agent (map to known cause)
  → auto_fix_agent (apply safe fix or create GitHub issue)
  → report_agent (save JSON report)
  → email_notifier (send ⚠️ email)

Agent returns CRITICAL
  → rollback_agent (redeploy last good SHA via GitHub Actions)
  → email_notifier (send 🚨 CRITICAL email immediately)

Every 2 hours
  → all monitoring agents (parallel)
  → report_agent
  → email_notifier

Every 24 hours
  → postmortem_agent (markdown report + email attachment)
  → trend_agent (7-day trend analysis)

On startup / GitHub push
  → all testing agents (parallel)
```

---

## Gmail App Password Setup

1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable 2-Step Verification
3. Search for "App passwords"
4. Create a new app password → copy the 16-character code
5. Paste into `GMAIL_APP_PASSWORD` in `.env.agents`

---

## Rules & Constraints

- ✅ No agent imports from `admin-panel/`, `mechanic-app/`, `mechbook-backend/`, `my-app-amar/`, or `user-app/`
- ✅ All secrets come from `agents/config/.env.agents` only
- ✅ All agents are async (httpx + asyncio)
- ✅ Orchestrator catches all exceptions — one agent crash never stops others
- ✅ All logs go to `agents/logs/agent.log`
- ✅ All reports saved to `agents/reports/`

---

## File Outputs

| File pattern | Generated by | When |
|---|---|---|
| `reports/YYYY-MM-DD-HH.json` | `report_agent` | Every run |
| `reports/postmortem-YYYY-MM-DD.md` | `postmortem_agent` | Every 24h |
| `reports/weekly-trend-YYYY-MM-DD.json` | `trend_agent` | Every 24h |
| `reports/uptime_history.json` | `uptime_agent` | Every 2h |
| `testing/regression/baseline_responses.json` | `regression_agent` | First run |
| `logs/agent.log` | All agents | Continuously |

---

*Built for SWARAMA — roadside vehicle assistance platform.*
