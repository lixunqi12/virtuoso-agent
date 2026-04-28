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

import ast
import logging
import math
import operator
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

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


def _mt_suffix_int(basename: str) -> int | None:
    """Return the trailing ``.mtN`` integer, or ``None`` if absent."""
    m = _MT_SUFFIX_RE.search(basename)
    return int(m.group(1)) if m is not None else None


__all__ = [
    "EvaluationResult",
    "HspiceConfigError",
    "HspiceMetricNotFoundError",
    "HspiceShapeError",
    "evaluate_hspice",
]


_REDUCE_OPS = ("linregress", "mean", "max", "min", "std", "range", "diff_paired")
_LINREGRESS_OUTPUTS = ("slope", "slope_abs", "r_squared", "intercept")
_DIFF_PAIRED_OUTPUTS = ("max_abs_diff", "signed_diff")
_REDUCE_ACROSS = ("mt_files", "sweep_rows", "all")

# T8.7c: whitelist of AST nodes allowed in derived `source.expr`.
# Only column names + arithmetic + numeric literals + unary minus.
# No attribute access, no calls, no comparisons, no comprehensions.
_EXPR_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


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


class HspiceConfigError(Exception):
    """Raised when a metric's ``reduce:`` block is misconfigured.

    Examples: unknown ``op``/``output``/``across`` enum, ``x`` length
    does not match the number of ``.mt`` files, ``x`` carries a
    non-finite value, or ``source`` column is missing in any input
    ``.mt`` file. These are spec/netlist-author bugs, not numerical
    issues — surface eagerly so the human fixes the YAML.
    """


class HspiceShapeError(Exception):
    """Raised when the per-row reducer cannot align rows across
    ``.mt`` files because their row counts disagree.

    For ``across: mt_files`` the resolver groups one value from each
    file at each TRAN sweep row, which requires every input table
    to have the same number of rows.
    """


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
        eval_rows = _coerce_eval_rows(m, name)

        reduce_block = m.get("reduce")
        if reduce_block is not None:
            values, reasons = _evaluate_reduce_metric(
                mt_results, name, reduce_block, m.get("source"), scale,
            )
            verdicts = [
                _verdict(v, pass_range, sanity_range, r)
                for v, r in zip(values, reasons)
            ]
            value_list = [
                v if v is not None else float("nan") for v in values
            ]
            measurements[name] = value_list
            per_row[name] = verdicts
            pass_fail[name] = _aggregate_verdict_filtered(
                name, verdicts, value_list, eval_rows
            )
            continue

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
        pass_fail[name] = _aggregate_verdict_filtered(
            name, verdicts, values, eval_rows
        )

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


def _coerce_eval_rows(m: dict, name: str) -> list[int] | None:
    """T8.7b: parse metric-level ``eval_rows: [...]``.

    When set, only the listed row indices count toward the aggregate
    PASS/FAIL verdict. Per-row verdicts for ALL rows are still emitted
    (observability is preserved). When omitted (None), every row counts
    — same semantic as T8.6.
    """
    raw = m.get("eval_rows")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or not raw:
        raise HspiceConfigError(
            f"metric {name!r}: eval_rows must be a non-empty list of "
            f"row indices"
        )
    out: list[int] = []
    seen: set[int] = set()
    for i, ix in enumerate(raw):
        if not isinstance(ix, int) or isinstance(ix, bool) or ix < 0:
            raise HspiceConfigError(
                f"metric {name!r}: eval_rows[{i}] must be a non-negative "
                f"integer; got {ix!r}"
            )
        if ix in seen:
            raise HspiceConfigError(
                f"metric {name!r}: eval_rows contains duplicate {ix}"
            )
        seen.add(ix)
        out.append(ix)
    return out


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


def _aggregate_verdict_filtered(
    metric_name: str,
    verdicts: Sequence[str],
    values: Sequence[float],
    eval_rows: Sequence[int] | None,
) -> str:
    """Aggregate verdicts, optionally restricted to ``eval_rows``."""
    if eval_rows is None:
        return _aggregate_verdict(verdicts, values)
    n = len(verdicts)
    selected_v: list[str] = []
    selected_y: list[float] = []
    for ix in eval_rows:
        if ix >= n:
            raise HspiceConfigError(
                f"metric {metric_name!r}: eval_rows entry {ix} is out of "
                f"range (only {n} rows produced)"
            )
        selected_v.append(verdicts[ix])
        selected_y.append(values[ix])
    return _aggregate_verdict(selected_v, selected_y)


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


