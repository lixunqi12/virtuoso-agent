"""CircuitAgent: OCEAN-driven closed-loop optimizer.

Response format, iteration flow, and stop conditions are defined in
``docs/llm_protocol.md``; per-spec design variables and metrics are
loaded at import time from the target spec Markdown (default:
``config/LC_VCO_spec.md``, overridable via ``VIRTUOSO_SPEC_PATH`` /
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

from . import spec_evaluator
from .failure_codes import DumpStatus
from .llm_client import LLMClient
from .ocean_worker import (
    OceanWorker,
    OceanWorkerError,
    OceanWorkerScriptError,
    OceanWorkerTimeout,
)
from .plan_auto import PlanAuto
from .safe_bridge import SafeBridge
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
_VALID_RESPONSE_KEYS = frozenset({
    "iteration", "measurements", "pass_fail", "reasoning", "design_vars",
})
# Required top-level keys — absence is a schema violation.
# `iteration` is advisory (platform controls the counter), so it's
# intentionally optional.
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
}


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
        raise RuntimeError(
            f"Design variables table in {spec_path.name} contains no "
            f"design variables — expected rows like '| `<name>` | ...'"
        )
    return tuple(var_names)


_DEFAULT_SPEC_PATH = Path(__file__).resolve().parent.parent / "config" / "LC_VCO_spec.md"
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


class CircuitAgent:
    """LLM-driven OCEAN optimization agent."""

    def __init__(
        self,
        bridge: SafeBridge,
        llm: LLMClient,
        spec: dict | str,
        analysis_type: str = "tran",
        ocean_worker: OceanWorker | None = None,
    ):
        """Construct the agent.

        ``spec`` accepts either:
          - a Markdown **string** (preferred): the raw target-spec text
            (e.g. ``config/LC_VCO_spec.md``). Embedded directly into the
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

        def _append_transcript(iteration: int, role: str, content: str) -> None:
            if transcript_file is None:
                return
            try:
                entry = {
                    "iteration": iteration,
                    "role": role,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "content": content,
                }
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
                f"{', '.join(sorted(_VALID_DESIGN_VAR_NAMES))}\n"
                "- **Value format**: engineering suffixes ONLY "
                "(e.g. `500u`, `1.5f`, `10n`, `3k`). "
                "Do NOT use physical units like mA, pF, nH, V, GHz.\n"
                "- Do NOT invent variable names — use only the names in "
                "the whitelist above.\n\n"
                "## Region enum (per-device op-point table)\n"
                "Each next-turn prompt carries a ``tranOp`` DC operating-"
                "point table. The ``region`` integer maps as:\n"
                "- 0 cutoff — off; `id ≈ 0`; `gm ≈ 0`.\n"
                "- 1 triode — `vds < vdsat`; linear region; `gds >> gm`.\n"
                "- 2 saturation — `vds > vdsat`, `vgs > vth`; normal "
                "amplifier. Current sources and amplifying transistors "
                "belong here.\n"
                "- 3 subthreshold — `vgs < vth`, weak inversion.\n"
                "- 4 breakdown — `vds > Vdsbreak`; never expected.\n"
                "Capacitor-like devices (e.g. MOS varactors with "
                "gate-bulk tied) may legitimately sit in cutoff/triode "
                "with `id ≈ 0`; consult the spec's topology section "
                "for per-device intent.\n\n"
                f"## Target Specifications\n{spec_block}\n\n"
                "Emit the first JSON block now — pick a reasonable "
                "starting point inside the ranges in the spec's "
                "design-variable table."
            ),
        }]
        _append_transcript(0, "user", messages[0]["content"])
        response = self.llm.chat(messages)
        messages.append({"role": "assistant", "content": response})
        _append_transcript(0, "assistant", response)

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
                for entry in discovered:
                    name = entry["name"]
                    if self.bridge._is_allowed_param_name(name):
                        baseline_vars[name] = entry["default"]
                    else:
                        filtered_out.append(name)
                if filtered_out:
                    logger.warning(
                        "Filtered %d discovered variable(s) not on "
                        "allowed_params whitelist: %s (won't be sent to "
                        "OCEAN; their Maestro defaults still apply via "
                        "the testbench netlist).",
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
        except Exception as exc:  # noqa: BLE001 — topology is optional
            # A schematic read failure degrades the LLM prompt but must
            # not stop the optimization loop; spec §1 topology narrative
            # remains available as a fallback.
            logger.warning(
                "Schematic read failed (%s: %s); prompt will omit the "
                "live topology section this run.",
                type(exc).__name__, exc,
            )

        accumulated_vars: dict[str, Any] = dict(baseline_vars)
        last_measurements: dict = {}
        last_pass_fail: dict = {}
        converged = False
        abort_reason: str | None = None
        safeguard_streak = 0
        stuck_streak = 0
        topology_streak = 0

        for i in range(max_iter):
            logger.info("=== Iteration %d/%d ===", i + 1, max_iter)

            parsed = self._parse_llm_response(response)
            # One-shot repair: if response violates §4 contract, send a
            # corrective message and re-request once.
            repair_reason = self._check_contract_violation(parsed)
            if repair_reason:
                logger.warning(
                    "Contract violation (iter %d): %s — sending repair prompt",
                    i + 1, repair_reason,
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
                response = self.llm.chat(messages)
                messages.append({"role": "assistant", "content": response})
                parsed = self._parse_llm_response(response)
                # If still violating after repair, abort this iteration.
                repair_reason_2 = self._check_contract_violation(parsed)
                if repair_reason_2:
                    logger.warning(
                        "Contract violation persists after repair (iter %d): "
                        "%s — aborting.", i + 1, repair_reason_2,
                    )
                    abort_reason = "contract_violation"
                    break

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
            sim_result = self.bridge.run_ocean_sim(
                lib=lib,
                cell=cell,
                tb_cell=tb_cell,
                design_vars=accumulated_vars,
                analyses=analyses_for_run,
            )

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
            # `errset(... t)` silences the CIW *Error* print when
            # `unselectResult` is not defined in this IC23.1 build
            # (some SKILL cores only expose it after a results session
            # opens). The Python try/except was catching the thrown
            # exception but SKILL still logged the error to CIW first.
            try:
                self.bridge.client.execute_skill("errset(unselectResult() t)")
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug(
                    "unselectResult() cleanup failed (%s); continuing.",
                    type(exc).__name__,
                )

            # Per-iter diagnostic surface (Bug 0/2/4, rev 11 2026-04-20).
            diagnostic = IterationDiagnostic()

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
            if plan_auto is not None:
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
            try:
                op_point = self.bridge.read_op_point_after_tran()
            except Exception as exc:  # noqa: BLE001 — see docstring
                logger.warning(
                    "read_op_point_after_tran failed (%s: %s); LLM will "
                    "not see per-device op-point this iter.",
                    type(exc).__name__,
                    exc,
                )
                diagnostic.op_point_available = False
            else:
                diagnostic.op_point_available = bool(op_point)
            if op_point:
                logger.info(
                    "Op-point (%d devs): %s",
                    len(op_point),
                    ", ".join(
                        f"{inst.rsplit('/', 1)[-1]}={params.get('region_label', '?')}"
                        for inst, params in op_point.items()
                    ),
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

            # Best-effort waveform display — never let a display failure
            # abort the optimization. Runs AFTER dumpAll (see note above
            # run_ocean_sim call site).
            self._display_waveform(sim_result)

            met = self._all_pass(pass_fail)
            record = IterationRecord(
                iteration=i + 1,
                design_vars=dict(accumulated_vars),
                measurements=measurements,
                pass_fail=pass_fail,
                meets_spec=met,
                llm_reasoning=reasoning,
                diagnostic=diagnostic,
            )
            self.history.append(record)
            last_measurements = measurements
            last_pass_fail = pass_fail

            # T8.8: track sanity-range UNMEASURABLE streak. Reset on any
            # iter without a "suspect:" verdict so a transient glitch
            # cannot strand a converging run with a topology label.
            if _has_sanity_violation(pass_fail):
                topology_streak += 1
            else:
                topology_streak = 0

            if met:
                logger.info("Specifications met at iteration %d", i + 1)
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
            sim_summary = _format_sim_summary(sim_result)
            op_point_summary = _format_op_point_summary(op_point)
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
                eval_summary = _format_eval_summary(
                    measurements, pass_fail, dumps, diagnostic=diagnostic,
                )
                next_prompt = (
                    f"{topology_block}"
                    f"## Iteration {i + 1} measurements (platform-computed)\n"
                    f"{eval_summary}\n\n"
                    f"## Per-device DC op-point (tranOp @ t=0)\n"
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
                    f"## Per-device DC op-point (tranOp @ t=0)\n"
                    f"{op_point_summary}\n\n"
                    f"## History\n{history_brief}\n\n"
                    "Emit the next JSON block. If every pass_fail entry was "
                    "PASS, repeat the same design_vars and mark all PASS — "
                    "the agent will stop on its own."
                )
            messages.append({"role": "user", "content": next_prompt})
            _append_transcript(i + 1, "user", next_prompt)
            response = self.llm.chat(messages)
            messages.append({"role": "assistant", "content": response})
            _append_transcript(i + 1, "assistant", response)
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

        writeback_status = self._run_writeback(accumulated_vars)

        return {
            "measurements": last_measurements,
            "pass_fail": last_pass_fail,
            "design_vars": accumulated_vars,
            "converged": converged,
            "abort_reason": abort_reason,
            "writeback_status": writeback_status,
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
    def _check_contract_violation(parsed: dict) -> str | None:
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
            bad_names = set(new_vars.keys()) - _VALID_DESIGN_VAR_NAMES
            if bad_names:
                problems.append(
                    f"invalid design_vars key(s): {sorted(bad_names)}. "
                    f"Valid names: {sorted(_VALID_DESIGN_VAR_NAMES)}"
                )
            for k, v in new_vars.items():
                if isinstance(v, str) and _FORBIDDEN_UNIT_RE.search(v):
                    problems.append(
                        f"design_vars['{k}'] = '{v}' contains a physical "
                        f"unit — use engineering suffixes (u/n/p/f/k/M/G)"
                    )

        return "; ".join(problems) if problems else None

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
        lines.append(f"- {name} {cell} | {net_str} | {param_str}")
    return "\n".join(lines)


def _format_op_point_summary(op_point: dict) -> str:
    """Render per-device DC op-point as a compact Markdown table.

    ``op_point`` comes from ``bridge.read_op_point_after_tran()`` after
    ``_sanitize_op_point`` + ``_decorate_op_point``, so it is a flat
    ``{"/I0/Mx": {"vgs": ..., "region": ..., "region_label": ..., ...}}``
    with no PDK-proprietary keys. Missing fields render as "-".
    """
    if not isinstance(op_point, dict) or not op_point:
        return "(unavailable — op-point read returned empty or failed)"
    cols = ["vgs", "vds", "vov", "id", "gm", "gds", "vth", "vdsat"]
    col_units = {"vgs": "V", "vds": "V", "vov": "V", "id": "A",
                 "gm": "S", "gds": "S", "vth": "V", "vdsat": "V"}
    lines = [
        "| Inst | Region | " + " | ".join(cols) + " |",
        "|" + "|".join(["---"] * (2 + len(cols))) + "|",
    ]
    for inst_name, params in op_point.items():
        if not isinstance(params, dict):
            continue
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
    return "\n".join(lines)


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

import yaml                        # noqa: E402  -- spec block extraction

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

        def _append_transcript(iteration: int, role: str, content: str) -> None:
            if transcript_file is None:
                return
            try:
                entry = {
                    "iteration": iteration,
                    "role": role,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "content": content,
                }
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
        _append_transcript(0, "assistant", response)

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
                _append_transcript(i + 1, "assistant", response)
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
            _append_transcript(i + 1, "assistant", response)
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
