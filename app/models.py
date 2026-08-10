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
    payload: Union[BatsPayload, GoTestPayload, GoCompilerPayload, InfraFatalPayload, TailContextPayload]
    metrics: PipelineMetrics
