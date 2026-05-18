"""Sync spec.metrics → Maestro Outputs Setup (Track C Option I).

For every metric declared in the spec's ``signals/windows/metrics`` YAML
block, declare a matching Maestro Output Setup row with an OCEAN measure
expression that mirrors the PC-side evaluator formula, plus optional
pass/fail spec bounds. This lets an interactive Maestro user see the
same measurements the PC evaluator computes from the PSF dump, without
hand-typing every formula into the Outputs tab.

Generality (per Track C scope):
- No circuit-shape assumptions — no per-design symbol hardcoding.
- No analysis-type assumptions — templates work on any tran/ac waveform
  that the OCEAN calculator can clip to a data window.
- Unsupported metric shape (unknown stat / unsupported signal kind /
  ``compound: t_cross_frac``) → ``logger.warning`` + skip; never abort.
- ``SafeBridge`` write failures (e.g. session vanished, allow-list
  rejected the expr after a future tightening) → log + skip the offending
  metric; the rest still try.

The PC-side evaluator remains the authoritative pass/fail source.
This sync is an authoring-time convenience, not a correctness gate.
"""

from __future__ import annotations

import logging
from typing import Any

from .safe_bridge import SafeBridge, _PROBE_PATH_RE


_DEFAULT_LOGGER = logging.getLogger(__name__)

# Stat names emitted by ``safeOcean_statsJson`` in skill/safe_ocean.il
# (see safe_ocean.il:867-939). Each maps to an OCEAN/Calculator function
# applied to the windowed waveform. ``mean_abs`` is composite
# (``average(abs(...))``); ``duty_pct`` uses OCEAN's built-in
# ``dutyCycle`` (may return nil for non-square signals — that's a
# Maestro-side display issue, not a sync issue).
_STAT_TO_OCEAN_FN: dict[str, str] = {
    "mean":     "average",
    "min":      "ymin",
    "max":      "ymax",
    "ptp":      "peakToPeak",
    "rms":      "rms",
    "freq_Hz":  "frequency",
    "duty_pct": "dutyCycle",
}

# Signal kinds the OCEAN-expression builder knows how to render.
# Mirrors ``SafeBridge.run_ocean_dump_all`` so the Maestro expression
# matches the PSF dump's per-signal waveform exactly.
_KNOWN_SIGNAL_KINDS: frozenset[str] = frozenset({
    "V", "I", "Vdiff", "Vsum_half",
})


def _format_skill_number(x: float | int) -> str:
    """Render a Python number as an OCEAN/SKILL numeric literal.

    Booleans are rejected (``isinstance(True, int)`` is True in Python,
    so an accidental ``True`` would otherwise silently become ``1``).
    """
    if isinstance(x, bool):
        raise TypeError(f"can't format bool as OCEAN number")
    if not isinstance(x, (int, float)):
        raise TypeError(
            f"expected int or float, got {type(x).__name__}"
        )
    return repr(float(x))


def _waveform_expr(signal_entry: dict) -> str | None:
    """Build the base OCEAN waveform expression for one signal entry.

    Returns None for unknown ``kind`` or malformed ``paths`` — the
    caller treats that as warn-skip.
    """
    kind = signal_entry.get("kind")
    if kind not in _KNOWN_SIGNAL_KINDS:
        return None
    paths = signal_entry.get("paths")
    if paths is None and "path" in signal_entry:
        paths = [signal_entry["path"]]
    if not isinstance(paths, list) or not paths:
        return None
    if not all(isinstance(p, str) and p for p in paths):
        return None
    # SECURITY: spec.signals.paths arrives from user-authored YAML and
    # is f-string-spliced into the OCEAN expression below. Without the
    # ``_PROBE_PATH_RE`` check (reused from safe_bridge — single source
    # of truth), a crafted path like ``/V) 0.0 1e-09)) + average(clip(
    # VT("/SECRET")`` would close the ``VT(...)`` form and inject extra
    # measurements, breaking the spec-containment invariant. Same RE
    # SafeBridge applies before it ships paths to SKILL, so anything
    # the dump pipeline accepts will also clear this gate.
    if not all(_PROBE_PATH_RE.match(p) for p in paths):
        return None
    if kind == "V":
        return f'VT("{paths[0]}")'
    if kind == "I":
        return f'IT("{paths[0]}")'
    if kind == "Vdiff":
        if len(paths) < 2:
            return None
        return f'(VT("{paths[0]}") - VT("{paths[1]}"))'
    if kind == "Vsum_half":
        if len(paths) < 2:
            return None
        return f'((VT("{paths[0]}") + VT("{paths[1]}")) / 2.0)'
    return None


