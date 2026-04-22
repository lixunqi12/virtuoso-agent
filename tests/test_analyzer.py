"""Unit tests for WaveformAnalyzer: AC, DC, and transient metric extraction."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analyzer import WaveformAnalyzer


@pytest.fixture
def analyzer():
    return WaveformAnalyzer()


# ------------------------------------------------------------------ #
#  AC metrics tests
# ------------------------------------------------------------------ #

class TestACMetrics:
    def test_basic_ac_extraction(self):
        """Test with a simple single-pole transfer function."""
        freq = np.logspace(0, 9, 1000)  # 1 Hz to 1 GHz
        # Single pole at 1 MHz, DC gain = 60 dB (1000 V/V)
        pole = 1e6
        dc_gain = 1000.0
        gain_linear = dc_gain / np.sqrt(1 + (freq / pole) ** 2)
        gain_dB = 20 * np.log10(gain_linear)
        phase = -np.degrees(np.arctan(freq / pole))

        result = {
            "freq": freq,
            "gain": gain_dB,
            "phase": phase,
        }
        metrics = WaveformAnalyzer.extract_ac_metrics(result)

        # DC gain should be ~60 dB
        assert abs(metrics["gain_dB"] - 60.0) < 0.5

        # -3dB BW should be ~1 MHz
        assert abs(metrics["BW_Hz"] - 1e6) / 1e6 < 0.1

        # UGF should be ~1 GHz (gain * BW)
        assert metrics["unity_gain_freq"] is not None
        assert abs(metrics["unity_gain_freq"] - 1e9) / 1e9 < 0.1

    def test_complex_gain_input(self):
        """Test with complex-valued gain."""
        freq = np.logspace(0, 8, 500)
        pole = 1e5
        dc_gain = 100.0
        s = 1j * 2 * np.pi * freq
        gain_complex = dc_gain / (1 + s / (2 * np.pi * pole))

        result = {"freq": freq, "gain": gain_complex}
        metrics = WaveformAnalyzer.extract_ac_metrics(result)

        assert abs(metrics["gain_dB"] - 40.0) < 0.5
        assert metrics["BW_Hz"] is not None

    def test_no_ugf(self):
        """Test when gain never crosses 0 dB."""
        freq = np.logspace(0, 6, 100)
        gain_dB = np.full_like(freq, -10.0)  # Always below 0 dB

        result = {"freq": freq, "gain": gain_dB, "phase": np.zeros_like(freq)}
        metrics = WaveformAnalyzer.extract_ac_metrics(result)

        assert metrics["unity_gain_freq"] is None
        assert metrics["phase_margin_deg"] is None


# ------------------------------------------------------------------ #
#  DC metrics tests
# ------------------------------------------------------------------ #

class TestDCMetrics:
    def test_power_calculation(self):
        result = {
            "vdd": 1.8,
            "M1": {"gm": 1e-3, "gds": 1e-5, "id": 100e-6, "vth": 0.4},
            "M2": {"gm": 2e-3, "gds": 2e-5, "id": 200e-6, "vth": 0.35},
        }
        metrics = WaveformAnalyzer.extract_dc_metrics(result)

        expected_current = 300e-6
        assert abs(metrics["total_current_A"] - expected_current) < 1e-9
        assert abs(metrics["power_W"] - expected_current * 1.8) < 1e-9

    def test_op_point_keys_filtered(self):
        result = {
            "vdd": 1.8,
            "M1": {
                "gm": 1e-3,
                "gds": 1e-5,
                "id": 100e-6,
                "vth": 0.4,
                "vdsat": 0.15,
                "cgs": 10e-15,
                "cgd": 2e-15,
                "cdb": 5e-15,
                "toxe": 1.8e-9,  # should be filtered
                "u0": 400,       # should be filtered
            },
        }
        metrics = WaveformAnalyzer.extract_dc_metrics(result)
        m1_op = metrics["op_points"]["M1"]

        assert "gm" in m1_op
        assert "id" in m1_op
        assert "toxe" not in m1_op
        assert "u0" not in m1_op

    def test_missing_vdd_raises(self):
        result = {"M1": {"id": 100e-6}}
        with pytest.raises(ValueError, match="vdd"):
            WaveformAnalyzer.extract_dc_metrics(result)

    def test_explicit_vdd(self):
        result = {"M1": {"id": 100e-6}}
        metrics = WaveformAnalyzer.extract_dc_metrics(result, vdd=0.9)
        assert abs(metrics["power_W"] - 100e-6 * 0.9) < 1e-9


# ------------------------------------------------------------------ #
#  Transient metrics tests
# ------------------------------------------------------------------ #

class TestTranMetrics:
    def test_step_response(self):
        """Test with a first-order step response."""
        time = np.linspace(0, 10e-6, 10000)
        tau = 1e-6
        vout = 1.0 * (1 - np.exp(-time / tau))

        result = {"time": time, "vout": vout}
        metrics = WaveformAnalyzer.extract_tran_metrics(result)

        # No overshoot for first-order system
        assert metrics["overshoot_pct"] < 1.0
        # Settling time should be ~4-5 tau
        assert metrics["settling_time_s"] > 3 * tau
        assert metrics["settling_time_s"] < 7 * tau
        # Slew rate should be positive
        assert metrics["slew_rate_V_per_us"] > 0

    def test_underdamped_response(self):
        """Test overshoot detection with underdamped system."""
        time = np.linspace(0, 20e-6, 10000)
        wn = 2 * np.pi * 1e6
        zeta = 0.3
        wd = wn * np.sqrt(1 - zeta**2)
        vout = 1.0 - np.exp(-zeta * wn * time) * (
            np.cos(wd * time)
            + (zeta / np.sqrt(1 - zeta**2)) * np.sin(wd * time)
        )

        result = {"time": time, "vout": vout}
        metrics = WaveformAnalyzer.extract_tran_metrics(result)

        # Underdamped system should have overshoot
        assert metrics["overshoot_pct"] > 10.0

    def test_no_change(self):
        """Test with constant output."""
        time = np.linspace(0, 1e-6, 100)
        vout = np.ones_like(time) * 0.9

        result = {"time": time, "vout": vout}
        metrics = WaveformAnalyzer.extract_tran_metrics(result)

        assert metrics["settling_time_s"] == 0.0
        assert metrics["overshoot_pct"] == 0.0


# ------------------------------------------------------------------ #
#  Unified extract() tests
# ------------------------------------------------------------------ #

class TestExtract:
    def test_unknown_analysis(self, analyzer):
        with pytest.raises(ValueError, match="Unknown analysis type"):
            analyzer.extract({}, "noise")

    def test_ac_dispatch(self, analyzer):
        freq = np.logspace(0, 6, 100)
        result = {
            "freq": freq,
            "gain": np.ones(100) * 40.0,
            "phase": np.zeros(100),
        }
        metrics = analyzer.extract(result, "ac")
        assert "gain_dB" in metrics

    def test_tran_dispatch(self, analyzer):
        result = {
            "time": np.linspace(0, 1e-6, 100),
            "vout": np.ones(100),
        }
        metrics = analyzer.extract(result, "tran")
        assert "settling_time_s" in metrics
