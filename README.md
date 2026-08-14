# Flake Agent
### Autonomous CI Failure Diagnostics & Flake Classification

> **Status:** Active POC / Architecture & implementation in progress
>
> This repository contains the research, implementation, experiments, and
> architectural artifacts for an automated CI failure diagnostics system.
> The architecture is an evolving baseline and is not considered a finalized
> implementation. Some components described below are implemented, while
> others represent the current design direction and planned phases.

---

## Overview

CI failures in infrastructure-heavy projects such as **Podman** can produce
tens of thousands of lines of mixed build, test, runner, and teardown output.
Manually determining whether a failure is a genuine regression, test failure,
infrastructure problem, race condition, or transient environment issue is
expensive and difficult.

**Flake Agent** is designed as an asynchronous, event-driven microservice
pipeline that automatically:

1. Receives failed GitHub Actions workflow-job events.
2. Filters and deduplicates failures at the ingestion boundary.
3. Streams large CI logs without loading them entirely into memory.
4. Sanitizes noisy runner output and removes teardown artifacts.
5. Routes logs through context-aware parser cascades.
6. Extracts structured failure information.
7. Aggregates failures across multi-OS CI matrix jobs.
8. Uses an agentic workflow to diagnose ambiguous failures.
9. Produces a structured report for maintainers and CI tooling.

The system is specifically motivated by Podman's heterogeneous CI environment,
where Linux, macOS, and Windows runners execute different test frameworks and
produce substantially different failure signatures.

---

## Problem

A failed CI run is not necessarily a code regression.

Infrastructure-oriented CI can fail because of:

- flaky tests
- race conditions
- network timeouts
- runner failures
- QEMU / VM problems
- resource exhaustion
- compiler or build errors
- platform-specific failures
- genuine deterministic regressions

The raw logs themselves are also expensive to process. Large runs can contain
20,000–100,000+ lines of output, much of which consists of setup, environment,
and teardown noise.

The system therefore separates **log ingestion, deterministic parsing, and
agentic reasoning** instead of sending raw logs directly to an LLM.

---

# Architecture

The current architecture is divided into five conceptual phases:

```text
GitHub Actions
      │
      ▼
┌─────────────────────┐
│ Phase 0             │
│ Edge Ingestion      │
│ FastAPI + Redis     │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Phase 1             │
│ Worker Ingestion    │
│ Streaming + NVMe    │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Phase 2             │
│ Context-Aware       │
│ Parser Matrix       │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Phase 3             │
│ Matrix Aggregation  │
│ PostgreSQL          │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Phase 4             │
│ Agentic Diagnosis   │
│ LangGraph           │
└──────────┬──────────┘
           │
           ▼
    FlakeAnalysisReport
           │
           ▼
   PR / Issue / Storage
````

The detailed architecture is documented in [`Architectural Breakdown`](docs/ARCHITECTURE.md).

---

## Phase 0 — Event-Driven Ingestion

GitHub sends a `workflow_job` completion webhook to the FastAPI gateway.

The edge layer performs inexpensive operations before any heavy processing:

* HMAC-SHA256 signature validation
* failure-only filtering
* extraction of `job_id` and `run_id`
* extraction of `matrix_os` and `runner_shell`
* duplicate event detection
* Redis task queueing

Successful, skipped, or cancelled jobs are discarded before entering the
processing pipeline.

```text
GitHub Cloud
     │
     │ workflow_job.completed
     ▼
FastAPI Webhook Gateway
     │
     ├── HMAC validation
     ├── failure filter
     ├── metadata extraction
     ├── Redis SETNX deduplication
     │
     ▼
Redis FIFO Queue
```

The architectural goal is to keep the public HTTP path extremely lightweight
and move all expensive work out-of-band.

---

# Phase 1 — Streaming Ingestion & Sanitization

Background workers consume queued jobs and retrieve the corresponding GitHub
Actions logs.

Large logs are processed as streams rather than being loaded into memory as a
single string.

### Processing strategy

```text
GitHub Actions Log
       │
       ▼
