# Flake Agent

### Autonomous CI Failure Diagnostics & Flake Classification

> **Status:** Active POC / Architecture & implementation in progress

Flake Agent is a proposed **event-driven CI failure analysis system** for Podman, designed to turn large, noisy, multi-OS CI logs into structured failure signals and use agentic reasoning only where deeper analysis is required.

The core principle is:

> **Deterministic ingestion and parsing before agentic reasoning.**

Rather than sending complete CI logs directly to an LLM, the system progressively **ingests, sanitizes, parses, structures, and correlates** failure data before passing targeted context to an agentic diagnostic layer.

## Repository Structure

```text
flake-agent/
├── README.md
├── pyproject.toml
│
├── app/
│   ├── models.py
│   ├── parser.py
│   ├── test_parser.py
│   └── test_logs/
│
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DECISIONS.md
│   └── PROPOSAL.md
│
└── simulations/
    └── event-driven/
```

## Where to Start

**Architecture Deep-Dive**
→ [`ARCHITECTURE.md`](docs/ARCHITECTURE.md)

**Architectural Decisions & Rationale**
→ [`DECISIONS.md`](docs/DECISIONS.md)

**Project Proposal**
→ [`PROPOSAL.md`](docs/PROPOSAL.md)

**Event-Driven Simulations**
→ [`simulations/event-driven/`](simulations/event-driven/)

**Parser Implementation**
→ [`app/parser.py`](app/parser.py)

**Full Architecture & Design Deep-Dive**
→ [**Excalidraw ↗**](https://drive.google.com/file/d/1wdFDEeHXppabQ4JGrg2_2GkPS8znzqRh/view?usp=sharing)

## Current Scope

The repository currently contains the **research, architectural design, simulations, and initial deterministic parsing implementation** for the proposed system.

The architecture is intentionally treated as an evolving baseline; implementation details and system boundaries may change as the design is validated through further experimentation and review.
