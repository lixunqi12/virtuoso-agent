"""Unit tests for SafeBridge path-2 sweep read primitives (2026-05-19).

Mocks ``_execute_skill_json`` so no remote connection is needed. The
defense-in-depth regexes (``_SAFE_SWEEP_ROOT_RE``,
``_SAFE_INTERACTIVE_TAIL_RE``) and the per-point psfDir assembly are
the only attack surface for path-2; everything else flows back through
the existing single-point dump path that ``test_safe_bridge.py``
already covers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.safe_bridge import SafeBridge  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures (mirror test_safe_bridge.py so the surface is identical)
# --------------------------------------------------------------------------

@pytest.fixture
def pdk_map_file(tmp_path):
    content = """\
generic_cell_name: \"GENERIC_DEVICE\"

valid_aliases:
  - NMOS
  - PMOS
  - MIM_CAP

model_info_keys:
  - toxe
  - u0

allowed_params:
  - w
  - l
  - nf
"""
    path = tmp_path / "pdk_map.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def bridge(mock_client, pdk_map_file, tmp_path, monkeypatch):
    """SafeBridge with SKILL loading bypassed; tb_cell pre-bound."""
    b = SafeBridge(
        mock_client, pdk_map_file,
        skill_dir=tmp_path / "no_skill",
    )
    monkeypatch.setattr(b, "_skill_loaded", True)
    b._scope_tb_cell = "pll_LC_VCO_tb"
    return b


# --------------------------------------------------------------------------
# _validate_sweep_root — defense-in-depth gate
# --------------------------------------------------------------------------

def test_sweep_root_accepts_valid_interactive_path():
    SafeBridge._validate_sweep_root(
        "/home/u/sim/cell_tb/maestro/results/maestro/Interactive.0"
    )
    SafeBridge._validate_sweep_root(
        "/home/u/sim/cell_tb/maestro/results/maestro/Interactive.42/"
    )


def test_sweep_root_rejects_etc_passwd_style():
    with pytest.raises(ValueError, match=r"(?i)interactive"):
        SafeBridge._validate_sweep_root("/etc/passwd")


def test_sweep_root_rejects_shell_metachars():
    with pytest.raises(ValueError, match=r"(?i)illegal"):
        SafeBridge._validate_sweep_root(
            "/home/u/sim;rm -rf/Interactive.0"
        )
    with pytest.raises(ValueError, match=r"(?i)illegal"):
        SafeBridge._validate_sweep_root(
            "/home/u/sim$(whoami)/Interactive.0"
        )
    with pytest.raises(ValueError, match=r"(?i)illegal"):
        SafeBridge._validate_sweep_root(
            '/home/u/sim"injected/Interactive.0'
        )


def test_sweep_root_rejects_missing_interactive_tail():
    with pytest.raises(ValueError, match=r"(?i)interactive"):
        SafeBridge._validate_sweep_root(
            "/home/u/sim/maestro/results/maestro/Configuration.0"
        )


def test_sweep_root_rejects_non_string():
    with pytest.raises(ValueError):
        SafeBridge._validate_sweep_root(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SafeBridge._validate_sweep_root(42)  # type: ignore[arg-type]


def test_sweep_root_rejects_overlength():
    long = "/a" * 200 + "/Interactive.0"
    with pytest.raises(ValueError, match=r"(?i)too long|illegal"):
        SafeBridge._validate_sweep_root(long)


def test_sweep_root_rejects_empty():
    with pytest.raises(ValueError):
        SafeBridge._validate_sweep_root("")


# R2 (2026-05-19, codex P1-1) — path traversal probes that the charset
# regex alone allowed because every character is individually inside the
# alphabet. Each must raise BEFORE _SAFE_INTERACTIVE_TAIL_RE / charset
# fires, so the assertion is just "ValueError" with a traversal-shaped
# message; the exact wording is left to the validator.

def test_sweep_root_rejects_relative_traversal_prefix():
    """`../Interactive.1` passes charset + Interactive tail but escapes
    the intended Maestro root."""
    with pytest.raises(ValueError, match=r"(?i)absolute|relative|\.\.|segment"):
        SafeBridge._validate_sweep_root("../Interactive.1")


def test_sweep_root_rejects_dotdot_middle_segment():
    """The classic chroot escape: a properly-shaped Interactive tail
    can still walk out via `..` segments in the middle of the path."""
    with pytest.raises(ValueError, match=r"(?i)\.\.|segment"):
        SafeBridge._validate_sweep_root(
            "/home/u/sim/Interactive.0/../../secret/Interactive.1"
        )


def test_sweep_root_rejects_single_dot_segment():
    """`/./` is rarely malicious on its own but indicates a caller
    constructed the path incorrectly — reject for hygiene + parity with
    `..`."""
    with pytest.raises(ValueError, match=r"(?i)\.|segment"):
        SafeBridge._validate_sweep_root(
            "/home/u/sim/./maestro/Interactive.0"
        )


def test_sweep_root_rejects_double_slash():
    """`//` between segments would normalize to a single `/` on some
    filesystems but is a code-smell about caller hygiene; explicit
    reject prevents `/path//../Interactive.0`-style tricks."""
    with pytest.raises(ValueError, match=r"(?i)//|segment"):
        SafeBridge._validate_sweep_root(
            "/home/u//sim/maestro/Interactive.0"
        )


# --------------------------------------------------------------------------
# read_sweep_manifest — happy + degenerate
# --------------------------------------------------------------------------

def _manifest_response(entries: list[dict]) -> dict:
    return {"ok": True, "raw": json.dumps(entries)}


def test_read_sweep_manifest_parses_and_sorts(bridge, monkeypatch):
    entries = [
        {"point": 3, "vctrl": 0.4},
        {"point": 1, "vctrl": 0.0},
        {"point": 2, "vctrl": 0.2},
    ]
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: _manifest_response(entries),
    )
    out = bridge.read_sweep_manifest(
        "/home/u/sim/Interactive.0"
    )
    assert list(out.keys()) == [1, 2, 3]
    assert out[1] == pytest.approx(0.0)
    assert out[3] == pytest.approx(0.4)


def test_read_sweep_manifest_invokes_safe_entrypoint(bridge, monkeypatch):
    captured = {}

    def fake_exec(expr):
        captured["expr"] = expr
        return _manifest_response([{"point": 1, "vctrl": 0.0}])

    monkeypatch.setattr(bridge, "_execute_skill_json", fake_exec)
    bridge.read_sweep_manifest("/home/u/sim/Interactive.7")
    assert captured["expr"].startswith("safeReadSweepManifest(")
    assert "/Interactive.7" in captured["expr"]


def test_read_sweep_manifest_rejects_bad_root(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError):
        bridge.read_sweep_manifest("/etc/passwd")


def test_read_sweep_manifest_skill_not_ok(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: {"ok": False, "error": "file missing"},
    )
    with pytest.raises(RuntimeError, match=r"safeReadSweepManifest failed"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_read_sweep_manifest_invalid_json(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: {"ok": True, "raw": "not-json"},
    )
    with pytest.raises(RuntimeError, match=r"(?i)not valid json"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_read_sweep_manifest_empty_list(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: {"ok": True, "raw": "[]"},
    )
    with pytest.raises(RuntimeError, match=r"non-empty"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_read_sweep_manifest_duplicate_point(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: _manifest_response([
            {"point": 1, "vctrl": 0.0},
            {"point": 1, "vctrl": 0.2},
        ]),
    )
    with pytest.raises(RuntimeError, match=r"duplicate"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_read_sweep_manifest_non_finite_vctrl(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: _manifest_response([
            {"point": 1, "vctrl": float("inf")},
        ]),
    )
    with pytest.raises(RuntimeError, match=r"(?i)non-finite"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_read_sweep_manifest_rejects_bool_coercion(bridge, monkeypatch):
    """R2 (2026-05-19, codex P3) — strict coercion. `{"point": true,
    "vctrl": false}` round-trips through `int(True)`/`float(False)` to
    `(1, 0.0)` silently in vanilla Python; the manifest reader must
    catch this serializer bug instead of treating it as a real point."""
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: _manifest_response([
            {"point": True, "vctrl": False},
        ]),
    )
    with pytest.raises(RuntimeError, match=r"(?i)boolean|bool"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_read_sweep_manifest_point_out_of_range(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: _manifest_response([
            {"point": 9999, "vctrl": 0.0},
        ]),
    )
    with pytest.raises(RuntimeError, match=r"(?i)outside"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


# --------------------------------------------------------------------------
# run_ocean_dump_all_swept — psfDir assembly + per-point failure tolerance
# --------------------------------------------------------------------------

_SIGNALS = [("Vdiff", "Vdiff", ["/Vout_p", "/Vout_n"])]
_WINDOWS = [("late", 1.5e-7, 2.0e-7)]


def test_run_swept_assembles_psf_dir_per_point(bridge, monkeypatch):
    seen_exprs: list[str] = []

    def fake_exec(expr):
        seen_exprs.append(expr)
        return {"ok": True, "values": {"Vdiff": {"late": {"freq_Hz": 2e10}}}}

    monkeypatch.setattr(bridge, "_execute_skill_json", fake_exec)
    sweep_root = "/home/u/sim/cell_tb/maestro/results/maestro/Interactive.0"
    out = bridge.run_ocean_dump_all_swept(
        _SIGNALS, _WINDOWS,
        sweep_root=sweep_root,
        points=[1, 2, 3],
    )
    assert set(out.keys()) == {1, 2, 3}
    assert all(out[p]["ok"] for p in (1, 2, 3))
    assert len(seen_exprs) == 3
    for point, expr in zip([1, 2, 3], seen_exprs):
        expected = f'"{sweep_root}/{point}/pll_LC_VCO_tb_1/psf"'
        assert expected in expr, (
            f"point {point}: missing psfDir {expected} in {expr!r}"
        )
        assert expr.startswith("safeOceanDumpAll(")


def test_run_swept_strips_trailing_slash_on_root(bridge, monkeypatch):
    seen_exprs: list[str] = []

    def fake_exec(expr):
        seen_exprs.append(expr)
        return {"ok": True, "values": {}}

    monkeypatch.setattr(bridge, "_execute_skill_json", fake_exec)
    bridge.run_ocean_dump_all_swept(
        _SIGNALS, _WINDOWS,
        sweep_root="/home/u/sim/Interactive.4/",
        points=[1],
    )
    assert '"/home/u/sim/Interactive.4/1/pll_LC_VCO_tb_1/psf"' in seen_exprs[0]


def test_run_swept_per_point_failure_is_captured(bridge, monkeypatch):
    def fake_exec(expr):
        if '/2/' in expr:
            raise RuntimeError("skill blew up")
        if '/3/' in expr:
            return {"ok": False, "error": "psf missing"}
        return {"ok": True, "values": {}}

    monkeypatch.setattr(bridge, "_execute_skill_json", fake_exec)
    out = bridge.run_ocean_dump_all_swept(
        _SIGNALS, _WINDOWS,
        sweep_root="/home/u/sim/Interactive.0",
        points=[1, 2, 3],
    )
    assert out[1]["ok"] is True
    assert out[2]["ok"] is False
    assert out[2]["error"] == "skill_exception"
    assert out[3]["ok"] is False


def test_run_swept_open_results_failure_does_not_leak_prior_point(
    bridge, monkeypatch
):
    """R2 (2026-05-19, codex P1-2) — SKILL-side stale-data fixture.

    Mirror what the hardened `safeOceanDumpAll(psfDir)` now returns when
    its `openResults` errset fails: ``{"ok": false, "error":
    "openResults failed"}``. The per-point loop must capture that as
    failed for the affected point and the next successful point must
    return ITS OWN values — not the prior point's dump aliased through
    a stale `selectResult` handle.
    """
    expected_p1 = {"Vdiff": {"late": {"freq_Hz": 1.9e10}}}
    expected_p3 = {"Vdiff": {"late": {"freq_Hz": 2.1e10}}}

    def fake_exec(expr):
        if '/1/' in expr:
            return {"ok": True, "dumps": expected_p1}
        if '/2/' in expr:
            return {"ok": False, "error": "openResults failed"}
        if '/3/' in expr:
            return {"ok": True, "dumps": expected_p3}
        pytest.fail(f"unexpected point in expr: {expr}")

    monkeypatch.setattr(bridge, "_execute_skill_json", fake_exec)
    out = bridge.run_ocean_dump_all_swept(
        _SIGNALS, _WINDOWS,
        sweep_root="/home/u/sim/Interactive.0",
        points=[1, 2, 3],
    )
    assert out[1]["ok"] is True
    assert out[1]["dumps"] == expected_p1
    assert out[2]["ok"] is False
    assert "openResults" in out[2]["error"]
    # Stale-data guard: point 3's dump must be its own, not point 1's
    # aliased through a leftover selectResult handle.
    assert out[3]["ok"] is True
    assert out[3]["dumps"] == expected_p3
    assert out[3]["dumps"] != out[1]["dumps"]


def test_run_swept_rejects_bad_root(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError):
        bridge.run_ocean_dump_all_swept(
            _SIGNALS, _WINDOWS,
            sweep_root="/etc/passwd",
            points=[1],
        )


def test_run_swept_requires_tb_cell(mock_client, pdk_map_file, tmp_path, monkeypatch):
    """No _scope_tb_cell set and no tb_cell kwarg → RuntimeError before any SKILL call."""
    b = SafeBridge(
        mock_client, pdk_map_file, skill_dir=tmp_path / "no_skill",
    )
    monkeypatch.setattr(b, "_skill_loaded", True)
    monkeypatch.setattr(
        b, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(RuntimeError, match=r"(?i)tb_cell"):
        b.run_ocean_dump_all_swept(
            _SIGNALS, _WINDOWS,
            sweep_root="/home/u/sim/Interactive.0",
            points=[1],
        )


def test_run_swept_tb_cell_kwarg_overrides_scope(bridge, monkeypatch):
    seen_exprs: list[str] = []
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: (seen_exprs.append(expr), {"ok": True})[1],
    )
    bridge.run_ocean_dump_all_swept(
        _SIGNALS, _WINDOWS,
        sweep_root="/home/u/sim/Interactive.0",
        points=[5],
        tb_cell="other_tb",
    )
    assert '"/home/u/sim/Interactive.0/5/other_tb_1/psf"' in seen_exprs[0]


def test_run_swept_result_test_kwarg_overrides_psf_dir_only(bridge, monkeypatch):
    seen_exprs: list[str] = []
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: (seen_exprs.append(expr), {"ok": True})[1],
    )
    bridge.run_ocean_dump_all_swept(
        _SIGNALS, _WINDOWS,
        sweep_root="/home/u/sim/Interactive.0",
        points=[5],
        tb_cell="LC_VCO_tb",
        result_test="pll_LC_VCO_tb_1",
    )
    assert '"/home/u/sim/Interactive.0/5/pll_LC_VCO_tb_1/psf"' in seen_exprs[0]


def test_run_swept_rejects_colon_result_test_leaf(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)result_test.*colon"):
        bridge.run_ocean_dump_all_swept(
            _SIGNALS, _WINDOWS,
            sweep_root="/home/u/sim/Interactive.0",
            points=[5],
            tb_cell="LC_VCO_tb",
            result_test="lib:cell:1",
        )


def test_run_swept_rejects_duplicate_points(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"duplicate"):
        bridge.run_ocean_dump_all_swept(
            _SIGNALS, _WINDOWS,
            sweep_root="/home/u/sim/Interactive.0",
            points=[1, 2, 1],
        )


def test_run_swept_rejects_point_out_of_range(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)outside"):
        bridge.run_ocean_dump_all_swept(
            _SIGNALS, _WINDOWS,
            sweep_root="/home/u/sim/Interactive.0",
            points=[0],
        )
    with pytest.raises(ValueError, match=r"(?i)outside"):
        bridge.run_ocean_dump_all_swept(
            _SIGNALS, _WINDOWS,
            sweep_root="/home/u/sim/Interactive.0",
            points=[2000],
        )


def test_run_swept_rejects_empty_points(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)non-empty"):
        bridge.run_ocean_dump_all_swept(
            _SIGNALS, _WINDOWS,
            sweep_root="/home/u/sim/Interactive.0",
            points=[],
        )


# --------------------------------------------------------------------------
# SKILL not-loaded gate
# --------------------------------------------------------------------------

def test_read_sweep_manifest_requires_skill_loaded(
    mock_client, pdk_map_file, tmp_path,
):
    b = SafeBridge(
        mock_client, pdk_map_file, skill_dir=tmp_path / "no_skill",
    )
    assert b._skill_loaded is False
    with pytest.raises(RuntimeError, match=r"(?i)remote-side skill"):
        b.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_run_swept_requires_skill_loaded(
    mock_client, pdk_map_file, tmp_path,
):
    b = SafeBridge(
        mock_client, pdk_map_file, skill_dir=tmp_path / "no_skill",
    )
    assert b._skill_loaded is False
    with pytest.raises(RuntimeError, match=r"(?i)remote-side skill"):
        b.run_ocean_dump_all_swept(
            _SIGNALS, _WINDOWS,
            sweep_root="/home/u/sim/Interactive.0",
            points=[1],
        )


# --------------------------------------------------------------------------
# write_sweep_manifest — author the file PC-side (Path-2, 2026-05-19)
# --------------------------------------------------------------------------

_VALID_ROOT = "/home/u/sim/cell_tb/maestro/results/maestro/Interactive.0"


def test_write_sweep_manifest_builds_typed_list_expr(bridge, monkeypatch):
    """No embedded JSON string in the SKILL expression — entries are
    passed as nested ``list(...)`` so the existing entrypoint allow-list
    + ``list`` nested-call permit are sufficient."""
    captured: dict = {}

    def fake_exec(expr):
        captured["expr"] = expr
        return {"ok": True, "count": 3}

    monkeypatch.setattr(bridge, "_execute_skill_json", fake_exec)
    entries = [
        {"point": 1, "vctrl": 0.0},
        {"point": 2, "vctrl": 0.1},
        {"point": 3, "vctrl": 0.2},
    ]
    n = bridge.write_sweep_manifest(_VALID_ROOT, entries)
    assert n == 3
    expr = captured["expr"]
    assert expr.startswith("safeWriteSweepManifest(")
    assert f'"{_VALID_ROOT}"' in expr
    assert "list(1 0.0)" in expr
    assert "list(2 0.1)" in expr
    assert "list(3 0.2)" in expr
    # No embedded JSON braces / quotes inside the SKILL expression.
    assert '"point"' not in expr
    assert "\\\"" not in expr


def test_write_sweep_manifest_rejects_bad_root(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError):
        bridge.write_sweep_manifest(
            "/etc/passwd", [{"point": 1, "vctrl": 0.0}],
        )


def test_write_sweep_manifest_rejects_empty_entries(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)non-empty"):
        bridge.write_sweep_manifest(_VALID_ROOT, [])


def test_write_sweep_manifest_rejects_non_dict_entry(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)dict"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [[1, 0.0]],  # type: ignore[list-item]
        )


def test_write_sweep_manifest_rejects_missing_keys(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)missing"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 1}],
        )


def test_write_sweep_manifest_rejects_bool_point(bridge, monkeypatch):
    """Mirror of read-side codex-P3 strict-bool gate. ``isinstance(True,
    int)`` is True in Python — a bool would silently round-trip through
    ``int()`` and corrupt the manifest with a serializer bug."""
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)boolean|bool"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": True, "vctrl": 0.0}],
        )
    with pytest.raises(ValueError, match=r"(?i)boolean|bool"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 1, "vctrl": False}],
        )


def test_write_sweep_manifest_rejects_point_out_of_range(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)outside"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 0, "vctrl": 0.0}],
        )
    with pytest.raises(ValueError, match=r"(?i)outside"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 9999, "vctrl": 0.0}],
        )


def test_write_sweep_manifest_rejects_non_finite_vctrl(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)non-finite"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 1, "vctrl": float("inf")}],
        )


def test_write_sweep_manifest_rejects_duplicate_points(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)duplicate"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [
                {"point": 1, "vctrl": 0.0},
                {"point": 1, "vctrl": 0.1},
            ],
        )


def test_write_sweep_manifest_skill_not_ok(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: {"ok": False, "error": "disk full"},
    )
    with pytest.raises(RuntimeError, match=r"safeWriteSweepManifest failed"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 1, "vctrl": 0.0}],
        )


def test_write_sweep_manifest_count_mismatch(bridge, monkeypatch):
    """SKILL reports a count that disagrees with the entries the PC
    sent — surface as RuntimeError instead of silently accepting a
    short write."""
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: {"ok": True, "count": 99},
    )
    with pytest.raises(RuntimeError, match=r"(?i)count mismatch"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 1, "vctrl": 0.0}],
        )


def test_write_sweep_manifest_requires_skill_loaded(
    mock_client, pdk_map_file, tmp_path,
):
    b = SafeBridge(
        mock_client, pdk_map_file, skill_dir=tmp_path / "no_skill",
    )
    assert b._skill_loaded is False
    with pytest.raises(RuntimeError, match=r"(?i)remote-side skill"):
        b.write_sweep_manifest(
            _VALID_ROOT, [{"point": 1, "vctrl": 0.0}],
        )


def test_write_sweep_manifest_passes_entrypoint_gate(bridge, monkeypatch):
    """Defense-in-depth: the composed expression must survive the
    real ``_check_skill_entrypoint`` scanner (no nested-call bypass)."""
    real_check = bridge._check_skill_entrypoint
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: (real_check(expr), {"ok": True, "count": 2})[1],
    )
    bridge.write_sweep_manifest(_VALID_ROOT, [
        {"point": 1, "vctrl": 0.0},
        {"point": 9, "vctrl": 0.8},
    ])


# --------------------------------------------------------------------------
# R2 (2026-05-19, codex P3) — strict isinstance gates: no silent coercion of
# float→int for point, no string→numeric coercion for either field. Schema
# correctness is the caller's job.
# --------------------------------------------------------------------------

def test_write_sweep_manifest_rejects_float_point(bridge, monkeypatch):
    """``point=1.9`` would have silently rounded to ``1`` via ``int()``."""
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)point.*int|float"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 1.9, "vctrl": 0.0}],
        )


def test_write_sweep_manifest_rejects_string_point(bridge, monkeypatch):
    """``"2"`` would have silently parsed via ``int()``."""
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)point.*int|str"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": "2", "vctrl": 0.0}],
        )


def test_write_sweep_manifest_rejects_string_vctrl(bridge, monkeypatch):
    """``vctrl="0.3"`` would have silently parsed via ``float()``."""
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: pytest.fail("should not reach SKILL"),
    )
    with pytest.raises(ValueError, match=r"(?i)vctrl.*int.*float|str"):
        bridge.write_sweep_manifest(
            _VALID_ROOT, [{"point": 1, "vctrl": "0.3"}],
        )


def test_write_sweep_manifest_accepts_int_vctrl(bridge, monkeypatch):
    """Vctrl as a literal int (e.g. ``0``) is legitimate — strict
    means strict on TYPE, not on float-vs-int. Accept and coerce."""
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: {"ok": True, "count": 1},
    )
    n = bridge.write_sweep_manifest(
        _VALID_ROOT, [{"point": 1, "vctrl": 0}],
    )
    assert n == 1


def test_read_sweep_manifest_rejects_float_point(bridge, monkeypatch):
    """Read side parity: hand-edited manifest with ``"point": 1.9``
    must hard-fail, not silently round."""
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: _manifest_response([
            {"point": 1.9, "vctrl": 0.0},
        ]),
    )
    with pytest.raises(RuntimeError, match=r"(?i)point.*int|float"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_read_sweep_manifest_rejects_string_point(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: _manifest_response([
            {"point": "2", "vctrl": 0.0},
        ]),
    )
    with pytest.raises(RuntimeError, match=r"(?i)point.*int|str"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")


def test_read_sweep_manifest_rejects_string_vctrl(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "_execute_skill_json",
        lambda expr: _manifest_response([
            {"point": 1, "vctrl": "0.3"},
        ]),
    )
    with pytest.raises(RuntimeError, match=r"(?i)vctrl.*int.*float|str"):
        bridge.read_sweep_manifest("/home/u/sim/Interactive.0")
