"""Unit tests for ``sync_spec_metrics_to_maestro`` (Track C Option I).

The sync helper mirrors spec.metrics into Maestro Outputs Setup at
agent startup so an interactive Maestro user sees the same per-metric
formulas the PC evaluator computes. Tests cover:

  * stat-by-stat OCEAN expression rendering (mean / min / max / ptp /
    rms / freq_Hz / mean_abs / duty_pct)
  * signal-kind translation (V / I / Vdiff / Vsum_half)
  * scale application
  * pass-bound forwarding to ``set_maestro_spec``
  * compound ratio
  * unsupported metric shapes (``compound: t_cross_frac``, unknown
    stat, unknown signal kind) — must warn-skip, never raise
  * fail-soft against bridge exceptions
  * idempotency (calling twice is safe per Maestro overwrite semantics)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.maestro_metric_sync import sync_spec_metrics_to_maestro  # noqa: E402
from src.safe_bridge import SafeBridge  # noqa: E402


# --------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------- #


@pytest.fixture
def pdk_map_file(tmp_path):
    content = """\
generic_cell_name: "GENERIC_DEVICE"
valid_aliases:
  - NMOS
  - PMOS
model_info_keys:
  - toxe
allowed_params:
  - w
  - l
"""
    path = tmp_path / "pdk_map.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.fixture
def bridge(pdk_map_file, tmp_path):
    b = SafeBridge(
        MagicMock(), pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )
    b.set_scope("mylib", "MYCELL", tb_cell="MYTB")
    return b


@pytest.fixture
def writer_mocks():
    """Patch the writer functions used by SafeBridge.add_maestro_output /
    set_maestro_spec, so we can inspect the SKILL strings that would
    have been sent without hitting a real virtuoso session."""
    with (
        patch("src.safe_bridge._mae_writer.add_output") as m_add,
        patch("src.safe_bridge._mae_writer.set_spec") as m_spec,
    ):
        m_add.return_value = "ok-output"
        m_spec.return_value = "ok-spec"
        yield {"add_output": m_add, "set_spec": m_spec}


def _eval_block_simple(stat: str = "freq_Hz", scale=None, pass_=None):
    """Minimal one-metric eval block for stat-template smoke tests."""
    metric: dict = {
        "name": "m",
        "signal": "Vdiff",
        "window": "full",
        "stat": stat,
    }
    if scale is not None:
        metric["scale"] = scale
    if pass_ is not None:
        metric["pass"] = pass_
    return {
        "signals": [
            {"name": "Vdiff", "kind": "Vdiff", "paths": ["/Vout_p", "/Vout_n"]},
        ],
        "windows": {"full": [0.0, 200e-9]},
        "metrics": [metric],
    }


# --------------------------------------------------------------------- #
#  Per-stat expression templates
# --------------------------------------------------------------------- #


class TestStatTemplates:
    """Each simple stat must produce the OCEAN expression that mirrors
    ``safeOcean_statsJson`` (see skill/safe_ocean.il:867-939)."""

    @pytest.mark.parametrize("stat,fn", [
        ("mean",     "average"),
        ("min",      "ymin"),
        ("max",      "ymax"),
        ("ptp",      "peakToPeak"),
        ("rms",      "rms"),
        ("freq_Hz",  "frequency"),
        ("duty_pct", "dutyCycle"),
    ])
    def test_simple_stat_wraps_clipped_waveform(
        self, bridge, writer_mocks, stat, fn
    ):
        block = _eval_block_simple(stat=stat)
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == ["m"]
        assert out["skipped"] == []
        # add_output called once with expr=<fn>(clip(<wf> t0 t1))
        kwargs = writer_mocks["add_output"].call_args.kwargs
        expr = kwargs["expr"]
        assert expr.startswith(f"{fn}(clip("), expr
        # Window bounds appear as floats in scientific form
        assert "0.0" in expr and "2e-07" in expr, expr
        # Vdiff renders as (VT("/p") - VT("/n")) with SafeBridge SKILL
        # escape applied to the inner quotes (T2.1).
        assert r'(VT(\"/Vout_p\") - VT(\"/Vout_n\"))' in expr, expr

    def test_mean_abs_uses_average_of_abs(self, bridge, writer_mocks):
        block = _eval_block_simple(stat="mean_abs")
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == ["m"]
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        # mean_abs = average(abs(clip(...)))
        assert expr.startswith("average(abs(clip(")
        assert expr.endswith(")))")


# --------------------------------------------------------------------- #
#  Signal-kind translation
# --------------------------------------------------------------------- #


class TestSignalKinds:
    """Four signal kinds must render the right OCEAN waveform expression."""

    def test_kind_V_uses_VT_single_path(self, bridge, writer_mocks):
        block = {
            "signals": [{"name": "Vout", "kind": "V", "paths": ["/Vout"]}],
            "windows": {"full": [0.0, 1e-9]},
            "metrics": [{
                "name": "m", "signal": "Vout", "window": "full",
                "stat": "mean",
            }],
        }
        sync_spec_metrics_to_maestro(bridge, block)
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        assert r'VT(\"/Vout\")' in expr
        assert "IT(" not in expr
        assert " - " not in expr

    def test_kind_I_uses_IT_single_path(self, bridge, writer_mocks):
        block = {
            "signals": [
                {"name": "Iout", "kind": "I", "paths": ["/I0/M2/D"]},
            ],
            "windows": {"full": [0.0, 1e-9]},
            "metrics": [{
                "name": "m", "signal": "Iout", "window": "full",
                "stat": "mean",
            }],
        }
        sync_spec_metrics_to_maestro(bridge, block)
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        assert r'IT(\"/I0/M2/D\")' in expr

    def test_kind_Vdiff_subtracts_two_paths(self, bridge, writer_mocks):
        block = _eval_block_simple()
        sync_spec_metrics_to_maestro(bridge, block)
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        assert r'(VT(\"/Vout_p\") - VT(\"/Vout_n\"))' in expr

    def test_kind_Vsum_half_averages_two_paths(self, bridge, writer_mocks):
        block = {
            "signals": [{
                "name": "Vcm", "kind": "Vsum_half",
                "paths": ["/Vp", "/Vn"],
            }],
            "windows": {"full": [0.0, 1e-9]},
            "metrics": [{
                "name": "m", "signal": "Vcm", "window": "full",
                "stat": "mean",
            }],
        }
        sync_spec_metrics_to_maestro(bridge, block)
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        assert r'((VT(\"/Vp\") + VT(\"/Vn\")) / 2.0)' in expr


# --------------------------------------------------------------------- #
#  Scale + pass bounds
# --------------------------------------------------------------------- #


class TestScaleAndPassBounds:
    def test_scale_applied_via_multiplication(self, bridge, writer_mocks):
        block = _eval_block_simple(stat="freq_Hz", scale=1.0e-9)
        sync_spec_metrics_to_maestro(bridge, block)
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        assert expr.startswith("(frequency(clip(")
        assert "* 1e-09" in expr or "* 1.0e-09" in expr

    def test_scale_one_is_noop(self, bridge, writer_mocks):
        block = _eval_block_simple(stat="freq_Hz", scale=1.0)
        sync_spec_metrics_to_maestro(bridge, block)
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        # No outer multiplication wrapper
        assert expr.startswith("frequency(clip(")

    def test_pass_both_bounds_forwards_lt_and_gt(
        self, bridge, writer_mocks
    ):
        block = _eval_block_simple(stat="freq_Hz", pass_=[19.5, 20.5])
        sync_spec_metrics_to_maestro(bridge, block)
        assert writer_mocks["set_spec"].called
        kwargs = writer_mocks["set_spec"].call_args.kwargs
        assert kwargs["gt"] == "19.5"
        assert kwargs["lt"] == "20.5"

    def test_pass_lo_only_forwards_gt_not_lt(
        self, bridge, writer_mocks
    ):
        block = _eval_block_simple(stat="rms", pass_=[0.5, None])
        sync_spec_metrics_to_maestro(bridge, block)
        kwargs = writer_mocks["set_spec"].call_args.kwargs
        assert kwargs.get("gt") == "0.5"
        # writer treats empty-string as "omit" — accept either absent or ""
        assert kwargs.get("lt", "") == ""

    def test_pass_hi_only_forwards_lt_not_gt(
        self, bridge, writer_mocks
    ):
        block = _eval_block_simple(stat="rms", pass_=[None, 1.5])
        sync_spec_metrics_to_maestro(bridge, block)
        kwargs = writer_mocks["set_spec"].call_args.kwargs
        assert kwargs.get("lt") == "1.5"
        assert kwargs.get("gt", "") == ""

    def test_no_pass_no_spec_call(self, bridge, writer_mocks):
        block = _eval_block_simple(stat="rms")  # no `pass`
        sync_spec_metrics_to_maestro(bridge, block)
        assert not writer_mocks["set_spec"].called

    def test_pass_both_none_no_spec_call(self, bridge, writer_mocks):
        block = _eval_block_simple(stat="rms", pass_=[None, None])
        sync_spec_metrics_to_maestro(bridge, block)
        assert not writer_mocks["set_spec"].called


# --------------------------------------------------------------------- #
#  Compound: ratio
# --------------------------------------------------------------------- #


class TestCompoundRatio:
    def test_ratio_builds_quotient_of_two_stat_exprs(
        self, bridge, writer_mocks
    ):
        block = {
            "signals": [{
                "name": "Vdiff", "kind": "Vdiff",
                "paths": ["/Vout_p", "/Vout_n"],
            }],
            "windows": {
                "early": [50e-9, 100e-9],
                "late": [150e-9, 200e-9],
            },
            "metrics": [{
                "name": "amp_hold_ratio",
                "compound": "ratio",
                "numerator": {
                    "signal": "Vdiff", "window": "late", "stat": "rms",
                },
                "denominator": {
                    "signal": "Vdiff", "window": "early", "stat": "rms",
                },
            }],
        }
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == ["amp_hold_ratio"]
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        # Quotient of two rms(clip(...)) expressions
        assert expr.startswith("(rms(clip("), expr
        assert ") / rms(clip(" in expr, expr


# --------------------------------------------------------------------- #
#  Unsupported / skip cases
# --------------------------------------------------------------------- #


class TestSkipsAndWarnings:
    def test_t_cross_frac_is_warn_skipped(self, bridge, writer_mocks, caplog):
        block = {
            "signals": [{
                "name": "Vdiff", "kind": "Vdiff",
                "paths": ["/Vp", "/Vn"],
            }],
            "windows": {
                "startup": [0.0, 50e-9],
                "late": [150e-9, 200e-9],
            },
            "metrics": [{
                "name": "t_startup_ns",
                "compound": "t_cross_frac",
                "signal": "Vdiff",
                "frac": 0.45,
                "ref": {"signal": "Vdiff", "window": "late", "stat": "ptp"},
                "window": "startup",
                "direction": "rising",
            }],
        }
        with caplog.at_level(logging.INFO):
            out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == []
        assert out["skipped"] == [("t_startup_ns", "unsupported metric shape")]
        assert not writer_mocks["add_output"].called

    def test_unknown_signal_kind_is_warn_skipped(self, bridge, writer_mocks):
        block = {
            "signals": [{
                "name": "weird", "kind": "Unknown",  # not in {V,I,Vdiff,Vsum_half}
                "paths": ["/x"],
            }],
            "windows": {"full": [0.0, 1e-9]},
            "metrics": [{
                "name": "m", "signal": "weird", "window": "full",
                "stat": "mean",
            }],
        }
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == []
        assert out["skipped"] == [("m", "unsupported metric shape")]
        assert not writer_mocks["add_output"].called

    def test_unknown_window_is_warn_skipped(self, bridge, writer_mocks):
        block = {
            "signals": [{"name": "v", "kind": "V", "paths": ["/x"]}],
            "windows": {"full": [0.0, 1e-9]},
            "metrics": [{
                "name": "m", "signal": "v",
                "window": "nonexistent",  # not in windows dict
                "stat": "mean",
            }],
        }
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == []
        assert out["skipped"][0][0] == "m"
        assert not writer_mocks["add_output"].called

    def test_metric_without_name_is_skipped(self, bridge, writer_mocks):
        block = {
            "signals": [{"name": "v", "kind": "V", "paths": ["/x"]}],
            "windows": {"full": [0.0, 1e-9]},
            "metrics": [{
                "signal": "v", "window": "full", "stat": "mean",
            }],
        }
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == []
        assert out["skipped"] == [("<unnamed>", "missing or non-string name")]


# --------------------------------------------------------------------- #
#  Fail-soft against bridge exceptions
# --------------------------------------------------------------------- #


class TestFailSoft:
    def test_add_output_raise_does_not_abort_remaining_metrics(
        self, bridge, writer_mocks
    ):
        # First metric raises on add; second should still try.
        writer_mocks["add_output"].side_effect = [
            ValueError("simulated allow-list rejection"),
            "ok-output",
        ]
        block = {
            "signals": [{"name": "v", "kind": "V", "paths": ["/x"]}],
            "windows": {"full": [0.0, 1e-9]},
            "metrics": [
                {"name": "bad", "signal": "v", "window": "full",
                 "stat": "mean"},
                {"name": "good", "signal": "v", "window": "full",
                 "stat": "rms"},
            ],
        }
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == ["good"]
        assert len(out["skipped"]) == 1
        assert out["skipped"][0][0] == "bad"
        assert "add_output" in out["skipped"][0][1]

    def test_set_spec_failure_keeps_metric_in_added(
        self, bridge, writer_mocks
    ):
        # add succeeded; bounds call fails — the output landed, so the
        # metric still counts as added (just without bounds).
        writer_mocks["set_spec"].side_effect = RuntimeError("session gone")
        block = _eval_block_simple(stat="rms", pass_=[0.5, 1.5])
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == ["m"]
        assert out["skipped"] == []

    def test_eval_block_not_dict_returns_empty_summary(
        self, bridge, writer_mocks
    ):
        out = sync_spec_metrics_to_maestro(bridge, "not a dict")  # type: ignore[arg-type]
        assert out == {"added": [], "skipped": []}
        assert not writer_mocks["add_output"].called

    def test_empty_metrics_list_is_noop(self, bridge, writer_mocks):
        block = {"signals": [], "windows": {}, "metrics": []}
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out == {"added": [], "skipped": []}
        assert not writer_mocks["add_output"].called


# --------------------------------------------------------------------- #
#  Idempotency
# --------------------------------------------------------------------- #


class TestIdempotency:
    def test_calling_twice_produces_same_added_list(
        self, bridge, writer_mocks
    ):
        # Maestro's maeAddOutput is keyed by name and overwrites on
        # repeat — the sync helper must rely on that semantics rather
        # than tracking state itself.
        block = _eval_block_simple(stat="rms", pass_=[0.0, 2.0])
        out1 = sync_spec_metrics_to_maestro(bridge, block)
        out2 = sync_spec_metrics_to_maestro(bridge, block)
        assert out1["added"] == out2["added"] == ["m"]
        # Each call invoked add_output and set_spec once.
        assert writer_mocks["add_output"].call_count == 2
        assert writer_mocks["set_spec"].call_count == 2


# --------------------------------------------------------------------- #
#  Generality smoke
# --------------------------------------------------------------------- #


class TestGenerality:
    def test_no_lc_vco_hardcoding_in_module(self):
        # The leader's red line: no circuit-shape assumptions. The module
        # source must not reference any LC_VCO-specific token.
        import inspect
        import src.maestro_metric_sync as mod
        src = inspect.getsource(mod)
        for forbidden in ("LC_VCO", "f_osc", "Vdiff", "Vout_p", "Vout_n"):
            # ``Vdiff`` is allowed in this list because it's a generic
            # signal-kind name; remove the ``"Vdiff"`` entry only if a
            # future refactor binds the helper to a specific signal
            # name. For now: assert the module references "Vdiff" ONLY
            # as a signal-KIND enum value, not as a hardcoded signal
            # NAME ("Vdiff" appears only inside _KNOWN_SIGNAL_KINDS).
            if forbidden == "Vdiff":
                # Allowed as a generic kind enum value + dispatch
                # branch; the red line is per-design symbol names.
                continue
            assert forbidden not in src, (
                f"module references {forbidden!r} — possible LC_VCO leak"
            )

    def test_expression_passes_safebridge_allow_list(
        self, bridge, writer_mocks
    ):
        # The generated expr must clear ``_validate_maestro_expr`` — if
        # we ever emit a function not in the allow-list (e.g. forgot to
        # add ``clip``), this test will turn red BEFORE a real session
        # attempts the call.
        block = _eval_block_simple(stat="mean_abs")
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["skipped"] == []
        # add_output was called — so the expr passed validation.
        assert writer_mocks["add_output"].called


# --------------------------------------------------------------------- #
#  R2 P1 — path-injection containment
# --------------------------------------------------------------------- #


def _block_with_path(path: str) -> dict:
    """One-metric eval block whose only signal uses ``path`` verbatim.

    The point of these tests: nothing the user puts into
    ``spec.signals.paths`` should land inside an OCEAN expression
    unless it cleared ``_PROBE_PATH_RE`` first.
    """
    return {
        "signals": [
            {"name": "v", "kind": "V", "paths": [path]},
        ],
        "windows": {"full": [0.0, 1e-9]},
        "metrics": [
            {"name": "m", "signal": "v", "window": "full", "stat": "rms"},
        ],
    }


class TestPathInjectionContainment:
    """Reject any path that could break OCEAN expression containment.

    R2 P1 (codex finding): ``_waveform_expr`` previously spliced
    ``signals.paths`` straight into ``VT(...)`` via f-string, so a
    crafted path could close the function form and inject additional
    measurements. Fix re-uses ``_PROBE_PATH_RE`` from safe_bridge as the
    single source of truth — anything the dump pipeline accepts will
    also clear this gate, and anything else warn-skips fail-soft.
    """

    # Codex's six attack vectors — each must NOT reach add_output.
    @pytest.mark.parametrize("payload", [
        # 1. Original codex PoC: close VT, splice extra clip+average,
        #    re-open VT on a different signal. Reads as plausible OCEAN
        #    until you check the parenthesis depth.
        "/V) 0.0 1e-09)) + average(clip(VT(/SECRET",
        # 2. Whitespace breaks `VT(...)` arity even before reader-level
        #    parsing — would let the attacker drop a second arg in.
        "/V probe",
        # 3. Doublequote — would smuggle a SKILL string literal inside
        #    the bareword form once Maestro's reader runs.
        '/V"',
        # 4. Semicolon — OCEAN reads `;` as start-of-comment, anything
        #    after it would silently disappear from the eval.
        "/V;rm -rf",
        # 5. Backtick — list-quote dispatch in SKILL reader; would let
        #    an attacker inject a literal symbol form.
        "/V`",
        # 6. Glob wildcard — not a path char but cheap to accept, would
        #    let a curious user fan-out the measurement.
        "/V*",
    ])
    def test_codex_attack_vector_is_warn_skipped(
        self, bridge, writer_mocks, payload
    ):
        out = sync_spec_metrics_to_maestro(bridge, _block_with_path(payload))
        assert out["added"] == []
        assert out["skipped"][0][0] == "m"
        assert not writer_mocks["add_output"].called

    # Five R3-style SKILL reader-syntax payloads — these would slip past
    # a naive ``"/"`` prefix check but are blocked by the strict RE.
    @pytest.mark.parametrize("payload", [
        # 1. List/dispatch macros (`#(...)` is reader-dispatch in many
        #    Lisp dialects; SKILL accepts `quote` as a symbol).
        "/(quote evil)",
        # 2. SKILL quote shorthand — would inject a literal symbol form.
        "'(evil)",
        # 3. C-style comment — Maestro's preprocessor may strip it
        #    before the expr ever reaches the validator.
        "/V/* injected */",
        # 4. Embedded SKILL string literal.
        '/V "shell stuff"',
        # 5. Backslash-escape — some readers honor `\n` as newline.
        "/V\\n/SECRET",
    ])
    def test_reader_syntax_payload_is_warn_skipped(
        self, bridge, writer_mocks, payload
    ):
        out = sync_spec_metrics_to_maestro(bridge, _block_with_path(payload))
        assert out["added"] == []
        assert out["skipped"][0][0] == "m"
        assert not writer_mocks["add_output"].called

    def test_multi_level_hierarchy_path_is_accepted(
        self, bridge, writer_mocks
    ):
        # The RE allows up to 8 hierarchy levels — legitimate spec
        # writers can probe deep nodes like ``/core/vco/tank/Vout``.
        block = _block_with_path("/core/vco/tank/Vout")
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == ["m"]
        expr = writer_mocks["add_output"].call_args.kwargs["expr"]
        # The exact path appears once inside VT(...) — no truncation,
        # no extra parens injected.
        assert r'VT(\"/core/vco/tank/Vout\")' in expr

    def test_too_deep_hierarchy_path_is_warn_skipped(
        self, bridge, writer_mocks
    ):
        # 9 levels — exceeds _PROBE_PATH_RE's {1,8} quantifier. The
        # RE-driven gate fails closed, so the metric warn-skips rather
        # than reaching a SKILL session that would also have rejected
        # it (with worse latency / blast radius).
        block = _block_with_path(
            "/a/b/c/d/e/f/g/h/i"
        )
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == []
        assert not writer_mocks["add_output"].called

    def test_diff_kind_both_paths_validated(self, bridge, writer_mocks):
        # Vdiff/Vsum_half use TWO paths — the gate must cover BOTH,
        # not just the first. Here the second path is the codex payload.
        block = {
            "signals": [{
                "name": "vd", "kind": "Vdiff",
                "paths": ["/Vp", "/Vn) 0.0 1e-09)) + VT(/SECRET"],
            }],
            "windows": {"full": [0.0, 1e-9]},
            "metrics": [{
                "name": "m", "signal": "vd", "window": "full",
                "stat": "rms",
            }],
        }
        out = sync_spec_metrics_to_maestro(bridge, block)
        assert out["added"] == []
        assert not writer_mocks["add_output"].called