8 KB Streaming Chunks
       │
       ├── Strip ANSI escape codes
       ├── Remove runner timestamps
       ├── Detect platform-specific cleanup markers
       │
       ▼
Teardown Wall
       │
       ▼
Sanitized Log
       │
       ▼
Ephemeral NVMe Storage
```

### Why streaming?

A large CI log can become a significant memory burden when multiple matrix
jobs are processed concurrently. Fixed-size chunks allow the ingestion worker
to process logs incrementally while avoiding unnecessary memory spikes.

The sanitized log is staged on local ephemeral storage for subsequent parsing
and targeted retrieval by later diagnostic stages.

---

# The Teardown Wall

CI logs often end with misleading cleanup output:

```text
Sending SIGKILL to qemu driver...
Collecting logs...
Removing test containers...
Cleaning temporary files...
```

These messages frequently occur after the actual failure.

The sanitizer therefore establishes a **Teardown Wall** and truncates the
irrelevant portion of the execution stream before downstream analysis.

For example:

```text
                 REAL FAILURE
                     │
                     ▼
────────────────────────────────────
Test failed
Healthcheck timed out
make: *** Error 1
────────────────────────────────────
             TEARDOWN WALL
────────────────────────────────────
Sending SIGKILL...
Collecting logs...
Removing containers...
Cleanup...
────────────────────────────────────
              discarded
```

This reduces irrelevant context before deterministic parsing or AI reasoning.

---

# Phase 2 — Context-Aware Multi-OS Parser

The parser does not attempt to determine the operating system by guessing from
the log text.

Instead, execution metadata obtained during webhook ingestion is passed
alongside the sanitized log.

```text
matrix_os
    +
runner_shell
    +
sanitized log
       │
       ▼
Context-Aware Router
```

This allows the parser to select an appropriate cascade.

## Linux / macOS

```text
BATS
  │
  ▼
Go / Ginkgo
  │
  ▼
Go Compiler
  │
  ▼
Host / Infrastructure
  │
  ▼
Generic Tail Fallback
```

## Windows / PowerShell

```text
PowerShell ErrorRecord
  │
  ▼
MSBuild / csc.exe
  │
  ▼
Windows Path / Error Matcher
  │
  ▼
Windows Infrastructure
  │
  ▼
Generic Tail Fallback
```

The goal is deterministic, low-cost parsing before involving an LLM.

---

# Test Framework Disambiguation

Podman's tests are distributed across several repository directories, but
directory paths alone are not reliable enough for routing. The parser
therefore prioritizes **stdout/stderr signatures produced by the underlying
test driver**.

| Directory            | Primary Driver | Signature                                   | Parser             |
| -------------------- | -------------- | ------------------------------------------- | ------------------ |
| `/test/system/`      | BATS           | `Failed tests (\d+):`, `✗ <test_name>`      | BATS Parser        |
| `/test/e2e/`         | Go / Ginkgo    | `--- FAIL: Test...`, `Ginkgo daemon failed` | Go / Ginkgo Parser |
| `/test/apiv2/`       | Go REST API    | `--- FAIL: TestApi...`                      | Go / Ginkgo Parser |
| `/test/compose/`     | Go Integration | `--- FAIL: TestCompose...`                  | Go / Ginkgo Parser |
| `/test/buildah-bud/` | Go Integration | `--- FAIL: TestBuildah...`                  | Go / Ginkgo Parser |
| Any directory        | Go Compiler    | `*.go:\d+:\d+: syntax error`, `undefined:`  | Compiler Parser    |
| System-wide          | Host / Infra   | QEMU disconnect, SSH drop, network timeout  | Infra Fallback     |

---

# Multi-Stage Routing Strategy

The deterministic parser evaluates sanitized output through progressively
broader strategies:

### 1. BATS Matcher

Detects BATS/TAP-style failures and extracts information such as:

* failure ID
* test name
* line reference
* execution duration

### 2. Go / Ginkgo Matcher

Detects Go testing and Ginkgo failure structures and extracts:

* package
* test function
* stack trace context
* error information

### 3. Go Compiler Matcher

Detects build-time failures such as:

* syntax errors
* undefined identifiers
* missing imports
* compiler failures

### 4. Infrastructure Matcher

Detects system-level failures such as:

* QEMU failures
* SSH disconnects
* network timeouts
* resource/environment anomalies

### 5. Generic Fallback

When no structured signature is matched, the parser retains a bounded
contextual tail window instead of discarding the failure.

---

# Structured Parse Result

The parser produces a typed representation instead of passing arbitrary text
between pipeline stages.

Example:

```json
{
  "strategy": "ATTEMPT_2_GINKGO_GO_MATCH",
  "metrics": {
    "raw_line_count": 241,
    "sanitized_line_count": 95,
    "noise_reduction_percentage": 60.6
  },
  "payload": {
    "failing_test_name": "podman healthcheck",
    "duration_ms": 15645,
    "file_path": "pkg/drivers/podman_test.go",
    "line_number": 142,
    "error_message": "Healthcheck timed out after 15645ms"
  }
}
```

---

# Phase 3 — Multi-OS Matrix Aggregation

A single pull request may execute dozens of jobs across different operating
systems and environments.

Analyzing each job independently can produce fragmented conclusions and
multiple redundant notifications.

The proposed aggregation layer therefore collects parsed job results into a
single:

```text
ComprehensiveRunManifest
```

Conceptually:

```text
                    Pull Request
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
     Linux Job       macOS Job        Windows Job
        │                │                │
        ▼                ▼                ▼
    ParseResult       ParseResult      ParseResult
        │                │                │
        └────────────────┼────────────────┘
                         ▼
              ComprehensiveRunManifest
