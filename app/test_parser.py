import pathlib

import pytest

from app.models import ParseStatus, ParsingStrategy
from app.parser import apply_teardown_wall, parse_log, sanitize_stream

MOCK_BATS_LOG = """\
2026-08-09T08:58:14.1011123Z \x1b[32m[+0910s] ok 38 |030| podman run with --net=none in 2092ms\x1b[0m
2026-08-09T08:58:15.9123456Z \x1b[31mmake: *** [Makefile:735: localsystem] Error 1\x1b[0m
2026-08-09T08:58:15.9200000Z 
2026-08-09T08:58:15.9210000Z \x1b[31mFailed tests (1):\x1b[0m
2026-08-09T08:58:15.9220000Z \x1b[31m - 212 |220| podman healthcheck in 15645ms\x1b[0m
2026-08-09T08:58:16.0000000Z Collecting logs
2026-08-09T08:58:16.0123456Z time="2026-08-09T08:58:16Z" level=info msg="Sending SIGKILL to the qemu driver process 2982"
2026-08-09T08:58:16.0200000Z time="2026-08-09T08:58:16Z" level=info msg="Deleted \\"podman-ci\\""
"""

MOCK_GO_TEST_LOG = """\
2026-08-09T10:00:00.0000000Z === RUN   TestPodmanSuite/TestPodmanHealthcheck
2026-08-09T10:00:01.0000000Z     pod_test.go:42: expected healthy, got unhealthy
2026-08-09T10:00:01.0000000Z --- FAIL: TestPodmanSuite/TestPodmanHealthcheck (1.23s)
2026-08-09T10:00:02.0000000Z FAIL	github.com/containers/podman/v5/test	1.500s
2026-08-09T10:00:03.0000000Z Collecting logs
"""

MOCK_UNCLASSIFIED_LOG = """\
2026-08-09T10:00:00.0000000Z Starting CI run...
2026-08-09T10:00:01.0000000Z Setting up environment...
2026-08-09T10:00:02.0000000Z Running tests...
2026-08-09T10:00:03.0000000Z Test execution in progress...
2026-08-09T10:00:60.0000000Z Error: The operation was canceled.
"""


def test_sanitize_strips_ansi():
    raw = "\x1b[31mError 1\x1b[0m"
    cleaned = sanitize_stream(raw)
    assert cleaned == "Error 1"


def test_sanitize_strips_timestamps():
    raw = "2026-08-09T08:58:15.9123456Z Some message"
    cleaned = sanitize_stream(raw)
    assert cleaned == "Some message"


def test_teardown_wall_truncates():
    cleaned = sanitize_stream(MOCK_BATS_LOG)
    truncated = apply_teardown_wall(cleaned)
    assert "Collecting logs" not in truncated
    assert "podman healthcheck" in truncated


def test_teardown_wall_no_anchor():
    text = "line 1\\nline 2"
    assert apply_teardown_wall(text) == text


def test_bats_match():
    result = parse_log(MOCK_BATS_LOG)
    assert result.status == ParseStatus.SUCCESS
    assert result.parsing_strategy == ParsingStrategy.ATTEMPT_1_BATS_MATCH
    assert result.payload.failing_test_id == "212"
    assert result.payload.failing_test_name == "podman healthcheck"
    assert result.payload.duration_ms == 15645
    assert result.payload.makefile_target == "Makefile:735: localsystem"


def test_go_test_match():
    result = parse_log(MOCK_GO_TEST_LOG)
    assert result.status == ParseStatus.SUCCESS
    assert result.parsing_strategy == ParsingStrategy.ATTEMPT_2_GO_TEST_MATCH
    assert result.payload.failing_package == "github.com/containers/podman/v5/test"
    assert "pod_test.go:42" in result.payload.failing_line


def test_unclassified_fallback():
    result = parse_log(MOCK_UNCLASSIFIED_LOG)
    assert result.status == ParseStatus.FALLBACK
    assert result.parsing_strategy == ParsingStrategy.ATTEMPT_5_UNCLASSIFIED_TAIL
    assert "Error: The operation was canceled." in result.payload.raw_context_window


def test_metrics_noise_reduction():
    result = parse_log(MOCK_BATS_LOG)
    assert result.metrics.raw_lines_received == 8
    assert result.metrics.sanitized_lines_remaining == 5
    assert "37.5%" in result.metrics.noise_reduction_ratio


REAL_LOGS_DIR = pathlib.Path(__file__).parent / "test_logs"


@pytest.fixture(
    params=list(REAL_LOGS_DIR.glob("*.txt")) if REAL_LOGS_DIR.exists() else []
)
def real_log_file(request):
    return request.param.read_text(encoding="utf-8")


def test_real_log_parsing(real_log_file):
    result = parse_log(real_log_file)
    assert result.status in (ParseStatus.SUCCESS, ParseStatus.FALLBACK)
    assert result.payload is not None
    assert result.metrics.raw_lines_received > 0
    print(f"Strategy: {result.parsing_strategy}")
    print(f"Noise reduction: {result.metrics.noise_reduction_ratio}")
    print(result)
