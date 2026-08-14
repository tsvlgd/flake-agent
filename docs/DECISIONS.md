# DECISIONS.md

---

## 1. Problem Statement & Operational Reality

***Podman** is an enterprise container runtime with complex, multi-OS E2E test suites* 

*(Fedora, Ubuntu, macOS, Windows). Flaky CI runs severely degrade engineering velocity:*

- **Log Overload:** Raw Podman CI runs produce **20,000 to 100,000+ log lines** per failed run (~10MB to 50MB raw text).
- **Developer Fatigue:** Maintainers waste hours parsing noise or blindly triggering CI re-runs to pass builds.
- **Target:** Automatically ingest failing runs, strip 80%+ noise, classify root causes using AI/LLM workflows, and report actionable fixes directly to PR comments/issues.

## 2. Core Architectural Decision Records (ADRs)

> **Note:** The architecture and architectural decisions documented here, including `decisions.md`, `arch.md`, and the related design documents, are based on my own research, design exploration, iterative decision-making, and prior experience. I do not consider the current architecture to be a 100% finalized or confirmed implementation. It is intended as a baseline architecture that provides a strong initial direction and understanding of the system. As the project progresses, requirements become clearer, implementation constraints emerge, and mentor feedback is incorporated, some of these architectural decisions may change or evolve. The architecture may also be refined phase-by-phase based on practical findings and guidance throughout the mentorship. The goal at this stage is therefore to establish a well-reasoned foundation rather than prematurely lock every implementation detail.

### ADR-01: Push-Based Webhook Gateway vs. Active Polling

- **Decision:** Build an event-driven webhook ingestion gateway using **FastAPI** listening to GitHub `workflow_run.completed` events.
- **Technical Justification:** Active REST API polling across multiple active forks/repositories rapidly hits GitHub rate limits (5,000 req/hr) and adds 30–60s Triage Latency. Push webhooks consume zero API quota during idle state and deliver sub-second event initiation upon build failure.

### ADR-02: Rate-Limit Resilience & Ingestion Decoupling

- **Decision:** Immediate HMAC-SHA256 signature validation via FastAPI, offloading job payloads to a **Redis-backed Celery worker queue**, returning HTTP `200 OK` in $<50\text{ms}$.
- **Technical Justification:** GitHub drops webhook connections if processing exceeds 10 seconds, triggering duplicate retries. Decoupling ingestion from execution guarantees zero dropped payloads under concurrent matrix execution, while Celery handles execution limits via token-bucket rate limiters and exponential backoffs.

### ADR-03: Two-Tier High-Speed Deduplication

- **Decision:** Enforce a two-tier deduplication check before remote log fetching:
    1. **Tier 1 (In-Memory RAM):** Check atomic key `run_{id}` in Redis (TTL: 1 hour).
    2. **Tier 2 (Relational DB):** Check `ci_runs` table in PostgreSQL on Redis cache misses.
- **Technical Justification:** Filters duplicate webhook events in $<1\text{ms}$, preventing redundant remote log downloads from GitHub APIs and avoiding database lock contention.

### ADR-04: Ephemeral Raw Log File Storage

- **Decision:** Stream raw logs to an ephemeral local **NVMe Docker Volume** (`/app/tmp/raw_logs/run_{id}.log`) with an automated 24-hour cleanup cron task.
- **Technical Justification:** Storing multi-megabyte raw logs in Redis risks Kernel Out-Of-Memory (OOM) crashes, while writing raw blobs to PostgreSQL causes rapid storage bloat. NVMe local volumes provide near-zero I/O latency for background parsing.

### ADR-05: Signature-Based Multi-Stage Heuristic Parser