```

This allows the diagnostic layer to identify patterns across the matrix.

For example:

```text
30 jobs failed
│
├── 27 × same timeout / same test
├── 2 × Linux-specific failure
└── 1 × Windows-specific anomaly
```

Rather than treating these as 30 independent failures, the diagnostic engine
can reason about their relationship.

---

# Phase 4 — Agentic CI Diagnosis

The proposed agentic layer uses **LangGraph** to reason over the aggregated
`ComprehensiveRunManifest`.

The current design uses a bounded reasoning loop rather than sending complete
raw logs to an LLM.

```text
ComprehensiveRunManifest
          │
          ▼
┌───────────────────────┐
│ Node 1                 │
│ Triage Evaluator       │
│ First LLM Pass         │
└──────────┬────────────┘
           │
      confidence?
       /         \
     high         low
      │            │
      │            ▼
      │     ┌────────────────┐
      │     │ Node 2         │
      │     │ Log Miner Tool │
      │     └───────┬────────┘
      │             │
      │             ▼
      │     ┌────────────────┐
      │     │ Node 3         │
      │     │ Deep Reasoner  │
      │     └───────┬────────┘
      │             │
      └─────────────┘
                    │
                    ▼
          ┌──────────────────┐
          │ Node 4            │
          │ Publisher         │
          └────────┬─────────┘
                   │
                   ▼
             Final Report
```

## Targeted Log Retrieval

The agent does not need to ingest the complete raw log again.

If the first reasoning pass identifies an ambiguous job, it can request a
specific trace through the deterministic log-miner tool:

```python
log_miner_tool(
    job_id=981248,
    search_keyword="Write-Error",
    window_lines=20
)
```

The tool first checks local ephemeral storage and only re-fetches from GitHub
if the required log has already been evicted.

This keeps targeted context retrieval outside the LLM token budget.

---

# Agentic Decision Loop

The current design uses a confidence threshold:

<img src="docs/Architecture Proposal Specification @flake-agent/6f4197de-be0f-4ed8-b83d-1210c75b585b.png" alt="System Architecture" width="100%">

The final diagnostic output is intended to distinguish cases such as:

```text
FLAKE_TEST
INFRA_TIMEOUT
RACE_CONDITION
NETWORK_FAILURE
BUILD_FAILURE
GENUINE_BUG
UNKNOWN
```

The exact taxonomy and reasoning behavior remain subject to implementation
and validation.

---

# Why the Architecture Is Layered

The system intentionally avoids:

```text
Raw CI Log
     │
     ▼
     LLM
     │
     ▼
   Answer