def _windowed_stat_expr(
    wf_expr: str, stat: str, window_bounds: tuple[float, float]
) -> str | None:
    """Wrap ``wf_expr`` with ``clip(... t0 t1)`` and the stat function.

    Returns None for unknown stat names — the caller warn-skips.
    """
    t0, t1 = window_bounds
    clipped = f"clip({wf_expr} {_format_skill_number(t0)} {_format_skill_number(t1)})"
    if stat in _STAT_TO_OCEAN_FN:
        return f"{_STAT_TO_OCEAN_FN[stat]}({clipped})"
    if stat == "mean_abs":
        # safe_ocean.il computes mean_abs as average(clip(abs(w), tStart,
        # tEnd)). The order here is ``abs`` outside ``clip`` because
        # ``abs`` of an already-clipped waveform produces an equivalent
        # result and reads more obviously to a human inspecting the
        # Maestro outputs.
        return f"average(abs({clipped}))"
    return None


def _scale_expr(expr: str, scale: Any) -> str:
    """If ``scale`` is a non-unit finite number, multiply ``expr`` by it.

    Otherwise return ``expr`` unchanged. Non-numeric scale silently
    falls through (the spec_evaluator already would have rejected such
    a spec at parse time).
    """
    if not isinstance(scale, (int, float)) or isinstance(scale, bool):
        return expr
    if scale == 1.0 or scale == 1:
        return expr
    return f"({expr} * {_format_skill_number(scale)})"


def _window_bounds(
    windows: dict, name: Any
) -> tuple[float, float] | None:
    """Resolve a window name to a (t0, t1) tuple of floats, or None."""
    win = windows.get(name) if isinstance(windows, dict) else None
    if not isinstance(win, (list, tuple)) or len(win) != 2:
        return None
    t0, t1 = win
    if isinstance(t0, bool) or isinstance(t1, bool):
        return None
    if not isinstance(t0, (int, float)) or not isinstance(t1, (int, float)):
        return None
    return float(t0), float(t1)


def _component_expr(
    component: Any,
    signals_by_name: dict[str, dict],
    windows: dict,
) -> str | None:
    """Render a (signal, window, stat) sub-mapping as an OCEAN expr.

    Used for ``compound: ratio`` numerator/denominator entries; same
    skip-rules as a simple metric.
    """
    if not isinstance(component, dict):
        return None
    sig = signals_by_name.get(component.get("signal"))
    if sig is None:
        return None
    wf = _waveform_expr(sig)
    if wf is None:
        return None
    bounds = _window_bounds(windows, component.get("window"))
    if bounds is None:
        return None
    return _windowed_stat_expr(wf, component.get("stat"), bounds)


def _build_metric_expr(
    metric: dict,
    signals_by_name: dict[str, dict],
    windows: dict,
) -> str | None:
    """Render one metric's full OCEAN expression with scale applied.

    Returns None for unsupported metric shapes — caller warn-skips.
    """
    compound = metric.get("compound")
    scale = metric.get("scale")

    if compound is None:
        sig = signals_by_name.get(metric.get("signal"))
        if sig is None:
            return None
        wf = _waveform_expr(sig)
        if wf is None:
            return None
        bounds = _window_bounds(windows, metric.get("window"))
        if bounds is None:
            return None
        stat_expr = _windowed_stat_expr(wf, metric.get("stat"), bounds)
        if stat_expr is None:
            return None
        return _scale_expr(stat_expr, scale)

    if compound == "ratio":
        num = _component_expr(
            metric.get("numerator"), signals_by_name, windows,
        )
        den = _component_expr(
            metric.get("denominator"), signals_by_name, windows,
        )
        if num is None or den is None:
            return None
        return _scale_expr(f"({num} / {den})", scale)

    # ``compound: t_cross_frac`` builds a threshold-crossing time that
    # depends on a *runtime-computed* threshold (frac * ref_stat from
    # the dump). Encoding this as one OCEAN expression is possible
    # with nested ``cross(... (<frac> * <ref_stat_expr>) ...)``, but
    # OCEAN's cross() also takes integer args (which-crossing, edge)
    # that don't map 1-to-1 onto our spec's ``direction`` enum. The
    # PC evaluator (run_ocean_t_cross) handles this correctly; mirroring
    # it imperfectly into Maestro could mislead an interactive user.
    # Warn-skip per the leader's "未知 stat warn-skip 不挂" directive.
    return None


