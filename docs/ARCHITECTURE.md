# Architecture Proposal Specification: @flake-agent

---

## 1. Executive Summary & Architectural Core

In container runtimes such as **Podman**, continuous integration (CI) pipelines execute thousands of low-level system integration tests per pull request. Systems interfacing directly with the Linux kernel, Linux namespaces, storage drivers, and CNI interfaces (such as Netavark) are inherently susceptible to transient environment failures, resource contention, and timing races.

When a CI run fails, developers face two immediate issues:

1. **High Triage Overhead:** CI execution logs routinely span 10,000 to 50,000 lines of mixed standard output and standard error across multiple concurrent jobs.
2. **Failure Disambiguation:** Differentiating between a **deterministic regression** (a hard bug in new code) and a **"flaky"** tests,  tests that randomly produce both false positive and false negative outcomes severely degrade developer velocity and erode trust in the CI signal. 
3. When maintainers are forced to manually dig through extensive GitHub Actions logs to identify, categorize, and troubleshoot these inconsistent failures, it creates a massive, time-consuming operational burden.

*Non-deterministic* (**"flaky"**) test failures degrade developer velocity and create significant operational overhead. Manually scanning multi-megabyte GitHub Actions logs to isolate environmental blips, race conditions, network timeouts, and compiler errors is inefficient.

*The **Goal** of this architecture is to design a **microservice*** an asynchronous, event-driven system to be designed to capture, sanitize, parse, and analyze text execution logs from mixed-operating-system (Linux, macOS, Windows) matrix Continuous Integration (CI) pipelines ***[eg Podman]***.

<img src="../assets/excalidraw-1.png" alt="System Architecture" width="100%">

> **Note:** This architecture and its technical decisions are based on my research, design exploration, iterative reasoning, and prior experience. It should be considered a baseline rather than a finalized implementation; decisions may evolve phase-by-phase as requirements, practical findings, and mentor guidance shape the project.

### ***CORE Architectural decisions taken***

#### log Ingestion Strategy  [Push-Based Webhooks over Active Polling]

- **Context:** Continually polling the GitHub Workflow Runs API  every 30 to 60 seconds across active repositories quickly exhausts upstream rate limits while introducing up to a minute of pipeline triage delay.
- **Decision:** Implement an event-driven, push-based webhook gateway using **FastAPI**. The ingestion pipeline remains dormant until GitHub emits an authenticated HTTP `POST` event upon workflow completion.

#### **Rate-Limit Resilience & Ingestion Decoupling**

Podman’s CI grid triggers thousands of parallel matrix runs across multiple active forks. Pulling full log payloads sequentially or naively from the REST API quickly exhausts GitHub's secondary rate limits, causing immediate `403 Forbidden` failures.

*source: GitHub*

<img src="Architecture%20Proposal%20Specification%20%40flake-agent/image.png" alt="GitHub Webhook" width="800">

If an endpoint takes longer than 10 seconds to respond, GitHub drops the connection, flags the payload as failed, and initiates aggressive retry sequences that flood the server.

- *The system splits incoming webhooks from the core compute layer using an asynchronous message queue, satisfying GitHub's tight timeout rules by executing long-running agent workflows within isolated worker container pools.*

### **The Bottlenecks of a Naive Approach:**

- **The Webhook Delivery Timeout Engine Block:** GitHub actions [webhooks](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks) enforce a strict 10-second request-response timeout window. Running complex parsing matrices or long-running multi-turn LLM loops inside the incoming HTTP connection loop guarantees connection drops, network retries, and systemic failure loops.
- **API Rate-Limit Exhaustion:** High-frequency, parallel continuous integration matrix grids trigger thousands of active pipelines simultaneously. Blindsided polling or naive downloading of massive raw log files quickly triggers secondary GitHub API rate limits, resulting in immediate `403 Forbidden` lockout states.
- **System memory (OOM) Exhaustion:** Pulling raw 50MB to 100MB text log files straight into volatile system memory strings across parallel processing threads creates immediate RAM spikes, leading to Linux kernel Out-Of-Memory (OOM) daemon terminations.
- **Context Window Overload and Cost Inflation:** Raw text files from verbose container runtimes easily reach 10,000+  lines, consisting mostly of generic build environment noise. Injecting this **unparsed text** directly into an **LLM** *causes severe context window* bloat, drives up API bills, and degrades model evaluation accuracy.

