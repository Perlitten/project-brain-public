"""Event Logging and Telemetry Accounting for Benchmark Runs.

Captures normalized event stream, tool usage, file reads/modifications,
Brain endpoint telemetry, and token consumption tracking.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional
from benchmarks.utility_pilot.schemas.run_model import AgentEvent, BrainCallRecord, EventType


class TelemetryTracker:
    """Tracks normalized events and Project Brain usage for a single run."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events: List[AgentEvent] = []
        self.brain_calls: List[BrainCallRecord] = []
        self.files_read: List[str] = []
        self.files_changed: List[str] = []
        self.commands_run: List[str] = []
        self.tool_calls_count: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.start_time: float = time.time()
        self.end_time: Optional[float] = None

    def log_event(self, event_type: EventType, details: Optional[Dict[str, Any]] = None) -> AgentEvent:
        evt = AgentEvent(
            event_id=f"evt-{uuid.uuid4().hex[:10]}",
            run_id=self.run_id,
            event_type=event_type.value,
            timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            details=details or {},
        )
        self.events.append(evt)
        return evt

    def record_tool_call(self, tool_name: str, args: Dict[str, Any]):
        self.tool_calls_count += 1
        self.log_event(EventType.TOOL_CALLED, {"tool_name": tool_name, "args": args})

        if tool_name in ("view_file", "read_file", "view_file_content"):
            path = str(args.get("AbsolutePath") or args.get("TargetFile") or args.get("file_path") or "")
            if path and path not in self.files_read:
                self.files_read.append(path)
                self.log_event(EventType.FILE_READ, {"file_path": path})

        elif tool_name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            path = str(args.get("TargetFile") or args.get("path") or "")
            if path and path not in self.files_changed:
                self.files_changed.append(path)
                self.log_event(EventType.FILE_MODIFIED, {"file_path": path})

        elif tool_name == "run_command":
            cmd = str(args.get("CommandLine") or "")
            if cmd:
                self.commands_run.append(cmd)
                if "pytest" in cmd:
                    self.log_event(EventType.TEST_EXECUTED, {"command": cmd})

    def record_brain_call(
        self,
        endpoint_or_tool: str,
        query: str,
        result_ids: List[str],
        bytes_returned: int,
        tokens_returned: int,
        latency_ms: float,
        used_by_agent: bool = True,
        context_type: str = "",
    ) -> BrainCallRecord:
        record = BrainCallRecord(
            call_id=f"bc-{uuid.uuid4().hex[:10]}",
            endpoint_or_tool=endpoint_or_tool,
            query=query,
            result_ids=result_ids,
            bytes_returned=bytes_returned,
            tokens_returned=tokens_returned,
            latency_ms=latency_ms,
            used_by_agent=used_by_agent,
            context_type=context_type,
        )
        self.brain_calls.append(record)
        self.log_event(
            EventType.BRAIN_QUERY_EXECUTED,
            {
                "call_id": record.call_id,
                "endpoint": endpoint_or_tool,
                "tokens": tokens_returned,
                "latency_ms": latency_ms,
            },
        )
        return record

    def add_tokens(self, input_tokens: int, output_tokens: int):
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def finalize(self) -> float:
        self.end_time = time.time()
        duration = round(self.end_time - self.start_time, 2)
        self.log_event(EventType.RUN_COMPLETED, {"duration_s": duration})
        return duration
