#!/usr/bin/env bash
# Master orchestration script — simulates the full GitHub event-driven pipeline lifecycle.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CYAN='\033[1;36m'
YELLOW='\033[1;33m'
GREEN='\033[1;32m'
RED='\033[1;31m'
RESET='\033[0m'

sep() { printf '\n%s\n' "$(printf '%.0s━' {1..60})"; }

# ─── STEP 1: GitHub Actions CI Run (Initial — Expected Failure) ──────────────
sep
printf "${CYAN}[STEP 1] GITHUB ACTIONS — CI RUNNER${RESET}\n"
printf "${YELLOW}Real-world equivalent: A developer pushes to 'main'. GitHub spins up\n"
printf "a runner VM, clones the repo, and executes the workflow jobs.${RESET}\n"
sep

# Run and allow failure (exit 1 is expected here)
python3 "$DIR/ci_runner.py" || true

# ─── STEP 2: Webhook Delivery → Monitoring Controller ────────────────────────
sep
printf "${CYAN}[STEP 2] WEBHOOK DELIVERY → MONITORING CONTROLLER${RESET}\n"
printf "${YELLOW}Real-world equivalent: GitHub's internal notification engine detects\n"
printf "the 'workflow_run.completed' event with conclusion=failure. It fires\n"
printf "an HTTP POST to your configured Payload URL, delivering the JSON body\n"
printf "to your production server. The server parses it, downloads run logs\n"
printf "via the Actions API, diagnoses the failure, and acts.${RESET}\n"
sep

cat "$DIR/webhook_payload.json" | python3 "$DIR/monitoring_controller.py"

# ─── SUMMARY ─────────────────────────────────────────────────────────────────
sep
printf "${GREEN}[DONE] Full event-driven simulation complete.${RESET}\n"
printf "Artifacts produced:\n"
printf "  • github_actions_run.log   — CI runner output\n"
printf "  • MOCK_GITHUB_ISSUES.md    — (only if hard failure path triggered)\n"
sep