moreover 

```markdown
+--------------------------------------------------------------------------------------------------+
|                                    CORE ARCHITECTURAL COMMITMENTS                                |
+--------------------------------------------------------------------------------------------------+
| 1. Ingestion Decoupling via Memory Buffer                                                        |
|    Edge HTTP operations complete in <50ms to meet GitHub's strict 10-second webhook timeout.     |
|                                                                                                  |
| 2. Zero-Runner Footprint & Pure Out-of-Band Operations                                           |
|    Metadata is extracted from inbound webhook payloads; no scripts run on target runners.        |
|                                                                                                  |
| 3. Fast-Drop Failure Filtering                                                                   |
|    Successful or non-failing workflow jobs are discarded at the edge gateway in <2ms.            |
|                                                                                                  |
| 4. Context-Aware Dynamic OS Parsing                                                              |
|    Routing labels (matrix_os, runner_shell) dynamically trigger OS-specific regex cascades.      |
|                                                                                                  |
| 5. Memory-Safe Stream Processing                                                                 |
|    Logs stream in fixed 8KB chunks, passing through stream-sanitizers directly to NVMe storage.  |
+--------------------------------------------------------------------------------------------------+
```

*see the core [decisions.md](./DECISIONS.md) !* 

---

## 2. End-to-End System Data Flow Architecture

### End-to-End Multi-OS Diagnostics Flow Diagram

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
| PHASE 2: CONTEXT-AWARE CASCADING PARSER                                                               |
| [NVMe Plaintext Log] + [Execution Context Metadata] ---> [Dynamic OS Router Switch]                   |
|                                                                                                       |
|    ROUTE A: Linux / macOS                              ROUTE B: Windows / PowerShell                  |
|    ├──► Attempt 1: BATS Pattern                        ├──► Attempt 1: Native Pwsh ErrorRecord        |
|    ├──► Attempt 2: Go / Ginkgo Matcher                 ├──► Attempt 2: MSBuild / csc.exe Matcher      |
|    ├──► Attempt 3: Go Compiler Matcher                 ├──► Attempt 3: Windows File Path Engine       |
|    ├──► Attempt 4: Host Level Fatal Matcher            ├──► Attempt 4: Host Level Fatal Matcher       |
|    └──► Attempt 5: Generic Tail Fallback               └──► Attempt 5: Generic Tail Fallback          |
|                                                                                                       |
| [ParseResult Object] ---> [PostgreSQL DB: matrix_job_results]                                         |
+--------------------------------------------------|----------------------------------------------------+
                                                   |
                                                   v
+-------------------------------------------------------------------------------------------------------+
| PHASE 3: TRANSACTIONAL LEDGER AGGREGATION                                                             |
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

---

## 3. Deep Phase Breakdown (Chronological Pipeline Execution)

## Phase 0:

### [Edge Ingestion, Fast-Filtering & Out-of-Band Metadata Extraction]

When a matrix workflow job completes, GitHub Cloud issues an HTTP POST payload (`workflow_job` event) to the FastAPI edge gateway `/webhook`.

#### 1. Fast-Drop Failure Filter

GitHub sends `workflow_job` completion webhooks for all outcomes (`success`, `failure`, `cancelled`, `skipped`). To eliminate unnecessary downstream processing, the edge layer inspects the JSON payload state immediately after HMAC signature validation. If `workflow_job.conclusion != "failure"`, the request is dropped in $<2\text{ms}$ with an HTTP `200 OK`.

#### 2. Out-of-Band Context Extraction

Rather than scanning text logs to infer runner properties, the gateway extracts execution metadata directly from the native webhook payload JSON fields:

- `payload.workflow_job.id` $\rightarrow$ `job_id`
- `payload.workflow_job.run_id` $\rightarrow$ `run_id`
- `payload.workflow_job.labels` / `runner_name` $\rightarrow$ `matrix_os` (`windows-latest`, `ubuntu-latest`, `macos-latest`)
- `payload.workflow_job.steps` $\rightarrow$ `runner_shell` (`bash`, `pwsh`, `zsh`)

#### 3. Edge Deduplication & Task Queueing

