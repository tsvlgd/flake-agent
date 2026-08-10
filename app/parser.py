import re

from app.models import (
    BatsPayload,
    GoCompilerPayload,
    GoTestPayload,
    InfraFatalPayload,
    ParseResult,
    ParseStatus,
    ParsingStrategy,
    PipelineMetrics,
    TailContextPayload,
)


def sanitize_stream(raw: str) -> str:
    # Strip ANSI escape codes
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    no_ansi = ansi_escape.sub("", raw)

    # Strip ISO-8601 CI runner timestamp prefixes
    timestamp_prefix = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*", re.MULTILINE
    )
    cleaned = timestamp_prefix.sub("", no_ansi)

    return cleaned


def apply_teardown_wall(cleaned: str) -> str:
    anchors = [
        "collecting logs",
        "sending sigkill",
        "removing *.pid",
        'deleted "podman-ci"',
    ]

    lines = cleaned.splitlines()
    for i in range(len(lines)):
        line_lower = lines[i].lower()
        if any(anchor in line_lower for anchor in anchors):
            return "\n".join(lines[:i])

    return cleaned


def _match_bats(text: str) -> BatsPayload | None:
    make_match = re.search(r"make: \*\*\* \[([^\]]+)\] Error", text)
    makefile_target = make_match.group(1) if make_match else None

    failed_tests_match = re.search(
        r"Failed tests \(\d+\):\s*(?:\n\s*- \d+ \|\d+\| [^\n]+)+", text
    )
    if not failed_tests_match:
        return None

    failed_block = failed_tests_match.group(0)

    test_line_match = re.search(
        r"-\s+(\d+)\s+\|\d+\|\s+(.+?)\s+in\s+(\d+)ms", failed_block
    )
    if not test_line_match:
        return None

    failing_test_id = test_line_match.group(1)
    failing_test_name = test_line_match.group(2)
    duration_ms = int(test_line_match.group(3))

    context_lines = []
    if make_match:
        context_lines.append(make_match.group(0))
    context_lines.append(failed_block)

    return BatsPayload(
        failing_test_id=failing_test_id,
        failing_test_name=failing_test_name,
        duration_ms=duration_ms,
        makefile_target=makefile_target,
        raw_context_window="\n".join(context_lines),
    )


def _match_go_test(text: str) -> GoTestPayload | None:
    fail_match = re.search(r"--- FAIL: (.+?) ", text)
    if not fail_match:
        return None

    pkg_match = re.search(r"^FAIL\s+(\S+)", text, re.MULTILINE)
    failing_package = pkg_match.group(1) if pkg_match else "unknown"

    lines = text.splitlines()
    fail_line_idx = -1
    for i, line in enumerate(lines):
        if fail_match.group(0) in line:
            fail_line_idx = i
            break

    if fail_line_idx == -1:
        return None

    start_idx = max(0, fail_line_idx - 10)
    end_idx = min(len(lines), fail_line_idx + 11)
    context_window = "\n".join(lines[start_idx:end_idx])

    failing_line = lines[fail_line_idx - 1].strip() if fail_line_idx > 0 else ""

    return GoTestPayload(
        failing_package=failing_package,
        failing_line=failing_line,
        raw_context_window=context_window,
    )


def _match_go_compiler(text: str) -> GoCompilerPayload | None:
    match = re.search(r"(\S+\.go):(\d+):(\d+):\s+(.*)", text)
    if not match:
        return None

    lines = text.splitlines()
    match_line_idx = -1
    for i, line in enumerate(lines):
        if match.group(0) in line:
            match_line_idx = i
            break

    start_idx = max(0, match_line_idx - 5)
    end_idx = min(len(lines), match_line_idx + 6)

    return GoCompilerPayload(
        file_path=match.group(1),
        line=int(match.group(2)),
        column=int(match.group(3)),
        message=match.group(4),
        raw_context_window="\n".join(lines[start_idx:end_idx]),
    )


def _match_infra_fatal(text: str) -> InfraFatalPayload | None:
    lines = text.splitlines()
    fatal_idx = -1
    for i, line in enumerate(lines):
        if "level=fatal" in line.lower():
            fatal_idx = i
            break

    if fatal_idx == -1:
        return None

    start_idx = max(0, fatal_idx - 5)
    end_idx = min(len(lines), fatal_idx + 6)

    return InfraFatalPayload(
        message=lines[fatal_idx].strip(),
        raw_context_window="\n".join(lines[start_idx:end_idx]),
    )


def _safety_net(text: str) -> TailContextPayload:
    lines = text.splitlines()
    tail_lines = lines[-50:] if len(lines) > 50 else lines
    return TailContextPayload(raw_context_window="\n".join(tail_lines))


def parse_log(raw: str) -> ParseResult:
    raw_lines = len(raw.splitlines())

    cleaned = sanitize_stream(raw)
    cleaned = apply_teardown_wall(cleaned)

    sanitized_lines = len(cleaned.splitlines())

    if raw_lines > 0:
        reduction = (1.0 - (sanitized_lines / raw_lines)) * 100
        noise_reduction = f"{reduction:.1f}%"
    else:
        noise_reduction = "0.0%"

    metrics = PipelineMetrics(
        raw_lines_received=raw_lines,
        sanitized_lines_remaining=sanitized_lines,
        noise_reduction_ratio=noise_reduction,
    )

    bats = _match_bats(cleaned)
    if bats:
        return ParseResult(
            status=ParseStatus.SUCCESS,
            parsing_strategy=ParsingStrategy.ATTEMPT_1_BATS_MATCH,
            payload=bats,
            metrics=metrics,
        )

    gotest = _match_go_test(cleaned)
    if gotest:
        return ParseResult(
            status=ParseStatus.SUCCESS,
            parsing_strategy=ParsingStrategy.ATTEMPT_2_GO_TEST_MATCH,
            payload=gotest,
            metrics=metrics,
        )

    gocomp = _match_go_compiler(cleaned)
    if gocomp:
        return ParseResult(
            status=ParseStatus.SUCCESS,
            parsing_strategy=ParsingStrategy.ATTEMPT_3_GO_COMPILER_MATCH,
            payload=gocomp,
            metrics=metrics,
        )

    infra = _match_infra_fatal(cleaned)
    if infra:
        return ParseResult(
            status=ParseStatus.SUCCESS,
            parsing_strategy=ParsingStrategy.ATTEMPT_4_INFRA_FATAL_MATCH,
            payload=infra,
            metrics=metrics,
        )

    tail = _safety_net(cleaned)
    return ParseResult(
        status=ParseStatus.FALLBACK,
        parsing_strategy=ParsingStrategy.ATTEMPT_5_UNCLASSIFIED_TAIL,
        payload=tail,
        metrics=metrics,
    )
