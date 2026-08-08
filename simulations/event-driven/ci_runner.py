#!/usr/bin/env python3
"""Simulates a GitHub Actions CI Runner Engine.

Runs a lint check (always passes) and an integration test.
Without --fix: integration test fails with CRITICAL_DB_TIMEOUT.
With --fix: integration test passes (simulates a successful re-run).
All output is written to github_actions_run.log.
"""

import os
import sys

os.makedirs(
    LOG_DIR := os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
    exist_ok=True,
)
LOG_FILE = os.path.join(LOG_DIR, "github_actions_run.log")

FIX_MODE = "--fix" in sys.argv


def log(line):
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def main():
    # Clear log on fresh run (not on --fix retry)
    if not FIX_MODE:
        open(LOG_FILE, "w").close()

    log("=" * 60)
    log("GitHub Actions  —  CI Pipeline  —  Run #482910384")
    log("=" * 60)

    # Step 1: Lint check — always passes
    log("\n>> Job 1/2: Lint Check")
    log("[PASS] eslint: 0 errors, 0 warnings")
    log("[PASS] Lint Check completed successfully.")

    # Step 2: Integration test
    log("\n>> Job 2/2: Integration Test")

    if FIX_MODE:
        log(
            "[INFO] Re-run triggered with --fix flag. Retrying transient connections..."
        )
        log("[SUCCESS] Database connection re-established. All checks passed!")
        log("\n>> Workflow conclusion: success")
        sys.exit(0)
    else:
        log("[ERROR] CRITICAL_DB_TIMEOUT: Database failed to respond in 5000ms")
        log("[ERROR] Integration Test FAILED.")
        log("\n>> Workflow conclusion: failure")
        sys.exit(1)


if __name__ == "__main__":
    main()