To protect against duplicate webhook deliveries, the gateway executes an atomic check-and-set operation (`SETNX`) inside Redis using a deterministically generated key (`lock:{run_id}:{job_id}`). If unique, the task payload is enqueued into the Redis FIFO list via `LPUSH tasks:fifo_queue`, and an HTTP `202 Accepted` response is returned in $<50\text{ms}$.

#### proposed: Webhook Ingestion Data  `[Internal Schema]`

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
      "logs_url": "<https://api.github.com/repos/org/repo/actions/jobs/981248/logs>"
    }
  },
  "idempotencyKey": "idemp_job_981248_sha_a1b2c3d4"
}
```

#### proposed: `reference implementation`

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

---

## Phase 1:

### [Worker Ingestion, Streaming &  Storage]

#### 1. Atomic Queue Consumption via `BRPOP`

Isolated **Celery worker** processes poll the Redis broker using `BRPOP tasks:fifo_queue 5`.

- **Atomic Task Leasing:** Redis `BRPOP` ensures that each task envelope is leased to exactly one worker container, eliminating race conditions.
- **Concurrency Shock Absorption:** Under heavy parallel matrix bursts, the Redis list queues inbound tasks safely while Celery workers consume them at maximum processing throughput.
- **Zero Idle CPU Overhead:** Worker threads block safely while awaiting new tasks, consuming negligible CPU cycles during idle periods.

```markdown
[ FASTAPI EDGE GATEWAY ] ──► (LPUSH tasks:fifo_queue) ──► [ REDIS LIST BUFFER ]
                                                                 │
                                 ┌───────────────────────────────┴───────────────────────────────┐
                                 ▼ (BRPOP tasks:fifo_queue 5)    ▼ (BRPOP tasks:fifo_queue 5)    ▼ (BRPOP)
                          [ CELERY WORKER 1 ]             [ CELERY WORKER 2 ]             [ CELERY WORKER 3 ]
                          (Leases Task Envelope)          (Idles efficiently)             (Idles efficiently)
```

#### 2. Streaming & Stream Sanitization

Using the `job_id` extracted in **Phase 0**, the worker **streams** the raw execution log from GitHub's **REST API** over network sockets in fixed chunks. 

*The log stream is processed on-the-fly to prevent high RAM memory utilization:*

1. **ANSI Code Removal:** Strips terminal color codes (e.g., `\x1b[32m`).
2. **Timestamp Truncation:** Strips leading ISO-8601 runner timestamp strings.
3. **Dynamic Teardown Truncation (The Teardown Wall):** Evaluates `matrix_os` and `runner_shell` properties to truncate non-essential teardown logs below platform cleanup anchors:
- *Linux / macOS Anchors:* `["Collecting logs", "SIGKILL", "make: *** [clean]"]`
- *Windows pwsh Anchors:* `["##[section]Finishing:", "Post Job Cleanup", "Write-Output"]`

The cleaned plaintext stream is written directly to a shared NVMe volume path in the **worker container**  (`/app/tmp/raw_logs/job_{job_id}.log`).

#### 3. Inter-Phase Task State Object

Once log extraction finishes, the worker constructs an ephemeral `State` passed to the parsing layer:

*contextually looks*  

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

---

## Phase 2:

### [Context-Aware Multi-OS Cascading Parser Matrix]

The `Multitier Heuristic Parser` reads the file path provided in the `State` object and applies platform-specific regex cascades directed by `matrix_os [Windows, Ubuntu/Unix/MacOS]` and `runner_shell [Pwsh, Bash, etc]`.

```markdown
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ STEP: Multi-Driver Cascading Router -- [PLATFORM MATRICES REDIRECTION]                           │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│   IF Context == Linux / macOS (Bash standard execution environment)                               │
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

### Key Engineering Findings:

#### PODMAN’s  CI workflow Structure & Dynamic Router Architectural Rationale

### 1. Upstream `ci.yml` Audit & Engineering Findings

ref: https://github.com/podman-container-tools/podman/blob/main/.github/workflows/ci.yml

on a Deep Analysis of **Podman’s** repository CI workflow) revealed critical environmental constraints that directly dictated the design of the **parsing** tier:

> **💡 Key Finding 1: Embedded Environmental Workarounds (`skip-cache: true`)**
> 
> - **Observation:** The core `ci.yml` pipeline explicitly disables action caching inside linting workflow blocks:YAML
>     
>     ```yaml
>     - uses: golangci/golangci-lint-action@v9.3.0
>       with:
>         skip-cache: true # cache causes flaky results (e.g., Issue #28893)
>     ```
>     
> - **Insight:** Non-deterministic caching on GitHub Actions runners creates artificial linting and compilation flakes. The source code itself is correct, but stale or corrupted runner cache state causes false-positive failures. This proves that pipeline failures cannot be assumed to originate from code defects alone, necessitating out-of-band categorization of runner infrastructure blips.

> **💡 Key Finding 2: Matrix Path Boundaries & Execution Diversity**
> 
> - **Observation:** Pipelines utilize path-filtering actions (`dorny/paths-filter`) to split matrix workloads dynamically across heterogeneous operating systems:
>     - **POSIX Linux / macOS Nodes:** Run standard Makefile targets inside native `bash` environments. Errors display standard GNU `make: ***` markers and forward-slash path configurations (`/app/cmd/...`).
>     - **Windows Server Nodes:** Execute custom powershell wrappers (`.\winmake.ps1 localmachine`) inside PowerShell Core (`pwsh`) on nested Hyper-V or WSL runner instances. Errors emit native PowerShell `$ErrorRecord` objects (`+ CategoryInfo : ...`) and backslash path syntax (`C:\Users\...`).
> - **Engineering Insight:** Pipeline execution environments are fundamentally non-uniform. A parser designed for POSIX terminal output will completely fail to extract file paths, line numbers, and panic traces when encountering a PowerShell `ErrorRecord` block.

### 2. Architectural Rationale: Why the Dynamic Multi-OS Router Strategy?

To address the findings from the upstream pipeline audit, the system implements a 

**Context-Aware Dynamic Multi-OS Router**.

```yaml
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│                      CORE ARCHITECTURAL RATIONALE & COMMITMENTs                          │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                          │
│  1. Out-of-Band Routing over Text Scanning                                               │
│     The parser does NOT scan or guess the operating system by reading log lines.         │
│     It extracts `matrix_os` and `runner_shell` directly from Phase 0 webhook headers     │
│     and passes them as structured metadata alongside the log file path.                  │
│                                                                                          │
│  2. Deterministic Routing Cascades                                                       │
│     The worker uses execution metadata to route the log stream directly into OS-specific │
│     specific parsers (POSIX Bash Cascade vs. Windows PowerShell Cascade).                │
│                                                                                          │
│  3. Zero-Token / Minimal CPU Execution                                                   │
│     Bypasses heavy LLM scanning or multi-pass regex matching over unverified streams.    │
│     This guarantees deterministic, high-throughput parsing at sub-millisecond speeds.    │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Phase 3:

### Multi-OS Matrix Synchronization & Aggregation

*ref eg*: https://github.com/podman-container-tools/podman/pull/29420

```markdown
🛑 GitHub Actions Status Check: Multi-OS Matrix Failure Storm

*Some checks were not successful*

