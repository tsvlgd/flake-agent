# Architecture: agentic CI flake categorization for Podman

## 1. Problem and goal

Podman's CI runs thousands of low-level system integration tests per pull request against a container runtime that talks directly to the Linux kernel, namespaces, storage drivers, and CNI interfaces such as Netavark. That surface area is inherently prone to transient environment failures, resource contention, and timing races, on top of whatever real regressions land in a given PR.

When a run fails, a maintainer has to do two things by hand: read through 10,000 to 50,000 lines of mixed stdout and stderr across dozens of concurrent matrix jobs, and decide whether each failure is a real regression or a flaky test, one that produces false positives and false negatives at random. Doing that manually, repeatedly, across a matrix that spans Linux, macOS, and Windows, is the actual operational cost this project is meant to remove. It's a parsing and triage problem before it's an AI problem: an LLM pointed at a raw fifty-thousand-line log either misses the signal in the noise or burns tokens finding it, so the ingestion and filtering layer has to do the work first.

This document describes an asynchronous, event-driven microservice that ingests, sanitizes, parses, and triages failing CI logs from Podman's multi-OS matrix, and posts a single categorized summary back to the pull request. The system topology is shown below.

<img src="../assets/excalidraw-1.png" alt="System Architecture" width="100%">

> This architecture reflects research, design exploration, and prior experience, not a finalized implementation. Decisions are expected to evolve phase by phase as requirements firm up and mentor feedback comes in. See [DECISIONS.md](./DECISIONS.md) for the full set of architectural decision records and the reasoning behind each one; this document focuses on how the system actually flows.

## 2. Core architectural decisions

**Push-based webhooks over polling.** Polling the GitHub Workflow Runs API every 30 to 60 seconds across active forks exhausts rate limits and adds up to a minute of triage latency before analysis even starts. Instead, a FastAPI gateway stays idle until GitHub delivers an authenticated `workflow_job` webhook on completion, which costs nothing while there's no failure to look at.

**Ingestion decoupled from compute.** GitHub drops a webhook connection and starts retrying if the endpoint takes longer than 10 seconds to respond. So the edge gateway does the minimum: verify the HMAC-SHA256 signature, extract metadata, deduplicate, and enqueue, all in under 50ms, and return. The actual parsing and reasoning work happens later in a separate worker pool, decoupled from the incoming HTTP connection entirely.

**Why not the naive version of this.** A few failure modes show up quickly if you skip the above:

- Running parsing or multi-turn LLM loops inside the webhook's own request-response cycle guarantees timeouts, GitHub retries, and a feedback loop of duplicate work.
- Naively polling or pulling full log payloads across a matrix that fires thousands of parallel jobs trips GitHub's secondary rate limits and returns `403` lockouts.
- Reading a 50 to 100MB raw log straight into process memory across several concurrent workers is a fast way to trigger the Linux OOM killer.
- Feeding an unparsed multi-thousand-line log directly into an LLM context window bloats token usage, inflates cost, and degrades the model's ability to find the actual failure.

The system is built around five commitments that follow directly from those failure modes:

1. Edge HTTP operations complete in under 50ms to stay well inside GitHub's 10-second webhook timeout.
2. Zero-runner footprint: metadata comes from the webhook payload itself, nothing executes on Podman's own runners.
3. Non-failing jobs are dropped at the edge gateway in under 2ms.
4. OS and shell routing is driven by structured metadata (`matrix_os`, `runner_shell`), not by guessing from log content.
5. Logs stream through sanitization in fixed 8KB chunks straight to disk, never held whole in memory.

## 3. End-to-end data flow