def sync_spec_metrics_to_maestro(
    bridge: SafeBridge,
    eval_block: dict,
    *,
    logger: logging.Logger | None = None,
    test: str | None = None,
) -> dict[str, Any]:
    """Mirror every supported spec metric into Maestro Outputs Setup.

    Args:
        bridge: a scoped ``SafeBridge`` (caller must have already
            invoked ``set_scope(lib, cell, tb_cell=...)``).
        eval_block: the dict returned by
            ``spec_evaluator.extract_eval_block`` — must have
            ``signals`` (list of dicts), ``windows`` (dict of
            name → [t0, t1]), and ``metrics`` (list of dicts) keys.
        logger: optional logger; defaults to module logger.
        test: optional Maestro test name override; ``None`` defers to
            ``SafeBridge`` which falls back to the scope's tb_cell.

    Returns:
        ``{"added": [name, ...], "skipped": [(name, reason), ...]}``

    Never raises. Per-metric failures (unsupported shape, bridge
    rejection, missing optional fields) are logged and recorded in
    ``skipped`` so the agent's main loop is never blocked by this
    authoring-convenience sync.

    Idempotency: ``maeAddOutput`` is keyed by output name on the
    Maestro side; calling this twice for the same eval block replaces
    the existing entries rather than duplicating them. The agent
    invokes this once per ``run()`` startup, but a second call on a
    re-entered session is safe. (Verified against Cadence IC23.1 — if
    a future Maestro release breaks name-keyed overwrite semantics
    we'll need an explicit ``maeDeleteOutput`` sweep here, but as of
    IC23.1 the SKILL writer's documented behavior is overwrite.)
    """
    log = logger or _DEFAULT_LOGGER
    added: list[str] = []
    skipped: list[tuple[str, str]] = []

    if not isinstance(eval_block, dict):
        log.warning(
            "maestro_metric_sync: eval_block is not a dict "
            "(got %s); skipping all.", type(eval_block).__name__,
        )
        return {"added": added, "skipped": skipped}

    signals_list = eval_block.get("signals") or []
    if not isinstance(signals_list, list):
        log.warning("maestro_metric_sync: 'signals' is not a list; skipping all.")
        return {"added": added, "skipped": skipped}
    signals_by_name: dict[str, dict] = {}
    for s in signals_list:
        if isinstance(s, dict) and isinstance(s.get("name"), str):
            signals_by_name[s["name"]] = s

    windows = eval_block.get("windows") or {}
    if not isinstance(windows, dict):
        log.warning("maestro_metric_sync: 'windows' is not a dict; skipping all.")
        return {"added": added, "skipped": skipped}

    metrics = eval_block.get("metrics") or []
    if not isinstance(metrics, list):
        log.warning("maestro_metric_sync: 'metrics' is not a list; skipping all.")
        return {"added": added, "skipped": skipped}

    for metric in metrics:
        if not isinstance(metric, dict):
            skipped.append(("<non-dict>", "metric entry is not a mapping"))
            continue
        name = metric.get("name")
        if not isinstance(name, str) or not name:
            skipped.append(("<unnamed>", "missing or non-string name"))
            continue

        try:
            expr = _build_metric_expr(metric, signals_by_name, windows)
        except Exception as exc:  # noqa: BLE001 — never abort sync
            log.warning(
                "maestro_metric_sync: skip %r — expr build raised "
                "%s: %s", name, type(exc).__name__, exc,
            )
            skipped.append((name, f"{type(exc).__name__}: {exc}"))
            continue

        if expr is None:
            log.info(
                "maestro_metric_sync: skip %r — unsupported shape "
                "(compound=%s, stat=%s, signal=%s)",
                name, metric.get("compound"),
                metric.get("stat"), metric.get("signal"),
            )
            skipped.append((name, "unsupported metric shape"))
            continue

        try:
            bridge.add_maestro_output(name=name, expr=expr, test=test)
        except Exception as exc:  # noqa: BLE001 — fail-soft per spec
            log.warning(
                "maestro_metric_sync: add_maestro_output for %r failed "
                "(%s: %s); skipping spec bounds too.",
                name, type(exc).__name__, exc,
            )
            skipped.append((name, f"add_output: {type(exc).__name__}"))
            continue

        # Pass bounds (optional). spec_evaluator validates ``pass`` as
        # [lo, hi] where either may be None. Maestro's maeSetSpec only
        # accepts one bound per call ("More than one spec type passed"),
        # so a double-bounded range must be issued as two separate calls.
        pass_range = metric.get("pass")
        if isinstance(pass_range, (list, tuple)) and len(pass_range) == 2:
            lo, hi = pass_range
            bounds: list[tuple[str, Any]] = []
            if isinstance(lo, (int, float)) and not isinstance(lo, bool):
                bounds.append(("gt", lo))
            if isinstance(hi, (int, float)) and not isinstance(hi, bool):
                bounds.append(("lt", hi))
            for kind, value in bounds:
                try:
                    bridge.set_maestro_spec(
                        name=name, test=test,
                        **{kind: _format_skill_number(value)},
                    )
                except Exception as exc:  # noqa: BLE001 — fail-soft
                    log.warning(
                        "maestro_metric_sync: set_maestro_spec(%s) for "
                        "%r failed (%s: %s); output landed without that "
                        "bound.",
                        kind, name, type(exc).__name__, exc,
                    )
                    # Output itself succeeded — keep it in ``added``.

        added.append(name)

    log.info(
        "maestro_metric_sync: synced %d output(s); skipped %d.",
        len(added), len(skipped),
    )
    return {"added": added, "skipped": skipped}
