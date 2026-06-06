"""CircuitAgent: OCEAN-driven closed-loop optimizer.

Response format, iteration flow, and stop conditions are defined in
``docs/llm_protocol.md``; per-spec design variables and metrics are
loaded at import time from the target spec Markdown (default:
``projects/<name>/constraints/spec.md``, overridable via ``VIRTUOSO_SPEC_PATH`` /
legacy ``LC_VCO_SPEC_PATH``).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import curve_searcher, spec_evaluator
from .failure_codes import DumpStatus
from .llm_client import LLMClient
from .maestro_metric_sync import sync_spec_metrics_to_maestro
from .maestro_metric_sync import _build_metric_expr as _maestro_metric_expr
from .maestro_metric_sync import _waveform_expr as _maestro_waveform_expr
from .maestro_setup import (
    MAESTRO_SETUP_KEYS,
    apply_maestro_setup,
    validate_maestro_setup_block as _validate_maestro_setup_block,
)
from .ocean_worker import (
    OceanWorker,
    OceanWorkerError,
    OceanWorkerScriptError,
    OceanWorkerTimeout,
)
from .plan_auto import PlanAuto
from .safe_bridge import (
    SafeBridge,
    assess_op_point_save_effectiveness,
    assert_llm_feedback_safe,
    scrub,
)
from .remote_patch import RemotePatchError, RemotePatcher
from .sp_rewrite import ParamRewriteError

logger = logging.getLogger(__name__)


SAFEGUARD_AMP_HOLD_MIN = 0.3
SAFEGUARD_CONSECUTIVE_LIMIT = 3

# T8.8: when the LAST N consecutive iterations of a non-converged run
# all produced at least one sanity-range UNMEASURABLE verdict, the loop
# was burning iterations on physically-implausible measurements rather
# than tunable parameters — relabel the abort_reason from "max_iter"
# to "topology" so the post-mortem points at the topology/spec, not
# at iteration budget. The streak must hold at termination (a single
# non-violating iter resets it), so a transient measurement glitch
# mid-run does not trip the relabel.
TOPOLOGY_SANITY_VIOLATION_LIMIT = 3
_SANITY_VIOLATION_PREFIX = "UNMEASURABLE (suspect:"

# Path-2 (2026-05-19): after single-point convergence, the loop spends
# up to TUNING_RETRY_BUDGET further iterations trying to satisfy the
# spec's `tuning_metrics` (sweep-driven Kvco / monotonicity / range).
# Sized so a typical 20-iter cap still leaves headroom for the
# single-point pass that precedes the tuning gate.
TUNING_RETRY_BUDGET = 7


@dataclass(frozen=True)
class _NumericHardPassBound:
    metric: str
    op: str
    value: float
    unit: str = ""

    @property
    def target_text(self) -> str:
        suffix = f" {self.unit}" if self.unit else ""
        return f"{self.op} {self.value:g}{suffix}"


def _has_sanity_violation(pass_fail: dict | None) -> bool:
    """True iff any verdict in ``pass_fail`` is a sanity-range UNMEASURABLE.

    ``spec_evaluator._verdict`` emits ``"UNMEASURABLE (suspect: value <X> <
    sanity lo <Y>)"`` / ``"... > sanity hi ..."`` when a metric value is
    outside the sanity range — physically implausible, blame the topology
    or the spec, not the parameters. Distinct from ``"UNMEASURABLE (no
    value)"`` / ``"UNMEASURABLE (<DumpStatus>)"`` / ``"UNMEASURABLE
    (<reducer reason>)"`` which point at instrumentation/measurement
    issues. Used by the topology streak counter in both backends.
    """
    if not pass_fail:
        return False
    for v in pass_fail.values():
        if isinstance(v, str) and v.startswith(_SANITY_VIOLATION_PREFIX):
            return True
    return False

# LLM-response JSON schema (see docs/llm_protocol.md).
#
# Track C v2 (2026-05-15): four optional structural blocks may appear
# alongside the legacy four required fields. Their internal shape is
# validated by ``maestro_setup.validate_maestro_setup_block``; the
# top-level type gate here only verifies they're lists (the per-entry
# validation is delegated so this module doesn't drift out of sync with
# the SafeBridge writer signatures).
_VALID_RESPONSE_KEYS = frozenset({
    "iteration", "measurements", "pass_fail", "reasoning", "design_vars",
    # Track C v2 (optional, structural-only on iter 0 / rescope path):
    "tests", "analyses", "outputs", "corners",
})
# Required top-level keys — absence is a schema violation.
# `iteration` is advisory (platform controls the counter), so it's
# intentionally optional. The Track C v2 structural blocks are
# intentionally NOT required so an LLM that emits only design_vars
# (the legacy contract) keeps passing.
_REQUIRED_RESPONSE_KEYS = frozenset({
    "measurements", "pass_fail", "reasoning", "design_vars",
})
# Expected top-level types — used by the schema validator.
_RESPONSE_KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "iteration":    (int, float),
    "measurements": dict,
    "pass_fail":    dict,
    "reasoning":    str,
    "design_vars":  dict,
    # Track C v2: top-level type gate only — the per-entry shape is
    # validated in ``maestro_setup.validate_maestro_setup_block``.
    "tests":     list,
    "analyses":  list,
    "outputs":   list,
    "corners":   list,
}

# Track C v2: cap how many times we'll re-prompt the LLM after a
# contract violation. The legacy flow only repaired once; the v2 brief
# raises that to 3 (per leader) so a structural-block typo doesn't
# burn an iter on the first try. After 3 failed attempts, abort the
# iter with ``contract_violation``.
_CONTRACT_REPAIR_MAX = 3

_NUMERIC_BOUND_RE = re.compile(
    r"(?:(?P<metric>[A-Za-z_][A-Za-z0-9_]*)\s*)?"
    r"(?P<op>>=|<=|>|<)\s*"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    r"\s*(?P<unit>[A-Za-zµμ]+)?"
)
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")


_DESIGN_VAR_SECTION_RE = re.compile(
    r"^##\s+(?:§\s*)?\d+\.?\s*Design\s+variables?\b",
    re.MULTILINE | re.IGNORECASE,
)


def _load_allowed_design_vars(spec_path: Path) -> tuple[str, ...]:
    """Parse the design-variable whitelist from the target spec.

    Reads the Markdown table in the ``Design variables`` section and
    extracts variable names from the first column (format:
    ``| \\`<var>\\` | ...``). Matches headings like ``## 3. Design
    variables ...`` or ``## §4 Design Variables`` (section number
    irrelevant; only the phrase "Design variables" is required).
    Raises RuntimeError on any failure — no silent fallback to
    hardcoded values.
    """
    if not spec_path.is_file():
        raise RuntimeError(
            f"Spec file not found: {spec_path} — cannot load "
            f"allowed design variable whitelist"
        )
    text = spec_path.read_text(encoding="utf-8")
    section_match = _DESIGN_VAR_SECTION_RE.search(text)
    if not section_match:
        raise RuntimeError(
            f"Design variables section not found in {spec_path.name} — "
            f"expected a heading like '## 3. Design variables ...'"
        )
    section_text = text[section_match.start():]
    next_section = re.search(r"\n##\s", section_text[1:])
    if next_section:
        section_text = section_text[: next_section.start() + 1]

    var_names: list[str] = []
    for match in re.finditer(
        r"^\|\s*`([^`]+)`\s*\|", section_text, re.MULTILINE
    ):
        name = match.group(1).strip()
        if name and name != "Var":
            var_names.append(name)

    if not var_names:
        for match in re.finditer(
            r"```(?:yaml|yml)\s*\n(.*?)\n```",
            section_text,
            re.DOTALL | re.IGNORECASE,
        ):
            block = match.group(1)
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if not isinstance(data, dict):
                continue
            design_vars = data.get("design_vars")
            if isinstance(design_vars, dict):
                var_names.extend(str(name) for name in design_vars.keys())
                break

    if not var_names:
        raise RuntimeError(
            f"Design variables table in {spec_path.name} contains no "
            f"design variables — expected rows like '| `<name>` | ...'"
        )
    return tuple(var_names)


_DEFAULT_SPEC_PATH = Path(__file__).resolve().parent.parent / "config" / "default_spec.md"
# VIRTUOSO_SPEC_PATH is the generic override; LC_VCO_SPEC_PATH is a
# legacy alias kept so existing call sites / tests keep working.
_SPEC_PATH = Path(
    os.environ.get("VIRTUOSO_SPEC_PATH")
    or os.environ.get("LC_VCO_SPEC_PATH")
    or _DEFAULT_SPEC_PATH
)
_VALID_DESIGN_VAR_NAMES = frozenset(_load_allowed_design_vars(_SPEC_PATH))
# Physical-unit suffixes that gemma4 hallucinates (mA, pF, nH, V, GHz...).
# Engineering suffixes (u, n, p, f, k, M, G, T, m) are fine.
_FORBIDDEN_UNIT_RE = re.compile(
    r"(?:mA|uA|nA|pA|pF|nF|uF|fF|nH|uH|mH|[kM]?Hz|GHz|ohm|Ohm|[Ω]|dB|"
    r"[AVWF]$)",
)

_BARE_NUMBER_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)
_BARE_INTEGER_RE = re.compile(r"^[+-]?\d+$")


def _design_var_kind(name: str) -> str:
    lower = str(name).strip().lower()
    if "ibias" in lower or lower in {"i", "idc"}:
        return "current"
    if (
        lower.startswith("nfin")
        or lower in {"nf", "fingers", "cmfb_bias", "cmfb_current"}
        or lower.startswith("cmfb_n")
        or lower.startswith("cmfb_p")
    ):
        return "integer sizing"
    if lower in {"r", "rcm"} or lower.endswith("_r") or "res" in lower:
        return "resistance"
    if lower in {"c"} or lower.endswith("_c") or "cap" in lower:
        return "capacitance"
    if lower.startswith("v") or lower in {"vicm", "vctrl", "vcm"}:
        return "voltage"
    if lower.endswith("phase"):
        return "phase"
    if "magnitude" in lower:
        return "magnitude"
    return "scalar"


def _design_var_value_contract(valid_names: Iterable[str]) -> str:
    groups: dict[str, list[str]] = {}
    for name in sorted(valid_names):
        groups.setdefault(_design_var_kind(name), []).append(name)

    def _fmt(kind: str, rule: str) -> str:
        names = groups.get(kind, [])
        if not names:
            return ""
        return f"- {kind}: {', '.join(names)} -> {rule}\n"

    return (
        "- **Value format by `design_vars` type**:\n"
        + _fmt(
            "current",
            "use engineering-suffix current values such as `10u`; "
            "do not use bare `1`/`2`.",
        )
        + _fmt(
            "integer sizing",
            "use bare integers only, e.g. `1`, `4`, `20`.",
        )
        + _fmt(
            "resistance",
            "use engineering suffixes or bare numeric values, e.g. `5k`, `10000`.",
        )
        + _fmt("capacitance", "use engineering suffixes, e.g. `50f`.")
        + _fmt("voltage", "use bare volts without the `V` suffix, e.g. `0.4`.")
        + _fmt("phase", "use bare degrees without the `deg` suffix, e.g. `0`, `180`.")
        + _fmt("magnitude", "use bare scalar values, e.g. `1`.")
        + _fmt("scalar", "use bare numbers or engineering suffixes.")
        + "- Do NOT use physical units like mA, pF, nH, V, GHz.\n"
    )


def _design_var_value_problem(name: str, value: Any) -> str | None:
    value_text = str(value).strip()
    if isinstance(value, str) and _FORBIDDEN_UNIT_RE.search(value):
        return (
            f"design_vars['{name}'] = '{value}' contains a physical unit; "
            "use engineering suffixes (u/n/p/f/k/M/G) or bare numbers by type"
        )

    kind = _design_var_kind(name)
    if kind == "current":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (
                f"design_vars['{name}'] = '{value}' is a bare current; "
                "use an engineering suffix such as `10u`"
            )
        if isinstance(value, str) and _BARE_NUMBER_RE.fullmatch(value_text):
            return (
                f"design_vars['{name}'] = '{value}' is a bare current; "
                "use an engineering suffix such as `10u`"
            )
    if kind == "integer sizing":
        if isinstance(value, bool):
            return f"design_vars['{name}'] must be a bare integer"
        if isinstance(value, int):
            return None
        if isinstance(value, float):
            if value.is_integer():
                return None
            return f"design_vars['{name}'] must be a bare integer"
        if isinstance(value, str) and not _BARE_INTEGER_RE.fullmatch(value_text):
            return f"design_vars['{name}'] must be a bare integer"
    return None


@dataclass
class IterationDiagnostic:
    """Bug 0/2/4 — per-iter machine-readable failure surface.

    Populated by the agent as it walks the dump/probe/op-point/ic-patch
    chain. Rendered into the next iteration's LLM prompt by
    ``_format_eval_summary`` so the LLM can see *why* this iter
    produced no metrics and does not silently repeat the same
    design_vars (run_20260420_033152 iter 9→10 replay bug).
    """

    dump_status: str = DumpStatus.OK
    dump_raw_error: str = ""
    op_point_available: bool = True
    ic_patch_applied: bool = True
    ic_patch_reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def has_failure(self) -> bool:
        return (
            self.dump_status != DumpStatus.OK
            or not self.op_point_available
            or not self.ic_patch_applied
        )


@dataclass
class IterationRecord:
    """Record of a single optimization iteration."""

    iteration: int
    design_vars: dict
    measurements: dict
    pass_fail: dict
    meets_spec: bool
    llm_reasoning: str = ""
    timestamp: float = field(default_factory=time.time)
    diagnostic: IterationDiagnostic = field(default_factory=IterationDiagnostic)
    # Path-2 (2026-05-19): populated only on iterations that ran a
    # sweep phase (single-point PASS + spec declared `sweep:` + CLI
    # `--sweep-results-root` supplied). Empty dict otherwise — easier
    # to read in the transcript than `None`.
    tuning_measurements: dict = field(default_factory=dict)
    tuning_pass_fail: dict = field(default_factory=dict)


class CircuitAgent:
    """LLM-driven OCEAN optimization agent."""

    def __init__(
        self,
        bridge: SafeBridge,
        llm: LLMClient,
        spec: dict | str,
        analysis_type: str = "tran",
        ocean_worker: OceanWorker | None = None,
        valid_design_vars: Iterable[str] | None = None,
        fixed_design_vars: Iterable[str] | None = None,
        allow_llm_maestro_setup: bool = True,
    ):
        """Construct the agent.

        ``spec`` accepts either:
          - a Markdown **string** (preferred): the raw target-spec text
            (e.g. ``projects/<name>/constraints/spec.md``). Embedded directly into the
            first LLM prompt so the topology narrative, design-variable
            table, and eval-block YAML flow through with their original
            Markdown structure preserved.
          - a ``dict`` (legacy JSON path): rendered via ``json.dumps``.
            Kept so existing callers passing ``{"f_osc": "19.5"}`` don't
            break.
        """
        self.bridge = bridge
        self.llm = llm
        self.spec = spec
        self.analysis_type = analysis_type
        self.fixed_design_var_names = frozenset(fixed_design_vars or ())
        self.valid_design_var_names = (
            frozenset(valid_design_vars or _VALID_DESIGN_VAR_NAMES)
            - self.fixed_design_var_names
        )
        self.allow_llm_maestro_setup = bool(allow_llm_maestro_setup)
        # Stage 1 rev 12 (2026-04-20): PSF dumping now runs in a
        # throwaway virtuoso subprocess so a pathological PSF cannot
        # wedge the main RAMIC SKILL daemon. Required — no fallback
        # to bridge.run_ocean_dump_all (that path hung 30 s on every
        # non-oscillating iteration, see run_20260420_033152).
        if ocean_worker is None:
            raise ValueError(
                "ocean_worker is required (stage 1 rev 12 architecture)"
            )
        self.ocean_worker = ocean_worker
        self.history: list[IterationRecord] = []
        # Stage 1 rev 4 (2026-04-18): if the spec is a Markdown string
        # that carries a `signals/windows/metrics` YAML fence, switch
        # the loop onto the generic SafeBridge dump + PC evaluator path
        # (see src/spec_evaluator.py). Otherwise, fall back to the
        # legacy LLM-judged flow (sim_result["measurements"] from
        # safeOceanMeasure, LLM's own pass_fail).
        self.eval_block: dict | None = None
        # R2 (2026-05-19, claude P3): per-run cache for the tuning
        # manifest. Keyed by sweep_results_root so test pivots that
        # change the root invalidate themselves; within one root the
        # file is static (created out-of-band before the agent runs),
        # so re-reading it on every tuning retry is wasted SSH/SKILL
        # round-trips. None on cache miss, dict on hit.
        self._sweep_manifest_cache: dict[str, dict[int, float]] = {}
        # Path-3 prep (2026-05-24): per-call cache populated by
        # `_run_sweep_phase` so the optional curve-level searcher (off
        # by default; gated by `curve_searcher_enabled` on `run()`)
        # can summarize the f-Vctrl curve and rank candidates without
        # re-reading the manifest or rerunning the sweep. None when no
        # sweep has yet completed this run.
        self._last_sweep_curve_state: dict | None = None
        self._legacy_hard_pass_bounds: dict[str, _NumericHardPassBound] = (
            _extract_legacy_hard_pass_bounds(spec)
        )
        # Path-2.5 R2 P3 NIT 3 (2026-05-19): tracks whether the
        # spec-derived Maestro Outputs Setup write has successfully
        # landed on the remote testbench. The derived payload is
        # idempotent — re-issuing it after a SafeBridge / SKILL hiccup
        # is safe — so the dispatch slot re-attempts on each
        # subsequent iter until the flag flips. False on a fresh
        # agent; only the Spectre / Maestro path flips it (HSpice
        # ignores the flag entirely).
        self._maestro_setup_applied: bool = False
        if isinstance(spec, str):
            try:
                self.eval_block = spec_evaluator.extract_eval_block(spec)
            except ValueError as exc:
                # A malformed eval block is a spec-author error worth
                # surfacing loudly, but not worth aborting __init__ —
                # the legacy path still works if the author wants it.
                logger.warning(
                    "Spec eval block rejected (%s); falling back to "
                    "legacy LLM-judged flow.", exc,
                )
        if self.eval_block is not None:
            logger.info(
                "Spec eval block loaded: %d signals, %d windows, %d metrics",
                len(self.eval_block["signals"]),
                len(self.eval_block["windows"]),
                len(self.eval_block["metrics"]),
            )
        elif self._legacy_hard_pass_bounds:
            logger.info(
                "Legacy numeric hard-pass bounds loaded: %s",
                {
                    name: bound.target_text
                    for name, bound in self._legacy_hard_pass_bounds.items()
                },
            )

    # ------------------------------------------------------------------ #
    #  Public entrypoint
    # ------------------------------------------------------------------ #

    def run(
        self,
        lib: str,
        cell: str,
        tb_cell: str,
        max_iter: int = 20,
        scs_path: str | None = None,
        transcript_path: str | Path | None = None,
        plan_auto: PlanAuto | None = None,
        sweep_results_root: str | None = None,
        maestro_test: str | None = None,
        curve_searcher_enabled: bool = False,
        curve_searcher_max_candidates: int = curve_searcher.DEFAULT_MAX_CANDIDATES,
        writeback_enabled: bool = True,
    ) -> dict:
        """Run the closed-loop optimization against a Maestro testbench cell.

        Iteration flow (see ``docs/llm_protocol.md`` for the full contract):
            1. Merge the LLM's ``design_vars`` into the accumulated dict.
            2. Call ``bridge.run_ocean_sim(lib, cell, tb_cell, design_vars,
               analyses)`` — OCEAN executes the Maestro-configured analyses.
            3. Call ``safeOceanDumpAll`` for per-signal / per-window stats.
            4. Compute ``measurements`` + ``pass_fail`` on the PC side from
               the spec's YAML eval block (authoritative).
            5. Request a best-effort waveform display.
            6. Feed computed metrics, raw dump stats, DC op-point table, and
               history into the next-turn prompt.

        Stop conditions (``abort_reason``):
            - ``None`` (converged=True) — every metric PASS in one iteration.
            - ``"max_iter"``             — ``max_iter`` hit without PASS.
            - ``"topology"``             — ``max_iter`` hit AND the last
                                            ``TOPOLOGY_SANITY_VIOLATION_LIMIT``
                                            consecutive iterations all
                                            produced a sanity-range
                                            UNMEASURABLE verdict; the
                                            iteration budget was spent on
                                            physically-implausible
                                            measurements, so the topology
                                            or the spec is the culprit,
                                            not parameter tuning.
            - ``"safeguard"``            — ``amp_hold_ratio < 0.3`` for 3
                                            consecutive iterations.
            - ``"stuck_identical_vars"`` — identical design_vars 2× while
                                            metrics still fail.
            - ``"contract_violation"``   — schema/whitelist violation after
                                            one repair attempt.
            - ``"no_changes"``           — no baseline AND no LLM proposal
                                            on iter 1.

        When ``scs_path`` is provided, the agent first calls
        ``bridge.list_design_vars(scs_path)`` to auto-discover the
        testbench design variables + Maestro defaults, which seed
        ``accumulated_vars`` beneath the LLM's per-iter proposal. This
        keeps the agent fully generic — swapping testbench/cell is a
        spec-only change, no code edits required.

        Returns a dict with:
            measurements      — last PC-computed measurements
            pass_fail         — last PC-computed pass/fail verdicts
            design_vars       — final accumulated design variable values
            converged         — True iff every metric passed in some iteration
            abort_reason      — None | one of the strings above
            writeback_status  — "ok" | "skipped" | "failed: <short reason>"
        """
        # Stage 1 rev 9 (2026-04-19): persist every LLM turn as a JSONL
        # transcript so post-mortem diagnosis no longer depends on
        # reconstructing Kimi's reasoning from Applying design_vars log
        # lines alone. Incremental append (one JSON line per message,
        # flushed immediately) so a mid-run crash still leaves a useful
        # partial transcript. When transcript_path is None the write is
        # a no-op — existing callers see no behavior change.
        transcript_file: Path | None = None
        if transcript_path is not None:
            transcript_file = Path(transcript_path)
            transcript_file.parent.mkdir(parents=True, exist_ok=True)
            # Truncate any existing file so a re-run with the same path
            # starts clean — each run owns its transcript exclusively.
            transcript_file.write_text("", encoding="utf-8")
            logger.info("LLM transcript: %s", transcript_file)

        # Stage 1 rev 10 (2026-04-19): Plan Auto status line at startup.
        # When inactive (--auto-bias-ic off or no startup: block) this
        # is a single info log and the per-iter patch call becomes a
        # no-op via PlanAuto.active==False.
        if plan_auto is not None:
            logger.info(plan_auto.describe())

        def _append_transcript(
            iteration: int,
            role: str,
            content: str,
            usage: dict[str, Any] | None = None,
        ) -> None:
            if transcript_file is None:
                return
            try:
                entry: dict[str, Any] = {
                    "iteration": iteration,
                    "role": role,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "content": content,
                }
                # Type-check rather than truthiness — a MagicMock from a
                # unit test that mocks self.llm would otherwise leak in
                # and break json.dumps.
                if isinstance(usage, dict):
                    entry["usage"] = usage
                with transcript_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as exc:
                # Transcript is best-effort — a disk hiccup must not
                # stop the optimization loop.
                logger.warning(
                    "Transcript append failed (%s); continuing.",
                    type(exc).__name__,
                )

        # Render the spec for the prompt. If the caller passed MD text
        # (the user's preferred format — spec.md is the authoring target),
        # embed verbatim so §3 tables / §4 ranges / §1 topology narrative
        # reach the LLM with their original MD structure. If the caller
        # passed a dict (legacy JSON path), dump it inside a json fence.
        if isinstance(self.spec, str):
            spec_block = self.spec
        else:
            spec_block = "```json\n" + json.dumps(self.spec, indent=2) + "\n```"
        if self.eval_block is not None:
            contract_note = (
                "\n\n## Contract (Stage 1 rev 5)\n"
                "The platform runs the OCEAN transient, then a generic "
                "`safeOceanDumpAll` to collect per-node/per-window "
                "statistics, then computes `measurements` + `pass_fail` "
                "on the PC side from the spec's `signals/windows/metrics` "
                "yaml block. The `measurements` and `pass_fail` you emit "
                "are **advisory** — the platform recomputes both every "
                "turn from the authoritative dump. Your real job is the "
                "`reasoning` + `design_vars` fields: diagnose why metrics "
                "pass or fail and propose the next-iteration design "
                "variables.\n\n"
                "**Verdict three-state semantics** (rev 5): every metric "
                "in the platform's `pass_fail` dict starts with ONE of:\n"
                "- `PASS` — value is inside the spec's pass range; do "
                "nothing for this metric.\n"
                "- `FAIL (...)` — value is outside pass range but inside "
                "the physical sanity envelope; the circuit genuinely "
                "misses the target — propose `design_vars` that move "
                "this metric toward the pass range.\n"
                "- `UNMEASURABLE (...)` — the value could not be computed "
                "(dump missing, SKILL helper error, t_cross found no "
                "crossing) OR the value is outside the physical sanity "
                "envelope and therefore suspect. Do NOT tune "
                "`design_vars` to fix an UNMEASURABLE metric — it means "
                "the measurement chain or spec math is broken, not the "
                "circuit. Report it in `reasoning` so the human can "
                "debug; keep other `design_vars` changes focused on "
                "real FAIL metrics.\n\n"
                "**Anti-hallucination rules:**\n"
                "1. Do NOT fabricate or guess measurement values. "
                "The `measurements` you emit are ignored by the platform "
                "— it recomputes them from the SKILL dump. Put 0 or null "
                "for any value you cannot derive from the data shown.\n"
                "2. When ANY metric is FAIL, you MUST propose at least "
                "one change in `design_vars`. An empty or unchanged "
                "`design_vars` after a FAIL wastes an iteration.\n"
                "3. Do NOT copy `design_vars` verbatim from a previous "
                "iteration when metrics are still failing — the platform "
                "detects identical diffs and force-perturbs a "
                "current-source design variable (whichever whitelisted "
                "key begins with `Ibias`) ×2 as a last-resort "
                "exploration kick."
            )
        else:
            contract_note = ""
        fixed_vars_note = ""
        if self.fixed_design_var_names:
            fixed_vars_note = (
                "- **Fixed `design_vars` seeded from Maestro/spec**: "
                f"{', '.join(sorted(self.fixed_design_var_names))}. "
                "Do NOT emit or change these variables.\n"
            )
        # Path-2.5 (2026-05-19): only tell the LLM to drop the four
        # structural Maestro blocks when the spec-derived path is
        # active (eval_block present). Dict-spec / legacy JSON projects
        # still rely on the LLM emit path — issuing this instruction
        # unconditionally would regress those flows.
        if self.eval_block is not None or not self.allow_llm_maestro_setup:
            setup_reason = (
                "spec-derived path is authoritative"
                if self.eval_block is not None
                else "Maestro setup is already configured"
            )
            derived_setup_note = (
                f"- **Maestro setup policy**: {setup_reason}. "
                f"{'Maestro setup is derived from the spec automatically. ' if self.eval_block is not None else ''}"
                "Do NOT emit `tests`, `analyses`, `outputs`, or `corners` "
                "blocks — the agent translates spec §2 "
                "(`signals`/`windows`/`metrics`) into Maestro Outputs "
                "Setup rows + the analyses-enable list deterministically. "
                "Emit ONLY `measurements`, `pass_fail`, `reasoning`, "
                "`design_vars`.\n"
            )
        else:
            derived_setup_note = ""

        # Read sanitized schematic before the first LLM prompt so the initial
        # proposal sees real device connectivity, not only the spec prose.
        schematic_instances: list[dict] = []
        try:
            _circuit = self.bridge.read_circuit(lib, cell)
            _insts = _circuit.get("instances") or []
            if isinstance(_insts, list):
                schematic_instances = _insts
            logger.info(
                "Schematic cached: %d instances (top-level keys=%s)",
                len(schematic_instances),
                sorted(_circuit.keys()) if isinstance(_circuit, dict) else [],
            )
        except Exception as exc:  # noqa: BLE001 - topology is optional
            # A schematic read failure degrades the LLM prompt but must not
            # stop the optimization loop; spec topology narrative remains a
            # fallback.
            logger.warning(
                "Schematic read failed (%s: %s); prompt will omit the "
                "live topology section this run.",
                type(exc).__name__, exc,
            )

        first_topology_section = _guard_llm_feedback(
            _format_topology_with_live_vars(schematic_instances, {}),
            context="first-turn topology",
        )
        first_topology_block = (
            "## Topology (instances x design-var references)\n"
            f"{first_topology_section}\n\n"
            if first_topology_section else ""
        )
        messages = [{
            "role": "user",
            "content": (
                "You are an analog-design optimization agent. The target "
                "specification is pinned below. At each iteration you will "
                "receive raw simulation output; respond with ONE fenced "
                "JSON block in the format described by the spec "
                "(`measurements`, `pass_fail`, `reasoning`, `design_vars`)."
                f"{contract_note}\n\n"
                "## HARD CONSTRAINTS (violating these causes a retry)\n"
                "- **Valid top-level JSON keys**: "
                f"{', '.join(sorted(_VALID_RESPONSE_KEYS))}\n"
                "- **Valid `design_vars` variable names**: "
                f"{', '.join(sorted(self.valid_design_var_names))}\n"
                f"{_design_var_value_contract(self.valid_design_var_names)}"
                "- Do NOT invent variable names — use only the names in "
                "the whitelist above.\n"
                f"{fixed_vars_note}"
                f"{derived_setup_note}\n"
                "## Region enum (per-device op-point table)\n"
                "Each next-turn prompt carries a ``tranOp`` DC operating-"
                "point table. The ``region`` integer maps as:\n"
                "- 0 cutoff — off; `id ≈ 0`; `gm ≈ 0`.\n"
                "- 1 triode: linear region; usually high `gds` and weak "
                "`gm/gds` intrinsic gain.\n"
                "- 2 saturation: normal amplifier/current-source region; "
                "cross-check with finite `id`, useful `gm`, reasonable "
                "`gm/gds`, and `vds` versus derived `vov = vgs - vth`. "
                "Current sources and amplifying transistors belong here.\n"
                "- 3 subthreshold — `vgs < vth`, weak inversion.\n"
                "- 4 breakdown — `vds > Vdsbreak`; never expected.\n"
                "Capacitor-like devices (e.g. MOS varactors with "
                "gate-bulk tied) may legitimately sit in cutoff/triode "
                "with `id ≈ 0`; consult the spec's topology section "
                "for per-device intent.\n\n"
                f"{first_topology_block}"
                f"## Target Specifications\n{spec_block}\n\n"
                "If the spec declares a model-comparison starting "
                "`design_vars` table, treat it as the Maestro/pre-run "
                "baseline for fairness. When you emit optimization "
                "`design_vars`, still obey the typed value contract above; "
                "bare `1` entries for current-source variables in a stress "
                "table are not legal current proposals. Emit the first JSON "
                "block now — pick a reasonable starting point inside the "
                "ranges in the spec's design-variable table."
            ),
        }]
        _append_transcript(0, "user", messages[0]["content"])
        try:
            response = self.llm.chat(messages)
        except Exception as exc:  # noqa: BLE001 - provider outages are runtime data
            safe_exc = scrub(f"{type(exc).__name__}: {exc}")
            logger.error("LLM chat failed before first iteration: %s", safe_exc)
            return {
                "measurements": {},
                "pass_fail": {},
                "design_vars": {},
                "converged": False,
                "abort_reason": "llm_error",
                "writeback_status": "skipped: llm_error",
                "tuning_measurements": {},
                "tuning_pass_fail": {},
            }
        messages.append({"role": "assistant", "content": response})
        _append_transcript(
            0, "assistant", response,
            usage=getattr(self.llm, "last_usage", None),
        )

        # Stage 1 rev 6 (2026-04-18): auto-discover Maestro design
        # variables via the bridge if scs_path was given. Baseline
        # defaults seed accumulated_vars so the LLM may propose a
        # partial delta (e.g. just {"<desvar>": 12}) without dropping
        # the rest of the testbench state. Purely dict-merge semantics —
        # no if-branches on variable names, nothing circuit-specific.
        baseline_vars: dict[str, str] = {}
        if scs_path:
            try:
                discovered = self.bridge.list_design_vars(scs_path)
                # Rev 6 M1 (claude_reviewer 2026-04-18): filter discovered
                # names through the SafeBridge allow-list so the two
                # validators agree. The SKILL side accepts [A-Za-z_]
                # prefix but SafeBridge requires [a-zA-Z] prefix plus a
                # blocked-word check; an underscore-prefixed testbench
                # variable would otherwise pass discovery and then
                # crash ``run_ocean_sim`` on the first iteration.
                filtered_out: list[str] = []
                active_seed_names = (
                    self.valid_design_var_names | self.fixed_design_var_names
                )
                for entry in discovered:
                    name = entry["name"]
                    if (
                        self.bridge._is_allowed_param_name(name)
                        and name in active_seed_names
                    ):
                        baseline_vars[name] = entry["default"]
                    else:
                        filtered_out.append(name)
                if filtered_out:
                    logger.warning(
                        "Filtered %d discovered variable(s) not on the "
                        "active spec/fixed whitelist or SafeBridge param "
                        "allow-list: %s (won't be sent to OCEAN; their "
                        "Maestro defaults still apply via the testbench "
                        "netlist).",
                        len(filtered_out), sorted(filtered_out),
                    )
                logger.info(
                    "Auto-discovered %d design variables: %s",
                    len(baseline_vars), sorted(baseline_vars),
                )
            except Exception as exc:  # noqa: BLE001 — discovery is
                # optional; fall back to the legacy flow where the LLM
                # must propose every variable explicitly.
                logger.warning(
                    "Design variable auto-discovery failed (%s: %s); "
                    "proceeding without Maestro defaults.",
                    type(exc).__name__, exc,
                )

        # Stage 1 rev 6 B2 (2026-04-18): auto-discover analysis kwargs
        # (e.g. tran stop=200n) from the same input.scs. Root cause of
        # OCN-6038: bare ``analysis('tran)`` in a fresh OCEAN design()
        # session inherits no stop-time, so spectre completes with 0
        # errors but writes no tran psf and every downstream metric
        # drops to null. Forwarding Maestro's own kwargs restores the
        # intended tran window without hardcoding any numbers in Python.
        baseline_analyses: list[tuple[str, dict[str, str]]] = []
        if scs_path:
            try:
                discovered_analyses = self.bridge.list_analyses(scs_path)
                for item in discovered_analyses:
                    name = item["name"]
                    kwargs_list = item.get("kwargs") or []
                    kwargs_dict = {
                        k: v for (k, v) in kwargs_list
                        if isinstance(k, str) and isinstance(v, str)
                    }
                    baseline_analyses.append((name, kwargs_dict))
                logger.info(
                    "Auto-discovered %d analyses: %s",
                    len(baseline_analyses),
                    [(n, kw) for n, kw in baseline_analyses],
                )
            except Exception as exc:  # noqa: BLE001 — discovery optional
                logger.warning(
                    "Analysis auto-discovery failed (%s: %s); "
                    "falling back to bare [%s] (may hit OCN-6038 if "
                    "testbench needs a stop time).",
                    type(exc).__name__, exc, self.analysis_type,
                )

        # Stage 1 rev 8 (2026-04-19): one-shot sanitized schematic read at
        # startup. The returned topology (instance list + per-inst nets +
        # geometry + CDF desVar references) is invariant across a single
        # optimization run — only the Maestro desVar values change each
        # iter via ``bridge.run_ocean_sim``. Plan ★: cache here, and on
        # each iter merge with ``accumulated_vars`` via
        # ``_format_topology_with_live_vars`` before injecting into the
        # LLM's next_prompt. Sanitization audit (2026-04-19 rev 8 probe
        # run_20260419_133135.log) confirmed no foundry cell/lib names
        # and no BSIM coefficients reach this layer.
        # Schematic topology was already cached before the first prompt.
        try:
            _circuit = {"instances": schematic_instances}
            _insts = _circuit.get("instances") or []
            if isinstance(_insts, list):
                schematic_instances = _insts
            logger.info(
                "Schematic cached: %d instances (top-level keys=%s)",
                len(schematic_instances),
                sorted(_circuit.keys()) if isinstance(_circuit, dict) else [],
            )
        except Exception as exc:  # noqa: BLE001 — topology is optional
            # A schematic read failure degrades the LLM prompt but must
            # not stop the optimization loop; spec §1 topology narrative
            # remains available as a fallback.
            logger.warning(
                "Schematic read failed (%s: %s); prompt will omit the "
                "live topology section this run.",
                type(exc).__name__, exc,
            )

        # Track C Option I (2026-05-14): mirror spec.metrics into the
        # Maestro Outputs Setup so an interactive Maestro user sees the
        # same per-metric formulas the PC evaluator computes from the
        # PSF dump. Authoring convenience only — the PC-side eval block
        # remains the authoritative pass/fail source, so every failure
        # mode here is fail-soft (try/except wraps the whole call).
        # Skipped when no eval_block is present (legacy LLM-judged
        # flow) — there are no PC-computed metrics to mirror.
        maestro_setup_test: str | None = None
        if self.eval_block is not None:
            maestro_setup_test = self._resolve_maestro_setup_test(
                tb_cell=tb_cell,
                maestro_test=maestro_test,
            )

        if self.eval_block is not None and maestro_setup_test is not None:
            try:
                sync_summary = sync_spec_metrics_to_maestro(
                    self.bridge, self.eval_block,
                    logger=logger,
                    test=maestro_setup_test,
                )
                logger.info(
                    "Maestro Outputs sync: %d added, %d skipped.",
                    len(sync_summary["added"]),
                    len(sync_summary["skipped"]),
                )
            except Exception as exc:  # noqa: BLE001 — fail-soft
                logger.warning(
                    "Maestro Outputs sync failed at startup (%s: %s); "
                    "continuing without it. PC-side metrics still "
                    "authoritative.", type(exc).__name__, exc,
                )

        accumulated_vars: dict[str, Any] = dict(baseline_vars)
        last_measurements: dict = {}
        last_pass_fail: dict = {}
        best_design_vars: dict[str, Any] | None = None
        best_measurements: dict = {}
        best_pass_fail: dict = {}
        best_iteration: int | None = None
        best_score: tuple[int, float] | None = None
        converged = False
        abort_reason: str | None = None
        safeguard_streak = 0
        stuck_streak = 0
        topology_streak = 0
        # Path-3 prep (2026-05-24, R2 codex fix #2): clear any sweep
        # curve state left over from a previous run() invocation on the
        # same CircuitAgent instance, so a fresh run that hits an early
        # sweep failure cannot inherit a leftover successful curve
        # cache from the prior run.
        self._last_sweep_curve_state = None
        # Path-2: sweep-phase state. `tuning_*` track the most recent
        # sweep verdicts so they can flow into the next-iteration prompt
        # AND into the run() return payload.
        tuning_retries = 0
        last_tuning_measurements: dict = {}
        last_tuning_pass_fail: dict = {}
        # Path-3 prep (2026-05-24): optional curve-level searcher
        # sensitivity-window state, carried across iterations so the
        # next sweep-FAIL can compute observed dy/d(ln var). The
        # `last_curve_summary_md` prompt section itself is reset at
        # the TOP of each iteration (see fix #1 below) — do not init
        # it here, the per-iter reset is the source of truth.
        prev_tuning_measurements: dict = {}
        prev_tuning_design_vars: dict = {}
        sweep_enabled = bool(
            sweep_results_root
            and self.eval_block
            and self.eval_block.get("sweep")
            and self.eval_block.get("tuning_metrics")
        )
        if sweep_results_root and not sweep_enabled:
            logger.info(
                "--sweep-results-root supplied but spec has no `sweep:` + "
                "`tuning_metrics:` block; ignoring."
            )

        for i in range(max_iter):
            logger.info("=== Iteration %d/%d ===", i + 1, max_iter)

            # Path-3 prep (2026-05-24, R2 codex fix #1): reset the
            # curve-searcher prompt section at the TOP of every iter so
            # a summary built from iter N's sweep-FAIL cannot leak into
            # iter N+1's prompt when iter N+1 takes a different branch
            # (e.g. single-point FAIL before _run_sweep_phase runs).
            # The section is repopulated only by the single-point-PASS
            # + sweep-FAIL branch below.
            last_curve_summary_md = ""

            parsed = self._parse_llm_response(response)
            # Path-2.5 (2026-05-19, R2 P1 codex blocker): when the
            # spec-derived Maestro setup path is active, the LLM's
            # ``tests`` / ``analyses`` / ``outputs`` / ``corners``
            # blocks are ignored downstream — feeding them to the
            # contract validator would only generate a fake violation
            # (e.g. haiku-4-5 emitting ``outputs: dict`` instead of
            # list) that burns all 3 repair retries before aborting.
            # Strip-and-warn at the top of the iter so the static
            # checker sees a clean payload, then again after every
            # repair-loop re-parse (the repair prompt does NOT
            # re-instruct the LLM about these blocks, so a corrected
            # response may still carry them).
            self._strip_llm_setup_blocks_if_derived(parsed, iter_idx=i)
            # Repair loop: if the response violates the §4 contract,
            # send a corrective message and re-request. Track C v2
            # raised the cap from 1 to ``_CONTRACT_REPAIR_MAX`` (3) so
            # a structural-block typo in the new ``tests/analyses/
            # outputs/corners`` payload doesn't burn an iter on the
            # first try. After all attempts fail, abort the iter.
            # TODO(post-MLCAD): record ``repair_attempts`` count in
            # ``IterationRecord`` (claude P3 NIT) so the audit log
            # reflects how many corrective prompts a given iter
            # consumed. Currently only the final pass/fail surfaces.
            repair_reason = self._check_contract_violation(
                parsed, self.valid_design_var_names
            )
            repair_attempts = 0
            while repair_reason and repair_attempts < _CONTRACT_REPAIR_MAX:
                repair_attempts += 1
                logger.warning(
                    "Contract violation (iter %d, attempt %d/%d): "
                    "%s — sending repair prompt",
                    i + 1, repair_attempts, _CONTRACT_REPAIR_MAX,
                    repair_reason,
                )
                # response is already the last assistant message in
                # messages (appended by the previous iteration or the
                # initial chat). Only append the repair user message.
                repair_msg = (
                    f"Your previous response violated HARD CONSTRAINTS: "
                    f"{repair_reason}. Please re-emit using EXACTLY the "
                    f"keys and variable names from HARD CONSTRAINTS."
                )
                messages.append({"role": "user", "content": repair_msg})
                try:
                    response = self.llm.chat(messages)
                except Exception as exc:  # noqa: BLE001 - fail gracefully
                    safe_exc = scrub(f"{type(exc).__name__}: {exc}")
                    logger.error(
                        "LLM chat failed during contract repair "
                        "(iter %d, attempt %d/%d): %s",
                        i + 1, repair_attempts,
                        _CONTRACT_REPAIR_MAX, safe_exc,
                    )
                    abort_reason = "llm_error"
                    break
                messages.append({"role": "assistant", "content": response})
                parsed = self._parse_llm_response(response)
                self._strip_llm_setup_blocks_if_derived(parsed, iter_idx=i)
                repair_reason = self._check_contract_violation(
                    parsed, self.valid_design_var_names
                )
            if abort_reason == "llm_error":
                break
            if repair_reason:
                logger.warning(
                    "Contract violation persists after %d repair "
                    "attempts (iter %d): %s — aborting iter.",
                    _CONTRACT_REPAIR_MAX, i + 1, repair_reason,
                )
                abort_reason = "contract_violation"
                break

            # Track C v2 (2026-05-15): if the LLM emitted any structural
            # Maestro blocks (tests / analyses / outputs / corners),
            # apply them through the bridge BEFORE running the
            # simulation. Per leader: tests/analyses/corners are only
            # honored on iter 0 (the initial setup phase) — later iters
            # only accept outputs / design_vars. This prevents an LLM
            # mid-loop from accidentally restructuring the testbench
            # under us. Application is fail-soft per entry; the
            # design_vars path remains the authoritative pass/fail
            # surface.
            # TODO(post-MLCAD): HSpice dispatch path currently shares
            # ``_check_contract_violation`` so v2 keys are *accepted* but
            # never applied (this block only runs for the Spectre /
            # Maestro path). Decide whether to (a) explicitly reject
            # v2 keys with a clear repair-prompt in the HSpice branch,
            # or (b) share the v2 apply path with HSpice once the
            # HSpice backend grows an equivalent Outputs Setup. Codex P3.
            #
            # Path-2.5 (2026-05-19): when a §2 eval block is present,
            # derive the Maestro analyses + outputs payload from spec
            # rather than trusting the LLM's emit. Small models (haiku-
            # 4-5) regularly mis-shape these blocks
            # (``outputs: dict`` instead of ``list``, ``analysis:
            # 'transient'`` instead of ``'tran'``) which used to burn
            # every contract-repair retry before aborting the iter —
            # the strip-and-warn helper above now drops them before the
            # contract check, so iters always reach this dispatch slot.
            #
            # The derived payload is normally applied once on iter 0;
            # ``self._maestro_setup_applied`` tracks success so a SKILL
            # failure on iter 0 retries automatically on iter 1 instead
            # of leaving the testbench Outputs Setup empty for the rest
            # of the run (R2 P3 NIT 3).
            setup_payload: dict | None = None
            if self.eval_block is not None:
                if not self._maestro_setup_applied and maestro_setup_test is not None:
                    setup_payload = self._derive_maestro_setup_from_spec(
                        maestro_setup_test,
                    )
            elif self.allow_llm_maestro_setup:
                setup_payload = self._slice_maestro_setup_payload(
                    parsed, iter_idx=i, log=logger,
                )
            if setup_payload:
                try:
                    setup_summary = apply_maestro_setup(
                        self.bridge, setup_payload, logger=logger,
                    )
                    logger.info(
                        "Maestro setup applied (iter %d): %s",
                        i + 1,
                        {k: len(v) for k, v in setup_summary["applied"].items()},
                    )
                    if self.eval_block is not None:
                        self._maestro_setup_applied = True
                except Exception as exc:  # noqa: BLE001 — fail-soft
                    logger.warning(
                        "apply_maestro_setup raised at iter %d "
                        "(%s: %s); continuing — design_vars path "
                        "is authoritative.",
                        i + 1, type(exc).__name__, exc,
                    )

            new_vars = parsed.get("design_vars", {}) or {}
            llm_measurements = parsed.get("measurements", {}) or {}
            llm_pass_fail = parsed.get("pass_fail", {}) or {}
            reasoning = parsed.get("reasoning", "") or ""

            if not new_vars and not accumulated_vars and i == 0:
                # No baseline AND LLM gave nothing — genuinely no seed.
                # With scs_path, accumulated_vars is non-empty from
                # baseline, so we can sim even if the LLM proposed
                # nothing this turn.
                logger.warning(
                    "No baseline (scs_path missing) and LLM did not "
                    "propose any design_vars on iteration 1; aborting."
                )
                abort_reason = "no_changes"
                break

            # Accumulate: an omitted key keeps its previous value
            # (either baseline or prior LLM setting).
            accumulated_vars.update(new_vars)

            # Empty-diff guard (rev 16, 2026-04-20): detect when the LLM
            # proposes no effective change despite failing metrics.
            # Covers two cases:
            #   A. diagnostic failure (timeout / no_saved_outputs) — the
            #      original Bug 2 anti-replay (rev 11).
            #   B. normal metric FAILs but LLM emitted empty new_vars
            #      or identical accumulated_vars — wastes an iteration.
            # R1a (rev 16b): cap consecutive identical-vars at 2 then
            # abort — _auto_perturb_ibias silently no-ops when no
            # Ibias key exists, so without this cap the loop burns
            # every remaining iter on the same degenerate point.
            if self.history:
                prev = self.history[-1]
                same_vars = (
                    dict(prev.design_vars) == dict(accumulated_vars)
                )
                has_diag_failure = prev.diagnostic.has_failure
                has_metric_fail = not prev.meets_spec
                if same_vars and (has_diag_failure or has_metric_fail):
                    stuck_streak += 1
                    reason = (
                        f"diagnostic={prev.diagnostic.dump_status}"
                        if has_diag_failure
                        else "metrics still failing"
                    )
                    logger.error(
                        "LLM repeated identical design_vars after a "
                        "non-passing iter (%s, streak=%d). "
                        "Forcing exploration: current-source variable ×2.",
                        reason, stuck_streak,
                    )
                    # Snapshot LLM's raw proposal BEFORE guard mutates it.
                    proposed_vars_snapshot = dict(accumulated_vars)
                    perturb_ok, perturb_keys = _auto_perturb_ibias(
                        accumulated_vars, factor=2.0,
                    )
                    # R1b: observability — write guard event to JSONL
                    # transcript so post-hoc debugging doesn't require
                    # cross-referencing loggers.
                    _append_transcript(
                        i + 1, "system",
                        json.dumps({
                            "kind": "empty_diff_guard_fired",
                            "iteration": i + 1,
                            "proposed_vars": proposed_vars_snapshot,
                            "live_vars_after_guard": dict(accumulated_vars),
                            "prev_vars": dict(prev.design_vars),
                            "prev_meets_spec": prev.meets_spec,
                            "prev_dump_status": prev.diagnostic.dump_status,
                            "perturb_applied": perturb_ok,
                            "perturb_keys": perturb_keys,
                            "stuck_streak": stuck_streak,
                            "reason": reason,
                        }),
                    )
                    if stuck_streak >= 2:
                        logger.warning(
                            "STUCK: identical design_vars for %d "
                            "consecutive iterations — aborting.",
                            stuck_streak,
                        )
                        abort_reason = "stuck_identical_vars"
                        break
                else:
                    stuck_streak = 0

            logger.info("Applying design_vars: %s", accumulated_vars)
            analyses_for_run: list[Any] = (
                baseline_analyses if baseline_analyses
                else [self.analysis_type]
            )
            diagnostic = IterationDiagnostic()
            try:
                sim_result = self.bridge.run_ocean_sim(
                    lib=lib,
                    cell=cell,
                    tb_cell=tb_cell,
                    design_vars=accumulated_vars,
                    analyses=analyses_for_run,
                )
            except RuntimeError as exc:
                msg = str(exc)
                diagnostic.dump_status = DumpStatus.classify_runtime_error(msg)
                if diagnostic.dump_status == DumpStatus.UNKNOWN:
                    diagnostic.dump_status = DumpStatus.SIM_FAILED
                diagnostic.dump_raw_error = msg[:200]
                diagnostic.notes.append("run_ocean_sim failed")
                logger.warning(
                    "run_ocean_sim failed (%s); surfacing as %s so "
                    "the LLM can adjust design_vars next iter.",
                    msg[:160],
                    diagnostic.dump_status,
                )
                if hasattr(self.bridge, "_last_results_dir"):
                    self.bridge._last_results_dir = None
                sim_result = {
                    "ok": False,
                    "measurements": {},
                    "measure_error": msg,
                    "run_error": msg,
                }

            # Stage 1 rev 13 (2026-04-20): Drop any lingering
            # selectResult('tran) handle left over from the PREVIOUS
            # iter's _display_waveform call. Cadence binds selectResult
            # process-globally to the PSF dir; when the next safeOceanRun
            # overwrites that dir in place, downstream post-run readers
            # (read_op_point_after_tran, ocean_worker.dump_all) hang on
            # the stale mapping. Placing the cleanup AFTER run_ocean_sim
            # covers those post-run consumers; intra-run readers such as
            # safeOceanMeasure/safeOceanTCross execute inside run_ocean_sim
            # itself and are not affected by this call. Idempotent: a
            # no-op on iter 1 (no prior selectResult). Best-effort:
            # swallow any error so a SKILL-side glitch does not abort the
            # optimization. Dual-reviewer approved (Claude + Codex, see
            # docs/phase3_selectResult_leak_fix.md).
            # Guard unselectResult: some IC23.1/OCEAN builds do not
            # define it, and calling an undefined SKILL function inside
            # errset still prints a CIW error before Python can catch it.
            try:
                self.bridge.client.execute_skill(
                    "when(isCallable('unselectResult) "
                    "errset(unselectResult() t))"
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug(
                    "unselectResult() cleanup failed (%s); continuing.",
                    type(exc).__name__,
                )

            # Stage 1 rev 10 (2026-04-19): Plan Auto — patch input.scs's
            # ic line from this iter's spectre.fc so the next iter's
            # skipdc=yes tran starts from a valid bias snapshot rather
            # than zeroing every non-IC'd node. Best-effort; failures
            # log a warning and the next iter re-attempts.
            #
            # Rev 11 (Bug 4): capture the patched/reason payload into
            # diagnostic so the LLM sees when the ic line was NOT updated
            # (e.g. VT(/Vout_p)-VT(/Vout_n) unavailable because circuit
            # didn't oscillate) — without that feedback the LLM mistakenly
            # assumes the next iter starts from a fresh equilibrium.
            if plan_auto is not None and plan_auto.active:
                patch_status = plan_auto.patch_after_run(self.bridge, i + 1)
                diagnostic.ic_patch_applied = bool(patch_status.get("patched"))
                if not diagnostic.ic_patch_applied:
                    diagnostic.ic_patch_reason = str(
                        patch_status.get("reason", "unknown")
                    )

            # NOTE (2026-04-20): _display_waveform used to run here, right
            # after run_ocean_sim. On non-oscillating iters that produced
            # pathological waveforms, the Viva renderer it triggers held a
            # lock on the selectResult('tran) handle and serialized with
            # the subsequent safeOceanDumpAll → VT()/IT() calls, causing a
            # repeatable 30 s RAMIC-daemon timeout (run_20260420_023703
            # iters 2 and 3). The same design vars via a direct CIW
            # safeOceanRun + safeOceanDumpAll (rev14_dump_timeout_repro.il)
            # return in <1 s. Moved to AFTER dumpAll + safeOceanMeasure
            # (below) so the display side-effect never overlaps dump reads.

            # Stage 1 rev 7 (2026-04-19): pull per-device DC op-point
            # from the tranOp sub-result. Lets the LLM reason about
            # cutoff/triode/saturation and fix the tail/pair biasing
            # when the 7 macro-metrics are all null (stuck-at-equilibrium).
            # A failure here must NEVER abort the loop — surface as an
            # empty dict so the feedback section says "(unavailable)".
            op_point: dict = {}
            op_reader = "read_op_point_after_tran"
            try:
                if _analyses_use_dc_op(analyses_for_run):
                    nets, insts = _op_point_probe_paths(schematic_instances)
                    op_reader = "read_dc_op_point_from_results"
                    op_point = self.bridge.read_dc_op_point_from_results(
                        nets=nets,
                        instances=insts,
                    )
                else:
                    op_point = self.bridge.read_op_point_after_tran()
            except Exception as exc:  # noqa: BLE001 — see docstring
                logger.warning(
                    "%s failed (%s: %s); LLM will not see per-device "
                    "op-point this iter.",
                    op_reader,
                    type(exc).__name__,
                    exc,
                )
                diagnostic.op_point_available = False
            else:
                diagnostic.op_point_available = bool(op_point)
            if op_point:
                _enrich_op_point_terminal_biases(
                    op_point,
                    schematic_instances,
                    accumulated_vars,
                )
                op_instances = _op_point_instances(op_point)
                op_nodes = op_point.get("nodes") if isinstance(
                    op_point.get("nodes"), dict,
                ) else {}
                region_items = [
                    f"{inst.rsplit('/', 1)[-1]}="
                    f"{params.get('region_label', '?')}"
                    for inst, params in op_instances.items()
                    if isinstance(params, dict)
                ]
                logger.info(
                    "Op-point via %s (%d devs, %d nodes): %s",
                    op_reader,
                    len(op_instances),
                    len(op_nodes),
                    ", ".join(region_items) if region_items else "no regions",
                )
                if op_reader == "read_dc_op_point_from_results":
                    save_check = assess_op_point_save_effectiveness(
                        sim_result,
                        op_point,
                    )
                    msg = (
                        "saveOpPoint check: requested=%s, devices=%s/%s, "
                        "keys=%s"
                    ) % (
                        save_check.get("opPointsRequested"),
                        save_check.get("devicesWithSavedScalars"),
                        save_check.get("instancesReturned"),
                        ",".join(save_check.get("savedScalarKeys") or []),
                    )
                    if save_check.get("ok"):
                        logger.info("%s", msg)
                    else:
                        logger.warning(
                            "%s; issues=%s",
                            msg,
                            "; ".join(save_check.get("issues") or []),
                        )
                    if save_check.get("issues"):
                        op_point.setdefault("issues", [])
                        if isinstance(op_point["issues"], list):
                            for issue in save_check["issues"]:
                                op_point["issues"].append(
                                    f"saveOpPoint check: {issue}"
                                )

            # Stage 1 rev 4 (2026-04-18): measurements + pass_fail are
            # now PC-computed from a generic safeOceanDumpAll + spec
            # evaluator when the spec declares a signals/windows/metrics
            # block. Removes both the LLM-fabricated-numbers failure mode
            # (rev 1/2) and the LC_VCO-specific SKILL coupling (rev 3).
            # Legacy flow (no eval block) still accepts LLM-judged
            # pass_fail and the rev-3 safeOceanMeasure payload.
            dumps: dict = {}
            if self.eval_block is not None:
                signals, windows = spec_evaluator.build_dump_spec(
                    self.eval_block
                )
                # Oscillation gate: if the spec declares a Vdiff signal
                # (kind=="Vdiff", 2 paths), hand those paths to the OCEAN
                # worker so it can skip safeOceanDumpAll when the output
                # pair isn't swinging — prevents cross-based stats from
                # infinite-looping on degenerate iters. General across
                # topologies; opt-in by declaring Vdiff in the spec.
                osc_signals = spec_evaluator.extract_osc_signals(
                    self.eval_block
                )

                # Stage 1 rev 12 (2026-04-20): dumpAll runs in an
                # OCEAN subprocess (OceanWorker). If the PSF is
                # degenerate and safeOceanDumpAll wedges, the wall-
                # clock timer triggers kill -9 and we raise
                # OceanWorkerTimeout; the main RAMIC SKILL daemon is
                # untouched. The psf_dir comes from the SafeBridge's
                # captured resultsDir (un-scrubbed, internal only).
                psf_dir = self.bridge.last_results_dir
                if not psf_dir:
                    diagnostic.dump_status = DumpStatus.UNKNOWN
                    diagnostic.dump_raw_error = (
                        "no resultsDir from last run_ocean_sim"
                    )
                    logger.warning(
                        "OceanWorker skipped: bridge.last_results_dir "
                        "is empty; cannot locate PSF. Metrics will be "
                        "unresolved this iter."
                    )
                else:
                    try:
                        dump_result = self.ocean_worker.dump_all(
                            psf_dir=psf_dir,
                            signals=signals,
                            windows=windows,
                            osc_signals=osc_signals,
                        )
                        dumps = dump_result.get("dumps") or {}
                        # Oscillation gate short-circuit: OCEAN worker
                        # detected the Vdiff pair isn't swinging, so
                        # dumpAll was skipped. Surface as NON_OSCILLATING
                        # so the LLM prompt shows a specific label.
                        if dump_result.get("degenerate"):
                            diagnostic.dump_status = (
                                DumpStatus.NON_OSCILLATING
                            )
                            diagnostic.dump_raw_error = (
                                "osc_gate: "
                                + str(dump_result.get("reason", ""))
                            )[:200]
                            logger.warning(
                                "OceanWorker osc_gate: %s; metrics "
                                "unresolved this iter.",
                                dump_result.get("reason"),
                            )
                    except OceanWorkerTimeout as exc:
                        diagnostic.dump_status = DumpStatus.TIMEOUT
                        diagnostic.dump_raw_error = str(exc)[:200]
                        logger.warning(
                            "OceanWorker timed out: %s. Metrics will "
                            "be unresolved this iter.", exc,
                        )
                    except OceanWorkerScriptError as exc:
                        diagnostic.dump_status = (
                            DumpStatus.classify_runtime_error(str(exc))
                        )
                        diagnostic.dump_raw_error = str(exc)[:200]
                        logger.warning(
                            "OceanWorker script error (%s); "
                            "diagnostic=%s.",
                            str(exc)[:120], diagnostic.dump_status,
                        )
                    except OceanWorkerError as exc:
                        diagnostic.dump_status = DumpStatus.UNKNOWN
                        diagnostic.dump_raw_error = str(exc)[:200]
                        logger.warning(
                            "OceanWorker failure (%s: %s).",
                            type(exc).__name__, str(exc)[:120],
                        )
                measurements, pass_fail = spec_evaluator.evaluate(
                    self.eval_block, dumps, bridge=self.bridge
                )
                # Bug 2: if dumpAll failed or was skipped, overwrite
                # pass_fail verdicts with an explicit failure label so
                # the LLM prompt shows "FAIL (dump_timeout)" rather than
                # silently blank values. measurements dict is preserved
                # as-is (may contain None placeholders from evaluator).
                if diagnostic.dump_status != DumpStatus.OK:
                    for metric_name in pass_fail.keys():
                        pass_fail[metric_name] = (
                            f"UNMEASURABLE ({diagnostic.dump_status})"
                        )
                # LLM's own measurements/pass_fail are discarded here
                # on purpose. Keep them in the transcript (log only) so
                # post-mortems can see how far the LLM diverged from the
                # authoritative PC values.
                if llm_measurements or llm_pass_fail:
                    logger.debug(
                        "Discarded LLM-reported measurements=%s pass_fail=%s",
                        llm_measurements, llm_pass_fail,
                    )
            else:
                # Legacy path: sim_result["measurements"] comes from
                # safeOceanMeasure (rev 3) and pass_fail comes from the
                # LLM's own judgement.
                sim_measurements = sim_result.get("measurements") or {}
                if sim_measurements:
                    measurements = sim_measurements
                else:
                    logger.warning(
                        "safeOceanMeasure returned no metrics (error=%s); "
                        "falling back to LLM-reported measurements. "
                        "Pass_fail and SAFEGUARD may be unreliable this iter.",
                        sim_result.get("measure_error", "unknown"),
                    )
                    measurements = llm_measurements
                pass_fail = llm_pass_fail
                if self._legacy_hard_pass_bounds:
                    enforced_pass_fail = _enforce_legacy_hard_pass_bounds(
                        measurements,
                        pass_fail,
                        self._legacy_hard_pass_bounds,
                    )
                    changed = {
                        key: enforced_pass_fail[key]
                        for key in enforced_pass_fail
                        if pass_fail.get(key) != enforced_pass_fail[key]
                    }
                    if changed:
                        logger.warning(
                            "Overrode LLM pass_fail with numeric hard-pass "
                            "verdicts from measurements: %s",
                            changed,
                        )
                    pass_fail = enforced_pass_fail

            # Best-effort waveform display — never let a display failure
            # abort the optimization. Runs AFTER dumpAll (see note above
            # run_ocean_sim call site).
            self._display_waveform(sim_result)

            met = self._all_pass(pass_fail)
            hard_pass_met = self._hard_pass_met(pass_fail)
            record = IterationRecord(
                iteration=i + 1,
                design_vars=dict(accumulated_vars),
                measurements=measurements,
                pass_fail=pass_fail,
                meets_spec=hard_pass_met,
                llm_reasoning=reasoning,
                diagnostic=diagnostic,
            )
            self.history.append(record)
            last_measurements = measurements
            last_pass_fail = pass_fail
            candidate_score = self._legacy_hard_pass_score(measurements)
            should_update_best = hard_pass_met
            if (
                candidate_score is not None
                and (best_score is None or candidate_score > best_score)
            ):
                should_update_best = True
            if should_update_best:
                best_design_vars = dict(accumulated_vars)
                best_measurements = dict(measurements)
                best_pass_fail = dict(pass_fail)
                best_iteration = i + 1
                if candidate_score is not None:
                    best_score = candidate_score

            # T8.8: track sanity-range UNMEASURABLE streak. Reset on any
            # iter without a "suspect:" verdict so a transient glitch
            # cannot strand a converging run with a topology label.
            if _has_sanity_violation(pass_fail):
                topology_streak += 1
            else:
                topology_streak = 0

            if hard_pass_met:
                # Path-2: gate single-point PASS on the tuning-curve
                # phase when the spec declares a sweep and the CLI
                # supplied a results root. Otherwise single-point PASS
                # is enough for convergence (legacy behaviour).
                if sweep_enabled:
                    tuning_meas, tuning_pf = self._run_sweep_phase(
                        sweep_results_root=sweep_results_root,
                        tb_cell=tb_cell,
                        result_test=maestro_setup_test,
                        lib=lib,
                        cell=cell,
                        design_vars=accumulated_vars,
                        analyses=analyses_for_run,
                    )
                    record.tuning_measurements = tuning_meas
                    record.tuning_pass_fail = tuning_pf
                    last_tuning_measurements = tuning_meas
                    last_tuning_pass_fail = tuning_pf
                    if self._all_pass(tuning_pf):
                        logger.info(
                            "Specifications met (single-point + tuning) "
                            "at iteration %d", i + 1,
                        )
                        converged = True
                        self._log_final_converged_values(accumulated_vars)
                        break
                    # Path-3 prep (2026-05-24): optional curve searcher.
                    # Builds a structured f-Vctrl/Kvco/candidate summary
                    # from the data `_run_sweep_phase` already produced
                    # and appends it to the next-iter prompt. No new
                    # SafeBridge / OCEAN / .tran fetch; pure-Python over
                    # in-memory state. Returns "" when the searcher is
                    # disabled, the sweep curve state is unavailable
                    # (early-return sweep failure), or the safety gate
                    # rejects the rendered text.
                    if curve_searcher_enabled:
                        last_curve_summary_md = (
                            self._build_curve_searcher_section(
                                tuning_measurements=tuning_meas,
                                tuning_pass_fail=tuning_pf,
                                design_vars=accumulated_vars,
                                prev_design_vars=prev_tuning_design_vars,
                                prev_tuning_measurements=(
                                    prev_tuning_measurements
                                ),
                                max_candidates=(
                                    curve_searcher_max_candidates
                                ),
                            )
                        )
                    prev_tuning_measurements = dict(tuning_meas)
                    prev_tuning_design_vars = dict(accumulated_vars)
                    tuning_retries += 1
                    logger.info(
                        "Iter %d: single-point PASS but tuning FAIL "
                        "(retry %d/%d). Verdicts: %s",
                        i + 1, tuning_retries, TUNING_RETRY_BUDGET,
                        tuning_pf,
                    )
                    if tuning_retries >= TUNING_RETRY_BUDGET:
                        abort_reason = "tuning_budget"
                        logger.warning(
                            "tuning_retry_budget (%d) exhausted; single-"
                            "point PASS but tuning still FAIL.",
                            TUNING_RETRY_BUDGET,
                        )
                        break
                    # Fall through so the next-iter prompt picks up
                    # tuning verdicts and the LLM gets a chance to
                    # re-balance the LC/varactor sizing.
                else:
                    if self._legacy_hard_pass_bounds and not met:
                        logger.info(
                            "Hard-pass specifications met at iteration %d "
                            "(secondary verdicts may still be non-PASS).",
                            i + 1,
                        )
                    else:
                        logger.info(
                            "Specifications met at iteration %d", i + 1,
                        )
                    converged = True
                    self._log_final_converged_values(accumulated_vars)
                    break

            # SAFEGUARD: three consecutive iterations with amp_hold_ratio<0.3
            # means the tank isn't oscillating; further tweaks won't help.
            amp_hold = _coerce_float(measurements.get("amp_hold_ratio"))
            if amp_hold is not None and amp_hold < SAFEGUARD_AMP_HOLD_MIN:
                safeguard_streak += 1
            else:
                safeguard_streak = 0
            if safeguard_streak >= SAFEGUARD_CONSECUTIVE_LIMIT:
                logger.warning(
                    "SAFEGUARD: amp_hold_ratio<%.2f for %d consecutive "
                    "iterations — circuit not oscillating, aborting.",
                    SAFEGUARD_AMP_HOLD_MIN, safeguard_streak,
                )
                abort_reason = "safeguard"
                break

            # Prepare next prompt. With the eval block active the LLM
            # sees: (a) the authoritative PC-computed measurements +
            # pass_fail for this iter, (b) the raw per-signal/per-window
            # dump so it can reason about non-oscillating states (DC
            # operating point of every node is visible even when f_osc
            # is null), and (c) the running history.
            sim_summary = _guard_llm_feedback(
                _format_sim_summary(sim_result),
                context="OCEAN run meta",
            )
            op_point_summary = _guard_llm_feedback(
                _format_op_point_summary(op_point),
                context="op-point feedback",
            )
            history_brief = self._format_history_brief()
            # Plan ★ (2026-04-19): merge cached topology with current
            # accumulated_vars so Kimi sees which physical instance each
            # desVar drives and its live value this iteration. Empty
            # string when the startup schematic read failed — the
            # section header is also suppressed so the prompt stays
            # clean.
            topology_section = _format_topology_with_live_vars(
                schematic_instances, accumulated_vars
            )
            topology_block = (
                f"## Topology (instances × live desVars)\n{topology_section}\n\n"
                if topology_section else ""
            )
            if self.eval_block is not None:
                eval_summary = _guard_llm_feedback(
                    _format_eval_summary(
                        measurements, pass_fail, dumps,
                        diagnostic=diagnostic,
                    ),
                    context="evaluation feedback",
                )
                tuning_section = _format_tuning_summary(
                    last_tuning_measurements, last_tuning_pass_fail,
                ) if last_tuning_pass_fail else ""
                next_prompt = (
                    f"{topology_block}"
                    f"## Iteration {i + 1} measurements (platform-computed)\n"
                    f"{eval_summary}\n\n"
                    f"{tuning_section}"
                    f"{last_curve_summary_md}"
                    f"## Per-device DC operating point\n"
                    f"{op_point_summary}\n\n"
                    f"## OCEAN run meta\n{sim_summary}\n\n"
                    f"## History\n{history_brief}\n\n"
                    "Emit the next JSON block. `measurements` and "
                    "`pass_fail` you emit are advisory only — the "
                    "platform recomputes them from the dump next turn. "
                    "Focus on `reasoning` and `design_vars`: what "
                    "physical mechanism explains the pass/fail pattern "
                    "and which variables (from the spec's design-"
                    "variable table) close the gap. Use the op-point "
                    "table to diagnose biasing health — consult the "
                    "region enum in the initial prompt and the spec's "
                    "topology section to judge which devices should be "
                    "in saturation. If every metric already PASSes, "
                    "repeat the same design_vars and mark all PASS — "
                    "the agent stops on its own."
                )
            else:
                next_prompt = (
                    f"{topology_block}"
                    f"## Iteration {i + 1} OCEAN result\n{sim_summary}\n\n"
                    f"## Per-device DC operating point\n"
                    f"{op_point_summary}\n\n"
                    f"## History\n{history_brief}\n\n"
                    "Emit the next JSON block. If every pass_fail entry was "
                    "PASS, repeat the same design_vars and mark all PASS — "
                    "the agent will stop on its own."
                )
            try:
                assert_llm_feedback_safe(
                    next_prompt,
                    context=f"iteration {i + 1} feedback",
                )
            except ValueError as exc:
                logger.error(
                    "Withholding full iteration feedback from LLM: %s",
                    exc,
                )
                next_prompt = (
                    "## Iteration feedback\n"
                    "(withheld: iteration feedback failed sensitive-token "
                    "scan)\n\n"
                    "Emit the next JSON block. Use the prior safe context, "
                    "avoid PDK/model-card data, and propose a conservative "
                    "design_vars update."
                )
            messages.append({"role": "user", "content": next_prompt})
            _append_transcript(i + 1, "user", next_prompt)
            try:
                response = self.llm.chat(messages)
            except Exception as exc:  # noqa: BLE001 - provider outage
                safe_exc = scrub(f"{type(exc).__name__}: {exc}")
                logger.error(
                    "LLM chat failed after iteration %d; stopping with "
                    "partial results: %s",
                    i + 1, safe_exc,
                )
                abort_reason = "llm_error"
                break
            messages.append({"role": "assistant", "content": response})
            _append_transcript(
                i + 1, "assistant", response,
                usage=getattr(self.llm, "last_usage", None),
            )
        else:
            if topology_streak >= TOPOLOGY_SANITY_VIOLATION_LIMIT:
                abort_reason = "topology"
                logger.warning(
                    "TOPOLOGY: last %d iterations all produced sanity-range "
                    "UNMEASURABLE verdicts — iteration budget exhausted on "
                    "physically-implausible measurements; topology or spec "
                    "mismatch, not a tuning issue.", topology_streak,
                )
            else:
                abort_reason = "max_iter"
                logger.warning(
                    "Did not converge within %d iterations.", max_iter,
                )
                # R2 (2026-05-19, claude P3): when the sweep gate was
                # active and the loop exited on max_iter rather than on
                # tuning_budget, surface how many tuning retries were
                # still available — actionable for the operator (bump
                # max_iter vs. abandon the topology).
                if sweep_enabled:
                    retries_remaining = max(
                        0, TUNING_RETRY_BUDGET - tuning_retries,
                    )
                    logger.warning(
                        "tuning_retries_remaining_when_max_iter=%d "
                        "(budget=%d, used=%d); bump max_iter to give "
                        "the sweep gate more chances if the single-"
                        "point bands keep passing.",
                        retries_remaining, TUNING_RETRY_BUDGET,
                        tuning_retries,
                    )

        final_design_vars = accumulated_vars
        final_measurements = last_measurements
        final_pass_fail = last_pass_fail
        writeback_source = "last"
        if best_design_vars is not None and not converged:
            final_design_vars = best_design_vars
            final_measurements = best_measurements
            final_pass_fail = best_pass_fail
            writeback_source = f"best_iter_{best_iteration}"
            logger.warning(
                "Using best-so-far from iteration %s for final writeback "
                "after abort_reason=%s.",
                best_iteration, abort_reason,
            )
        elif best_design_vars is not None and converged:
            writeback_source = f"best_iter_{best_iteration}"

        if abort_reason == "llm_error" and best_design_vars is None:
            writeback_status = "skipped: llm_error"
        else:
            writeback_status = (
                self._run_writeback(final_design_vars)
                if writeback_enabled else "skipped: disabled"
            )

        return {
            "measurements": final_measurements,
            "pass_fail": final_pass_fail,
            "design_vars": final_design_vars,
            "converged": converged,
            "abort_reason": abort_reason,
            "writeback_status": writeback_status,
            "writeback_source": writeback_source,
            "last_measurements": last_measurements,
            "last_pass_fail": last_pass_fail,
            "last_design_vars": accumulated_vars,
            "best_iteration": best_iteration,
            "best_score": list(best_score) if best_score is not None else None,
            "best_design_vars": best_design_vars or {},
            "best_measurements": best_measurements,
            "best_pass_fail": best_pass_fail,
            # Path-2: present iff `sweep:` + `tuning_metrics:` were
            # declared and a sweep root was supplied. Empty dicts on
            # legacy runs so downstream code can treat them uniformly.
            "tuning_measurements": last_tuning_measurements,
            "tuning_pass_fail": last_tuning_pass_fail,
        }

    # ------------------------------------------------------------------ #
    #  LLM response parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_llm_response(response: str) -> dict:
        """Extract the first well-formed JSON block from the LLM response.

        Tries fenced blocks first (```json ...``` or ``` ... ```), then
        falls back to a best-effort bare-JSON match. Returns {} if nothing
        parses cleanly — the caller decides whether empty means abort.
        """
        for match in re.finditer(
            r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL
        ):
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data

        # Fallback: bare JSON object at top level.
        first_brace = response.find("{")
        last_brace = response.rfind("}")
        if 0 <= first_brace < last_brace:
            try:
                data = json.loads(response[first_brace : last_brace + 1])
            except json.JSONDecodeError:
                return {}
            if isinstance(data, dict):
                return data
        return {}

    @staticmethod
    def _check_contract_violation(
        parsed: dict,
        valid_design_vars: Iterable[str] | None = None,
    ) -> str | None:
        """Validate the parsed LLM response against the JSON schema.

        Schema contract (see ``docs/llm_protocol.md``):
          1. No unknown top-level keys (allow-list in
             ``_VALID_RESPONSE_KEYS``).
          2. Required top-level keys present (``_REQUIRED_RESPONSE_KEYS``).
          3. Each present key has the expected type
             (``_RESPONSE_KEY_TYPES``).
          4. ``design_vars`` keys are a subset of the spec's
             design-variable whitelist (``_VALID_DESIGN_VAR_NAMES``).
          5. ``design_vars`` values don't carry physical-unit suffixes
             (mA / pF / nH / V / GHz etc.) — engineering suffixes only.

        Returns a semicolon-joined problem string, or ``None`` if the
        response is schema-compliant.
        """
        problems: list[str] = []

        # 1. No unknown top-level keys.
        bad_keys = set(parsed.keys()) - _VALID_RESPONSE_KEYS
        if bad_keys:
            problems.append(
                f"unknown top-level key(s): {sorted(bad_keys)}"
            )

        # 2. Required top-level keys present.
        missing = _REQUIRED_RESPONSE_KEYS - set(parsed.keys())
        if missing:
            problems.append(
                f"missing required top-level key(s): {sorted(missing)}"
            )

        # 3. Type checks on present keys.
        for key, expected in _RESPONSE_KEY_TYPES.items():
            if key in parsed and not isinstance(parsed[key], expected):
                actual = type(parsed[key]).__name__
                exp_name = (
                    expected.__name__ if isinstance(expected, type)
                    else "|".join(t.__name__ for t in expected)
                )
                problems.append(
                    f"'{key}' has wrong type: got {actual}, expected {exp_name}"
                )

        # 4 + 5. design_vars whitelist and value-format checks.
        new_vars = parsed.get("design_vars")
        if new_vars and isinstance(new_vars, dict):
            valid_names = frozenset(
                valid_design_vars or _VALID_DESIGN_VAR_NAMES
            )
            bad_names = set(new_vars.keys()) - valid_names
            if bad_names:
                problems.append(
                    f"invalid design_vars key(s): {sorted(bad_names)}. "
                    f"Valid names: {sorted(valid_names)}"
                )
            for k, v in new_vars.items():
                value_problem = _design_var_value_problem(k, v)
                if value_problem:
                    problems.append(value_problem)
                    continue
                if isinstance(v, str) and _FORBIDDEN_UNIT_RE.search(v):
                    problems.append(
                        f"design_vars['{k}'] = '{v}' contains a physical "
                        f"unit — use engineering suffixes (u/n/p/f/k/M/G)"
                    )

        # 6. Track C v2 — per-entry shape of the four structural blocks.
        #    Delegated so this validator doesn't need to know the
        #    SafeBridge writer signatures (DRY against maestro_setup).
        setup_problem = _validate_maestro_setup_block(parsed)
        if setup_problem:
            problems.append(setup_problem)

        return "; ".join(problems) if problems else None

    def _strip_llm_setup_blocks_if_derived(
        self, parsed: dict, *, iter_idx: int,
    ) -> None:
        """Remove LLM-emitted Maestro setup keys when the derived path is
        active (mutates ``parsed`` in place).

        Path-2.5 R2 P1 (2026-05-19): when ``self.eval_block`` is set,
        the agent derives ``analyses`` / ``outputs`` deterministically
        from spec §2 and ignores whatever shape the LLM emits for
        ``tests``/``analyses``/``outputs``/``corners``. Letting those
        keys reach ``_check_contract_violation`` would falsely trip
        the contract repair loop on small-model typos (e.g.
        ``outputs: {…}`` instead of ``outputs: [{…}]``) and burn the
        full repair budget before aborting — defeating the whole
        purpose of the derived path. Strip them at the call site
        instead, with one WARN per iter so the divergence is visible.

        No-op when there's no eval_block and LLM setup is allowed
        (legacy LLM-emit path remains authoritative), or when ``parsed``
        carries none of the four keys.
        """
        if self.eval_block is None and self.allow_llm_maestro_setup:
            return
        present = [k for k in MAESTRO_SETUP_KEYS if k in parsed]
        if not present:
            return
        logger.warning(
            "iter %d: LLM emitted Maestro setup keys %s; ignoring "
            "(current Maestro setup policy is authoritative). Stripped before "
            "contract check.",
            iter_idx + 1, sorted(present),
        )
        for k in present:
            parsed.pop(k, None)

    def _derive_maestro_setup_from_spec(
        self, maestro_test: str,
    ) -> dict:
        """Derive the Maestro setup payload (analyses + outputs) from §2.

        Path-2.5 (2026-05-19): spec.md §2 already declares ``signals``,
        ``windows``, and ``metrics`` machine-readably. The agent can
        deterministically translate them into Maestro Outputs Setup rows
        using the same metric expressions as the PC evaluator, plus the
        analyses-enable list — no LLM round-trip needed. This
        helper replaces the prior LLM-emit path that was getting
        corrupted by small-model typos (e.g. haiku-4-5 emitting
        ``outputs: dict`` instead of ``list``, or ``analysis:
        'transient'`` instead of ``'tran'``), which burned all three
        contract-repair retries before aborting the iter.

        Returns a dict matching the ``apply_maestro_setup`` contract:

            {"analyses": [{"test": ..., "analysis": "tran",
                            "enable": True}],
             "outputs":  [{"name": ..., "output_type": "",
                            "expr": "frequency(clip(...))*...",
                            "test": ...}, ...]}

        Empty (``{}``) when the agent has no ``self.eval_block`` —
        callers should fall through to the legacy LLM-emit path in
        that case (``isinstance(self.spec, dict)`` JSON specs).

        ``tests`` / ``corners`` are intentionally NOT emitted: the ADE
        test row already exists in Maestro before the agent loop starts,
        and corners are PDK-specific (the spec doesn't carry them).
        """
        eval_block = self.eval_block
        if not eval_block:
            return {}

        signals_list = eval_block.get("signals") or []
        windows = eval_block.get("windows") or {}
        metrics_list = eval_block.get("metrics") or []

        signal_by_name: dict[str, dict] = {}
        for entry in signals_list:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                signal_by_name[entry["name"]] = entry

        outputs: list[dict] = []
        seen_names: set[str] = set()
        for metric in metrics_list:
            if not isinstance(metric, dict):
                continue
            name = metric.get("name")
            if not isinstance(name, str) or not name or name in seen_names:
                continue
            expr = _maestro_metric_expr(metric, signal_by_name, windows)
            if expr is None:
                continue
            outputs.append({
                "name": name,
                "test": maestro_test,
                "output_type": "",
                "expr": expr,
            })
            seen_names.add(name)

        analyses: list[dict] = [{
            "test": maestro_test,
            "analysis": "tran",
            "enable": True,
        }]

        return {"analyses": analyses, "outputs": outputs}

    @staticmethod
    def _metric_source_signal(metric: dict) -> str | None:
        """Return the signal name a metric depends on, or None.

        Simple metrics carry ``signal`` directly. Compound ``ratio``
        metrics resolve to the numerator signal (numerator and
        denominator typically share the same signal in practice;
        emitting one output suffices to surface the underlying
        waveform in Maestro). Compound ``t_cross_frac`` metrics
        carry their own ``signal``. Anything else returns None and
        the caller skips the metric.
        """
        sig = metric.get("signal")
        if isinstance(sig, str) and sig:
            return sig
        if metric.get("compound") == "ratio":
            num = metric.get("numerator")
            if isinstance(num, dict):
                sig = num.get("signal")
                if isinstance(sig, str) and sig:
                    return sig
        return None

    @staticmethod
    def _maestro_expr_for_signal(signal_entry: dict) -> str | None:
        """Translate a §2 signal entry into a Maestro waveform expression.

        Delegates to ``maestro_metric_sync._waveform_expr`` so the
        Option-I sync path and the derived-setup path emit BYTE-
        IDENTICAL expression strings for the same signal (R2 P3 NIT 2
        2026-05-19). Canonical kinds:

            V          → ``VT("/path")``
            I          → ``IT("/path")``
            Vdiff      → ``(VT("/p1") - VT("/p2"))``
            Vsum_half  → ``((VT("/p1") + VT("/p2")) / 2.0)``

        Returns None for unknown kinds / malformed paths / paths that
        fail the SafeBridge probe-path regex.
        """
        return _maestro_waveform_expr(signal_entry)

    @staticmethod
    def _slice_maestro_setup_payload(
        parsed: dict, *, iter_idx: int, log: logging.Logger,
    ) -> dict | None:
        """Compute the v2 Maestro-setup payload to dispatch this iter.

        Iter 0 forwards the full set of structural blocks the LLM sent.
        Iter > 0 only forwards ``outputs`` (additive new measurements)
        — proposed ``tests`` / ``analyses`` / ``corners`` are stripped
        and a WARNING logged so the LLM's intent is visible without
        actually restructuring the live testbench.

        Returns the payload to hand to ``apply_maestro_setup``, or
        ``None`` when there's nothing to apply (no structural keys
        present, or iter > 0 with only stripped keys).

        Extracted out of ``run()`` as a pure helper (Track C v2 R2
        P2-3) so the iter-gating slice can be unit-tested without
        spinning up the full agent loop.
        """
        setup_keys_present = {
            k for k in ("tests", "analyses", "outputs", "corners")
            if isinstance(parsed.get(k), list) and parsed[k]
        }
        if not setup_keys_present:
            return None
        if iter_idx == 0:
            return parsed
        rejected = setup_keys_present - {"outputs"}
        if rejected:
            log.warning(
                "iter %d: LLM proposed structural changes (%s) "
                "past iter 0 — only 'outputs' is honored after setup; "
                "ignoring the rest.",
                iter_idx + 1, sorted(rejected),
            )
        payload = {k: parsed[k] for k in ("outputs",) if k in parsed}
        return payload or None

    @staticmethod
    def _all_pass(pass_fail: dict) -> bool:
        """Return True iff every value in pass_fail starts with 'PASS'.

        Matches the LLM protocol (``docs/llm_protocol.md``): values
        look like ``"PASS"`` or ``"FAIL (target 19.5–20.5 GHz)"``. Uses
        a strict **prefix** check (case-insensitive) rather than
        substring matching — substring on "fail" would wrongly reject a
        legitimate annotation such as ``"PASS (target 19.5–20.5 GHz —
        previously FAILED once)"``.
        """
        if not pass_fail:
            return False
        for value in pass_fail.values():
            if not str(value).strip().upper().startswith("PASS"):
                return False
        return True

    def _hard_pass_met(self, pass_fail: dict) -> bool:
        """Return True when the run's acceptance gate has passed.

        Legacy Markdown specs may mark a subset of metrics as hard-pass
        acceptance gates while keeping other metrics as diagnostics. Specs
        without such annotations keep the historical all-metrics predicate.
        """
        if not self._legacy_hard_pass_bounds:
            return self._all_pass(pass_fail)
        if not pass_fail:
            return False
        for metric_name in self._legacy_hard_pass_bounds:
            verdict = pass_fail.get(metric_name)
            if not str(verdict).strip().upper().startswith("PASS"):
                return False
        return True

    def _legacy_hard_pass_score(
        self, measurements: dict,
    ) -> tuple[int, float] | None:
        """Rank legacy hard-pass progress, even before it passes.

        The first tuple element prefers more hard-pass metrics already passing.
        The second is the worst signed margin to target, so for a single
        ``A0_diff_db >= 50`` hard-pass metric higher gain always wins even
        when no iteration reaches 50 dB.
        """
        if not self._legacy_hard_pass_bounds:
            return None
        margins: list[float] = []
        passed = 0
        for metric_name, bound in sorted(self._legacy_hard_pass_bounds.items()):
            value = _coerce_numeric_scalar((measurements or {}).get(metric_name))
            if value is None:
                return None
            if _bound_passes(value, bound):
                passed += 1
            if bound.op in (">=", ">"):
                margins.append(value - bound.value)
            elif bound.op in ("<=", "<"):
                margins.append(bound.value - value)
        if not margins:
            return None
        return (passed, min(margins))

    # ------------------------------------------------------------------ #
    #  Waveform display (best-effort)
    # ------------------------------------------------------------------ #

    def _display_waveform(self, sim_result: dict) -> None:
        """Ask Virtuoso to show the transient waveform for this iteration.

        Pulls the differential net pair from the spec's
        ``signals[].kind == Vdiff`` entry and delegates to
        ``bridge.display_transient_waveform``.  No defaults — if the spec
        does not declare a Vdiff signal we skip the display (no circuit
        assumptions leak out of the spec).

        Best-effort: swallows any exception so a display glitch does not
        abort a healthy optimization run.
        """
        del sim_result  # intentionally unused
        psf_dir = self.bridge.last_results_dir
        if not psf_dir:
            logger.warning(
                "Waveform display skipped: no results dir available"
            )
            return
        if self.eval_block is None:
            logger.debug(
                "Waveform display skipped: no eval_block in spec"
            )
            return
        nets = spec_evaluator.extract_osc_signals(self.eval_block)
        if not nets or len(nets) != 2:
            logger.debug(
                "Waveform display skipped: spec declares no Vdiff signal"
            )
            return
        net_pos, net_neg = nets[0], nets[1]
        try:
            self.bridge.display_transient_waveform(psf_dir, net_pos, net_neg)
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            logger.warning(
                "Waveform display failed (non-fatal): %s: %s",
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------------------ #
    #  Converged-value banner
    # ------------------------------------------------------------------ #

    def _log_final_converged_values(self, design_vars: dict) -> None:
        """Log converged design variables with a prominent banner.

        Delegates to SafeBridge._log_manual_sync_table with a
        "FINAL CONVERGED VALUES" banner so the table is easy to grep
        in long log streams.
        """
        from .safe_bridge import SafeBridge

        SafeBridge._log_manual_sync_table(
            design_vars,
            banner="FINAL CONVERGED VALUES",
            scope_lib=self.bridge._scope_lib if hasattr(self.bridge, "_scope_lib") else None,
            scope_tb_cell=self.bridge._scope_tb_cell if hasattr(self.bridge, "_scope_tb_cell") else None,
        )

    # ------------------------------------------------------------------ #
    #  Path-2 (2026-05-19) — sweep-phase evaluator
    # ------------------------------------------------------------------ #

    def _tuning_all_unmeasurable(self, reason: str) -> tuple[dict, dict]:
        """Synthesize UNMEASURABLE verdicts for every declared tuning
        metric — used when the sweep phase fails before any per-point
        data is available."""
        if not self.eval_block:
            return {}, {}
        names = [
            t.get("name") for t in (self.eval_block.get("tuning_metrics") or [])
            if t.get("name")
        ]
        return (
            {n: None for n in names},
            {n: f"UNMEASURABLE ({reason})" for n in names},
        )

    def _derive_sweep_entries(self) -> list[dict]:
        """Derive the per-point manifest from spec.md §6.1 ``sweep:``.

        Returns ``[{"point": <int 1..N>, "vctrl": <float>}, ...]`` with
        N = ``sweep.points`` and vctrl evenly distributed between
        ``range[0]`` and ``range[1]`` inclusive. Raises ``ValueError``
        if the spec carries no usable ``sweep`` block (caller has
        already vetted ``self.eval_block`` is present).
        """
        sweep = (self.eval_block or {}).get("sweep")
        if not isinstance(sweep, dict):
            raise ValueError("spec has no `sweep:` block")
        lo = float(sweep["range"][0])
        hi = float(sweep["range"][1])
        n = int(sweep["points"])
        if n < 2:
            raise ValueError("sweep.points must be >= 2")
        step = (hi - lo) / (n - 1)
        return [
            {"point": i + 1, "vctrl": lo + i * step}
            for i in range(n)
        ]

    @staticmethod
    def _manifest_matches_spec(
        existing: dict[int, float], derived: list[dict],
    ) -> bool:
        """Return True iff the existing manifest aligns with the
        spec-derived entries.

        Maestro may run sweep points in a shuffled order, so the manifest's
        point->Vctrl mapping is authoritative for point identity. For
        compatibility with hand-authored shuffled manifests, accept any
        mapping whose point set is exactly the spec-derived point set and whose
        Vctrl multiset matches the spec grid within 1e-9 rel / 1e-12 abs.
        """
        if len(existing) != len(derived):
            return False
        derived_points = {entry["point"] for entry in derived}
        if set(existing) != derived_points:
            return False
        existing_vctrls = sorted(float(v) for v in existing.values())
        derived_vctrls = sorted(float(entry["vctrl"]) for entry in derived)
        for got, want in zip(existing_vctrls, derived_vctrls):
            if not math.isclose(
                got, want,
                rel_tol=1e-9, abs_tol=1e-12,
            ):
                return False
        return True

    @staticmethod
    def _manifest_subset_for_spec(
        existing: dict[int, float], derived: list[dict],
    ) -> dict[int, float] | None:
        """Return a spec-grid subset when an existing manifest is a superset.

        Older ADE runs may contain a wider sweep, e.g. a 9-point
        ``0.0..0.8`` V sweep while the current spec asks for the inner
        7-point ``0.1..0.7`` V curve. In that case we can reuse the
        manifest without overwriting the hand-authored file: select one
        manifest point per spec Vctrl value and ignore extra endpoints.
        Return ``None`` when any spec point is missing.
        """
        selected: dict[int, float] = {}
        used_points: set[int] = set()
        existing_items = sorted(existing.items())
        for entry in sorted(derived, key=lambda e: float(e["vctrl"])):
            want = float(entry["vctrl"])
            match: tuple[int, float] | None = None
            for point, got_raw in existing_items:
                if point in used_points:
                    continue
                got = float(got_raw)
                if math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12):
                    match = (point, got)
                    break
            if match is None:
                return None
            point, got = match
            selected[point] = got
            used_points.add(point)
        return dict(sorted(selected.items()))

    def _resolve_maestro_setup_test(
        self,
        *,
        tb_cell: str,
        maestro_test: str | None,
    ) -> str | None:
        """Return the ADE test row to use for Maestro setup sync.

        ``tb_cell`` remains the testbench cell for OCEAN and final writeback.
        Maestro setup writers need an ADE test-row name, which may differ from
        the testbench cell (for example ``pll_LC_VCO_tb_1``).
        """
        if maestro_test is not None:
            return self.bridge._resolve_maestro_test(maestro_test)
        try:
            tests = self.bridge._list_remote_maestro_tests()
        except Exception as exc:  # noqa: BLE001 - setup sync is optional
            logger.warning(
                "Maestro setup sync skipped: could not list ADE tests "
                "(%s: %s). Pass --maestro-test to select one explicitly.",
                type(exc).__name__, exc,
            )
            return None
        if not isinstance(tests, (set, list, tuple)):
            logger.warning(
                "Maestro setup sync skipped: ADE test probe returned %s; "
                "pass --maestro-test to select one explicitly.",
                type(tests).__name__,
            )
            return None
        test_set = {t for t in tests if isinstance(t, str)}
        if tb_cell in test_set:
            logger.info("Maestro setup sync using ADE test row: %s", tb_cell)
            return tb_cell
        if len(test_set) == 1:
            resolved = next(iter(test_set))
            logger.info(
                "Maestro setup sync using sole ADE test row %s "
                "(tb_cell is %s).",
                resolved, tb_cell,
            )
            return resolved
        logger.warning(
            "Maestro setup sync skipped: tb_cell %s is not an ADE test row "
            "and ADE tests are %s. Pass --maestro-test to select one.",
            tb_cell, sorted(test_set),
        )
        return None

    def _ensure_sweep_manifest(self, sweep_root: str) -> str | None:
        """Path-2 (2026-05-19): make sure ``.tuning_manifest.json``
        exists at ``sweep_root`` and matches spec.md §6.1.

        - No ``sweep:`` block in spec → no-op (legacy single-point
          flow, manifest authoring not applicable).
        - Manifest already on disk → validate it against the derived
          entries. Mismatch returns a string reason (caller aborts via
          UNMEASURABLE); match returns None (caller proceeds to read).
        - Manifest missing → derive + write it. Failure to write
          returns a string reason.

        Returning ``None`` means "manifest is ready, go read it."
        """
        if not self.eval_block or "sweep" not in self.eval_block:
            return None
        try:
            entries = self._derive_sweep_entries()
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Sweep manifest derive failed: %s", exc)
            return "manifest_derive_failed"

        try:
            existing = self.bridge.read_sweep_manifest(sweep_root)
        except (RuntimeError, ValueError) as exc:
            # File missing is the common case — fall through to write.
            # Other failures (skill error, malformed JSON on disk) also
            # land here; we still try to author a clean copy.
            logger.info(
                "Sweep manifest not readable (%s); authoring from spec.",
                exc,
            )
            existing = None

        if existing is not None:
            if self._manifest_matches_spec(existing, entries):
                logger.info(
                    "Sweep manifest already present at %s (matches spec).",
                    sweep_root,
                )
                self._sweep_manifest_cache[sweep_root] = existing
                return None
            if len(existing) > len(entries):
                subset = self._manifest_subset_for_spec(existing, entries)
                if subset is not None:
                    logger.info(
                        "Sweep manifest at %s is a superset of the spec "
                        "grid; using %d/%d matching point(s).",
                        sweep_root, len(subset), len(existing),
                    )
                    self._sweep_manifest_cache[sweep_root] = subset
                    return None
            logger.warning(
                "Sweep manifest at %s does not match spec sweep block; "
                "refusing to overwrite hand-written manifest.",
                sweep_root,
            )
            return "manifest_mismatch"

        try:
            count = self.bridge.write_sweep_manifest(sweep_root, entries)
        except (RuntimeError, ValueError) as exc:
            logger.warning("Sweep manifest write failed: %s", exc)
            return "manifest_write_failed"
        logger.info(
            "Wrote sweep manifest at %s (%d points).",
            sweep_root, count,
        )
        # Drop any stale cache so the subsequent read sees the new file.
        self._sweep_manifest_cache.pop(sweep_root, None)
        return None

    def _run_sweep_phase(
        self,
        *,
        sweep_results_root: str,
        tb_cell: str,
        result_test: str | None = None,
        lib: str | None = None,
        cell: str | None = None,
        design_vars: dict[str, Any] | None = None,
        analyses: list[Any] | None = None,
    ) -> tuple[dict, dict]:
        """Compute the spec's ``tuning_metrics`` over the sweep points.

        Preferred pipeline: read manifest → sort by Vctrl → re-run OCEAN
        for each point using the current design vars → dump_all →
        per-point ``evaluate(...)`` for the §2 metrics → ``evaluate_swept``.

        Legacy pipeline, used when current-run context is not supplied:
        read manifest → sort by Vctrl → read existing per-point PSFs →
        per-point ``evaluate(...)`` for the §2 metrics → ``evaluate_swept``
        for the tuning metrics. Any per-point dump failure leaves an
        empty measurements dict for that point — ``evaluate_swept``
        handles missing values via UNMEASURABLE on the affected ops.
        Infrastructure failures (manifest unreadable, swept-dump SKILL
        error) collapse to all-UNMEASURABLE so the agent can decide to
        retry rather than crash.
        """
        # Path-3 prep (2026-05-24, R2 codex fix #2): clear the curve
        # cache at entry, BEFORE any early-return path, so a sweep that
        # fails at the manifest / dump / evaluate stage cannot leave the
        # previous successful sweep's curve state in place for the
        # optional searcher to consume.
        self._last_sweep_curve_state = None
        if not self.eval_block:
            return {}, {}
        ensure_reason = self._ensure_sweep_manifest(sweep_results_root)
        if ensure_reason is not None:
            return self._tuning_all_unmeasurable(ensure_reason)
        manifest = self._sweep_manifest_cache.get(sweep_results_root)
        if manifest is None:
            try:
                manifest = self.bridge.read_sweep_manifest(sweep_results_root)
            except (RuntimeError, ValueError) as exc:
                logger.warning("Sweep manifest read failed: %s", exc)
                return self._tuning_all_unmeasurable("manifest_read_failed")
            if not manifest:
                logger.warning("Sweep manifest is empty.")
                return self._tuning_all_unmeasurable("manifest_empty")
            self._sweep_manifest_cache[sweep_results_root] = manifest

        # Sort by Vctrl ascending so evaluate_swept's segment-slope op
        # walks monotonically along the x-axis even when the underlying
        # Maestro point ordering is shuffled (it usually is, per
        # _ocean_tuning_extract.ocn).
        points_sorted = sorted(manifest.items(), key=lambda kv: kv[1])
        points = [p for p, _ in points_sorted]
        vctrls = [v for _, v in points_sorted]

        signals, windows = spec_evaluator.build_dump_spec(self.eval_block)
        base_measurements_per_point: list[dict] = []
        if lib and cell and design_vars is not None:
            sweep_cfg = self.eval_block.get("sweep") or {}
            sweep_var = str(sweep_cfg.get("variable") or "Vctrl")
            osc_signals = spec_evaluator.extract_osc_signals(self.eval_block)
            for point, vctrl in points_sorted:
                point_vars = dict(design_vars)
                point_vars[sweep_var] = vctrl
                try:
                    self.bridge.run_ocean_sim(
                        lib=lib,
                        cell=cell,
                        tb_cell=tb_cell,
                        design_vars=point_vars,
                        analyses=analyses,
                    )
                    psf_dir = self.bridge.last_results_dir
                    if not psf_dir:
                        logger.warning(
                            "Fresh sweep point %d has no resultsDir.",
                            point,
                        )
                        base_measurements_per_point.append({})
                        continue
                    dump_result = self.ocean_worker.dump_all(
                        psf_dir=psf_dir,
                        signals=signals,
                        windows=windows,
                        osc_signals=osc_signals,
                    )
                    if dump_result.get("degenerate"):
                        logger.warning(
                            "Fresh sweep point %d skipped by osc_gate: %s",
                            point, dump_result.get("reason"),
                        )
                        base_measurements_per_point.append({})
                        continue
                    dumps = dump_result.get("dumps") or {}
                    meas, _ = spec_evaluator.evaluate(
                        self.eval_block, dumps, bridge=self.bridge,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Fresh sweep point %d at %s=%s failed: %s",
                        point, sweep_var, vctrl, exc,
                    )
                    meas = {}
                base_measurements_per_point.append(meas)
        else:
            try:
                per_point_dumps = self.bridge.run_ocean_dump_all_swept(
                    signals, windows,
                    sweep_root=sweep_results_root,
                    points=points,
                    tb_cell=tb_cell,
                    result_test=result_test,
                )
            except (RuntimeError, ValueError) as exc:
                logger.warning("Sweep dump_all failed: %s", exc)
                return self._tuning_all_unmeasurable("dump_failed")

            for point in points:
                dump = per_point_dumps.get(point)
                if not isinstance(dump, dict) or not dump.get("ok"):
                    base_measurements_per_point.append({})
                    continue
                dump_payload = dump.get("dumps") if isinstance(
                    dump.get("dumps"), dict
                ) else dump
                try:
                    meas, _ = spec_evaluator.evaluate(
                        self.eval_block, dump_payload, bridge=self.bridge,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Sweep point %d evaluate() raised: %s", point, exc,
                    )
                    meas = {}
                base_measurements_per_point.append(meas)

        # Path-3 prep (2026-05-24): stash per-point + vctrl arrays so
        # the optional curve searcher (off by default; opted in via
        # `run(curve_searcher_enabled=True)`) can build its summary
        # without re-reading the manifest or re-running the sweep.
        # Side-effect only — public return shape is unchanged.
        self._last_sweep_curve_state = {
            "vctrls": list(vctrls),
            "base_per_point": list(base_measurements_per_point),
        }
        try:
            return spec_evaluator.evaluate_swept(
                self.eval_block,
                base_measurements_per_point,
                vctrls,
                bridge=self.bridge,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("evaluate_swept raised: %s", exc)
            return self._tuning_all_unmeasurable("evaluate_swept_failed")

    # ------------------------------------------------------------------ #
    #  Curve searcher prompt section (Path-3 prep, 2026-05-24)
    # ------------------------------------------------------------------ #

    def _build_curve_searcher_section(
        self,
        *,
        tuning_measurements: dict,
        tuning_pass_fail: dict,
        design_vars: dict,
        prev_design_vars: dict,
        prev_tuning_measurements: dict,
        max_candidates: int,
    ) -> str:
        """Return a Markdown summary of the f-Vctrl curve and ranked
        candidate edits for the next-iter prompt, or "" when there is
        no usable curve state or the safety gate rejects the rendered
        text.

        R2 codex fix #2: requires `_last_sweep_curve_state` to have
        been populated by the *current* iteration's `_run_sweep_phase`
        call. The cache is cleared at every `_run_sweep_phase` entry
        and at every `run()` start so a stale curve from a prior
        successful sweep cannot bleed into a later prompt.

        R2 codex fix #3 (defense-in-depth): even though
        `curve_searcher.build_summary` already passes through
        `assert_no_foundry_leak`, re-assert against the rendered text
        here so any future code path that adds caller-supplied content
        is also gated before reaching the LLM.
        """
        state = self._last_sweep_curve_state
        if state is None:
            return ""
        try:
            summary = curve_searcher.build_summary(
                vctrl_values=state["vctrls"],
                base_measurements_per_point=state["base_per_point"],
                tuning_measurements=tuning_measurements,
                tuning_pass_fail=tuning_pass_fail,
                design_vars=design_vars,
                prev_design_vars=prev_design_vars,
                prev_tuning_measurements=prev_tuning_measurements,
                max_candidates=max_candidates,
            )
            md = summary.to_markdown()
            curve_searcher.assert_no_foundry_leak(md)
            return md
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "curve_searcher build/sanitize failed (%s); skipping "
                "summary this iter.",
                exc,
            )
            return ""

    # ------------------------------------------------------------------ #
    #  Writeback (final Maestro state)
    # ------------------------------------------------------------------ #

    def _run_writeback(self, design_vars: dict) -> str:
        """Push final design_vars into the Maestro setup for the scoped testbench cell.

        Per user directive Q2: if the writeback fails (e.g. no matching
        Maestro session is open) the agent MUST still return the metrics
        report — it only surfaces the failure via ``writeback_status``.
        """
        if not design_vars:
            return "skipped"
        try:
            result = self.bridge.write_and_save_maestro(design_vars)
        except (RuntimeError, ValueError, ConnectionError) as exc:
            logger.warning(
                "Maestro writeback failed (%s); metrics report still valid. "
                "Open Maestro for the scoped testbench cell and rerun if needed.",
                type(exc).__name__,
            )
            return f"failed: {type(exc).__name__}"
        if not result.get("saved", False):
            # run_writeback's own defensive check; safe_bridge.write_and_save_maestro
            # already raises RuntimeError on saved=False, but be explicit.
            return "failed: saved=False"
        return "ok"

    # ------------------------------------------------------------------ #
    #  History formatting (for LLM context)
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  Topology formatter
    # ------------------------------------------------------------------ #
    # Retained as @staticmethod because `scripts/read_schematic.py` (the
    # user's standalone "pre-flight" tool that reads a schematic and
    # writes the sanitized topology to Markdown) imports it. Stage 1
    # rev 2's agent-loop no longer calls this in its first-turn prompt
    # — spec §1 embeds the topology narrative, and `run_agent.py`
    # already passes the full spec JSON to the LLM. The preflight tool
    # and the optimization loop are two distinct code paths per the
    # user's design ("开始先用我们目前的脚本读出指定位置的schematic …
    # 下面跑自动化流程").

    @staticmethod
    def _format_topology(circuit: dict) -> str:
        """Format sanitized circuit data into LLM-friendly Markdown."""
        lines = ["### Instances"]
        for inst in circuit.get("instances", []):
            name = inst.get("instName", inst.get("name", "?"))
            cell = inst.get("cell", "?")
            params = inst.get("params", {})
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())
            lines.append(f"- **{name}** (GENERIC_PDK/{cell}): {param_str}")

            # Per-instance net connections (SKILL output format)
            nets = inst.get("nets", {})
            if isinstance(nets, dict) and nets:
                net_str = ", ".join(
                    f"{pin}->{net}" for pin, net in nets.items()
                )
                lines.append(f"  Connections: {net_str}")

        # Top-level pins
        pins = circuit.get("pins", [])
        if pins:
            lines.append("\n### Pins")
            for pin in pins:
                if isinstance(pin, dict):
                    pname = pin.get("name", "?")
                    pdir = pin.get("direction", "")
                    lines.append(f"- {pname} ({pdir})" if pdir else f"- {pname}")
                else:
                    lines.append(f"- {pin}")

        return "\n".join(lines)

    def _format_history_brief(self) -> str:
        """Format optimization history as a brief summary."""
        if not self.history:
            return "No previous iterations."
        lines = []
        for rec in self.history[-5:]:
            status = "MET" if rec.meets_spec else "NOT MET"
            brief = {
                k: f"{v:.4g}" if isinstance(v, (int, float)) else str(v)
                for k, v in rec.measurements.items()
            }
            lines.append(
                f"- Iter {rec.iteration} [{status}] vars={rec.design_vars} "
                f"metrics={brief}"
            )
        return "\n".join(lines)

    def get_optimization_report(self) -> str:
        """Generate a full optimization history report."""
        lines = ["# Optimization Report", ""]
        for rec in self.history:
            status = "PASS" if rec.meets_spec else "FAIL"
            lines.append(f"## Iteration {rec.iteration} [{status}]")
            lines.append(f"design_vars: {rec.design_vars}")
            lines.append("measurements:")
            for key, value in rec.measurements.items():
                verdict = rec.pass_fail.get(key, "")
                lines.append(f"  - {key}: {value} {verdict}")
            if rec.llm_reasoning:
                lines.append(f"reasoning: {rec.llm_reasoning}")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
#  Module-level helpers
# ---------------------------------------------------------------------- #


def _auto_perturb_ibias(
    design_vars: dict, factor: float = 2.0,
) -> tuple[bool, list[str]]:
    """Bug 2 anti-replay (rev 11, 2026-04-20).

    When the LLM repeats identical design_vars after a diagnostic
    failure, mutate the Ibias key in place by ``factor`` so the next
    iter at least visits a new point. Handles the usual SPICE SI
    suffixes (``u``/``n``/``p``/``m``/``k``) and bare numeric strings;
    leaves the dict untouched if no Ibias-like key exists or the value
    cannot be parsed. Clamped to 1 mA upper bound.

    Returns ``(applied, perturbed_keys)`` so the caller can tell
    whether the perturbation actually changed anything.
    """
    candidate_keys = [k for k in design_vars
                      if isinstance(k, str) and k.lower().startswith("ibias")]
    if not candidate_keys:
        return False, []
    key = candidate_keys[0]
    raw = str(design_vars[key]).strip()
    suffix_map = {
        "k": 1e3, "m": 1e-3, "u": 1e-6, "n": 1e-9,
        "p": 1e-12, "f": 1e-15, "g": 1e9,
    }
    suffix = ""
    numeric = raw
    if raw and raw[-1].lower() in suffix_map:
        suffix = raw[-1]
        numeric = raw[:-1]
    try:
        val = float(numeric)
    except ValueError:
        return False, []
    scale = suffix_map.get(suffix.lower(), 1.0)
    val_A = val * scale
    new_A = min(val_A * factor, 1e-3)  # cap at 1 mA
    # Re-emit in the original suffix family to keep formatting stable.
    if suffix:
        design_vars[key] = f"{new_A / scale:g}{suffix}"
    else:
        design_vars[key] = f"{new_A:g}"
    return True, [key]


def _coerce_float(value: Any) -> float | None:
    """Best-effort numeric extraction for SAFEGUARD checks."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _coerce_numeric_scalar(value: Any) -> float | None:
    """Best-effort scalar extraction for measurement pass/fail gates.

    Unlike ``_coerce_float`` this accepts strings with a trailing unit such as
    ``"51.3 dB"``. It still refuses non-scalar containers so waveform arrays
    cannot accidentally be reduced by stringification.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        return val if math.isfinite(val) else None
    if isinstance(value, (list, tuple, dict)):
        return None
    text = str(value).strip()
    match = re.match(
        r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text,
    )
    if not match:
        return None
    try:
        val = float(match.group(0))
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def _normalize_bound_unit(unit: str | None) -> str:
    if not unit:
        return ""
    unit = unit.strip()
    return unit.replace("μ", "u").replace("µ", "u")


def _scaled_bound_value(value: float, unit: str) -> float:
    """Scale frequency units to base Hz; leave other units in-place."""
    scale = {
        "hz": 1.0,
        "khz": 1e3,
        "mhz": 1e6,
        "ghz": 1e9,
    }.get(unit.lower(), 1.0)
    return value * scale


def _parse_numeric_bound(
    text: str,
    *,
    metric_hint: str | None = None,
) -> _NumericHardPassBound | None:
    match = _NUMERIC_BOUND_RE.search(text or "")
    if not match:
        return None
    metric = metric_hint or match.group("metric")
    if not metric:
        return None
    unit = _normalize_bound_unit(match.group("unit"))
    try:
        value = float(match.group("value"))
    except (TypeError, ValueError):
        return None
    return _NumericHardPassBound(
        metric=metric,
        op=match.group("op"),
        value=_scaled_bound_value(value, unit),
        unit=unit,
    )


def _strip_md_cell(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        text = text[1:-1]
    return text.strip()


def _extract_legacy_hard_pass_bounds(
    spec: dict | str,
) -> dict[str, _NumericHardPassBound]:
    """Extract explicitly marked numeric hard-pass bounds from Markdown.

    This is a legacy-path guard for specs that do not yet carry the YAML
    ``signals/windows/metrics`` eval block. It intentionally ignores ordinary
    target rows unless the row/sentence marks them as hard pass, so secondary
    diagnostics do not become convergence gates by accident.
    """
    if not isinstance(spec, str):
        return {}

    bounds: dict[str, _NumericHardPassBound] = {}
    for raw_line in spec.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if "hard pass" not in lower and "hard-pass" not in lower:
            continue

        if line.startswith("|") and "|" in line[1:]:
            cells = [_strip_md_cell(c) for c in line.strip("|").split("|")]
            if len(cells) >= 3:
                metric = cells[0]
                target = cells[2]
                bound = _parse_numeric_bound(target, metric_hint=metric)
                if bound:
                    bounds[bound.metric] = bound

        for code_text in _CODE_SPAN_RE.findall(line):
            bound = _parse_numeric_bound(code_text)
            if bound:
                bounds[bound.metric] = bound

    return bounds


def _bound_passes(value: float, bound: _NumericHardPassBound) -> bool:
    if bound.op == ">=":
        return value >= bound.value
    if bound.op == ">":
        return value > bound.value
    if bound.op == "<=":
        return value <= bound.value
    if bound.op == "<":
        return value < bound.value
    return False


def _format_numeric_bound_verdict(
    value: float | None,
    bound: _NumericHardPassBound,
) -> str:
    if value is None:
        return f"UNMEASURABLE (no numeric value; target {bound.target_text})"
    if _bound_passes(value, bound):
        return f"PASS (value {value:g}, target {bound.target_text})"
    return f"FAIL (value {value:g}, target {bound.target_text})"


def _enforce_legacy_hard_pass_bounds(
    measurements: dict,
    pass_fail: dict,
    bounds: dict[str, _NumericHardPassBound],
) -> dict:
    enforced = dict(pass_fail or {})
    for metric, bound in bounds.items():
        value = _coerce_numeric_scalar((measurements or {}).get(metric))
        enforced[metric] = _format_numeric_bound_verdict(value, bound)
    return enforced


def _format_tuning_summary(
    tuning_measurements: dict, tuning_pass_fail: dict,
) -> str:
    """Path-2: render the most recent sweep-phase verdicts so the LLM
    sees WHY the loop kept going after single-point PASS. Skipped
    entirely (returns "") when no tuning data is available — single-
    point runs stay free of dead sections."""
    if not tuning_pass_fail:
        return ""
    rows: list[str] = ["## Tuning-curve verdicts (sweep)"]
    for name, verdict in tuning_pass_fail.items():
        value = tuning_measurements.get(name)
        rows.append(f"  - {name} = {value!s}  → {verdict}")
    rows.append(
        "Note: §2 metrics all PASS at the converged design vars; the "
        "spec's `tuning_metrics:` band failed. Re-tune varactor/LC "
        "sizing so the f–Vctrl curve flattens (Kvco band) AND covers "
        "the required range, without losing the single-point PASS."
    )
    return "\n".join(rows) + "\n\n"


def _format_eval_summary(
    measurements: dict, pass_fail: dict, dumps: dict,
    diagnostic: "IterationDiagnostic | None" = None,
) -> str:
    """Render the PC-evaluated measurements + raw dump for the LLM.

    Rev 11 (Bug 2/4, 2026-04-20) prepends a diagnostic block when
    ``diagnostic.has_failure`` so the LLM sees an explicit machine-
    readable failure label (``dump_timeout`` / ``no_saved_outputs`` /
    ``unknown``) rather than silently empty metrics. This stopped the
    iter 9→10 parameter-replay pattern observed in
    ``run_20260420_033152.log``.

    Sub-sections:
      0. ``Iteration diagnostic`` (conditional) — failure surface.
      1. ``Metrics`` — name, value, verdict (one line per metric).
      2. ``Raw signal stats`` — per-signal, per-window numerics so the
         LLM can reason about DC operating points when oscillation
         metrics are null (f_osc=null / duty=null is typical for a
         stuck-at-equilibrium tank).
    """
    lines: list[str] = []
    if diagnostic is not None and diagnostic.has_failure:
        lines.append("### Iteration diagnostic (FAILURE — read first)")
        lines.append(f"- dump_status: {diagnostic.dump_status}")
        if diagnostic.dump_raw_error:
            lines.append(f"- dump_raw_error: {diagnostic.dump_raw_error}")
        if not diagnostic.op_point_available:
            lines.append(
                "- op_point: UNAVAILABLE (safeReadOpPointAfterTran "
                "returned no devices — tranOp sub-result likely missing)"
            )
        if (
            not diagnostic.op_point_available
            and lines
            and lines[-1].startswith("- op_point: UNAVAILABLE")
        ):
            lines[-1] = (
                "- op_point: UNAVAILABLE (DC operating-point readback "
                "failed or returned no safe rows)"
            )
        if not diagnostic.ic_patch_applied:
            lines.append(
                f"- ic_patch_applied: NO — next iter starts from the "
                f"SAME initial condition as this one "
                f"(reason: {diagnostic.ic_patch_reason or 'unknown'}). "
                f"Parameter changes alone may not break the symmetric "
                f"equilibrium."
            )
        if diagnostic.dump_status == DumpStatus.TIMEOUT:
            lines.append(
                "- interpretation: the OCEAN dump subprocess exceeded "
                "its wall-clock budget and was killed. This is strongly "
                "correlated with a non-periodic or stuck-at-DC waveform "
                "(safeOceanDumpAll loops forever on a degenerate PSF). "
                "DO NOT repeat the same design_vars — propose a "
                "materially different delta on a P1-priority variable "
                "from the spec's design-variable table."
            )
        elif diagnostic.dump_status == DumpStatus.NO_SAVED_OUTPUTS:
            lines.append(
                "- interpretation: OCEAN ran but VT()/IT() on the "
                "probed paths returned nil. The testbench may have "
                "dropped saved outputs, or the tran stopped early."
            )
        elif diagnostic.dump_status == DumpStatus.SIM_FAILED:
            lines.append(
                "- interpretation: Spectre/OCEAN did not produce the "
                "requested analysis results, usually because the DC "
                "operating point failed or the analysis terminated early. "
                "Back off to a more conservative bias/sizing point before "
                "trying aggressive gain or bandwidth moves."
            )
        lines.append(
            "- action: propose a NEW design_vars delta. If you repeat "
            "the exact previous design_vars the agent auto-perturbs a "
            "current-source variable (key beginning with `Ibias`) ×2 "
            "and flags the turn as wasted."
        )
        lines.append("")

    lines.append("### Metrics")
    if not measurements:
        lines.append("- (no measurements — dump call failed this iter)")
    for key, value in measurements.items():
        verdict = pass_fail.get(key, "")
        if isinstance(value, (int, float)):
            lines.append(f"- {key}: {value:.6g} {verdict}")
        else:
            lines.append(f"- {key}: {value} {verdict}")
    lines.append("")
    lines.append("### Raw signal stats (SKILL dump)")
    if not dumps:
        lines.append("- (no dump — SKILL call failed; see agent log)")
    try:
        lines.append("```json")
        lines.append(json.dumps(dumps, indent=2, default=str))
        lines.append("```")
    except (TypeError, ValueError):
        lines.append(repr(dumps))
    return "\n".join(lines)


def _fmt_si(value: Any, unit: str = "") -> str:
    """Render a scalar with SI prefix suffix for compact LLM tables.

    Falls back to repr when the value isn't a usable number. Deliberately
    keeps 3 sig figs — the LLM reads magnitude and order-of-magnitude,
    not per-digit precision.
    """
    if value is None or not isinstance(value, (int, float)) or isinstance(
            value, bool):
        return "-"
    if not math.isfinite(value):
        return "-"
    if value == 0:
        return f"0{unit}"
    ax = abs(value)
    for pref, scale in (("G", 1e9), ("M", 1e6), ("k", 1e3),
                        ("", 1.0),
                        ("m", 1e-3), ("u", 1e-6), ("n", 1e-9),
                        ("p", 1e-12), ("f", 1e-15)):
        if ax >= scale:
            return f"{value / scale:.3g}{pref}{unit}"
    return f"{value:.3g}{unit}"


_ENG_FLOAT_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*"
    r"([A-Za-z]*)\s*$"
)
_ENG_SUFFIX_SCALE = {
    "": 1.0,
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "meg": 1e6,
    "g": 1e9,
    "t": 1e12,
}


def _parse_engineering_float(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if math.isfinite(value) else None
    if not isinstance(raw, str):
        return None
    match = _ENG_FLOAT_RE.fullmatch(raw)
    if not match:
        return None
    suffix = match.group(2)
    suffix_key = suffix if suffix == "M" else suffix.lower()
    if suffix_key == "M":
        suffix_key = "meg"
    scale = _ENG_SUFFIX_SCALE.get(suffix_key)
    if scale is None:
        return None
    value = float(match.group(1)) * scale
    return value if math.isfinite(value) else None


def _guard_llm_feedback(text: str, *, context: str) -> str:
    """Return LLM feedback text only if the final sensitive-token gate passes."""
    try:
        return assert_llm_feedback_safe(text, context=context)
    except ValueError as exc:
        logger.error("Withholding LLM feedback block: %s", exc)
        return f"(withheld: {context} failed sensitive-token scan)"


_OP_POINT_INST_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OP_POINT_MOS_INST_RE = re.compile(r"^[Mm][A-Za-z0-9_]*$")
_OP_POINT_NET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_!]*$")
_OP_POINT_NET_PATH_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_!]*/)*[A-Za-z_][A-Za-z0-9_!]*$"
)
_DEFAULT_OP_POINT_NETS = (
    "Vout_p", "Vout_n", "Vin_p", "Vin_n",
    "net1", "net2", "net3", "net5", "net6", "net9",
    "p_bias", "cmfb_out",
)
_COMMON_OPAMP_DEVICE_ROLES = {
    "M0": "input pair",
    "M1": "input pair",
    "M2": "first-stage PMOS load",
    "M3": "first-stage PMOS load",
    "M4": "NMOS bias diode",
    "M5": "tail current source",
    "M6": "second-stage PMOS pull-up",
    "M8": "second-stage PMOS pull-up",
    "M7": "CMFB-controlled NMOS pull-down",
    "M9": "CMFB-controlled NMOS pull-down",
    "M10": "PMOS bias diode",
}


def _analysis_names(analyses: Iterable[Any] | None) -> set[str]:
    """Return normalized OCEAN analysis names from run_ocean_sim specs."""
    names: set[str] = set()
    for raw in analyses or []:
        if isinstance(raw, str):
            names.add(raw.lower())
            continue
        if isinstance(raw, dict):
            name = raw.get("name")
            if isinstance(name, str):
                names.add(name.lower())
            continue
        if isinstance(raw, (tuple, list)) and raw:
            names.add(str(raw[0]).lower())
    return names


def _analyses_use_dc_op(analyses: Iterable[Any] | None) -> bool:
    """True when current results should expose a plain dc operating point."""
    names = _analysis_names(analyses)
    return "dc" in names and "tran" not in names


def _op_point_probe_paths(
    schematic_instances: Iterable[dict] | None,
) -> tuple[list[str], list[str]]:
    """Build bounded, explicit current-run DC OP readback paths.

    ``read_circuit(lib, cell)`` reads the DUT cell, while Maestro/OCEAN
    results expose DUT devices under the testbench instance path ``/I0``.
    This mirrors the existing safeOceanMeasure default ``dut_path="/I0"``.
    """
    nets: list[str] = []
    seen_nets: set[str] = set()

    def add_net_path(name: str) -> None:
        if not _OP_POINT_NET_PATH_RE.fullmatch(name):
            return
        path = f"/{name}"
        if path in seen_nets:
            return
        if len(nets) >= 64:
            return
        nets.append(path)
        seen_nets.add(path)

    def add_net(raw: Any, *, include_dut_alias: bool = True) -> None:
        if not isinstance(raw, str):
            return
        name = raw.strip()
        if name.startswith("/"):
            name = name[1:]
        if not _OP_POINT_NET_PATH_RE.fullmatch(name):
            return
        add_net_path(name)
        # DUT-cell topology is read from the schematic cell, but PSF node
        # names may be either flattened (/net3) or kept under the testbench
        # DUT instance (/I0/net3). Probe both bounded aliases so the LLM can
        # see internal stage nodes without requiring PDK/netlist knowledge.
        if include_dut_alias and "/" not in name and name not in {"0"}:
            add_net_path(f"I0/{name}")

    def is_mos_instance(inst: dict, name: str) -> bool:
        cell = inst.get("cell")
        if isinstance(cell, str):
            cell_u = cell.upper()
            if cell_u.startswith(("NMOS", "PMOS")):
                return True
        return bool(_OP_POINT_MOS_INST_RE.fullmatch(name))

    for name in _DEFAULT_OP_POINT_NETS:
        add_net(name)

    instances: list[str] = []
    seen: set[str] = set()
    for inst in schematic_instances or []:
        if not isinstance(inst, dict):
            continue
        inst_nets = inst.get("nets")
        if isinstance(inst_nets, dict):
            for net_name in inst_nets.values():
                add_net(net_name)
        name = inst.get("instName") or inst.get("name")
        if not isinstance(name, str):
            continue
        if not _OP_POINT_INST_NAME_RE.fullmatch(name):
            continue
        if not is_mos_instance(inst, name):
            continue
        path = f"/I0/{name}"
        if path in seen:
            continue
        instances.append(path)
        seen.add(path)
        if len(instances) >= 128:
            break
    return nets, instances


def _op_point_instances(op_point: dict) -> dict:
    """Return the instance sub-dict from either wrapper or flat OP payload."""
    if not isinstance(op_point, dict):
        return {}
    instances = op_point.get("instances")
    if isinstance(instances, dict):
        return instances
    return op_point


def _enrich_op_point_terminal_biases(
    op_point: dict,
    schematic_instances: Iterable[dict] | None,
    live_vars: dict[str, Any] | None = None,
) -> None:
    """Derive vgs/vds/vbs from sanitized topology and DC node voltages.

    AC/DC PSF ``instance`` data can be sparse. Node voltages and net
    connectivity are already PDK-free, so terminal biases can be derived
    without touching model parameters. Mutates ``op_point`` in place.
    """
    if not isinstance(op_point, dict):
        return
    nodes = op_point.get("nodes") if isinstance(
        op_point.get("nodes"), dict
    ) else {}
    instances = op_point.get("instances") if isinstance(
        op_point.get("instances"), dict
    ) else op_point
    if not isinstance(nodes, dict) or not isinstance(instances, dict):
        return

    by_leaf: dict[str, float] = {}
    for path, raw in nodes.items():
        if not isinstance(path, str):
            continue
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            continue
        value = float(raw)
        if math.isfinite(value):
            by_leaf.setdefault(path.rsplit("/", 1)[-1], value)

    vicm = _parse_engineering_float((live_vars or {}).get("Vicm"))

    def net_value(raw_net: Any) -> float | None:
        if not isinstance(raw_net, str):
            return None
        net = raw_net.strip()
        if not net:
            return None
        if net in {"0", "gnd", "gnd!", "vss", "vss!"}:
            return 0.0
        if net in {"Vin_p", "Vin_n"} and vicm is not None:
            return vicm
        candidates: list[str] = []
        if net.startswith("/"):
            candidates.append(net)
        else:
            candidates.extend((f"/{net}", f"/I0/{net}"))
        for candidate in candidates:
            raw = nodes.get(candidate)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                value = float(raw)
                if math.isfinite(value):
                    return value
        return by_leaf.get(net)

    inst_by_name: dict[str, dict] = {}
    for inst in schematic_instances or []:
        if not isinstance(inst, dict):
            continue
        name = inst.get("instName") or inst.get("name")
        if isinstance(name, str):
            inst_by_name[name] = inst

    for path, params in instances.items():
        if not isinstance(path, str) or not isinstance(params, dict):
            continue
        inst = inst_by_name.get(path.rsplit("/", 1)[-1])
        if not isinstance(inst, dict):
            continue
        nets = inst.get("nets")
        if not isinstance(nets, dict):
            continue
        vg = net_value(nets.get("G"))
        vd = net_value(nets.get("D"))
        vs = net_value(nets.get("S"))
        vb = net_value(nets.get("B"))
        if vg is not None and vs is not None:
            params.setdefault("vgs", vg - vs)
        if vd is not None and vs is not None:
            params.setdefault("vds", vd - vs)
        if vb is not None and vs is not None:
            params.setdefault("vbs", vb - vs)

    issues = op_point.get("issues")
    if not isinstance(issues, list):
        return
    refreshed: list[Any] = []
    missing_re = re.compile(r"^(/[^ ]+) missing OP fields: ([A-Za-z0-9_,]+)$")
    for issue in issues:
        if not isinstance(issue, str):
            refreshed.append(issue)
            continue
        match = missing_re.fullmatch(issue)
        if not match:
            refreshed.append(issue)
            continue
        path = match.group(1)
        params = instances.get(path)
        if not isinstance(params, dict):
            refreshed.append(issue)
            continue
        missing = [
            key for key in match.group(2).split(",")
            if key and key not in params
        ]
        if missing:
            refreshed.append(
                f"{path} missing OP fields: {','.join(missing)}"
            )
    op_point["issues"] = refreshed


def _format_topology_with_live_vars(
    instances: list[dict], live_vars: dict,
) -> str:
    """Render cached schematic topology with current desVar values merged.

    Each instance becomes one line:
        - <instName> <CELL> G=<netA> D=<netB> S=<netC> B=<netD> | nfin=3 fingers=<desvar>[=40] l=20n w=106.00n

    When a param value (as a stripped string) matches a key in
    ``live_vars`` it is treated as a CDF desVar reference and annotated
    with ``[=<current_value>]``. Literal/numeric params (``l='20n'``,
    ``vdc='800.0m'``) pass through untouched. Returns an empty string
    when no instances are cached — the caller suppresses the section
    header in that case.
    """
    if not instances:
        return ""
    lines = []
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        name = inst.get("instName", inst.get("name", "?"))
        cell = inst.get("cell", "?")
        nets = inst.get("nets") or {}
        params = inst.get("params") or {}
        role = _COMMON_OPAMP_DEVICE_ROLES.get(str(name), "")
        role_part = f" role={role}" if role else ""
        net_parts = [
            f"{pin}={net}" for pin, net in nets.items()
        ] if isinstance(nets, dict) else []
        param_parts = []
        if isinstance(params, dict):
            for k, v in params.items():
                v_str = str(v).strip()
                if v_str in live_vars:
                    param_parts.append(
                        f"{k}={v_str}[={live_vars[v_str]}]"
                    )
                else:
                    param_parts.append(f"{k}={v_str}")
        net_str = " ".join(net_parts) if net_parts else "(no nets)"
        param_str = " ".join(param_parts) if param_parts else "(no params)"
        lines.append(f"- {name} {cell}{role_part} | {net_str} | {param_str}")
    return "\n".join(lines)


def _format_dc_node_diagnostics(nodes: dict[str, Any]) -> str:
    def val(*paths: str) -> float | None:
        for path in paths:
            raw = nodes.get(path)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                if math.isfinite(float(raw)):
                    return float(raw)
        return None

    diagnostics: list[tuple[str, float, str]] = []

    def add(name: str, value: float | None, unit: str = "V") -> None:
        if value is not None and math.isfinite(value):
            diagnostics.append((name, value, unit))

    vp = val("/Vout_p", "/I0/Vout_p")
    vn = val("/Vout_n", "/I0/Vout_n")
    if vp is not None and vn is not None:
        add("Vout_cm", 0.5 * (vp + vn))
        add("Vout_diff_dc", vp - vn)

    vnp = val("/Vin_p", "/I0/Vin_p")
    vnn = val("/Vin_n", "/I0/Vin_n")
    if vnp is not None and vnn is not None:
        add("Vin_cm", 0.5 * (vnp + vnn))
        add("Vin_diff_dc", vnp - vnn)

    n3 = val("/net3", "/I0/net3")
    n6 = val("/net6", "/I0/net6")
    if n3 is not None and n6 is not None:
        add("first_stage_output_cm", 0.5 * (n3 + n6))
        add("first_stage_output_diff_dc", n3 - n6)

    sense = val("/net5", "/I0/net5")
    ref = val("/net1", "/I0/net1")
    if sense is not None and ref is not None:
        add("cmfb_error", sense - ref)

    if not diagnostics:
        return ""
    lines = [
        "| Diagnostic | Value |",
        "|---|---|",
    ]
    for name, value, unit in diagnostics:
        lines.append(f"| {name} | {_fmt_si(value, unit)} |")
    return "### Derived DC diagnostics\n" + "\n".join(lines)


def _format_op_point_summary(op_point: dict) -> str:
    """Render per-device DC op-point as a compact Markdown table.

    ``op_point`` comes from ``bridge.read_op_point_after_tran()`` after
    ``_sanitize_op_point`` + ``_decorate_op_point``, so it is a flat
    ``{"/I0/Mx": {"vgs": ..., "region": ..., "region_label": ..., ...}}``
    with no PDK-proprietary keys. Missing fields render as "-".
    """
    if not isinstance(op_point, dict) or not op_point:
        return "(unavailable — op-point read returned empty or failed)"
    nodes = op_point.get("nodes") if isinstance(
        op_point.get("nodes"), dict,
    ) else {}
    instances = op_point.get("instances") if isinstance(
        op_point.get("instances"), dict,
    ) else op_point
    issues = op_point.get("issues") if isinstance(
        op_point.get("issues"), list,
    ) else []

    blocks: list[str] = []
    if nodes:
        node_lines = [
            "| Node | DC voltage |",
            "|---|---|",
        ]
        for node, value in nodes.items():
            node_lines.append(f"| {node} | {_fmt_si(value, 'V')} |")
        blocks.append("### DC node voltages\n" + "\n".join(node_lines))
        diag_block = _format_dc_node_diagnostics(nodes)
        if diag_block:
            blocks.append(diag_block)

    cols = [
        "vgs", "vds", "vov", "id", "gm", "gds",
        "vth", "vdsat", "cgs", "cgd",
    ]
    col_units = {"vgs": "V", "vds": "V", "vov": "V", "id": "A",
                 "gm": "S", "gds": "S", "vth": "V", "vdsat": "V",
                 "cgs": "F", "cgd": "F"}
    lines = [
        "| Inst | Region | " + " | ".join(cols) + " |",
        "|" + "|".join(["---"] * (2 + len(cols))) + "|",
    ]
    any_instance = False
    for inst_name, params in instances.items():
        if not isinstance(params, dict):
            continue
        any_instance = True
        region_int = params.get("region")
        region_lbl = params.get("region_label", "")
        if isinstance(region_int, (int, float)) and not isinstance(
                region_int, bool):
            region_cell = f"{region_lbl}({int(region_int)})"
        else:
            region_cell = "-"
        vals = [
            _fmt_si(params.get(col), col_units.get(col, "")) for col in cols
        ]
        lines.append(
            f"| {inst_name} | {region_cell} | " + " | ".join(vals) + " |"
        )
    if any_instance:
        blocks.append("### Device operating point\n" + "\n".join(lines))

    issue_lines = [
        f"- {issue}" for issue in issues if isinstance(issue, str)
    ]
    if issue_lines:
        blocks.append("### Readback issues\n" + "\n".join(issue_lines))

    if not blocks:
        return "(unavailable: op-point read returned no safe numeric data)"
    return "\n\n".join(blocks)


def _format_sim_summary(sim_result: dict) -> str:
    """Pretty-print the keys of an OCEAN result for the LLM.

    Stage 1 rev 3 (2026-04-18): the ``measurements`` sub-dict is now
    SKILL-computed (safeOceanMeasure) — authoritative. The LLM's job on
    the next turn is to (a) judge pass_fail against the spec §3 windows
    using these numbers and (b) propose a design_vars delta. It must
    NOT re-derive the metrics; the windows and formulas are already
    baked into SKILL. Keys that ``_scrub`` replaced with ``<path>`` stay
    redacted; that's fine.
    """
    try:
        return "```json\n" + json.dumps(sim_result, indent=2, default=str) + "\n```"
    except (TypeError, ValueError):
        return repr(sim_result)


# ====================================================================== #
#  HSpice closed-loop agent (T8.3, 2026-04-25)
# ====================================================================== #
#
#  ``HspiceAgent`` is the HSpice peer of ``CircuitAgent``: same
#  request/response protocol with the LLM, same JSON contract, but the
#  per-iteration mechanics swap out OCEAN/Maestro for HSpice over ssh.
#  No SafeBridge, no SKILL daemon, no Maestro writeback -- the only
#  remote state mutated between iterations is the ``.PARAM`` block of
#  one file (whichever ``hspice.param_rewrite_target`` names).
#
#  The class is deliberately self-contained and does NOT inherit from
#  ``CircuitAgent``: the two share the LLM transcript style and the
#  JSON envelope but nothing else, and forcing common machinery would
#  drag SafeBridge into HSpice runs that should not need it.
from .hspice_resolver import (     # noqa: E402
    EvaluationResult,
    HspiceMetricNotFoundError,
    evaluate_hspice,
)
from .hspice_worker import (       # noqa: E402
    HspiceRunResult,
    HspiceWorker,
    HspiceWorkerError,
    HspiceWorkerScriptError,
    HspiceWorkerSpawnError,
    HspiceWorkerTimeout,
)


_HSPICE_TARGETS: frozenset[str] = frozenset({"netlist", "testbench"})


def extract_hspice_spec_blocks(spec_text: str) -> tuple[list[dict], dict]:
    """Pull the HSpice-specific yaml fences out of a spec markdown.

    A spec for the HSpice backend carries two yaml fences:

      * a ``metrics:`` block (HSpice-only -- no signals/windows because
        ``.measure`` already encodes the time window inside the .sp);
      * an ``hspice:`` block carrying file paths, topcell name, options,
        and the ``param_rewrite_target`` selector.

    Returns ``(metrics, hspice_cfg)``. Raises ``ValueError`` if either
    fence is missing or malformed -- callers should treat this as a
    spec-author error and abort before any remote round-trip.
    """
    if not isinstance(spec_text, str):
        raise ValueError("spec_text must be a str")
    metrics: list[dict] | None = None
    hspice_cfg: dict | None = None
    for match in re.finditer(
        r"```(?:yaml|yml)\s*\n(.*?)\n```", spec_text,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        if metrics is None and isinstance(data.get("metrics"), list):
            metrics = data["metrics"]
        if hspice_cfg is None and isinstance(data.get("hspice"), dict):
            hspice_cfg = data["hspice"]
    if metrics is None:
        raise ValueError(
            "HSpice spec is missing a yaml fence with a 'metrics:' list"
        )
    if hspice_cfg is None:
        raise ValueError(
            "HSpice spec is missing a yaml fence with an 'hspice:' block"
        )
    target = hspice_cfg.get("param_rewrite_target", "testbench")
    if target not in _HSPICE_TARGETS:
        raise ValueError(
            f"hspice.param_rewrite_target must be one of "
            f"{sorted(_HSPICE_TARGETS)}; got {target!r}"
        )
    return metrics, hspice_cfg


@dataclass
class HspiceIterationRecord:
    iteration: int
    design_vars: dict
    measurements: dict
    pass_fail: dict
    meets_spec: bool
    llm_reasoning: str = ""
    timestamp: float = field(default_factory=time.time)


class HspiceAgent:
    """LLM-driven closed-loop optimizer for the HSpice backend.

    Per iteration:
      1. Parse the LLM's previous JSON response, validate against the
         spec's design-var whitelist + value-format rules.
      2. Merge new ``design_vars`` into ``accumulated_vars``.
      3. Patch the REMOTE param-rewrite-target .sp in place via
         :class:`src.remote_patch.RemotePatcher` (atomic remote write;
         one-time backup; rewrite executes on cobi -- the local
         scrubbed copy never goes back over ssh).
      4. Run HSpice on the remote testbench via the supplied
         :class:`HspiceWorker`.
      5. Resolve metrics through :func:`hspice_resolver.evaluate_hspice`.
      6. Build the next-turn LLM prompt with verdicts and history.

    Stop conditions (mirror :class:`CircuitAgent`):
      * ``None`` (converged=True) -- every metric PASS.
      * ``"max_iter"``           -- ran out of iterations.
      * ``"topology"``           -- ran out of iterations AND the last
                                    ``TOPOLOGY_SANITY_VIOLATION_LIMIT``
                                    consecutive iterations all produced
                                    a sanity-range UNMEASURABLE verdict;
                                    points at the topology / spec rather
                                    than parameter tuning.
      * ``"contract_violation"`` -- LLM emitted a bad schema after one
                                    repair attempt.
      * ``"hspice_failure"``     -- ssh / HSpice transport or script
                                    error that the worker raised.
      * ``"rewrite_failure"``    -- :class:`ParamRewriteError` from the
                                    .sp rewriter (e.g. LLM proposed a
                                    key the netlist's .PARAM block
                                    does not declare).
    """

    def __init__(
        self,
        llm: LLMClient,
        worker: HspiceWorker,
        spec_text: str,
        spec_metrics: list[dict],
        whitelist: Iterable[str],
        remote_target_path: str,
        remote_run_path: str,
    ) -> None:
        self.llm = llm
        self.worker = worker
        self.spec_text = spec_text
        self.spec_metrics = list(spec_metrics)
        # Whitelist is normalised to a lowercased frozenset -- the
        # rewriter and the contract validator both compare case-
        # insensitively, and a frozenset reads cleanly in error
        # messages and survives accidental mutation.
        self.whitelist: frozenset[str] = frozenset(
            str(k).lower() for k in whitelist
        )
        if not self.whitelist:
            raise ValueError(
                "HspiceAgent requires a non-empty design_vars whitelist"
            )
        self.remote_target_path = remote_target_path
        self.remote_run_path = remote_run_path
        # T8.3-fix: rewrite executes on the remote box (see
        # src.remote_patch). The local scrubbed copy is intentionally
        # NOT shipped back -- pushing it would overwrite the original
        # PDK file with `<redacted>` placeholders.
        self._remote_patcher = RemotePatcher(
            ssh_args=worker.cfg.ssh_base_args(),
            timeout_s=worker.cfg.ssh_connect_timeout_s + 30,
        )
        self.history: list[HspiceIterationRecord] = []

    # ------------------------------------------------------------------ #
    #  Public entrypoint
    # ------------------------------------------------------------------ #

    def run(
        self,
        max_iter: int = 20,
        transcript_path: str | Path | None = None,
    ) -> dict:
        """Run the closed-loop optimization against an HSpice testbench.

        Returns a dict with ``measurements``, ``pass_fail``,
        ``design_vars``, ``converged``, and ``abort_reason`` -- shape
        compatible with :meth:`CircuitAgent.run` so downstream
        reporting code can treat both backends uniformly. There is no
        ``writeback_status`` key: HSpice writes its design-var values
        directly into the .sp on the remote, which IS the writeback.
        """
        transcript_file: Path | None = None
        if transcript_path is not None:
            transcript_file = Path(transcript_path)
            transcript_file.parent.mkdir(parents=True, exist_ok=True)
            transcript_file.write_text("", encoding="utf-8")
            logger.info("LLM transcript: %s", transcript_file)

        def _append_transcript(
            iteration: int,
            role: str,
            content: str,
            usage: dict[str, Any] | None = None,
        ) -> None:
            if transcript_file is None:
                return
            try:
                entry: dict[str, Any] = {
                    "iteration": iteration,
                    "role": role,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "content": content,
                }
                # Type-check rather than truthiness — a MagicMock from a
                # unit test that mocks self.llm would otherwise leak in
                # and break json.dumps.
                if isinstance(usage, dict):
                    entry["usage"] = usage
                with transcript_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.warning(
                    "Transcript append failed (%s); continuing.",
                    type(exc).__name__,
                )

        messages = [{
            "role": "user",
            "content": self._first_prompt(),
        }]
        _append_transcript(0, "user", messages[0]["content"])
        response = self.llm.chat(messages)
        messages.append({"role": "assistant", "content": response})
        _append_transcript(
            0, "assistant", response,
            usage=getattr(self.llm, "last_usage", None),
        )

        accumulated_vars: dict[str, Any] = {}
        last_measurements: dict = {}
        last_pass_fail: dict = {}
        converged = False
        abort_reason: str | None = None
        topology_streak = 0

        for i in range(max_iter):
            logger.info("=== HSpice iteration %d/%d ===", i + 1, max_iter)

            parsed = CircuitAgent._parse_llm_response(response)
            repair_reason = self._check_contract_violation(parsed)
            if repair_reason:
                logger.warning(
                    "Contract violation (iter %d): %s -- sending repair prompt",
                    i + 1, repair_reason,
                )
                repair_msg = (
                    f"Your previous response violated HARD CONSTRAINTS: "
                    f"{repair_reason}. Please re-emit using EXACTLY the "
                    f"keys and variable names from HARD CONSTRAINTS."
                )
                messages.append({"role": "user", "content": repair_msg})
                _append_transcript(i + 1, "user", repair_msg)
                response = self.llm.chat(messages)
                messages.append({"role": "assistant", "content": response})
                _append_transcript(
                    i + 1, "assistant", response,
                    usage=getattr(self.llm, "last_usage", None),
                )
                parsed = CircuitAgent._parse_llm_response(response)
                if self._check_contract_violation(parsed):
                    abort_reason = "contract_violation"
                    break

            new_vars = parsed.get("design_vars", {}) or {}
            reasoning = parsed.get("reasoning", "") or ""

            if not new_vars and i == 0:
                logger.warning(
                    "LLM proposed no design_vars on iteration 1; "
                    "HSpice cannot iterate without an initial proposal."
                )
                abort_reason = "no_changes"
                break
            accumulated_vars.update(new_vars)

            try:
                patch_result = self._remote_patcher.patch(
                    self.remote_target_path,
                    accumulated_vars,
                    self.whitelist,
                )
            except ParamRewriteError as exc:
                # ParamRewriteError can be raised LOCALLY by the
                # remote patcher's pre-validation (whitelist check
                # could be added there in the future). Today it would
                # only fire from a direct local validation -- the
                # remote-side equivalent surfaces as RemotePatchError
                # below ("FAIL: rewrite_error ..."). Treat both as
                # rewrite_failure for stop-condition reporting.
                logger.error(
                    "design_vars rejected (iter %d): %s", i + 1, exc,
                )
                abort_reason = "rewrite_failure"
                break
            except RemotePatchError as exc:
                # Remote-side failure: ssh transport problem, remote
                # rewriter error, or unexpected exception. The message
                # has already been sanitized by RemotePatcher (single
                # chokepoint at _sanitize_remote_stderr).
                logger.error(
                    "remote patch failed (iter %d): %s", i + 1, exc,
                )
                # Distinguish rewrite_error (LLM proposal problem) from
                # transport / unexpected (operator should investigate).
                if "rewrite_error" in str(exc):
                    abort_reason = "rewrite_failure"
                else:
                    abort_reason = "hspice_failure"
                break
            logger.info(
                "remote patch iter=%d keys=%d backup=%s noop=%s",
                i + 1, patch_result.keys_patched,
                patch_result.backup_path or "(none)",
                patch_result.noop,
            )

            try:
                run_result = self.worker.run(self.remote_run_path)
            except HspiceWorkerTimeout as exc:
                logger.error("HSpice timeout (iter %d): %s", i + 1, exc)
                abort_reason = "hspice_failure"
                break
            except (HspiceWorkerSpawnError, HspiceWorkerScriptError) as exc:
                logger.error(
                    "HSpice transport/script failure (iter %d): %s",
                    i + 1, exc,
                )
                abort_reason = "hspice_failure"
                break

            try:
                evaluation = evaluate_hspice(
                    run_result.mt_files, self.spec_metrics,
                )
            except HspiceMetricNotFoundError as exc:
                logger.error(
                    "Metric %r missing from .mt<k> tables (iter %d); "
                    "fix the spec name or the .measure directive.",
                    exc.metric_name, i + 1,
                )
                abort_reason = "hspice_failure"
                break

            measurements = dict(evaluation.measurements)
            pass_fail = dict(evaluation.pass_fail)
            met = bool(pass_fail) and all(
                str(v).strip().upper().startswith("PASS")
                for v in pass_fail.values()
            )
            self.history.append(HspiceIterationRecord(
                iteration=i + 1,
                design_vars=dict(accumulated_vars),
                measurements=measurements,
                pass_fail=pass_fail,
                meets_spec=met,
                llm_reasoning=reasoning,
            ))
            last_measurements = measurements
            last_pass_fail = pass_fail

            # T8.8: track sanity-range UNMEASURABLE streak. Reset on any
            # iter without a "suspect:" verdict so a transient measurement
            # glitch cannot strand a converging run with a topology label.
            if _has_sanity_violation(pass_fail):
                topology_streak += 1
            else:
                topology_streak = 0

            if met:
                logger.info("Specifications met at HSpice iteration %d", i + 1)
                converged = True
                break

            next_prompt = self._next_prompt(
                i + 1, evaluation, run_result,
            )
            messages.append({"role": "user", "content": next_prompt})
            _append_transcript(i + 1, "user", next_prompt)
            response = self.llm.chat(messages)
            messages.append({"role": "assistant", "content": response})
            _append_transcript(
                i + 1, "assistant", response,
                usage=getattr(self.llm, "last_usage", None),
            )
        else:
            if topology_streak >= TOPOLOGY_SANITY_VIOLATION_LIMIT:
                abort_reason = "topology"
                logger.warning(
                    "TOPOLOGY: last %d HSpice iterations all produced "
                    "sanity-range UNMEASURABLE verdicts -- iteration budget "
                    "exhausted on physically-implausible measurements; "
                    "topology or spec mismatch, not a tuning issue.",
                    topology_streak,
                )
            else:
                abort_reason = "max_iter"
                logger.warning(
                    "HSpice loop did not converge within %d iterations.",
                    max_iter,
                )

        return {
            "measurements": last_measurements,
            "pass_fail": last_pass_fail,
            "design_vars": accumulated_vars,
            "converged": converged,
            "abort_reason": abort_reason,
        }

    # ------------------------------------------------------------------ #
    #  Prompt assembly
    # ------------------------------------------------------------------ #

    def _first_prompt(self) -> str:
        whitelist_sorted = sorted(self.whitelist)
        return (
            "You are an analog-design optimization agent. The target "
            "specification is pinned below. At each iteration you will "
            "receive HSpice .mt<k> measurement results; respond with ONE "
            "fenced JSON block carrying `measurements`, `pass_fail`, "
            "`reasoning`, and `design_vars`.\n\n"
            "## HARD CONSTRAINTS (violating these triggers a single repair "
            "retry, then the loop aborts)\n"
            "- **Valid top-level JSON keys**: design_vars, iteration, "
            "measurements, pass_fail, reasoning\n"
            "- **Valid `design_vars` variable names** (case-insensitive): "
            f"{', '.join(whitelist_sorted)}\n"
            "- **Value format**: bare numbers OR engineering suffixes "
            "(e.g. `75p`, `0.8`, `2`, `1.5n`). Do NOT use physical units "
            "like mA/pF/nH/GHz. The rewriter will preserve the .sp's "
            "existing suffix when you supply a bare number.\n"
            "- The platform rewrites the FIRST `.PARAM` block of the "
            "spec-designated .sp file in place; `.alter` blocks downstream "
            "are intentionally left alone (they encode the test sweep).\n\n"
            f"## Target Specification\n{self.spec_text}\n\n"
            "Emit the first JSON block now -- pick a reasonable starting "
            "point inside the ranges in the spec's design-variable table."
        )

    def _next_prompt(
        self,
        iteration: int,
        evaluation: EvaluationResult,
        run_result: HspiceRunResult,
    ) -> str:
        verdict_lines = [
            f"- {name}: {evaluation.pass_fail.get(name, '?')} "
            f"(values={evaluation.measurements.get(name)})"
            for name in evaluation.pass_fail
        ]
        history_brief = self._format_history_brief()
        return (
            f"## Iteration {iteration} HSpice results\n"
            f"- run_dir: {run_result.run_dir_remote}\n"
            f"- hspice_rc: {run_result.returncode}\n"
            f"- mt_tables: {sorted(run_result.mt_files.keys())}\n\n"
            f"## Metrics\n" + "\n".join(verdict_lines) + "\n\n"
            f"## History\n{history_brief}\n\n"
            "Emit the next JSON block. The platform recomputes "
            "`measurements` and `pass_fail` from the .mt<k> tables next "
            "turn -- focus on `reasoning` and `design_vars`. Propose a "
            "delta on a whitelisted variable that moves a FAIL metric "
            "toward its pass range. Repeat the same design_vars only if "
            "every metric already PASSes; the agent stops on its own."
        )

    def _format_history_brief(self) -> str:
        if not self.history:
            return "No previous iterations."
        lines: list[str] = []
        for rec in self.history[-5:]:
            status = "MET" if rec.meets_spec else "NOT MET"
            metric_brief = {
                k: v[0] if isinstance(v, list) and v else v
                for k, v in rec.measurements.items()
            }
            lines.append(
                f"- Iter {rec.iteration} [{status}] "
                f"vars={rec.design_vars} metrics={metric_brief}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  Schema validation (HSpice variant of the OCEAN one above)
    # ------------------------------------------------------------------ #

    def _check_contract_violation(self, parsed: dict) -> str | None:
        """Same shape as :meth:`CircuitAgent._check_contract_violation`
        but resolves the design-var whitelist against the per-spec
        ``self.whitelist`` instead of the module-level OCEAN whitelist.
        """
        problems: list[str] = []
        bad_keys = set(parsed.keys()) - _VALID_RESPONSE_KEYS
        if bad_keys:
            problems.append(f"unknown top-level key(s): {sorted(bad_keys)}")
        missing = _REQUIRED_RESPONSE_KEYS - set(parsed.keys())
        if missing:
            problems.append(
                f"missing required top-level key(s): {sorted(missing)}"
            )
        for key, expected in _RESPONSE_KEY_TYPES.items():
            if key in parsed and not isinstance(parsed[key], expected):
                actual = type(parsed[key]).__name__
                exp_name = (
                    expected.__name__ if isinstance(expected, type)
                    else "|".join(t.__name__ for t in expected)
                )
                problems.append(
                    f"'{key}' has wrong type: got {actual}, expected {exp_name}"
                )
        new_vars = parsed.get("design_vars")
        if new_vars and isinstance(new_vars, dict):
            bad_names = {
                k for k in new_vars if str(k).lower() not in self.whitelist
            }
            if bad_names:
                problems.append(
                    f"invalid design_vars key(s): {sorted(bad_names)}. "
                    f"Valid names (case-insensitive): {sorted(self.whitelist)}"
                )
            for k, v in new_vars.items():
                if isinstance(v, str) and _FORBIDDEN_UNIT_RE.search(v):
                    problems.append(
                        f"design_vars['{k}'] = '{v}' contains a physical "
                        f"unit -- use bare numbers or engineering suffixes"
                    )
        return "; ".join(problems) if problems else None

    # ------------------------------------------------------------------ #
    #  T8.3-fix: the previous _push_target_to_remote was REMOVED (it
    #  shipped the locally-scrubbed .sp body back to the remote and
    #  overwrote the original PDK file). The replacement -- remote
    #  in-place patch via src.remote_patch.RemotePatcher -- lives in
    #  __init__'s self._remote_patcher and is invoked from run().
    # ------------------------------------------------------------------ #