```markdown
+-------------------------------------------------------------------------------------------------------+
| PHASE 0: EDGE INGESTION GATEWAY                                                                       |
| [GitHub Cloud] ---> (POST /webhook) ---> [FastAPI Gateway]                                            |
|                                                |                                                      |
|                                                ├─► HMAC SHA256 Signature Check (<2ms)                 |
|                                                ├─► Fast-Drop Filter (conclusion == "failure")         |
|                                                ├─► Extract Metadata (job_id, run_id, os, shell)       |
|                                                ├─► Redis SETNX Lock (lock:run_id:job_id)              |
|                                                └─► LPUSH tasks:fifo_queue ---> [Returns HTTP 202]     |
+--------------------------------------------------|----------------------------------------------------+
                                                   |
                                                   v
+-------------------------------------------------------------------------------------------------------+
| PHASE 1: WORKER INGESTION & STORAGE                                                                   |
| [Redis FIFO Queue] ---> (BRPOP) ---> [Celery Worker Fleet]                                            |
|                                               |                                                       |
|                                               ├─► Stream HTTP Logs in 8KB Chunks                      |
|                                               ├─► Stream Sanitization (Strip ANSI & ISO Timestamps)   |
|                                               ├─► Teardown Wall Truncation (Platform Cleanup Anchors) |
|                                               └─► Write to Shared NVMe (/app/tmp/raw_logs/job_id.log) |
+--------------------------------------------------|----------------------------------------------------+
                                                   |
                                                   v
+-------------------------------------------------------------------------------------------------------+
| PHASE 2: MULTI-OS CASCADING PARSER                                                                    |
| [NVMe Plaintext Log] + [Execution Context Metadata] ---> [Dynamic OS Router Switch]                   |
|                                                                                                       |
|    ROUTE A: Linux / macOS                              ROUTE B: Windows / PowerShell                  |
|    ├──► Attempt 1: BATS Pattern                        ├──► Attempt 1: Native Pwsh ErrorRecord        |
|    ├──► Attempt 2: Go / Ginkgo Matcher                 ├──► Attempt 2: MSBuild / csc.exe Matcher      |
|    ├──► Attempt 3: Go Compiler Matcher                 ├──► Attempt 3: Windows File Path Engine       |
|    ├──► Attempt 4: Host Level Fatal Matcher            ├──► Attempt 4: Host Level Fatal Matcher       |
|    └──► Attempt 5: Generic Tail Fallback               └──► Attempt 5: Generic Tail Fallback          |
|                                                                                                       |
| [ParseResult Object] ---> [PostgreSQL : matrix_job_results]                                           |
+--------------------------------------------------|----------------------------------------------------+
                                                   |
                                                   v
+-------------------------------------------------------------------------------------------------------+
| PHASE 3: LEDGER AGGREGATION                                                                           |
| [Master Sync Gate Step] ---> Intercept "all-success" Node                                             |
|                                      |                                                                |
|                                      └─► Query PostgreSQL ---> [ComprehensiveRunManifest]             |
+--------------------------------------------------|----------------------------------------------------+
                                                   |
                                                   v
+-------------------------------------------------------------------------------------------------------+
| PHASE 4: AGENTIC REASONING LOOP (LANGGRAPH)                                                           |
| [ComprehensiveRunManifest] ---> [Node 1: Triage Evaluator]                                            |
|                                                |                                                      |
|                 +------------------------------+------------------------------+                       |
|                 |                                                             |                       |
|      Confidence >= 0.85                                            Confidence < 0.85                  |
|                 |                                                             |                       |
|                 v                                                             v                       |
|  [GitHub PR Bot Commenter]                                    [Node 2: Log Miner System Tool]         |
|                                                                               |                       |
|                                                                               ├─► Local NVMe Lookup   |
|                                                                               └─► Re-evaluate Node 1  |
+-------------------------------------------------------------------------------------------------------+
```

## 4. Phase-by-phase breakdown

### Phase 0: edge ingestion and fast filtering

When a matrix job completes, GitHub POSTs a `workflow_job` event to `/webhook`.

**Fast-drop filter.** GitHub sends `workflow_job` events for every outcome (`success`, `failure`, `cancelled`, `skipped`). Right after the HMAC check, the gateway inspects `conclusion`. Anything other than `failure` gets a `200 OK` and is dropped in under 2ms.

**Out-of-band metadata extraction.** Rather than inferring runner properties by scanning log text later, the gateway reads them straight from the webhook payload: `workflow_job.id` as `job_id`, `workflow_job.run_id` as `run_id`, `workflow_job.labels`/`runner_name` as `matrix_os`, and `workflow_job.steps` as `runner_shell`.

**Deduplication and queueing.** An atomic `SETNX` against `lock:{run_id}:{job_id}` in Redis blocks duplicate deliveries. On a unique key, the task is pushed to `tasks:fifo_queue` and the gateway returns `202 Accepted` in under 50ms.

Internal task envelope:

```json
{
  "metadata": {
    "eventId": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
    "correlationId": "f8e7d6c5-b4a3-2f1e-0d9c-8b7a6f5e4d3c",
    "timestamp": "2026-08-13T12:00:00Z",
    "source": "github.workflow_job",
    "target_workflow": "ci.yml",
    "matrix_os": "windows-latest",
    "runner_shell": "pwsh"
  },
  "payload": {
    "action": "completed",
    "workflow_job": {
      "id": 981248,
      "run_id": 456789,
      "head_sha": "a1b2c3d4e5f67890",
      "conclusion": "failure",
      "logs_url": "https://api.github.com/repos/org/repo/actions/jobs/981248/logs"
    }
  },
  "idempotencyKey": "idemp_job_981248_sha_a1b2c3d4"
}
```

Reference implementation:

```python
# app/routers/webhook.py
from fastapi import APIRouter, Request, Header, HTTPException, status
import hmac
import hashlib
import json

router = APIRouter()

@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def handle_github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(...),
    x_github_event: str = Header(...),
):
    raw_body = await request.body()

    expected_hash = hmac.new(
        b"WEBHOOK_SECRET_TOKEN",
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        f"sha256={expected_hash}",
        x_hub_signature_256,
    ):
        raise HTTPException(status_code=401, detail="Invalid signature")

    if x_github_event != "workflow_job":
        return {"status": "ignored"}

    payload = json.loads(raw_body)
    job = payload.get("workflow_job", {})

    if payload.get("action") != "completed" or job.get("conclusion") != "failure":
        return {"status": "ignored"}

    job_id = job.get("id")
    run_id = job.get("run_id")

    key = f"lock:{run_id}:{job_id}"
    if not await redis_client.set(key, "processing", nx=True, ex=3600):
        return {"status": "ignored"}

    task = {
        "job_id": job_id,
        "run_id": run_id,
        "logs_url": job.get("logs_url"),
        "execution_metadata": {
            "matrix_os": job.get("runner_name"),
            "runner_shell": (
                "pwsh"
                if "windows" in job.get("runner_name", "").lower()
                else "bash"
            ),
        },
    }

    await redis_client.lpush(
        "tasks:fifo_queue",
        json.dumps(task),
    )

    return {
        "status": "accepted",
        "job_id": job_id,
        "run_id": run_id,
    }
```

### Phase 1: worker ingestion and streaming

**Atomic queue consumption.** Celery workers block on `BRPOP tasks:fifo_queue 5`. Redis guarantees each task is leased to exactly one worker, so there's no risk of two workers processing the same job, and idle workers cost essentially nothing while waiting.

```
[ FASTAPI EDGE GATEWAY ] --(LPUSH)--> [ REDIS LIST BUFFER ]
                                              |
                    +-------------------------+-------------------------+
                    v (BRPOP)                 v (BRPOP)                 v (BRPOP)
             [ CELERY WORKER 1 ]        [ CELERY WORKER 2 ]        [ CELERY WORKER 3 ]
              leases a task envelope      idles                      idles
```

**Streaming and sanitization.** Using the `job_id` from Phase 0, the worker streams the raw log from GitHub's REST API in fixed chunks rather than buffering the whole file, and cleans it on the fly:

1. Strip ANSI color codes (`\x1b[32m` and similar).
2. Strip leading ISO-8601 runner timestamps.
3. Truncate below the "teardown wall": platform-specific cleanup anchors that mark where the log stops being test signal and starts being shutdown noise. Linux and macOS anchors include `"Collecting logs"`, `"SIGKILL"`, `"make: *** [clean]"`. Windows/pwsh anchors include `"##[section]Finishing:"`, `"Post Job Cleanup"`, `"Write-Output"`.

The cleaned stream is written to `/app/tmp/raw_logs/job_{job_id}.log` on a shared NVMe volume.

Task state handed to the parsing layer:

```json
{
  "execution_metadata": {
    "matrix_os": "windows-latest",
    "runner_shell": "pwsh",
    "job_id": "981248",
    "run_id": "456789"
  },
  "result_file_path": "/app/tmp/raw_logs/job_981248.log"
}
```
## Phase 2:

### Phase 2: multi-OS cascading parser

The parser reads the file path from the state object and applies platform-specific regex cascades chosen by `matrix_os` (Windows, Ubuntu, macOS) and `runner_shell` (pwsh, bash, and so on), rather than by scanning log content to guess the platform:

```markdown
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STEP: Multi-Driver Cascading Router -- [PLATFORM MATRICES REDIRECTION]                           │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│   IF Context == Linux / macOS (Bash standard execution environment)                              │
│   ├───► Attempt 1: BATS Pattern Matcher                                                          │
│   ├───► Attempt 2: Go / Ginkgo Test Pattern Matcher                                              │
│   ├───► Attempt 3: Go Compiler Syntax Matcher                                                    │
│   ├───► Attempt 4: Infra / Host Level Fatal Matcher                                              │
│   └───► Attempt 5: [SAFETY NET] Generic Tail Window Fallback                                     │
│                                                                                                  │
│   IF Context == Windows (PowerShell Core execution environment)                                  │
│   ├───► Attempt 1: Native Pwsh ErrorRecord Format Engine                                         │
│   ├───► Attempt 2: MSBuild / csc.exe Compiler Syntax Matcher                                     │
│   ├───► Attempt 3: Windows File Path Drive-Letter (\) Regex Engine                               │
│   ├───► Attempt 4: Windows Infra / Host Level Fatal Matcher                                      │
│   └───► Attempt 5: [SAFETY NET] Generic Tail Window Fallback                                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### Parsing Scenarios

#### Scenario A: Linux Matrix Execution

- **Input Context:** `{"job_id": 981247, "matrix_os": "ubuntu-latest", "runner_shell": "bash"}`
- **Raw Log Window:**

```bash
2026-08-13T17:20:11.124Z \x1b[34m[INFO]\x1b[0m Starting system testing hooks...
2026-08-13T17:20:14.502Z \x1b[31m✗ podman healthcheck failure\x1b[0m
2026-08-13T17:20:14.503Z  --> pkg/drivers/podman_test.go:142: Healthcheck timed out after 15645ms
2026-08-13T17:20:14.505Z make: *** [Makefile:735: localsystem] Error 1
```

- **Output (Pydantic) JSON stored in PostgreSQL `matrix_job_results`:**

```json
{
  "strategy": "ParsingStrategy.ATTEMPT_2_GINKGO_GO_MATCH",
  "metrics": {
    "raw_line_count": 4,
    "sanitized_line_count": 3,
    "noise_reduction_percentage": 25
  },
  "payload": {
    "failing_test_id": "981247-linux-go",
    "failing_test_name": "podman healthcheck",
    "duration_ms": 15645,
    "file_path": "pkg/drivers/podman_test.go",
    "line_number": 142,
    "error_message": "Healthcheck timed out after 15645ms",
    "compiler_target": "Makefile:735: localsystem"
  }
}
```

#### Scenario B: Windows Matrix Execution

- **Input Context:** `{"job_id": 981248, "matrix_os": "windows-latest", "runner_shell": "pwsh"}`
- **Raw Log Window:**

```powershell
[17:20:11] Windows Worker Runner Node Active. Executing build pipeline commands.
[17:20:15] Write-Error: Test Execution Failure tracked on node environment.
[17:20:15] At C:\actions-runner\_work\repo\pkg\drivers\podman_test.ps1:89 char:13
[17:20:15] +             Throw "Healthcheck timed out after 15645ms"
[17:20:15]     + CategoryInfo          : OperationStopped: (Healthcheck tim...after 15645ms:String) [], RuntimeException
```

- **Output Payload (Pydantic) JSON stored in PostgreSQL `matrix_job_results`:**

```json
{
  "strategy": "ParsingStrategy.ATTEMPT_1_PWSH_ERROR_RECORD_MATCH",
  "metrics": {
    "raw_line_count": 5,
    "sanitized_line_count": 4,
    "noise_reduction_percentage": 20
  },
  "payload": {
    "failing_test_id": "981248-windows-pwsh",
    "failing_test_name": "Write-Error: Test Execution Failure tracked on node environment.",
    "duration_ms": 15645,
    "file_path": "C:\\actions-runner\\_work\\repo\\pkg\\drivers\\podman_test.ps1",
    "line_number": 89,
    "error_message": "OperationStopped: (Healthcheck tim...after 15645ms:String) [], RuntimeException",
    "compiler_target": "PowerShell Command Char Script Block 89:13"
  }
}
```

**Why the router is designed this way.** A review of Podman's own `ci.yml` (github.com/podman-container-tools/podman, `.github/workflows/ci.yml`) surfaced two things that shaped this design directly:

- The pipeline explicitly disables action caching in lint jobs (`skip-cache: true`), with a comment citing flaky results from stale cache state (see Issue #28893). That's a concrete example of a pipeline failure with nothing to do with the code under test, which is exactly why categorization has to happen out of band rather than assuming every red check is a real bug.
- `dorny/paths-filter` splits the matrix across genuinely different execution environments: POSIX nodes run Makefile targets in bash and fail with GNU `make: ***` markers and forward-slash paths, while Windows nodes run PowerShell wrappers (`.\winmake.ps1`) and fail with `$ErrorRecord` objects and backslash paths. A parser tuned for one will silently fail to extract anything useful from the other, it won't error, it'll just miss the file path and line number entirely.

That's why routing is driven by structured `matrix_os` and `runner_shell` metadata captured in Phase 0, not by scanning log text to guess the platform, and why Linux/macOS and Windows get entirely separate regex cascades rather than one cascade with conditionals bolted on.

### Phase 3: ledger aggregation

A single PR can trigger a matrix of 30+ jobs. Without synchronization, every worker that finishes would post its own comment to the PR, which is worse than no automation at all. Instead, each worker checks whether its job corresponds to the pipeline's final sync-gate check (Podman's `all-success` job, see *ref eg*: https://github.com/podman-container-tools/podman/pull/29420, which shows a single PR with 30 failing checks across the matrix). Once that gate fires, the aggregator queries PostgreSQL for every failure tied to the `run_id` and compiles a single manifest:

```json
{
  "run_id": 456789,
  "repository": "container-tools/podman",
  "head_sha": "a1b2c3d4e5f67890",
  "total_failed_jobs": 30,
  "failures": [
    { "job_id": 981247, "os": "fedora-current", "test": "podman healthcheck", "file": "pkg/drivers/podman_test.go:142", "error": "Healthcheck timed out after 15645ms" },
    { "job_id": 981248, "os": "debian-sid", "test": "podman healthcheck", "file": "pkg/drivers/podman_test.go:142", "error": "Healthcheck timed out after 15645ms" },
    { "job_id": 981249, "os": "fedora-prior", "test": "podman healthcheck", "file": "pkg/drivers/podman_test.go:142", "error": "Healthcheck timed out after 15645ms" },
    { "job_id": 981250, "os": "fedora-rawhide", "test": "podman healthcheck", "file": "pkg/drivers/podman_test.go:142", "error": "Healthcheck timed out after 15645ms" }
  ]
}
```

### Phase 4: agentic triage
![image.png](Architecture%20Proposal%20Specification%20@flake-agent/6f4197de-be0f-4ed8-b83d-1210c75b585b.png)

The manifest goes to a LangGraph loop for analysis. Unlike a single-job model, where one node evaluates one log, this receives all 30 failing jobs at once:

```python
state = {
    "run_id": 456789,
    "manifest": ComprehensiveRunManifest,   # all failing job DTOs
    "confidence": 0.0,
    "extra_log_context": {},                # { job_id: "20 lines of raw trace" }
    "final_report": None,
    "iteration_count": 0,
}
```

On its first pass, Node 1 might find that 28 of 30 failures trace to the same timeout at `podman_test.go:142`, but one Windows job failed with an ambiguous exit code the others don't share. Because confidence on that one job is below 0.85, Node 1 makes a targeted tool call to Node 2 rather than re-reasoning over the whole manifest again:

```python
@tool
def log_miner_tool(job_id: int, search_keyword: str = None, window_lines: int = 20) -> str:
    """
    Zero-token local lookup for a specific matrix job's log.
    Path: /app/tmp/raw_logs/job_{job_id}.log
    """
    log_path = f"/app/tmp/raw_logs/job_{job_id}.log"

    if os.path.exists(log_path):
        return extract_trace_window(log_path, search_keyword, window_lines)

    raw_log = refetch_github_job_log(job_id)
    return extract_trace_window_from_string(raw_log, search_keyword, window_lines)
```

Node 2 reads 20 lines around the keyword straight off disk at zero token cost and hydrates `state["extra_log_context"][job_id]`, and Node 1 re-evaluates with that added context.

```
                           [ ComprehensiveRunManifest ]
                                        |
                                        v
                        [ NODE 1: TRIAGE EVALUATOR (1st LLM pass) ]
                                        |
                    +-------------------+-------------------+
                    v                                        v
          (confidence >= 0.85)                     (confidence < 0.85)
                    |                                        |
                    |                          [ NODE 2: LOG-MINER (deterministic) ]
                    |                                        |
                    |                          [ NODE 3: DEEP REASONER (2nd LLM pass) ]
                    |                                        |
                    |                          [ Loop guard check? ] -> (>= 0.85)
                    +-------------------+-------------------+
                                        v
                        [ NODE 4: FAST-PATH PUBLISHER ]
                                        |
                                        v
                  Post one consolidated comment to the PR
                    and write to the permanent DB index
```

- **Node 1, triage evaluator.** Reads the full manifest, separates common cross-matrix patterns from isolated per-OS anomalies. If every failure resolves with high confidence, it goes straight to Node 4.
- **Node 2, log miner.** Deterministic Python, no model call. Pulls a targeted slice of a specific job's log for whichever entries Node 1 flagged as ambiguous.
- **Node 3, deep reasoner.** Re-evaluates the flagged entries with the extra log context, updating classification (for example `FLAKE_TEST` vs `GENUINE_BUG`) and pushing confidence past 0.85.
- **Node 4, publisher.** Writes the final `flake-report.json` to the database and posts one consolidated PR comment for the entire run, not one per job.

## 5. Deployment

Deployment topology isn't finalized. The current plan is a single Docker image containing FastAPI, Celery, and the agent code, split into two roles at runtime by entrypoint rather than by separate images:
```
                  ┌──────────────────────────────────────────┐
                  │        SINGLE DOCKER IMAGE BLUEPRINT     │
                  │   Contains: FastAPI + Celery + Agent     │
                  └────────────────────┬─────────────────────┘
                                       │
                ┌──────────────────────┴──────────────────────┐
                ▼                                             ▼
┌──────────────────────────────┐              ┌──────────────────────────────┐
│  CONTAINER 1: WEB TIER       │              │  CONTAINER 2: COMPUTE TIER   │
│  (Ingestion Ingress Facade)  │              │  (Asynchronous Compute Node) │
├──────────────────────────────┤              ├──────────────────────────────┤
│ Command:                     │              │ Command:                     │
│ `uvicorn app.main:app`       │              │ `celery -A app.tasks worker` │
├──────────────────────────────┤              ├──────────────────────────────┤
│ • Port 8000 exposed.         │              │ • 0 public network ports.    │
│ • Listens for GitHub Cloud.  │              │ • Listens ONLY to Redis FIFO.│
│ • Handles signature & queue. │              │ • Manages Disk IO & LLM.     │
└──────────────────────────────┘              └──────────────────────────────┘
```
The web tier runs `uvicorn app.main:app` and serves the public webhook endpoint. The compute tier runs `celery -A app.tasks worker`, handling streaming, parsing, and LLM reasoning with zero public network ports.

## 6. Deliverables

**Single-comment PR triage.** One markdown table posted to the PR once Phase 3's sync check completes, instead of one comment per failing job. Eliminates notification spam and gives maintainers one place to look.

**Context-aware multi-OS parser.** Selects a regex cascade based on `matrix_os` and `runner_shell` rather than one parser trying to handle both POSIX and PowerShell output, which is what breaks silently when a single-platform parser meets the other platform's error format.

**Isolated compute fleet.** Workers consume tasks over Redis (`BRPOP`) and stage logs on NVMe, fully decoupled from the web tier, so a burst of concurrent matrix failures can't starve the process handling incoming webhooks or push it into OOM.

## 7. Data lineage

| Phase | Boundary | Data object | Component | Storage | Retention |
| --- | --- | --- | --- | --- | --- |
| 0 | Edge ingestion | Webhook task envelope (JSON) | FastAPI gateway | Redis | 1 hour (key TTL) |
| 1 | Extraction & sanitization | Ephemeral task state | Celery worker fleet | NVMe volume | 24 hours (cron purge) |
| 2 | Parsing | Filtered parse result (JSON) | OS-specific parser | PostgreSQL | Permanent, indexed |
| 3 | Ledger aggregation | Run manifest | Sync checkpoint node | In-memory | Ephemeral (execution state) |
| 4 | Agentic triage | Flake report (JSON) | LangGraph agent | PostgreSQL | Permanent, indexed |

## 8. Further reading

The reasoning and trade-offs behind each decision above, including the two-tier deduplication strategy, the local-only LLM choice for Phase 4, and the reporting layer, are documented in [DECISIONS.md](./DECISIONS.md).