# --------------------------------------------------------------------------
# T8.7c — derived ``source.expr`` support.
#
# Spec authors who need a column not directly emitted by the testbench
# (e.g. common-mode midpoint = (h_tphl + v_tphl) / 2) write::
#
#     source: { expr: "(h_tphl + v_tphl) / 2" }
#
# The expression is parsed once at config time into an AST, validated
# against a whitelist (Name + BinOp + Constant + UnaryOp(USub)), and then
# evaluated against each row's column values. Anything outside the
# whitelist (function calls, comparisons, attribute access, etc.) is
# rejected with HspiceConfigError.
# --------------------------------------------------------------------------


def _compile_source_expr(metric_name: str, expr_str: str) -> tuple[ast.Expression, list[str]]:
    """Parse ``expr_str``, validate AST, and return (tree, column_names_used).

    Raises HspiceConfigError on any disallowed construct.
    """
    if not isinstance(expr_str, str) or not expr_str.strip():
        raise HspiceConfigError(
            f"metric {metric_name!r}: source.expr must be a non-empty string"
        )
    try:
        tree = ast.parse(expr_str, mode="eval")
    except SyntaxError as exc:
        raise HspiceConfigError(
            f"metric {metric_name!r}: source.expr syntax error: {exc.msg}"
        ) from None
    cols_used: list[str] = []
    _validate_expr_node(metric_name, tree.body, cols_used)
    return tree, cols_used


def _validate_expr_node(metric_name: str, node: ast.AST, cols: list[str]) -> None:
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _EXPR_BINOPS:
            raise HspiceConfigError(
                f"metric {metric_name!r}: source.expr disallowed operator "
                f"{type(node.op).__name__}; allowed: + - * /"
            )
        _validate_expr_node(metric_name, node.left, cols)
        _validate_expr_node(metric_name, node.right, cols)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub):
            raise HspiceConfigError(
                f"metric {metric_name!r}: source.expr disallowed unary "
                f"{type(node.op).__name__}; only unary minus allowed"
            )
        _validate_expr_node(metric_name, node.operand, cols)
        return
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise HspiceConfigError(
                f"metric {metric_name!r}: source.expr literal must be numeric; "
                f"got {type(node.value).__name__}"
            )
        return
    if isinstance(node, ast.Name):
        if node.id not in cols:
            cols.append(node.id)
        return
    raise HspiceConfigError(
        f"metric {metric_name!r}: source.expr disallowed AST node "
        f"{type(node).__name__}"
    )


def _eval_expr(tree: ast.Expression, env: Mapping[str, float]) -> float:
    return _eval_expr_node(tree.body, env)


def _eval_expr_node(node: ast.AST, env: Mapping[str, float]) -> float:
    if isinstance(node, ast.BinOp):
        op_func = _EXPR_BINOPS[type(node.op)]
        return op_func(_eval_expr_node(node.left, env), _eval_expr_node(node.right, env))
    if isinstance(node, ast.UnaryOp):  # only USub passed validation
        return -_eval_expr_node(node.operand, env)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return env[node.id]
    # Unreachable — validator rejected anything else.
    raise RuntimeError(f"unexpected AST node at eval time: {type(node).__name__}")


# --------------------------------------------------------------------------
# reduce: block (T8.6) — cross-mt-file or cross-row reductions.
#
# For ``across: mt_files`` the resolver groups the i-th sweep row of
# every input ``.mt`` file into a single y-vector and feeds it through
# the chosen op + spec-supplied x-axis. Output shape is one scalar per
# TRAN sweep row. ``sweep_rows`` and ``all`` are reserved by the contract
# but not yet implemented.
#
# T8.7 additions (2026-04-27):
#   - ``mt_indices: [0, 1, 2, 3]`` — subset filter, picks ``.mtN`` files
#     by trailing integer suffix. ``x`` length must match the subset
#     size when present. Used to split POS/NEG halves of an .alter
#     sweep without authoring two separate specs.
#   - ``op: diff_paired`` + ``pairs: [[a, b], ...]`` — paired
#     subtraction across mt files; emits per-row ``max_abs_diff`` (max
#     of |y[a]-y[b]| over pairs) or ``signed_diff`` (single-pair only).
#   - ``source: { expr: "..." }`` — derived columns via whitelisted AST
#     evaluation; lets specs reference combinations like
#     ``(h_tphl + v_tphl) / 2`` without round-tripping through HSpice.
#   - ``eval_rows: [0, 12]`` (metric-level, not inside ``reduce``) —
#     subset of TRAN sweep rows that count toward the aggregate
#     PASS/FAIL verdict; per-row verdicts for all rows are still
#     emitted for observability.
# --------------------------------------------------------------------------


