"""Shared failure codes between the Python agent and the SKILL side.

Stage 1 rev 11 (2026-04-20): introduced for Bug 1/2/3 fix pass. One
string vocabulary lets ``src/agent.py`` classify both
``run_ocean_dump_all`` errors (raised as ``RuntimeError`` by
``src/safe_bridge.py``) and ``safeOceanProbePtp`` results, and renders
a single unambiguous label into the next-iteration LLM prompt.
"""

from __future__ import annotations


class DumpStatus:
    """Outcome of one iteration's dumpAll attempt.

    Values are intentionally plain strings (not an ``Enum``) so they
    can flow through ``dataclasses.asdict``, JSONL transcripts, and
    SKILL JSON payloads without bespoke serialization.
    """

    OK = "ok"
    TIMEOUT = "dump_timeout"               # SKILL or socket hit 30s deadline
    NON_OSCILLATING = "non_oscillating"    # probe_ptp detected flat waveform
    NO_SAVED_OUTPUTS = "no_saved_outputs"  # OCN-6034 / VT() returns nil
    SELECT_RESULT_FAILED = "select_result_failed"
    UNKNOWN = "unknown"

    @classmethod
    def classify_runtime_error(cls, msg: str) -> str:
        """Map a ``RuntimeError`` message to one of the status strings."""
        if not isinstance(msg, str):
            return cls.UNKNOWN
        low = msg.lower()
        if "dump_timeout" in low or "socket timeout" in low or "timed out" in low:
            return cls.TIMEOUT
        if "unavailable" in low or "no saved outputs" in low:
            return cls.NO_SAVED_OUTPUTS
        if "selectresult" in low.replace(" ", ""):
            return cls.SELECT_RESULT_FAILED
        return cls.UNKNOWN
