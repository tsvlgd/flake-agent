# Technical proposal: agentic CI flake categorization for Podman (Project #1963)

## Problem analysis

Podman's CI runs a dual-engine test setup: BATS integration tests under `test/system/`, and Go/Ginkgo compilation and structural tests under `test/e2e/`, both replicated across a multi-OS matrix (Fedora, Ubuntu, macOS, Windows, root and rootless). A single failed run can produce logs that run from the tens of thousands of lines into six figures once VM teardown noise, ANSI escape codes, and per-matrix-leg duplication are counted. Two things fall out of that:

1. A maintainer manually scrolling that output to find the one failing assertion among thousands of passing ones is a real, recurring time cost, not a hypothetical one, and it's separate from whether the failure is actually interesting.
2. Feeding that raw log straight into an LLM context window either truncates the part that matters or burns tokens on teardown noise the model doesn't need to see. The AI layer is only useful once the ingestion and parsing layer has already done the filtering.

This proposal treats those as two different engineering problems and solves them in that order: deterministic parsing first, agentic reasoning only for what the parser can't resolve on its own.

## Architecture: phases 0 through 4

The system runs as an independent microservice, not a change to Podman's own runners. It listens to `workflow_job` webhooks and does all of its work out of band, using a stack I already work in daily: FastAPI, Celery, Redis, and PostgreSQL.

**Phase 0, push-based ingestion gate.** A FastAPI endpoint validates each webhook with an HMAC-SHA256 signature check, and a Redis lock blocks duplicate deliveries from GitHub's retry behavior. Push over polling matters here specifically because GitHub Actions rate limits (5,000 requests per hour) make polling multiple repositories a non-starter, and GitHub drops webhook connections that take longer than ten seconds to acknowledge, so the handler does the minimum work needed to return a fast 200 and hands everything else off.

**Phase 1, chunked extraction and local volume staging.** A Celery worker pulls the job off the queue, fetches the run's raw logs from the GitHub Actions API, and streams them to a local NVMe volume in small chunks rather than holding the whole file in memory or in Redis, which avoids the OOM risk of buffering a multi-megabyte log in a service that's also handling concurrent matrix runs. A 24-hour cleanup cron keeps the volume from growing unbounded.

**Phase 2, the multi-OS teardown wall parser.** This is the deterministic filtering step, and it's the one doing most of the actual work. It reads the job's OS and shell metadata from the webhook payload, applies a normalization pass that strips ANSI codes and timestamps, locates a "teardown wall," a point past which the log is cleanup noise (container kills, VM shutdown) rather than test signal, and truncates below it. Windows PowerShell traces and Linux/macOS GNU-style traces get separate regex paths, because their failure signatures don't look alike. On LogIQ, an earlier version of this same filtering approach cut downstream LLM calls by 70 to 80 percent on a 2,000-log evaluation set; that's the target here too, though it hasn't been measured against real Podman output yet, since that's exactly what the first few weeks of this project would establish.

**Phases 3 and 4, ledger sync and LangGraph triage.** Once a matrix run's individual legs have all reported in, PostgreSQL compiles them into a single manifest. A triage agent checks a semantic cache first, so a failure signature Podman has already seen gets resolved without any model call at all, and only genuinely novel failures go to a LangGraph loop that reasons about root cause (infra flake, race condition, network timeout, and so on) using local models through Ollama or vLLM rather than a hosted API. That last choice is a deliberate departure from how LogIQ itself is built: LogIQ's own LLM fallback isn't local. Podman doesn't need a vendor dependency or a per-token cost on every CI failure, so this proposal swaps that piece out rather than porting it as-is. The result posts back to the PR as a single markdown table: root cause, confidence, and a suggested next step.

## Milestone timeline

- **Weeks 1 to 3.** Fork and port LogIQ's ingestion pipeline into the new microservice: the webhook gateway, HMAC validation, Redis dedup, and local volume staging. Deliverable: a working ingestion path from a real Podman webhook to a staged raw log on disk.
- **Weeks 4 to 6.** Build the teardown wall parser and the BATS/Ginkgo-specific matchers, tested against real historical failing runs pulled from Podman's own CI history.
- **Weeks 7 to 9.** Stand up the PostgreSQL manifest, the pgvector semantic cache, and the LangGraph triage loop running local models, with the fast-path (cache hit) and slow-path (novel failure) branches both exercised end to end.
- **Weeks 10 to 12.** Reporting automation (PR comments, GitHub Issues, weekly rollups), benchmarking against a set of known historical flakes, and documentation covering deployment and prompt tuning for maintainers.

## Stretch goal

If time allows in weeks 10 and 11, an exporter that anonymizes resolved log traces and their classifications into an open dataset, so smaller, purpose-built classification models could eventually be trained on real Podman flake data instead of starting from a general-purpose LLM every time.

## Further detail

Full architectural decision records, including the reasoning and trade-offs behind each choice above, are in the accompanying [DECISIONS.md](https://github.com/tsvlgd/flake-agent/blob/main/docs/DECISIONS.md). A visual system topology diagram covering the ingestion, parsing, and agentic-triage layers is provided separately.