🔴 30 failing, ⚪ 1 cancelled, 🟡 3 skipped, 🟢 35 successful checks
```

> **The Core Problem:** A **single PR** triggers massive *multi-OS matrix suites* .without synchronization, all concurrent worker threads would spam individuals comment bots onto the GitHub PR thread.
**The Engineering Solution:** An asynchronous, ledger-backed checkpoint that ingests failures independently, aggregates them via a master sync gate, and posts a single unified summary report.
> 

To prevent this fragmentation a synchronization checkpoint coordinates PR responses.

When a job completes parsing, the worker checks if the job corresponds to the master pipeline synchronization step (`all-success` or pipeline finish check ci.yml). 

![image.png](Architecture%20Proposal%20Specification%20@flake-agent/image%201.png)

Upon reaching this check, the gate sync coordinator queries **PostgreSQL**, aggregating error records for different jobs registered for the given `run_id` into a single 

`ComprehensiveRunManifest`:

```json
{
  "run_id": 456789,
  "repository": "container-tools/podman",
  "head_sha": "a1b2c3d4e5f67890",
  "total_failed_jobs": 30,
  "failures": [
    { 
      "job_id": 981247, 
      "os": "fedora-current", 
      "test": "podman healthcheck", 
      "file": "pkg/drivers/podman_test.go:142",
      "error": "Healthcheck timed out after 15645ms" 
    },
    { 
      "job_id": 981248, 
      "os": "debian-sid", 
      "test": "podman healthcheck", 
      "file": "pkg/drivers/podman_test.go:142",
      "error": "Healthcheck timed out after 15645ms" 
    },
    { 
      "job_id": 981249, 
      "os": "fedora-prior", 
      "test": "podman healthcheck", 
      "file": "pkg/drivers/podman_test.go:142",
      "error": "Healthcheck timed out after 15645ms" 
    },
    { 
      "job_id": 981250, 
      "os": "fedora-rawhide", 
      "test": "podman healthcheck", 
      "file": "pkg/drivers/podman_test.go:142",
      "error": "Healthcheck timed out after 15645ms" 
    }
    // ... continues dynamically up to job 30
  ]
}
```

---

## Phase 4:

### Agentic Reasoning & Flake Tracking (ref: LangGraph AI Workflow)

![image.png](Architecture%20Proposal%20Specification%20@flake-agent/6f4197de-be0f-4ed8-b83d-1210c75b585b.png)

The compiled `ComprehensiveRunManifest` payload is passed to a multi-agent LangGraph network loop for automated analysis:

#### Single-Job vs. Aggregated Matrix Input

In the single-job model, Node 1 evaluated one log file for one `job_id`. In the aggregated matrix model, Phase 4 receives **all 30 failing jobs simultaneously** inside `ComprehensiveRunManifest`.

### Shared State Evolution

```python
# AFTER (Aggregated Multi-OS Matrix State)
state = {
    "run_id": 456789,
    "manifest": ComprehensiveRunManifest,  # Contains all 30 failing job DTOs
    "confidence": 0.0,
    "extra_log_context": {},  # Map of { job_id: "20 lines of raw trace" }
    "final_report": None,
    "iteration_count": 0
}
```

### How Tool Calling Works with Aggregated Failures (Node 2)

When Node 1 runs its first LLM pass over all 30 failures, it might notice:

> *"28 jobs failed due to a global timeout at `podman_test.go:142`, but Job `981248` on Windows `pwsh` failed with an ambiguous exit code."*
> 

Because confidence for Job `981248` is low ($< 0.85$), Node 1 triggers a **parameterized tool call** to Node 2 (**Log-Miner Tool**).

#### The Tool Signature

**Node 2** it takes **specific parameters** targeting individual `job_id` entries inside the manifest:

```python
@tool
def log_miner_tool(job_id: int, search_keyword: str = None, window_lines: int = 20) -> str:
    """
    Zero-Token Local NVMe Lookup for a specific matrix job log.
    Path: /app/tmp/raw_logs/job_{job_id}.log
    """
    log_path = f"/app/tmp/raw_logs/job_{job_id}.log"

    # 1. Zero-Token NVMe Cache Lookup
    if os.path.exists(log_path):
        return extract_trace_window(log_path, search_keyword, window_lines)

    # 2. Fallback Re-fetch from GitHub REST API if evicted
    raw_log = refetch_github_job_log(job_id)
    return extract_trace_window_from_string(raw_log, search_keyword, window_lines)
```

1. The LLM specifies `job_id=981248` and `search_keyword="Write-Error"`.
2. Node 2 performs a **0-token local Python file read** directly on disk at `/app/tmp/raw_logs/job_981248.log`.
3. **tool** returns a slice of 20 log lines and hydrates `state["extra_log_context"][981248]`.

## 3. Node-by-Node Execution Flow in Phase 4

```markdown
                           [ ComprehensiveRunManifest ]
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │   NODE 1: TRIAGE EVALUATOR    │
                       │       (1st LLM Pass)          │
                       └───────────────┬───────────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
          (Confidence >= 0.85)                  (Confidence < 0.85)
                    │                                     │
                    │                         ┌───────────┴───────────┐
                    │                         │  NODE 2: LOG-MINER    │
                    │                         │      SYSTEM TOOL      │
                    │                         └───────────┬───────────┘
                    │                                     │ Targeted Job Trace
                    │                                     ▼
                    │                         ┌───────────────────────┐
                    │                         │ NODE 3: DEEP REASONER │
                    │                         │    (2nd LLM Pass)     │
                    │                         └───────────┬───────────┘
                    │                                     │
                    │                         ┌───────────┴───────────┐
                    │                         │  Loop Guard Check?    │
                    │                         └───────────┬───────────┘
                    │                                     │ (Confidence >= 0.85)
                    └──────────────────┬──────────────────┘
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │   NODE 4: FAST-PATH PUBLISHER │
                       │     (Standard Dispatcher)     │
                       └───────────────┬───────────────┘
                                       │
                                       ▼
                     Post 1 Consolidated Comment on PR
                       & Write to Permanent DB Index
