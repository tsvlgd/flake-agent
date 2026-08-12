# DrawThat — Agentic CI Flake Categorization & Analysis

**Codename:** `flake-agent` · **Target Repository:** `containers/podman` · **Program:** CNCF LFX Mentorship (Issue [#29265](https://github.com/containers/podman/issues/29265))

---

## 0. Requirement Mapping: Internship Objectives

| Objective (LFX Expected Outcome) | Technical Implementation Component | Responsibility |
|---|---|---|
| **Data Ingestion Pipeline** — Fetch, filter, and parse flaky CI run data from GitHub Actions API | FastAPI HMAC-secured webhook gateway + Redis broker + Celery streaming worker with NVMe disk persistence | Phase 0 (Ingestion Gateway) + Phase 1 (Worker Fleet) |
| **Agentic Analysis Engine** — LLM framework to read failure logs, categorize root cause, generate plain-English analysis | LangGraph state machine: Node 1 (Triage Evaluator) → Confidence Router → Node 2 (Log-Miner Tool) → Node 3 (Deep Reasoner w/ Circuit Breaker) | Phase 2 (Agentic Loop) |
| **Mitigation & Reporting** — Auto-generate GitHub Issues, weekly flake reports, PR comments with suggested fixes | Node 4 (Fast-Path Publisher): Pydantic-validated `FlakeAnalysisReport` → PostgreSQL commit → GitHub API dispatcher | Phase 3 (Dispatch & Reporting) |
| **Documentation** — Architecture, deployment guides, prompt/behavior tuning | This document + `docs/decisions.md` (ADR log) + inline docstrings on agent prompt templates | Continuous |

---

## 1. System Engineering Problem Statement

### The Flaky Test Problem

Podman is an enterprise-grade container runtime. Every PR triggers dozens of heavy E2E, integration, and unit test matrices across Fedora, Ubuntu, macOS, and Windows. A single failed workflow run generates **20,000–100,000+ lines** of raw text. Maintainers manually scan multi-megabyte log files to distinguish infrastructure blips from actual code bugs. This kills developer velocity.

**The flake taxonomy we target:**

```
RACE_CONDITION        — async timing / unflushed buffers (journald, cgroup locks)
INFRA_TIMEOUT         — QEMU VM hangs, network unreachable, DNS resolution failures
NETWORK_FLAKE         — transient socket resets, registry pull failures
GENUINE_BUG           — actual assertion failures introduced by the PR
UNKNOWN_FLAKE_AMBIGUOUS — circuit-breaker exhaustion, agent could not resolve
```

### Naive Approach vs. DrawThat Architecture

| Dimension | Naive (Sync Script) | DrawThat (Async Microservice) |
|---|---|---|
| **Ingestion** | `requests.get()` inside webhook handler → blocks for 30s+ → GitHub drops connection, retries flood server | FastAPI validates HMAC, returns `202 Accepted` in <50ms, offloads to Redis broker |
| **Memory** | `response.text` loads 50–100MB into RAM per worker → 5 concurrent workers = 500MB spike → OOM kill | Streaming 8KB chunk ingestion → on-the-fly regex sanitization → writes to NVMe volume. Worker RAM ≈ 0 |
| **LLM Costs** | Dumps 500K tokens of raw log per call → $1.50/run, context window overflow | Multi-stage heuristic parser reduces to ~2KB `ParseResult` (98% token reduction). Node 2 uses 0-token local file reads |
| **Fault Tolerance** | Single crash = lost webhook, no retry, no dedup | Redis dedup TTL + PostgreSQL permanent index + NVMe fallback re-hydration from GitHub API |
| **Reasoning Safety** | Unbounded LLM loops → infinite token burn | `max_iterations = 2` circuit breaker → forces `UNKNOWN_FLAKE_AMBIGUOUS` classification |

---

## 2. End-to-End System Data Flow

```
[ GITHUB CLOUD ]
       │
       │ HTTP POST (workflow_run.completed, conclusion=failure)
       │ X-Hub-Signature-256: sha256=<hmac>
       ▼
┌──────────────────────────────────────────────────────────────┐
│  FASTAPI GATEWAY (Independent WebServer Container)           │
│                                                              │
│  1. Verify HMAC signature (2ms)                              │
│  2. Redis dedup check: GET run_{id} (1ms)                    │
│     ├── HIT  → return 200 OK (abort)                         │
│     └── MISS → SET run_{id} "processed" EX 3600              │
│  3. Push task envelope to Redis broker queue (1ms)            │
│  4. Return HTTP 202 Accepted                                 │
│                                                              │
│  Total gateway latency: <5ms                                 │
└──────────────────────────────────────────────────────────────┘
       │
       │  (Task sits in Redis FIFO queue: BRPOP celery)
       ▼
┌──────────────────────────────────────────────────────────────┐
│  CELERY WORKER (Background Compute Container)                │
│                                                              │
│  Phase A: Two-Tier Deduplication                             │
│    ├── Tier 1: Redis cache check (GET run_{id})              │
│    └── Tier 2: PostgreSQL lookup (SELECT 1 FROM ci_runs)     │
│                                                              │
│  Phase B: Streaming Download (httpx, stream=True)            │
│    └── GET /repos/{owner}/{repo}/actions/runs/{id}/logs      │
│                                                              │
│  Phase C: Dual-Channel Processing                            │
│    ┌────────────────────┬────────────────────────────┐       │
│    │ CHANNEL 1: DISK    │ CHANNEL 2: PARSER ENGINE   │       │
│    │ Sanitize + Write   │ Matrix Router → ParseResult│       │
│    │ /app/tmp/raw_logs/ │ ~2KB Pydantic object       │       │
│    └────────────────────┴────────────────────────────┘       │
│                                                              │
│  Phase D: LangGraph Agentic Loop                             │
│    Node 1 (Triage) → Confidence Gate → Node 2 (Log-Miner)   │
│    → Node 3 (Deep Reasoner, circuit-breaker) → Node 4       │
│                                                              │
│  Phase E: Persistence & Dispatch                             │
│    └── FlakeAnalysisReport → PostgreSQL + GitHub PR Comment  │
└──────────────────────────────────────────────────────────────┘
```

![High-Level System Flow](/home/mehfooj/Dev/workspace/flake-agent/assets/excalidraw-1.png)

---

## 3. Data Lineage & Storage Strategy

| Stage / Phase | Data Format | Primary Storage Engine | Purpose | TTL |
|---|---|---|---|---|
| **Phase 0** — Webhook Event | JSON task envelope (~1KB) | Redis Broker (RAM) | Queueing & deduplication | 1 Hour |
| **Phase 0** — Dedup Lock | String KV `run_{id}: "processed"` (~60 bytes) | Redis Cache (RAM) | Prevent duplicate webhook storms | 1 Hour |
| **Phase 1, Step 2** — Deep Cleaned Log | Plain text file (~2MB) | Local NVMe Docker Volume (`/app/tmp/raw_logs/`) | Zero-token Node 2 log-mining lookups | 24 Hours (cron purge) |
| **Phase 1, Step 4** — ParseResult | Structured Pydantic JSON (~2KB) | PostgreSQL (`parsed_summaries`) | Deterministic indexing + Node 1 input | Permanent |
| **Phase 2** — Graph State | `TypedDict` in-memory | LangGraph runtime (worker thread) | Agent execution context | Ephemeral (thread lifetime) |
| **Phase 2, Final** — FlakeAnalysisReport | Validated Pydantic JSON (~50KB) | PostgreSQL (`flake_analysis_reports`) | PR bot comments, weekly analytics, flake dashboards | Permanent |

**Key constraint:** Redis **never** stores log content. Logs reside on NVMe volumes. Redis holds only dedup keys (~60 bytes each) and broker task envelopes.

---

## 4. Technical Component Deep-Dives

### 4.1 Ingestion Gateway (FastAPI)

**What:** A stateless, isolated Docker container exposing a single `/webhook` endpoint. Zero coupling to the CI pipeline — if the gateway crashes, GitHub Actions runs unaffected.

**How:**

```
async def webhook_handler(request: Request):
    # 1. HMAC verification (X-Hub-Signature-256)
    verify_hmac(request.headers, await request.body(), WEBHOOK_SECRET)

    # 2. Dedup short-circuit
    if await redis.get(f"run_{payload.run_id}"):
        return JSONResponse(status_code=200, content={"status": "already_processed"})

    # 3. Claim the lock
    await redis.set(f"run_{payload.run_id}", "processed", ex=3600)

    # 4. Fire-and-forget to Celery
    process_ci_failure.delay(payload.dict())

    # 5. Instant return — GitHub sees <5ms response
    return JSONResponse(status_code=202, content={"status": "accepted"})
```

**Why 202, not 200:** GitHub webhooks enforce a **10-second timeout window**. If the server doesn't respond in time, GitHub drops the connection and aggressively retries, flooding the server. By returning 202 Accepted immediately and offloading to the broker, we satisfy GitHub's timeout rules while processing 50MB logs asynchronously in background workers.

**Security:** HMAC-SHA256 signature verification on every inbound payload. Spoofed webhooks from malicious actors are rejected with 403 before touching the broker.

---

### 4.2 Worker Compute Fleet (Celery + Streaming Chunk Sanitization)

**What:** Background Celery workers that consume tasks from the Redis broker, download logs via streaming, sanitize them, run the heuristic parser, and drive the LangGraph agentic loop.

**How — Streaming Ingestion:**

```python
with open(f"/app/tmp/raw_logs/run_{run_id}.log", "wb") as disk_file:
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", logs_url, headers=github_auth) as response:
            async for chunk in response.aiter_bytes(chunk_size=8192):
                clean_chunk = sanitize_regex(chunk)
                disk_file.write(clean_chunk)
```

Each 8KB chunk is regex-filtered in-flight (ANSI codes, ISO-8601 timestamps stripped) and flushed directly to disk. The worker's peak RAM footprint stays at **~8KB** regardless of log size.

**How — Dual-Channel Split:**

After streaming completes, the pipeline bifurcates:

```
                    [ Sanitized Log Stream ]
                              │
             ┌────────────────┴────────────────┐
             ▼                                 ▼
  [ CHANNEL 1: DEEP DISK CACHE ]    [ CHANNEL 2: PARSER ENGINE ]
  • Full cleaned text on NVMe       • Multi-Stage Heuristic Router
  • /app/tmp/raw_logs/run_{id}.log  • BATS → Go Test → Go Compiler
  • For Node 2 zero-token lookups   •   → Infra Fatal → Safety Net
                                    • Output: ~2KB ParseResult
```

**The Matrix Router** inspects console output signatures, not file paths. This is critical because Podman's test directory structure changes frequently, but **BATS** and **Go/Ginkgo** stdout patterns are stable:

| Attempt | Pattern Target | Regex Anchor | Coverage |
|---|---|---|---|
| 1 | BATS shell tests | `Failed tests (\d+):` | `/test/system/`, `/test/buildah-bud/` |
| 2 | Go / Ginkgo tests | `--- FAIL:` | `/test/e2e/`, `/test/apiv2/`, `/test/compose/` |
| 3 | Go compiler errors | `*.go:\d+:\d+:` | Any package with syntax/import errors |
| 4 | Infrastructure fatal | `level=fatal` | QEMU timeouts, network disconnects |
| 5 | **Safety Net** (guaranteed) | Last 50 lines before teardown | Catch-all fallback, zero dropped logs |

**The Teardown Wall:** Before routing, a hard truncation boundary is applied at known cleanup anchors (`"Collecting logs"`, `"Sending SIGKILL"`, `"Removing *.pid"`, `"Deleted \"podman-ci\""`) to eliminate post-execution QEMU/Lima teardown noise.

---

### 4.3 NVMe Caching Array

**What:** A Docker volume mounted at `/app/tmp/raw_logs/` on each worker container, backed by local NVMe SSD.

**Why local disk, not Redis RAM:**

| Factor | Redis RAM | Local NVMe Volume |
|---|---|---|
| Storage cost per log | 2MB × RAM price = expensive | 2MB × SSD price = pennies |
| 500 failures/day | 1GB RAM consumed → OOM risk | 1GB disk → negligible |
| Access pattern | Network hop to Redis container | Direct filesystem `open()` → zero latency |
| Agent tool interface | Must deserialize from Redis string | `with open(path) as f:` — native Python file I/O |

**TTL & Purge:** A Celery Beat scheduled task (or system cron) runs hourly, scanning `/app/tmp/raw_logs/` for files with `st_mtime` older than 86400 seconds (24 hours) and deleting them.

**Fallback when file is missing:** If Node 2 attempts to read a purged file:
1. Query PostgreSQL for the `run_id` metadata (repo, commit SHA)
2. Re-fetch the log from GitHub API: `GET /repos/{owner}/{repo}/actions/runs/{id}/logs`
3. Re-sanitize on-the-fly and recreate the local file temporarily

PostgreSQL is the anchor that makes this recovery possible — it permanently stores run metadata even after ephemeral files are purged.

---

### 4.4 LangGraph Circuit Breaker

**What:** A `max_iterations = 2` guard on the agentic reasoning loop that prevents infinite LLM token burn on ambiguous logs.

**How it works:**

```
┌─────────────────────────────────────────────────────────────────────┐
│  LANGGRAPH AGENTIC WORKFLOW                                         │
│                                                                     │
│  ┌─────────────────────────┐                                        │
│  │ NODE 1: TRIAGE          │                                        │
│  │ Input: 2KB ParseResult  │                                        │
│  │ Engine: LLM Call #1     │                                        │
│  └────────────┬────────────┘                                        │
│               │                                                     │
│               ▼                                                     │
│  ┌─────────────────────────┐                                        │
│  │ CONFIDENCE ROUTER       │                                        │
│  │ Score >= 0.85?          │                                        │
│  └─────┬───────────┬───────┘                                        │
│        │           │                                                │
│     (YES)        (NO)                                               │
│        │           │                                                │
│        │           ▼                                                │
│        │  ┌──────────────────────────────────────────────┐          │
│        │  │ NODE 2: LOG-MINER TOOL                       │          │
│        │  │ • Check /app/tmp/raw_logs/run_{id}.log       │          │
│        │  │ • If exists: local 0-token Python search     │          │
│        │  │ • If missing: re-fetch from GitHub API       │          │
│        │  └──────────────────┬───────────────────────────┘          │
│        │                     │                                      │
│        │                     ▼                                      │
│        │  ┌──────────────────────────────────────────────┐          │
│        │  │ NODE 3: DEEP REASONER                        │          │
│        │  │ • Engine: Frontier LLM (advanced reasoning)  │          │
│        │  │ • Guardrail: iteration_count tracked         │          │
│        │  │ • If iter > 2 AND confidence < 0.85:         │          │
│        │  │   CIRCUIT BREAKER TRIPS →                    │          │
│        │  │   classification = UNKNOWN_FLAKE_AMBIGUOUS   │          │
│        │  └──────────────────┬───────────────────────────┘          │
│        │                     │                                      │
│        └──────────┬──────────┘                                      │
│                   ▼                                                  │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ NODE 4: FAST-PATH PUBLISHER                                  │   │
│  │ • Validates FlakeAnalysisReport via Pydantic                 │   │
│  │ • INSERT INTO flake_analysis_reports (PostgreSQL)            │   │
│  │ • POST markdown summary → GitHub PR comment                  │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

**Why the circuit breaker exists:** Without a loop guard, an ambiguous log (e.g., a partial network trace with no clear error anchor) could trap Node 3 in an infinite optimization loop. The `iteration_count` in the shared graph state increments on each pass. After 2 failed attempts to clear the 0.85 threshold, the breaker forces a safe classification and pushes the record to Node 4 for persistence.

**Shared Graph State:**

```python
class AgentState(TypedDict):
    run_id: int
    parse_result_payload: dict       # 2KB structured summary from Channel 2
    confidence_score: float          # 0.0 → 1.0
    classification: str             # Final failure mode category
    mined_log_slice: Optional[str]  # Deep context lines from Node 2
    reasoning_summary: str
    actionable_mitigation: str
    iteration_count: int            # Circuit breaker counter
```

---

## 5. Interface Contracts & Determinism

### ParseResult — Output of the Multi-Stage Heuristic Parser

This is the ~2KB structured payload generated by Channel 2 and consumed by Node 1 as its sole input. It replaces the raw 50MB log entirely for the initial triage pass.

```python
from pydantic import BaseModel
from typing import Optional, Union
from enum import Enum

class ParsingStrategy(str, Enum):
    ATTEMPT_1_BATS_MATCH = "ATTEMPT_1_BATS_MATCH"
    ATTEMPT_2_GO_TEST_MATCH = "ATTEMPT_2_GO_TEST_MATCH"
    ATTEMPT_3_GO_COMPILER_MATCH = "ATTEMPT_3_GO_COMPILER_MATCH"
    ATTEMPT_4_INFRA_FATAL_MATCH = "ATTEMPT_4_INFRA_FATAL_MATCH"
    ATTEMPT_5_UNCLASSIFIED_TAIL = "ATTEMPT_5_UNCLASSIFIED_TAIL"

class ParseStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"

class BatsPayload(BaseModel):
    failing_test_id: str
    failing_test_name: str
    duration_ms: int
    makefile_target: Optional[str] = None
    raw_context_window: str

class GoTestPayload(BaseModel):
    failing_package: str
    failing_line: str
    raw_context_window: str

class GoCompilerPayload(BaseModel):
    file_path: str
    line: int
    column: int
    message: str
    raw_context_window: str

class InfraFatalPayload(BaseModel):
    message: str
    raw_context_window: str

class TailContextPayload(BaseModel):
    raw_context_window: str
    failure_hint: Optional[str] = None

class PipelineMetrics(BaseModel):
    raw_lines_received: int
    sanitized_lines_remaining: int
    noise_reduction_ratio: str

class ParseResult(BaseModel):
    status: ParseStatus
    parsing_strategy: ParsingStrategy
    payload: Union[BatsPayload, GoTestPayload, GoCompilerPayload,
                   InfraFatalPayload, TailContextPayload]
    metrics: PipelineMetrics
```

### FlakeAnalysisReport — Output of the LangGraph Agentic Loop

This is the terminal output committed to PostgreSQL and used to generate GitHub PR comments and weekly analytics.

```python
from pydantic import BaseModel, Field
from enum import Enum

class FlakeCategory(str, Enum):
    RACE_CONDITION = "RACE_CONDITION"
    INFRA_TIMEOUT = "INFRA_TIMEOUT"
    NETWORK_FLAKE = "NETWORK_FLAKE"
    GENUINE_BUG = "GENUINE_BUG"
    FLAKY_TEST_LOGIC = "FLAKY_TEST_LOGIC"
    UNKNOWN_FLAKE_AMBIGUOUS = "UNKNOWN_FLAKE_AMBIGUOUS"

class FlakeAnalysisReport(BaseModel):
    run_id: int
    classification: FlakeCategory
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    iteration_count: int = Field(default=1, ge=1, le=3)
    reasoning_summary: str
    actionable_mitigation: str
    failing_test_name: str
    should_quarantine_test: bool = False
```

### Node Execution Logic

```
Node 1 (Triage Evaluator)
    Input:  ParseResult (~2KB)
    Output: confidence_score, preliminary classification
    Cost:   1 LLM call against compact structured data

         ┌──── confidence >= 0.85 ──── FAST PATH → Node 4
         │
         └──── confidence < 0.85  ──── FALLBACK → Node 2

Node 2 (Log-Miner Tool)
    Input:  run_id + local file path
    Output: mined_log_slice (targeted deep context)
    Cost:   0 LLM tokens (pure Python file I/O)

    Defensive fallback: if FileNotFoundError (file purged by cron),
    re-hydrate from GitHub API using run metadata from PostgreSQL.

Node 3 (Deep Reasoner)
    Input:  ParseResult + mined_log_slice (enriched context)
    Output: refined classification + confidence_score
    Cost:   1 LLM call with augmented context
    Guard:  max_iterations = 2, then circuit breaker trips

Node 4 (Fast-Path Publisher)
    Input:  Final AgentState
    Output: Pydantic-validated FlakeAnalysisReport
    Actions: PostgreSQL INSERT + GitHub PR comment POST
```

---

## 6. Microservice Scalability: Single-Image Dual-Identity

### The Architecture

One Dockerfile. One Python codebase. Two container identities at runtime.

```dockerfile
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ./app /app/app
EXPOSE 8000
# No default CMD — identity determined by docker-compose entrypoint
```

### Runtime Identity Split

```
              ┌──────────────────────────────────────────┐
              │        SINGLE DOCKER IMAGE               │
              │  Contains: FastAPI + Celery + LangGraph  │
              └────────────────────┬─────────────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
┌───────────────────────────┐          ┌───────────────────────────┐
│  WEB NODE                 │          │  WORKER NODE              │
│  (The Gateway Facade)     │          │  (The Compute Engine)     │
├───────────────────────────┤          ├───────────────────────────┤
│ CMD: uvicorn app.main:app │          │ CMD: celery -A app.tasks  │
│      --host 0.0.0.0       │          │      worker --concurrency │
│      --port 8000           │          │      =4 --loglevel=info  │
├───────────────────────────┤          ├───────────────────────────┤
│ • Port 8000 open          │          │ • 0 ports open            │
│ • Receives GitHub traffic │          │ • Invisible to internet   │
│ • CPU usage: near zero    │          │ • Connects to Redis only  │
│ • Never runs LangGraph    │          │ • Runs LangGraph loops    │
│ • Stateless               │          │ • Heavy CPU/RAM consumer  │
└───────────────────────────┘          └───────────────────────────┘
```

### docker-compose.yml (Production Topology)

```yaml
version: '3.8'
services:
  redis:
    image: redis:7-alpine
    container_name: drawthat_redis
    ports: ["6379:6379"]

  postgres:
    image: postgres:15-alpine
    container_name: drawthat_postgres
    environment:
      POSTGRES_USER: drawthat_user
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: drawthat_db
    volumes:
      - postgres_data:/var/lib/postgresql/data

  web:
    build: .
    container_name: drawthat_gateway
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DATABASE_URL=postgresql://drawthat_user:${POSTGRES_PASSWORD}@postgres:5432/drawthat_db
    depends_on: [redis, postgres]

  worker:
    build: .
    container_name: drawthat_worker
    command: celery -A app.tasks worker --loglevel=info --concurrency=4
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DATABASE_URL=postgresql://drawthat_user:${POSTGRES_PASSWORD}@postgres:5432/drawthat_db
    depends_on: [redis, postgres]
    volumes:
      - shared_raw_logs:/app/tmp/raw_logs

volumes:
  postgres_data:
  shared_raw_logs:
```

### Why This Scales

- **Webhook flood?** Scale `web` containers horizontally behind a load balancer (Nginx/ALB). Workers untouched.
- **AI backlog?** Scale `worker` containers independently. Each pulls tasks from the same Redis queue via `BRPOP`. Web nodes untouched.
- **Single image to maintain.** One `docker build`, one CI pipeline, one dependency tree.

---

## Appendix: Architectural Reference Diagrams

![Microservice Architecture — Ingestion & Infrastructure](![alt text](image.png))
![Full Pipeline — Ingestion through Application Layer](/home/mehfooj/Dev/workspace/flake-agent/assets/excalidraw-2.pngg)

![Application Layer — Agentic Loop & Data Lineage](/home/mehfooj/Dev/workspace/flake-agent/assets/excalidraw-3.png)
