"""WaveformAnalyzer: Extract circuit performance metrics from simulation results.

Extracts gain, bandwidth, phase margin, slew rate, settling time, etc.
from AC, DC, and transient simulation data. Uses numpy for signal processing
and python-control for stability metrics.

Reference: EEsizer's 11 performance indicators.
"""

from __future__ import annotations

import numpy as np

try:
    import control
except ImportError:
    control = None


class WaveformAnalyzer:
    """Extract performance metrics from Spectre simulation results."""

    # ------------------------------------------------------------------ #
    #  AC analysis metrics
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_ac_metrics(result: dict, gain_format: str = "auto") -> dict:
        """Extract AC small-signal metrics from simulation result.

        Args:
            result: Dict with 'freq' (Hz), 'gain' (V/V complex or dB),
                    and 'phase' (degrees) arrays.
            gain_format: "dB", "linear", or "auto". When "auto", complex
                    values are treated as linear; real values use heuristic.

        Returns:
            Dict with gain_dB, BW_Hz, unity_gain_freq, phase_margin_deg.
        """
        freq = np.asarray(result["freq"], dtype=float)
        gain = np.asarray(result["gain"])
        phase = np.asarray(result.get("phase"), dtype=float) if "phase" in result else None

        # Handle complex gain (convert to magnitude in dB)
        if np.iscomplexobj(gain):
            if phase is None:
                phase = np.degrees(np.angle(gain))
            gain_dB = 20.0 * np.log10(np.abs(gain) + 1e-30)
        elif gain_format == "dB":
            gain_dB = np.asarray(gain, dtype=float)
        elif gain_format == "linear":
            gain_dB = 20.0 * np.log10(np.abs(np.asarray(gain, dtype=float)) + 1e-30)
        else:
            # "auto" for real values: values >100 are likely linear magnitude
            # (a 100 dB gain is extreme; use gain_format="dB" for >100 dB amps)
            gain_dB = np.asarray(gain, dtype=float)
            if gain_dB.max() > 100:
                gain_dB = 20.0 * np.log10(np.abs(gain_dB) + 1e-30)

        metrics = {}

        # DC gain (low-frequency gain)
        metrics["gain_dB"] = float(gain_dB[0])

        # -3dB bandwidth
        dc_gain = gain_dB[0]
        bw_threshold = dc_gain - 3.0
        bw_crossings = np.where(gain_dB < bw_threshold)[0]
        if len(bw_crossings) > 0:
            idx = bw_crossings[0]
            # Linear interpolation for better accuracy
            if idx > 0:
                frac = (bw_threshold - gain_dB[idx - 1]) / (
                    gain_dB[idx] - gain_dB[idx - 1] + 1e-30
                )
                metrics["BW_Hz"] = float(
                    freq[idx - 1] + frac * (freq[idx] - freq[idx - 1])
                )
            else:
                metrics["BW_Hz"] = float(freq[idx])
        else:
            metrics["BW_Hz"] = float(freq[-1])  # BW exceeds sim range

        # Unity-gain frequency (0 dB crossing)
        ugf_crossings = np.where(gain_dB < 0)[0]
        if len(ugf_crossings) > 0 and gain_dB[0] >= 0:
            idx = ugf_crossings[0]
            if idx > 0:
                frac = (0 - gain_dB[idx - 1]) / (
                    gain_dB[idx] - gain_dB[idx - 1] + 1e-30
                )
                metrics["unity_gain_freq"] = float(
                    freq[idx - 1] + frac * (freq[idx] - freq[idx - 1])
                )
            else:
                metrics["unity_gain_freq"] = float(freq[idx])
        else:
            metrics["unity_gain_freq"] = None

        # Phase margin
        if phase is not None and metrics["unity_gain_freq"] is not None:
            ugf = metrics["unity_gain_freq"]
            # Interpolate phase at unity-gain frequency
            phase_at_ugf = float(np.interp(ugf, freq, phase))
            metrics["phase_margin_deg"] = float(phase_at_ugf + 180.0)
        elif control is not None and phase is not None:
            # Use python-control for more robust calculation
            try:
                gain_linear = 10 ** (gain_dB / 20.0)
                complex_tf = gain_linear * np.exp(1j * np.radians(phase))
                # Build frequency response data
                mag = np.abs(complex_tf)
                ph = np.angle(complex_tf)
                gm, pm, wgc, wpc = control.margin((mag, ph, 2 * np.pi * freq))
                metrics["phase_margin_deg"] = float(pm) if np.isfinite(pm) else None
            except Exception:
                metrics["phase_margin_deg"] = None
        else:
            metrics["phase_margin_deg"] = None

        # Gain-bandwidth product
        if metrics["unity_gain_freq"] is not None:
            metrics["GBW_Hz"] = metrics["unity_gain_freq"]
        else:
            metrics["GBW_Hz"] = None

        return metrics

    # ------------------------------------------------------------------ #
    #  DC analysis metrics
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_dc_metrics(result: dict, vdd: float | None = None) -> dict:
        """Extract DC operating-point metrics.

        Args:
            result: Dict with operating-point data per instance, e.g.
                    {'M1': {'gm': 1e-3, 'id': 100e-6, ...}, ...}
            vdd: Supply voltage in volts. If None, reads from result["vdd"]
                 or raises ValueError.

        Returns:
            Dict with aggregated DC metrics.
        """
        metrics = {}

        # Total power consumption
        total_current = 0.0
        if vdd is None:
            vdd = result.get("vdd")
            if vdd is None:
                raise ValueError(
                    "Supply voltage (vdd) not found in result and not provided. "
                    "Pass vdd explicitly or include 'vdd' in the result dict."
                )

        for inst_name, op_data in result.items():
            if isinstance(op_data, dict) and "id" in op_data:
                total_current += abs(op_data["id"])

        metrics["total_current_A"] = total_current
        metrics["power_W"] = total_current * vdd

        # Per-instance OP data summary
        op_summary = {}
        safe_keys = {"gm", "gds", "id", "vth", "vdsat", "cgs", "cgd", "cdb"}
        for inst_name, op_data in result.items():
            if isinstance(op_data, dict):
                op_summary[inst_name] = {
                    k: v for k, v in op_data.items() if k in safe_keys
                }
        metrics["op_points"] = op_summary

        return metrics

    # ------------------------------------------------------------------ #
    #  Transient analysis metrics
    # ------------------------------------------------------------------ #

    @staticmethod
    def extract_tran_metrics(result: dict) -> dict:
        """Extract transient performance metrics.

        Args:
            result: Dict with 'time' and 'vout' arrays, optionally 'vin'.

        Returns:
            Dict with settling_time_s, slew_rate_Vus, overshoot_pct.
        """
        time = np.asarray(result["time"], dtype=float)
        vout = np.asarray(result["vout"], dtype=float)

        metrics = {}

        # Final value (steady-state)
        final_value = vout[-1]
        initial_value = vout[0]
        step_size = final_value - initial_value

        if abs(step_size) < 1e-12:
            metrics["settling_time_s"] = 0.0
            metrics["slew_rate_V_per_us"] = 0.0
            metrics["overshoot_pct"] = 0.0
            return metrics

        # Settling time (to within 1% of final value)
        tolerance = 0.01 * abs(step_size)
        settled = np.abs(vout - final_value) < tolerance
        # Find last non-settled point
        not_settled = np.where(~settled)[0]
        if len(not_settled) > 0:
            metrics["settling_time_s"] = float(time[not_settled[-1]])
        else:
            metrics["settling_time_s"] = 0.0

        # Slew rate (max dV/dt)
        dt = np.diff(time)
        dv = np.diff(vout)
        dvdt = dv / (dt + 1e-30)
        max_slew = float(np.max(np.abs(dvdt)))
        metrics["slew_rate_V_per_us"] = max_slew * 1e-6  # Convert to V/us

        # Overshoot
        if step_size > 0:
            peak = np.max(vout)
        else:
            peak = np.min(vout)
        overshoot = abs(peak - final_value) / abs(step_size) * 100.0
        metrics["overshoot_pct"] = float(overshoot)

        return metrics

    # ------------------------------------------------------------------ #
    #  Unified extraction entry point
    # ------------------------------------------------------------------ #

    def extract(self, result: dict, analysis_type: str = "ac") -> dict:
        """Extract metrics based on analysis type.

        Args:
            result: Simulation result data.
            analysis_type: One of 'ac', 'dc', 'tran'.

        Returns:
            Dict of extracted metrics.
        """
        extractors = {
            "ac": self.extract_ac_metrics,
            "dc": self.extract_dc_metrics,
            "tran": self.extract_tran_metrics,
        }
        extractor = extractors.get(analysis_type)
        if extractor is None:
            raise ValueError(
                f"Unknown analysis type: {analysis_type!r}. "
                f"Supported: {list(extractors)}"
            )
        return extractor(result)