def _evaluate_reduce_metric(
    mt_results: Mapping[str, Mt0Result],
    metric_name: str,
    reduce_block: Any,
    source_spec: Any,
    scale: float,
) -> tuple[list[float | None], list[str | None]]:
    """Resolve a ``reduce:`` metric to per-row (value, reason) pairs.

    Returns ``(values, reasons)`` aligned by index. ``values[i]`` is
    ``None`` and ``reasons[i]`` is set when row i is UNMEASURABLE;
    otherwise ``values[i]`` is the reducer output and ``reasons[i]``
    is ``None``.
    """
    if not isinstance(reduce_block, dict):
        raise HspiceConfigError(
            f"metric {metric_name!r}: 'reduce' must be a mapping; "
            f"got {type(reduce_block).__name__}"
        )

    across = reduce_block.get("across")
    if across not in _REDUCE_ACROSS:
        raise HspiceConfigError(
            f"metric {metric_name!r}: reduce.across must be one of "
            f"{_REDUCE_ACROSS}; got {across!r}"
        )
    if across in ("sweep_rows", "all"):
        raise NotImplementedError(
            f"metric {metric_name!r}: reduce.across={across!r} is "
            f"reserved by the T8.6 contract but not implemented yet"
        )

    op = reduce_block.get("op")
    if op not in _REDUCE_OPS:
        raise HspiceConfigError(
            f"metric {metric_name!r}: reduce.op must be one of "
            f"{_REDUCE_OPS}; got {op!r}"
        )

    output = reduce_block.get("output")
    if op == "linregress":
        if output not in _LINREGRESS_OUTPUTS:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.output must be one of "
                f"{_LINREGRESS_OUTPUTS} for op=linregress; got {output!r}"
            )
    if op == "diff_paired":
        if output not in _DIFF_PAIRED_OUTPUTS:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.output must be one of "
                f"{_DIFF_PAIRED_OUTPUTS} for op=diff_paired; got {output!r}"
            )

    # T8.7c: parse source — string column name OR {expr: "..."}.
    source_kind, source_payload, source_cols_used = _parse_source_spec(
        metric_name, source_spec
    )

    # T8.7a: optional ``mt_indices`` subset filter. Keys are the integer
    # suffix of ``.mtN`` basenames. When omitted the resolver behaves
    # exactly as in T8.6 (all mt files in natural order).
    all_basenames = sorted(mt_results.keys(), key=_mt_sort_key)
    mt_indices_raw = reduce_block.get("mt_indices")
    if mt_indices_raw is not None:
        basenames = _filter_basenames_by_indices(
            metric_name, all_basenames, mt_indices_raw
        )
    else:
        basenames = all_basenames

    # Map basename → its global mt suffix integer, used by op=diff_paired
    # whose ``pairs`` field references mt indices directly (not subset
    # positions, so a spec can pair across the POS/NEG halves).
    suffix_to_basename: dict[int, str] = {}
    for bn in all_basenames:
        idx_int = _mt_suffix_int(bn)
        if idx_int is not None:
            suffix_to_basename[idx_int] = bn

    # T8.7d: parse ``pairs`` for diff_paired. Each pair references mt
    # suffix integers (not subset positions). diff_paired ignores
    # ``mt_indices`` because pairs already pin the participating files.
    pair_specs: list[tuple[int, int]] = []
    if op == "diff_paired":
        if mt_indices_raw is not None:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.mt_indices and "
                f"reduce.pairs cannot both be set — pairs already pin "
                f"the participating mt files"
            )
        pair_specs = _parse_pairs(metric_name, reduce_block.get("pairs"))
        if output == "signed_diff" and len(pair_specs) != 1:
            raise HspiceConfigError(
                f"metric {metric_name!r}: output=signed_diff requires "
                f"exactly one entry in reduce.pairs; got {len(pair_specs)}"
            )
        for a, b in pair_specs:
            for ix in (a, b):
                if ix not in suffix_to_basename:
                    raise HspiceConfigError(
                        f"metric {metric_name!r}: reduce.pairs references "
                        f"mt index {ix} but no '.mt{ix}' basename was "
                        f"provided"
                    )

    # ``reduce.x`` is only meaningful for linregress. Other ops ignore
    # it; building a placeholder lets the inner zip iterate every sample.
    x_arr: list[float] = [0.0] * len(basenames)
    if op == "linregress":
        x_raw = reduce_block.get("x")
        if not isinstance(x_raw, (list, tuple)) or len(x_raw) == 0:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.x must be a non-empty "
                f"list for op=linregress"
            )
        x_arr = []  # overwrite the non-linregress placeholder
        for i, xi in enumerate(x_raw):
            if not isinstance(xi, (int, float)) or isinstance(xi, bool):
                raise HspiceConfigError(
                    f"metric {metric_name!r}: reduce.x[{i}] must be "
                    f"numeric; got {type(xi).__name__}"
                )
            xf = float(xi)
            if not math.isfinite(xf):
                raise HspiceConfigError(
                    f"metric {metric_name!r}: reduce.x[{i}] is non-finite "
                    f"({xf!r}); NaN/inf rejected at config time"
                )
            x_arr.append(xf)
        if len(x_arr) != len(basenames):
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.x length {len(x_arr)} "
                f"does not match number of mt files after subset "
                f"({len(basenames)})"
            )

    # Build N×M y-matrix. For diff_paired we need the FULL mt set keyed
    # by suffix integer (so pair lookup is O(1)); for everything else we
    # use the (possibly subsetted) basenames list in deterministic order.
    if op == "diff_paired":
        y_by_suffix, n_rows = _build_y_matrix_by_suffix(
            mt_results, metric_name, source_kind, source_payload,
            source_cols_used, scale, suffix_to_basename,
        )
        values: list[float | None] = []
        reasons: list[str | None] = []
        for j in range(n_rows):
            v, reason = _apply_diff_paired(
                y_by_suffix, j, pair_specs, output
            )
            values.append(v)
            reasons.append(reason)
        return values, reasons

    # Standard path: build N×M (subset_basenames × n_rows) matrix and
    # feed each row's y-vector through ``_apply_reduce_op``.
    y_matrix: list[list[float]] = []
    n_rows: int | None = None
    for basename in basenames:
        col_vals = _read_source_column(
            mt_results, metric_name, basename, source_kind, source_payload,
            source_cols_used, scale,
        )
        if n_rows is None:
            n_rows = len(col_vals)
        elif len(col_vals) != n_rows:
            raise HspiceShapeError(
                f"metric {metric_name!r}: row count mismatch — "
                f"{basename} has {len(col_vals)} rows, expected {n_rows}"
            )
        y_matrix.append(col_vals)

    if n_rows is None or n_rows == 0:
        raise HspiceShapeError(
            f"metric {metric_name!r}: no data rows in any .mt file"
        )

    values = []
    reasons = []
    for j in range(n_rows):
        y_vec = [y_matrix[k][j] for k in range(len(basenames))]
        v, reason = _apply_reduce_op(y_vec, x_arr, op, output)
        values.append(v)
        reasons.append(reason)
    return values, reasons


