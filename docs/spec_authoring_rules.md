# Spec Authoring Rules — Pass-Range Design Principles

Pass ranges in the spec's machine-readable eval block must carry numerical
tolerance. A pass bound that equals a physical stable value or a bare
design target tends to be killed by simulator floating-point noise on
every run. These rules are authoring guidance for spec writers;
`src/spec_validator.py` does not currently enforce tolerance width —
the author is responsible for self-checking against these rules.

## Rules

1. **Two-sided pass ranges** (`pass: [lo, hi]`): `hi − lo` must exceed
   `max(2 × simulator_fp_noise, 1% of design target)`.
   Example: f_osc target 20 GHz → `[19.5, 20.5]` (±2.5%) is safe;
   `[19.99, 20.01]` is not, because spectre's `frequency()` jitter
   exceeds 10 MHz.

2. **One-sided pass ranges** (`pass: [lo, null]` or `pass: [null, hi]`):
   the bound may be tight only if it is strictly below/above any
   physically achievable value. If the bound equals a physical stable
   value (e.g. VDD for V_cm on an ideal-L topology where the inductor
   DC-shorts both differential rails to VDD), widen by at least
   `max(1% of the physical value, 10 × observed fp noise)` to absorb
   numerical noise while still catching real physical drift.
   Over-widening (e.g. 5%+ on a stable value that only drifts by
   ~1e-5 relative) will mask real failures — pick the smallest margin
   that comfortably covers simulator noise.

3. **Never** write `pass: [x, x]` or `pass: [x, x + ε]` with ε smaller
   than `1e-4 × x`; floating-point comparison on `safeOceanMeasure`
   output will FAIL such "exact" specs.

## Rationale

Ideal-element specs (L/C/R as pure reactances, no parasitics) deviate
from symbolic physics only via IEEE-754 roundoff (~1e-5 relative).
Real-device specs can be tighter because process variation far exceeds
simulator fp error; ideal-element specs cannot.

## Sanity vs pass vs bounds

Three envelopes per metric, from tightest to loosest:

- **`pass: [lo, hi]`** — the design target. In-range → `PASS`;
  out-of-range but within sanity → `FAIL`; propose `design_vars` that
  move the metric toward pass.
- **`sanity: [lo, hi]`** — the plausibility envelope. Values outside
  here are reported as `UNMEASURABLE (...)` — the measurement chain
  or spec math is broken, not the circuit. The LLM must NOT tune
  `design_vars` to fix an UNMEASURABLE metric.
- **`signals[].bounds`** — physical limits on the raw signal (e.g.
  `|V_diff| ≤ 4.0 V` on an ideal L/C tank). Violations indicate
  simulator runaway or a degenerate netlist.

## Worked example — `V_cm_V` on an ideal-L topology

Physical argument: on an ideal-L VCO the tank inductor DC-shorts both
differential rails to VDD (0.80 V). Simulator fp noise on the
window-mean is ~1e-5 relative, ≈7 µV absolute. If you write
`pass: [null, 0.80]`, the mean will exceed 0.80 by ~1400× fp noise on
every run and FAIL spuriously. `pass: [0.70, 0.81]` (10 mV margin
above VDD) absorbs the noise while still catching real drift (e.g.
tail pinch-off raising V_cm to ~0.83).