- **Decision:** Route log streams through a deterministic 3-stage parser (Normalization $\rightarrow$ Teardown Wall Cutoff $\rightarrow$ Console Signature Matcher) *before* invoking any AI/LLM components.
- **Technical Justification:**
    - Over 80% of Podman flakes occur in `/test/system/` (BATS) and `/test/e2e/` (Go/Ginkgo).
    - **Performance & Token Accuracy:** Truncates 20,000+ lines down to a ~2KB context window (**60%–80% noise reduction**), preventing LLM context window saturation.
    - **Zero-Breakage:** Matches console stdout signatures rather than fragile folder structures, protecting the parser against repository refactoring. Unmatched formats fallback to grabbing the last 30 contextual log lines (zero dropped logs).

### ADR-07: Local AI / Agentic Workflow Framework

- **Decision:** Utilize **LangGraph** paired with local LLM runtimes (**Ollama / vLLM** running Llama 3 or Qwen models) equipped with custom function tool calls (Code Searcher, Flake Classifier).
- **Technical Justification:** Supports complete offline execution, eliminates cloud LLM vendor lock-in, avoids per-token API costs on CI failures, and provides determinism via structured JSON schema outputs.

### ADR-08: Automated Mitigation & Dispatch Layer

- **Decision:** Modular reporting engine outputting markdown summaries directly to PR comments, auto-generated GitHub Issues, and weekly aggregated reports via the GitHub REST API.
- **Technical Justification:** Directly addresses developer workflow friction by bringing actionable insights directly into PR code review interfaces.

## 3. Proposal Requirement Mapping Matrix
How the design and technical decisions fulfill the requirements from [**CNCF LFX Proposal #1963**](https://github.com/cncf/mentoring/issues/1963):

| **CNCF LFX Proposal Requirement Line / Outcome** | **Architecture Component / ADR** | **Technical Fulfillment Verification** |
| --- | --- | --- |
| **Data Ingestion Pipeline:** "automatically fetch, filter, and parse flaky CI run data and logs directly from the GitHub Actions API." | **ADR-01, ADR-02, ADR-04**<br><br>*(FastAPI + Redis/Celery + NVMe Volume)* | Webhook gateway catches completion events, Celery pulls log artifacts asynchronously without hitting rate limits, and streams raw text to NVMe volumes. |
| **Log Noise Reduction & Parsing:** "Manually digging through extensive GitHub Actions logs... is a massive, time-consuming burden." | **ADR-05**<br><br>*(Multi-Stage Heuristic Parser Engine)* | Normalizes streams, cuts VM teardown logs (`SIGKILL`, cleanup), and reduces 20k+ lines down to a clean ~2KB error snippet (BATS / Go / Ginkgo). |
| **Agentic Analysis Engine:** "integration with an AI/LLM framework... categorizes root cause (infra blips, race conditions, network timeouts) and generates plain-English analysis." | **ADR-06, ADR-07**<br><br>*(LangGraph + Local Ollama/vLLM + Semantic Cache)* | Triage router runs vector search first; if novel, LangGraph agent uses local LLMs & code search tools to classify the failure (`RACE_CONDITION`, `INFRA_TIMEOUT`, etc.) with structured explanations. |
| **Mitigation & Reporting:** "takes findings and seamlessly integrates into developer workflow (auto-generating GitHub Issues, weekly reports, PR comments)." | **ADR-08**<br><br>*(Reporting Engine & GitHub REST API)* | Generates structured Markdown triage reports containing Root Cause, Actionable Mitigation, and Flake Frequency History directly as PR comments or issues. |
| **Documentation:** "Comprehensive documentation covering tool architecture, deployment, and prompt tweaking." | **System Architecture & Config Layer** | Modular YAML prompt templates and containerized Docker Compose setups enable maintainers to customize tools and system behavior without code changes. |
| **Technologies & Skills:** "AI, CI/CD, GitHub Actions, Go, Python, Local AI" | **Full Technical Stack** | Python backend (FastAPI, Celery, LangGraph), Go test suite parsers, Local LLMs (Ollama/vLLM), and GitHub Actions webhooks. |