def _parse_source_spec(
    metric_name: str, source_spec: Any
) -> tuple[str, Any, list[str]]:
    """Parse the ``source:`` field. Returns (kind, payload, cols_used).

    - kind="col", payload=str column name, cols_used=[name]
    - kind="expr", payload=ast.Expression, cols_used=column names referenced
    """
    if source_spec is None:
        raise HspiceConfigError(
            f"metric {metric_name!r}: reduce path requires 'source' "
            f"(the .mt column or derived expression) at the metric level"
        )
    if isinstance(source_spec, str):
        if not source_spec:
            raise HspiceConfigError(
                f"metric {metric_name!r}: 'source' must be a non-empty string"
            )
        return "col", source_spec, [source_spec]
    if isinstance(source_spec, dict):
        expr_str = source_spec.get("expr")
        if expr_str is None:
            raise HspiceConfigError(
                f"metric {metric_name!r}: source dict must contain 'expr' key"
            )
        tree, cols_used = _compile_source_expr(metric_name, expr_str)
        if not cols_used:
            raise HspiceConfigError(
                f"metric {metric_name!r}: source.expr references no columns"
            )
        return "expr", tree, cols_used
    raise HspiceConfigError(
        f"metric {metric_name!r}: 'source' must be a string or "
        f"{{'expr': '...'}} mapping; got {type(source_spec).__name__}"
    )