```

Instead:

```text
Raw Log
  │
  ▼
Streaming Sanitization
  │
  ▼
Deterministic Parsing
  │
  ▼
Structured Failure Data
  │
  ▼
Matrix Aggregation
  │
  ▼
Targeted Agentic Reasoning
  │
  ▼
Structured Diagnostic Report
```

This provides several advantages:

* lower memory pressure during ingestion
* deterministic extraction where possible
* reduced LLM context
* reduced unnecessary inference
* better handling of heterogeneous CI environments
* targeted retrieval for ambiguous failures
* structured downstream outputs

---

# Deployment Model

The proposed deployment separates public ingestion from background compute.

```text
                 GitHub Cloud
                      │
                      ▼
              ┌───────────────┐
              │ FastAPI       │
              │ Web Tier      │
              └───────┬───────┘
                      │
                      ▼
              ┌───────────────┐
              │ Redis Queue   │
              └───────┬───────┘
                      │
             ┌────────┼────────┐
             ▼        ▼        ▼
          Worker    Worker    Worker
             │        │        │
             └────────┼────────┘
                      │
                      ▼
              Parsing / Agents
                      │
                      ▼
                 PostgreSQL
```

The current deployment direction uses a common Docker image with separate
runtime entrypoints for the web and worker tiers.

This separation is still part of the evolving architecture and should not be
treated as a finalized production deployment topology.

---

# Repository & Documentation

The repository is organized around implementation, experimentation, and
architecture documentation.

Important areas include:

```text
.
├── README.md
├── docs/
│   ├── arch.md
│   ├── decisions.md
│   └── excalidraw-1.png
│
├── test_logs/
│   └── raw.txt
│
├── simulations/
│   └── event-driven/
│
└── ...
```

> The repository structure is evolving alongside implementation. The
> architecture documents are currently the primary reference for the intended
> system boundaries and future phases.

### Where to start

| Resource            | Purpose                                          |
| ------------------- | ------------------------------------------------ |
| `README.md`         | Project overview and implementation status       |
| `docs/arch.md`      | Detailed system architecture and phase breakdown |
| `docs/decisions.md` | Architectural decisions and their rationale      |
| `test_logs/`        | Real / experimental CI log inputs                |
| `simulations/`      | Event-driven workflow experiments                |
| `docs/*.png`        | Architecture and workflow diagrams               |

---

# Current Implementation Status

| Phase     | Status                          | Scope                                                         |
| --------- | ------------------------------- | ------------------------------------------------------------- |
| Phase 0   | **Implemented / POC**           | Webhook architecture, event ingestion simulation              |
| Phase 1   | **Implemented / POC**           | Stream sanitization, Teardown Wall, BATS parsing              |
| Phase 2   | **In Progress / Planned**       | Redis/Celery queueing, persistence, multi-OS parser expansion |
| Phase 3   | **Planned**                     | Matrix synchronization and `ComprehensiveRunManifest`         |
| Phase 4   | **Architecture / Experimental** | LangGraph agentic reasoning workflow                          |
| Reporting | **Planned**                     | PR comments, GitHub Issues, aggregated reports                |

The current implementation has been validated against a real Podman BATS system
test log, reducing 241 raw lines to 95 sanitized lines — a **60.6% reduction**
in log noise. 

---

# Engineering Findings

Analysis of Podman's CI structure influenced several architectural decisions.

### Heterogeneous execution environments

Podman's CI matrix contains substantially different execution environments,
including POSIX shells and Windows PowerShell runners. A single POSIX-oriented
parser cannot reliably extract Windows `ErrorRecord` structures or Windows
path/line information. 

### Failure output is not equivalent to source-code failure

CI failures can originate from runner state, caches, networking, timing, or
other environmental conditions. This motivates separating deterministic
failure extraction from higher-level root-cause classification. 

### Matrix failures need correlation

A single pull request can produce many concurrent failures. Aggregating parsed
job results allows the system to reason about common failures across operating
systems instead of producing one independent diagnosis per job.


Podman's tests are distributed across several repository directories, but
directory paths alone are not reliable enough for routing. The parser
therefore prioritizes **stdout/stderr signatures produced by the underlying
test driver**.

| Directory            | Primary Driver | Signature                                   | Parser             |
| -------------------- | -------------- | ------------------------------------------- | ------------------ |
| `/test/system/`      | BATS           | `Failed tests (\d+):`, `✗ <test_name>`      | BATS Parser        |
| `/test/e2e/`         | Go / Ginkgo    | `--- FAIL: Test...`, `Ginkgo daemon failed` | Go / Ginkgo Parser |
| `/test/apiv2/`       | Go REST API    | `--- FAIL: TestApi...`                      | Go / Ginkgo Parser |
| `/test/compose/`     | Go Integration | `--- FAIL: TestCompose...`                  | Go / Ginkgo Parser |
| `/test/buildah-bud/` | Go Integration | `--- FAIL: TestBuildah...`                  | Go / Ginkgo Parser |
| Any directory        | Go Compiler    | `*.go:\d+:\d+: syntax error`, `undefined:`  | Compiler Parser    |
| System-wide          | Host / Infra   | QEMU disconnect, SSH drop, network timeout  | Infra Fallback     |

---

# Design/Development Roadmap

## Phase 0 — Ingestion Architecture

* [x] Design event-driven ingestion model
* [x] Build webhook simulation
* [x] Define failure event envelope
* [x] Define out-of-band execution metadata

## Phase 1 — Deterministic Parsing

* [x] Stream sanitization
* [x] ANSI stripping
* [x] Timestamp normalization
* [x] Teardown Wall
* [x] BATS parser
* [x] Structured parse result
* [x] Validate against real Podman logs

## Phase 2 — Worker & Parser Expansion

* [ ] Redis/Celery worker integration
* [ ] Persistent result storage
* [ ] Payload fingerprinting / deduplication
* [X] Go / Ginkgo parser
* [X] Go compiler parser
* [X] Infrastructure failure matcher
* [ ] Windows / PowerShell parser
* [X] Generic fallback parser

## Phase 3 — Matrix Intelligence

* [ ] Matrix job synchronization
* [ ] PostgreSQL aggregation
* [ ] `ComprehensiveRunManifest`
* [ ] Cross-job failure correlation
* [ ] Historical failure tracking

## Phase 4 — Agentic Diagnosis

* [ ] LangGraph workflow
* [ ] Triage evaluator
* [ ] Deterministic log-miner tool
* [ ] Context-enriched deep reasoning
* [ ] Confidence-based routing
* [ ] Structured `FlakeAnalysisReport`
* [ ] Local LLM experimentation

## Reporting

* [ ] Consolidated PR comments
* [ ] GitHub Issue generation
* [ ] Historical flake reports
* [ ] Maintainer-facing diagnostics

---

# Design Principles

### 1. Deterministic before probabilistic

Use sanitization, signatures, parsers, and structured extraction before
invoking an LLM.

### 2. Stream large data

Do not materialize large CI logs in memory unnecessarily.

### 3. Context-aware routing

Use execution metadata to select the correct parser instead of guessing the
environment from arbitrary text.

### 4. Asynchronous by default

Webhook ingestion should remain lightweight while expensive work runs in
background workers.

### 5. Retrieve context on demand

Agentic reasoning should retrieve only the additional log context required to
resolve ambiguity.

### 6. Structured boundaries

Pass typed objects between stages instead of relying on free-form text.

### 7. Architecture evolves with evidence

The system is being developed as a research-driven POC. Architectural
decisions may change as implementation constraints, testing results, and
mentor feedback provide new evidence.

---

# Related Documentation

* [`Architectural Breakdown`](docs/ARCHITECTURE.md) — Detailed architecture specification
* [`Core Decisions`](docs/DECISIONS.md) — Architectural decisions and rationale
* [CNCF LFX Proposal #1963](https://github.com/cncf/mentoring/issues/1963)

---
