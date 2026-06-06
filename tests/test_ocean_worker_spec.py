"""Unit tests for OceanWorker spec-file rendering + osc-signal validation.

No SSH / Cadence needed — these exercise pure-Python helpers.
"""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.ocean_worker import (  # noqa: E402
    OceanWorker,
    OceanWorkerConfig,
    _render_spec_il,
    _validate_osc_signals,
    worker_from_env,
)
from src import spec_evaluator  # noqa: E402


# ---------------------------------------------------------------- #
#  _validate_osc_signals
# ---------------------------------------------------------------- #

def test_osc_signals_none_returns_empty():
    assert _validate_osc_signals(None) == []


def test_osc_signals_empty_returns_empty():
    assert _validate_osc_signals([]) == []


def test_osc_signals_valid_pair():
    out = _validate_osc_signals(["/Vout_p", "/Vout_n"])
    assert out == ["/Vout_p", "/Vout_n"]


def test_osc_signals_wrong_length():
    with pytest.raises(ValueError, match="exactly 2"):
        _validate_osc_signals(["/Vout_p"])
    with pytest.raises(ValueError, match="exactly 2"):
        _validate_osc_signals(["/a", "/b", "/c"])


def test_osc_signals_bad_path():
    with pytest.raises(ValueError, match="bad probe path"):
        _validate_osc_signals(["Vout_p", "/Vout_n"])  # missing leading /
    with pytest.raises(ValueError, match="bad probe path"):
        _validate_osc_signals(["/Vout_p", "/bad name"])  # space


def test_osc_signals_wrong_type():
    with pytest.raises(ValueError, match="must be a list"):
        _validate_osc_signals("/Vout_p /Vout_n")


# ---------------------------------------------------------------- #
#  _render_spec_il
# ---------------------------------------------------------------- #

_SIGNALS = [
    ("Vdiff", "Vdiff", ["/Vout_p", "/Vout_n"]),
    ("Vout_p", "V", ["/Vout_p"]),
]
_WINDOWS = [("full", 0.0, 2e-7)]


def test_render_spec_without_osc_emits_nil():
    body = _render_spec_il(_SIGNALS, _WINDOWS, osc_signals=None)
    assert "vbOscSignals = nil" in body


def test_render_spec_omitting_osc_defaults_to_nil():
    body = _render_spec_il(_SIGNALS, _WINDOWS)
    assert "vbOscSignals = nil" in body


def test_render_spec_empty_osc_emits_nil():
    body = _render_spec_il(_SIGNALS, _WINDOWS, osc_signals=[])
    assert "vbOscSignals = nil" in body


def test_render_spec_with_osc_emits_list():
    body = _render_spec_il(
        _SIGNALS, _WINDOWS, osc_signals=["/Vout_p", "/Vout_n"]
    )
    assert 'vbOscSignals = (list "/Vout_p" "/Vout_n")' in body


def test_extract_osc_signals_finds_vdiff_pair():
    block = {
        "signals": [
            {"name": "Vdiff", "kind": "Vdiff",
             "paths": ["/Vout_p", "/Vout_n"]},
            {"name": "Vout_p", "kind": "V", "paths": ["/Vout_p"]},
        ],
    }
    assert spec_evaluator.extract_osc_signals(block) == ["/Vout_p", "/Vout_n"]


def test_extract_osc_signals_returns_none_when_no_vdiff():
    block = {
        "signals": [
            {"name": "Vout_p", "kind": "V", "paths": ["/Vout_p"]},
        ],
    }
    assert spec_evaluator.extract_osc_signals(block) is None


def test_extract_osc_signals_skips_vdiff_with_wrong_path_count():
    block = {
        "signals": [
            {"name": "Vdiff", "kind": "Vdiff", "paths": ["/Vout_p"]},
        ],
    }
    assert spec_evaluator.extract_osc_signals(block) is None


def test_extract_osc_signals_tolerates_missing_signals_key():
    assert spec_evaluator.extract_osc_signals({}) is None


def test_render_spec_signal_and_window_lines_present():
    body = _render_spec_il(_SIGNALS, _WINDOWS, osc_signals=None)
    assert "vbSignalList = (list" in body
    assert "vbWindowList = (list" in body
    assert '"Vdiff" "Vdiff" (list "/Vout_p" "/Vout_n")' in body
    # Floats preserved with repr (trailing e-07 not stripped).
    assert "2e-07" in body


# ---------------------------------------------------------------- #
#  OCEAN remote init command
# ---------------------------------------------------------------- #

def test_worker_from_env_reads_ocean_init_cmd(monkeypatch):
    monkeypatch.setenv("VB_REMOTE_HOST", "cobi.example.edu")
    monkeypatch.setenv("VB_REMOTE_USER", "alice")
    monkeypatch.setenv("VB_REMOTE_SKILL_DIR", "/remote/skill")
    monkeypatch.setenv("VB_OCEAN_INIT_CMD", "module load cadence/ic_23.1")

    worker = worker_from_env()

    assert worker.cfg.remote_init_cmd == "module load cadence/ic_23.1"


def test_spawn_wrapper_runs_init_before_exports():
    cfg = OceanWorkerConfig(
        remote_host="remotehost",
        remote_user="alice",
        remote_skill_dir="/remote/skill",
        virtuoso_bin="/cad/virtuoso",
        remote_init_cmd='eval "$(/apps/modulecmd sh load cadence/ic_23.1)"',
    )
    worker = OceanWorker(cfg)

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"status":"ok"}\n', stderr=""
    )
    with mock.patch(
        "src.ocean_worker.subprocess.run", return_value=completed,
    ) as run_mock:
        worker._spawn_and_wait(
            psf_dir="/tmp/psf",
            remote_spec="/tmp/spec.il",
            remote_result="/tmp/result.json",
            remote_pidfile="/tmp/pid",
            budget_s=10.0,
            run_id="unit",
        )

    remote_cmd = run_mock.call_args.args[0][-1]
    assert "modulecmd sh load cadence/ic_23.1" in remote_cmd
    assert "export VB_PSF_DIR=/tmp/psf;" in remote_cmd
    assert (
        remote_cmd.index("modulecmd sh load cadence/ic_23.1")
        < remote_cmd.index("export VB_PSF_DIR=/tmp/psf;")
    )