```

- **Node 1 (Triage Evaluator - 1st LLM Pass):** Ingests `ComprehensiveRunManifest`. Identifies common cross-matrix patterns vs isolated OS anomalies. If all failures are clear, outputs high confidence (>= 0.85) directly to Node 4.
- **Node 2 (Log-Miner System Tool - Deterministic Python):** Executes zero-token Python line extractions for specific `job_id` values flagged as ambiguous by Node 1.

- **Node 3 (Deep Reasoner - Context-Enriched LLM Pass):** Receives the manifest *plus* the targeted log slices retrieved by Node 2. Re-evaluates root causes, updates classification (e.g., `FLAKE_TEST` vs `GENUINE_BUG`), and pushes confidence above **0.85**.
- **Node 4 (Fast-Path Publisher / Dispatcher):** Commits `flake-report.json` to the database and dispatches **one single consolidated PR comment** summarizing the entire 30-job run.

---

## Microservice Deployment Architecture

*not certainly sure on this yet, but Roughly:*

The application runs out of a single universal Docker image blueprint bundling FastAPI, Celery, LangGraph, and custom parsing modules.

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

At deployment runtime, container roles split based on execution entrypoints:

- **Web Tier Node:** Executes `uvicorn app.main:app` to serve public webhook endpoints.
- **Compute Worker Tier Node:** Executes `celery -A app.tasks worker` to perform background streaming, text processing, and AI reasoning with zero public ingress ports.

---

## System Deliverables & Engineering Justifications

### Deliverable A: Single-Comment Pull Request Triage Interface

- **Mechanism:** Posts a single markdown table summary pinned to the target Pull Request thread upon completion of Phase 3 sync checks.
- **Justification:** Eliminates automated notification noise. Developers review a single consolidated report spanning all failing operating system matrix jobs.

### Deliverable B: Context-Aware Multi-OS Parser

- **Mechanism:** Dynamic heuristic parser that evaluates incoming execution context (`matrix_os`, `runner_shell`) to select target regex cascades.
- **Justification:** Prevents structural pattern breaks caused when single-platform log parsers evaluate Windows PowerShell paths or exception blocks using POSIX match patterns.

### Deliverable C: Memory-Isolated Asynchronous Compute Fleet

- **Mechanism:** Network-isolated compute nodes consuming task envelopes via Redis RAM queues (`BRPOP`) and staging logs on NVMe volumes.
- **Justification:** Guarantees infrastructure stability during high-concurrency traffic bursts. Moving heavy text parsing and LLM calls out-of-band prevents web servers from encountering out-of-memory errors.

### Infrastructure Data Lineage Matrix

| Phase | Boundary Name | Data Transport Object (DTO) | Active Engine Target | IO Target Storage | Retention Window (TTL) |
| --- | --- | --- | --- | --- | --- |
| **Phase 0** | Edge Ingestion Gate | Webhook Task Envelope JSON | FastAPI Ingestion Gateway | Volatile Redis RAM Core | 1 Hour (String TTL) |
| **Phase 1** | Extraction & Sanitization | Ephemeral Task State Object | Celery Concurrency Fleet | Shared NVMe Disk Volume | 24 Hours (Cron Auto-Purge) |
| **Phase 2** | Text Parsing Matrix | Filtered Code Window JSON | Dynamic OS Strategy Parser | PostgreSQL Database | Permanent Indexed Record |
| **Phase 3** | Ledger Aggregation Gate | Unified Multi-OS Run Manifest | Synchronizer Checkpoint Node | System Thread Buffers | Ephemeral (Execution State) |
| **Phase 4** | Agentic Logic Triaging | Struct-Validated Flake Report | LangGraph Agent Network | PostgreSQL Database | Permanent Analytical Record |