def _read_source_column(
    mt_results: Mapping[str, Mt0Result],
    metric_name: str,
    basename: str,
    source_kind: str,
    source_payload: Any,
    source_cols_used: Sequence[str],
    scale: float,
) -> list[float]:
    """Read the (possibly derived) source column from one ``.mt`` file."""
    res = mt_results[basename]
    cols = list(res.columns)
    # Validate every referenced column exists. Error message keeps the
    # public spec-author identifiers and avoids leaking the full PDK
    # column list.
    for col_name in source_cols_used:
        if col_name not in cols:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.source references "
                f"{col_name!r} which is not present in {basename!r} "
                f"(table has {len(cols)} columns)"
            )
    indices = {c: cols.index(c) for c in source_cols_used}
    out: list[float] = []
    for row in res.rows:
        try:
            if source_kind == "col":
                raw = float(row[indices[source_payload]])
                v = raw * scale
            else:  # expr
                env = {c: float(row[indices[c]]) for c in source_cols_used}
                v = _eval_expr(source_payload, env) * scale
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            v = float("nan")
        out.append(v)
    return out


def _build_y_matrix_by_suffix(
    mt_results: Mapping[str, Mt0Result],
    metric_name: str,
    source_kind: str,
    source_payload: Any,
    source_cols_used: Sequence[str],
    scale: float,
    suffix_to_basename: Mapping[int, str],
) -> tuple[dict[int, list[float]], int]:
    """Build {mt_suffix: [row_values]} for the FULL mt set; used by
    diff_paired which references mt indices directly.

    Returns (mapping, n_rows). All entries share row count.
    """
    out: dict[int, list[float]] = {}
    n_rows: int | None = None
    for suffix, basename in sorted(suffix_to_basename.items()):
        col_vals = _read_source_column(
            mt_results, metric_name, basename, source_kind, source_payload,
            source_cols_used, scale,
        )
        if n_rows is None:
            n_rows = len(col_vals)
        elif len(col_vals) != n_rows:
            raise HspiceShapeError(
                f"metric {metric_name!r}: row count mismatch — "
                f"{basename} has {len(col_vals)} rows, expected {n_rows}"
            )
        out[suffix] = col_vals
    if n_rows is None or n_rows == 0:
        raise HspiceShapeError(
            f"metric {metric_name!r}: no data rows in any .mt file"
        )
    return out, n_rows


def _filter_basenames_by_indices(
    metric_name: str,
    all_basenames: Sequence[str],
    mt_indices_raw: Any,
) -> list[str]:
    """Apply ``reduce.mt_indices`` filter. Preserves natural mt ordering."""
    if not isinstance(mt_indices_raw, (list, tuple)) or not mt_indices_raw:
        raise HspiceConfigError(
            f"metric {metric_name!r}: reduce.mt_indices must be a "
            f"non-empty list of integers"
        )
    requested: list[int] = []
    seen: set[int] = set()
    for i, ix in enumerate(mt_indices_raw):
        if not isinstance(ix, int) or isinstance(ix, bool) or ix < 0:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.mt_indices[{i}] must be "
                f"a non-negative integer; got {ix!r}"
            )
        if ix in seen:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.mt_indices contains "
                f"duplicate entry {ix}"
            )
        seen.add(ix)
        requested.append(ix)
    by_suffix: dict[int, str] = {}
    for bn in all_basenames:
        sx = _mt_suffix_int(bn)
        if sx is not None:
            by_suffix[sx] = bn
    selected: list[str] = []
    for ix in requested:
        if ix not in by_suffix:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.mt_indices requests "
                f"mt{ix} but no matching '.mt{ix}' basename was provided"
            )
        selected.append(by_suffix[ix])
    # Re-sort selected basenames by natural mt suffix so deterministic
    # order matches the rest of the resolver (mt0 < mt1 < ... < mt10).
    selected.sort(key=_mt_sort_key)
    return selected


def _parse_pairs(metric_name: str, pairs_raw: Any) -> list[tuple[int, int]]:
    if not isinstance(pairs_raw, (list, tuple)) or not pairs_raw:
        raise HspiceConfigError(
            f"metric {metric_name!r}: op=diff_paired requires reduce.pairs "
            f"as a non-empty list of [a, b] pairs"
        )
    out: list[tuple[int, int]] = []
    for i, p in enumerate(pairs_raw):
        if (
            not isinstance(p, (list, tuple))
            or len(p) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                       for v in p)
        ):
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.pairs[{i}] must be a "
                f"[a, b] pair of non-negative integers; got {p!r}"
            )
        a, b = int(p[0]), int(p[1])
        if a == b:
            raise HspiceConfigError(
                f"metric {metric_name!r}: reduce.pairs[{i}] is degenerate "
                f"(a == b == {a})"
            )
        out.append((a, b))
    return out


