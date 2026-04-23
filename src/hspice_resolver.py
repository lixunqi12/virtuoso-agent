"""Resolve spec metrics against HSpice ``.mt0`` measurement tables.

Stage 1 rev 1 (2026-04-23): PC-side resolver that reads T3's
:class:`parse_mt0.Mt0Result` and produces per-metric
``(value, verdict)`` aligned with the existing OCEAN-path
:mod:`spec_evaluator` output shape.

MVP rule: ``metric["name"]`` must appear verbatim as a column in at
least one ``.mt<k>`` table. The value is read directly from the
corresponding cell (optionally multiplied by ``metric["scale"]``);
no ``.tr0`` waveform fallback exists. When HSpice produces multiple
alters (``.mt0`` / ``.mt1`` / ...) or a sweep with multiple rows
per table, all values flow through the same ``pass`` / ``sanity``
membership test — if any row fails, the aggregate verdict is
``FAIL``.

Compound metrics (``ratio`` / ``t_cross_frac``) declared in the
spec block cannot be computed against ``.mt0`` alone: ``ratio``
needs per-window RMS and ``t_cross_frac`` needs raw waveform.
Both are emitted with an ``UNMEASURABLE`` verdict so the consumer
sees the spec author's intent without conflating it with a
numerically FAIL metric. The full OCEAN flow in :mod:`spec_evaluator`
still handles those correctly.

Shape contract (mirrors :func:`spec_evaluator.evaluate`):

    mt_results:   ``dict[str, Mt0Result]`` — T3's
                  ``HspiceRunResult.mt_files``, keyed by basename.
    spec_metrics: ``list[dict]`` — the ``metrics:`` list from a
                  parsed spec YAML block.

Returned :class:`EvaluationResult` exposes:

    measurements      : {metric_name: [float, ...]}
                        Always a list; length 1 for the typical
                        single-alter / single-row run, longer when
                        the simulator swept or ran multiple alters.
    pass_fail         : {metric_name: verdict_str}
                        Aggregate verdict — PASS iff all rows pass.
    per_row_verdicts  : {metric_name: [verdict_str, ...]}
                        Per-(alter, row) verdict detail for
                        downstream LLM / log inspection.

Raises :class:`HspiceMetricNotFoundError` when a simple metric's
name matches no column across any ``mt_results`` table. This is a
hard error rather than a soft UNMEASURABLE — a missing measure
column means the ``.sp`` netlist never emitted a ``.measure`` for
that name, which is a spec/netlist mismatch the human should fix
rather than the LLM tuning design vars around.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.parse_mt0 import Mt0Result
from src.spec_evaluator import _verdict

logger = logging.getLogger(__name__)


# Natural-order sort for ``.mt<N>`` basenames. Lexicographic sort would
# order ``sim.mt10`` before ``sim.mt2`` which scrambles alter numbering
# once >10 alters exist (codex T4 R2). Entries whose basename ends in
# ``.mtN`` sort first, by integer N; unmatched basenames fall through
# to a stable lex bucket so the function still produces a total order
# on unexpected inputs.
_MT_SUFFIX_RE = re.compile(r"\.mt(\d+)$", re.IGNORECASE)


def _mt_sort_key(basename: str) -> tuple[int, int, str]:
    m = _MT_SUFFIX_RE.search(basename)
    if m is None:
        return (1, 0, basename)
    return (0, int(m.group(1)), basename)


__all__ = [
    "EvaluationResult",
    "HspiceMetricNotFoundError",
    "evaluate_hspice",
]


class HspiceMetricNotFoundError(Exception):
    """Raised when a simple metric's name matches no column in any
    of the provided ``Mt0Result`` tables.

    Attributes:
        metric_name: the offending ``metric["name"]``.
        available:   distinct column names seen across all tables
                     (de-duplicated, preserves first-seen order).
    """

    def __init__(self, metric_name: str, available: Sequence[str]) -> None:
        self.metric_name = metric_name
        self.available = list(available)
        super().__init__(
            f"metric {metric_name!r} not found in .mt0 columns "
            f"(saw {len(self.available)} distinct columns across tables)"
        )


@dataclass(frozen=True)
class EvaluationResult:
    measurements: Mapping[str, list[float]]
    pass_fail: Mapping[str, str]
    per_row_verdicts: Mapping[str, list[str]]


def evaluate_hspice(
    mt_results: Mapping[str, Mt0Result],
    spec_metrics: Sequence[dict],
) -> EvaluationResult:
    """Resolve spec metrics against HSpice ``.mt<k>`` tables.

    See module docstring for the full contract.
    """
    measurements: dict[str, list[float]] = {}
    pass_fail: dict[str, str] = {}
    per_row: dict[str, list[str]] = {}

    for m in spec_metrics:
        if not isinstance(m, dict):
            raise ValueError(f"metric entry must be a mapping; got {type(m)!r}")
        name = m.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"metric entry missing non-empty 'name': {m!r}")

        compound = m.get("compound")
        if compound is not None:
            measurements[name] = []
            per_row[name] = []
            pass_fail[name] = (
                f"UNMEASURABLE (compound {compound!r} not supported in HSpice MVP)"
            )
            continue

        scale = _coerce_scale(m, name)
        pass_range = m.get("pass")
        sanity_range = m.get("sanity")

        values = _read_column_values(mt_results, name, scale)
        if values is None:
            raise HspiceMetricNotFoundError(
                name, _distinct_columns(mt_results),
            )

        verdicts = [
            _verdict(v, pass_range, sanity_range, None) for v in values
        ]
        measurements[name] = values
        per_row[name] = verdicts
        pass_fail[name] = _aggregate_verdict(verdicts, values)

    return EvaluationResult(
        measurements=measurements,
        pass_fail=pass_fail,
        per_row_verdicts=per_row,
    )


def _coerce_scale(m: dict, name: str) -> float:
    raw = m.get("scale", 1.0)
    if not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
        raise ValueError(
            f"metric {name!r}: scale must be a finite number; got {raw!r}"
        )
    return float(raw)


def _read_column_values(
    mt_results: Mapping[str, Mt0Result],
    metric_name: str,
    scale: float,
) -> list[float] | None:
    """Return the flat list of scaled values for ``metric_name`` across
    all (basename, row) pairs, or ``None`` if the column is not present
    in any table.

    Deterministic order: basenames sorted by trailing ``.mt<N>`` integer
    (see ``_mt_sort_key``) so ``sim.mt0`` < ``sim.mt2`` < ``sim.mt10``.
    This matches HSpice's alter numbering for runs with >10 alters;
    plain lex sort would place ``.mt10`` before ``.mt2``.
    """
    found_column = False
    values: list[float] = []
    for basename in sorted(mt_results.keys(), key=_mt_sort_key):
        res = mt_results[basename]
        cols = list(res.columns)
        if metric_name not in cols:
            continue
        found_column = True
        idx = cols.index(metric_name)
        for row in res.rows:
            if idx >= len(row):
                # Malformed table — defensive skip; parse_mt0 already
                # validates row widths, so this should never fire.
                continue
            try:
                val = float(row[idx]) * scale
            except (TypeError, ValueError):
                continue
            if math.isfinite(val):
                values.append(val)
    return values if found_column else None


def _distinct_columns(mt_results: Mapping[str, Mt0Result]) -> list[str]:
    seen: list[str] = []
    for basename in sorted(mt_results.keys(), key=_mt_sort_key):
        for col in mt_results[basename].columns:
            if col not in seen:
                seen.append(col)
    return seen


def _aggregate_verdict(
    verdicts: Sequence[str], values: Sequence[float]
) -> str:
    """Aggregate per-row verdicts.

    - All ``PASS`` → ``"PASS"``
    - Any ``FAIL`` → first failing verdict; when multi-row, annotated
      with the row index + offending value so the LLM / log can
      identify which corner/sweep-point broke.
    - Else any ``UNMEASURABLE`` → same treatment.
    - No verdicts (impossible for simple metrics that reached here
      since ``found_column`` implied ≥1 row) → ``UNMEASURABLE``.
    """
    if not verdicts:
        return "UNMEASURABLE (no rows produced a finite value)"
    if all(v == "PASS" for v in verdicts):
        return "PASS"
    for idx, v in enumerate(verdicts):
        if v.startswith("FAIL"):
            return _annotate(v, idx, values)
    for idx, v in enumerate(verdicts):
        if v.startswith("UNMEASURABLE"):
            return _annotate(v, idx, values)
    return "PASS"  # unreachable — all non-PASS branches are handled above


def _annotate(
    verdict: str, idx: int, values: Sequence[float]
) -> str:
    if len(values) <= 1:
        return verdict
    return (
        f"{verdict} (row {idx}/{len(values)}, value={values[idx]:.6g})"
    )
