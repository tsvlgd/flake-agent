# DECISIONS.md

## 1. Problem statement and operational reality

Podman is a container runtime with complex, multi-OS end-to-end test suites (Fedora, Ubuntu, macOS, Windows). Flaky CI runs degrade engineering velocity in two concrete ways: raw failed-run logs commonly run from the tens of thousands of lines into six figures once a full multi-OS matrix is counted, and maintainers lose real time either parsing that noise by hand or re-running CI blind, hoping the failure was transient. The target is to automatically ingest failing runs, cut the noise down to what's actually signal, classify root cause with an AI/LLM workflow, and report actionable findings directly to PR comments and issues.

> This architecture and the decisions below are based on my own research, design exploration, and prior experience. None of it is a finalized or confirmed implementation. It's a baseline that gives a strong initial direction, and I expect some of these decisions to change as requirements firm up, implementation constraints surface, and mentor feedback comes in. The goal at this stage is a well-reasoned foundation, not every detail locked in advance.

## 2. Core architectural decision records

### ADR-01: push-based webhook gateway vs. active polling

**Decision.** Build an event-driven ingestion gateway using FastAPI, listening to GitHub `workflow_job` completion events.

**Justification.** Active polling across multiple forks and repositories rapidly hits GitHub's rate limit (5,000 requests/hour) and adds 30 to 60 seconds of triage latency before analysis even starts. Push webhooks consume zero API quota while idle and deliver sub-second event initiation on a build failure.

### ADR-02: rate-limit resilience and ingestion decoupling

**Decision.** Immediate HMAC-SHA256 signature validation in FastAPI, offloading job payloads to a Redis-backed Celery worker queue, returning `200 OK` in under 50ms.

**Justification.** GitHub drops a webhook connection and starts retrying if processing takes longer than 10 seconds. Decoupling ingestion from execution guarantees no dropped payloads under concurrent matrix execution, while Celery handles downstream rate limits with token-bucket limiting and exponential backoff.

### ADR-03: two-tier deduplication

**Decision.** Check for duplicates before any remote log fetch, in two tiers:
1. In-memory: atomic key `run_{id}` in Redis, 1 hour TTL.
2. Relational: `ci_runs` table in PostgreSQL, checked on a Redis miss.

**Justification.** Filters duplicate webhook events in under 1ms, avoiding redundant log downloads from the GitHub API and avoiding database lock contention under bursty matrix traffic.

### ADR-04: ephemeral raw log storage

**Decision.** Stream raw logs to an ephemeral local NVMe volume (`/app/tmp/raw_logs/run_{id}.log`), with an automated 24-hour cleanup cron.

**Justification.** Storing multi-megabyte raw logs in Redis risks OOM; writing raw blobs to PostgreSQL causes storage bloat fast. A local NVMe volume gives near-zero I/O latency for the parsing step that follows.

### ADR-05: signature-based multi-stage heuristic parser

**Decision.** Route log streams through a deterministic three-stage parser (normalization, then teardown-wall cutoff, then console signature matching) before any AI or LLM component runs.

**Justification.** A review of Podman's own `ci.yml` shows pipeline failures that have nothing to do with the code under test, for example lint jobs explicitly disabling action caching because stale cache state produces flaky results (see Issue #28893), which is why categorization has to happen out of band rather than assuming every red check is a real regression. The parser matches console stdout signatures rather than folder structure, so it doesn't break when the repository gets refactored, and unmatched formats fall back to the last 30 contextual log lines rather than dropping anything. The target noise reduction is 60 to 80 percent of raw lines, based on the same filtering approach measured at 70 to 80 percent fewer downstream LLM calls on a 2,000-log evaluation set in an earlier version of this parser (LogIQ); that hasn't been validated against real Podman output yet, which is exactly what the first few weeks of implementation would establish.

### ADR-06: semantic cache for repeat failures

**Decision.** Before a failure goes to the LangGraph triage loop, check a pgvector-backed similarity cache of previously classified failure signatures. A high-confidence match resolves the failure at zero additional compute cost; only signatures with no close match go to the agentic loop.

**Justification.** A meaningful share of CI flakes are the same underlying issue recurring across different PRs and runs, not novel each time. Resolving known signatures from cache avoids paying for a full LLM reasoning pass on something the system has already classified once, and keeps the agentic loop reserved for genuinely new failure modes.

### ADR-07: local AI and agentic workflow framework

**Decision.** LangGraph paired with local LLM runtimes (Ollama or vLLM, running Llama 3 or Qwen), with custom tool calls for log mining and classification.

**Justification.** Full offline execution, no cloud LLM vendor dependency, no per-token cost on every CI failure, and deterministic structured JSON output. This is a deliberate departure from the earlier LogIQ version this is adapted from, which does use a hosted model as a fallback; Podman doesn't need that dependency or its cost profile.

### ADR-08: automated mitigation and dispatch layer

**Decision.** A modular reporting engine that outputs markdown summaries directly to PR comments, auto-generated GitHub Issues, and weekly aggregated reports via the GitHub REST API.

**Justification.** Puts the finding directly into the interface maintainers already review PRs in, rather than a dashboard they have to remember to check.

## 3. Requirement mapping

How this design fulfills the outcomes from [CNCF LFX Proposal #1963](https://github.com/cncf/mentoring/issues/1963):

| LFX requirement | Architecture component | Fulfillment |
| --- | --- | --- |
| Data ingestion pipeline: automatically fetch, filter, and parse flaky CI run data from the GitHub Actions API | ADR-01, ADR-02, ADR-04 (FastAPI + Redis/Celery + NVMe) | Webhook gateway catches completion events, Celery pulls log artifacts asynchronously without hitting rate limits, raw text streams to NVMe |
| Log noise reduction and parsing: manually digging through extensive logs is a massive burden | ADR-05 | Normalizes streams, cuts teardown noise, reduces raw logs down to a clean signal snippet across BATS and Go/Ginkgo output |
| Agentic analysis engine: integrate an AI/LLM framework that categorizes root cause and explains it in plain English | ADR-06, ADR-07 | Semantic cache resolves known signatures first; LangGraph agent on local models classifies novel failures (race condition, infra timeout, and so on) with a structured explanation |
| Mitigation and reporting: findings integrate into developer workflow via issues, weekly reports, PR comments | ADR-08 | Structured markdown reports with root cause, suggested mitigation, and flake frequency history, posted as PR comments or issues |
| Documentation: architecture, deployment, prompt tweaking | Architecture doc + config layer | Modular prompt templates and a containerized deployment let maintainers adjust behavior without touching code |
| Technologies and skills: AI, CI/CD, GitHub Actions, Go, Python, local AI | Full stack | Python backend (FastAPI, Celery, LangGraph), Go test suite parsers, local LLMs via Ollama/vLLM, GitHub Actions webhooks |