def _apply_diff_paired(
    y_by_suffix: Mapping[int, list[float]],
    row_idx: int,
    pair_specs: Sequence[tuple[int, int]],
    output: str | None,
) -> tuple[float | None, str | None]:
    diffs: list[float] = []
    for a, b in pair_specs:
        ya = y_by_suffix[a][row_idx]
        yb = y_by_suffix[b][row_idx]
        if not (math.isfinite(ya) and math.isfinite(yb)):
            return None, (
                f"diff_paired: non-finite y at row {row_idx} for "
                f"pair (mt{a}, mt{b})"
            )
        diffs.append(ya - yb)
    if output == "max_abs_diff":
        return float(max(abs(d) for d in diffs)), None
    if output == "signed_diff":
        # Caller already validated len(pair_specs) == 1.
        return float(diffs[0]), None
    return None, f"diff_paired: unknown output {output!r}"


def _apply_reduce_op(
    y: Sequence[float],
    x: Sequence[float],
    op: str,
    output: str | None,
) -> tuple[float | None, str | None]:
    """Apply one reducer to a single (x, y) sample group.

    ``x`` is guaranteed all-finite by upstream config validation; ``y``
    may carry NaN from HSpice. Returns ``(value, None)`` when the op
    produces a finite scalar, or ``(None, reason)`` when degenerate.
    """
    pairs = [(xi, yi) for xi, yi in zip(x, y) if math.isfinite(yi)]
    n = len(pairs)
    if op in ("mean", "max", "min", "std", "range"):
        if n == 0:
            return None, f"{op}: 0 finite samples"
        # std/range collapse to a meaningless 0.0 on a single sample;
        # contract v2 requires UNMEASURABLE so a one-sample row never
        # silently passes a "spread <= X" gate.
        if op in ("std", "range") and n < 2:
            return None, f"{op}: needs >=2 finite samples, got {n}"
        finite_y = np.asarray([p[1] for p in pairs], dtype=float)
        if op == "mean":
            return float(np.mean(finite_y)), None
        if op == "max":
            return float(np.max(finite_y)), None
        if op == "min":
            return float(np.min(finite_y)), None
        if op == "std":
            return float(np.std(finite_y, ddof=0)), None
        if op == "range":
            return float(np.max(finite_y) - np.min(finite_y)), None

    if op == "linregress":
        if n < 2:
            return None, f"linregress: needs >=2 finite samples, got {n}"
        xs = np.asarray([p[0] for p in pairs], dtype=float)
        ys = np.asarray([p[1] for p in pairs], dtype=float)
        x_var_zero = bool(np.all(xs == xs[0]))
        if x_var_zero:
            return None, "linregress: x has zero variance"
        try:
            coeffs = np.polyfit(xs, ys, 1)
        except (np.linalg.LinAlgError, ValueError) as exc:
            return None, f"linregress: polyfit failed ({exc!r})"
        slope, intercept = float(coeffs[0]), float(coeffs[1])
        if not (math.isfinite(slope) and math.isfinite(intercept)):
            return None, "linregress: non-finite slope/intercept"
        if output == "slope":
            return slope, None
        if output == "slope_abs":
            return abs(slope), None
        if output == "intercept":
            return intercept, None
        if output == "r_squared":
            if n == 2:
                return None, "r_squared: undefined for n=2 (perfect fit)"
            y_var_zero = bool(np.all(ys == ys[0]))
            if y_var_zero:
                return None, "r_squared: y has zero variance"
            ss_res = float(np.sum((ys - (slope * xs + intercept)) ** 2))
            ss_tot = float(np.sum((ys - float(np.mean(ys))) ** 2))
            if ss_tot == 0.0:
                return None, "r_squared: ss_tot is zero"
            r2 = 1.0 - ss_res / ss_tot
            if not math.isfinite(r2):
                return None, "r_squared: non-finite result"
            return r2, None

    # Reachable only if a new op is added to _REDUCE_OPS without a branch.
    return None, f"reduce op {op!r} not handled"
