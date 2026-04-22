# LC_VCO Optimization Spec — 40 GHz Low-Power Concept Validation

> **Platform contract**: circuit-agent reads this file and passes its
> content to the LLM as the target-spec prompt. Every number below is
> authoritative.
>
> Supporting docs (generic, not circuit-specific):
> `docs/spec_authoring_rules.md` (pass-range rules) ·
> `docs/llm_protocol.md` (response format, iteration flow, stop conditions).

---

## 1. Design under test

- Library / Cell: `pll / LC_VCO`; Testbench cell: `pll / LC_VCO_tb`
- Process: 16nm FinFET (PDK placeholder), ideal L/C/R, PDK MOS; VDD = 0.8 V
- Topology: cross-coupled NMOS (M0/M1) + tail (M2) + diode mirror (M3) +
  MOS varactors (M4/M9) + on-chip differential inductor L_diff, with
  R0/R1 as tank Q-damping resistors tied to VDD
- Differential outputs: `/Vout_p`, `/Vout_n`; tail drain probe: `/I0/M2/D`
- **Target oscillation frequency: 40.0 GHz ± 0.5 GHz**
- **Primary optimization goal: minimize core current `I_core_uA`
  (≡ core power @ 0.8 V) while meeting all other pass ranges.**

---

## 2. Machine-readable eval block

Authoritative structured form for `src/spec_evaluator.py`. The agent
executes against this block; `safeOceanDumpAll` collects per-signal /
per-window stats, and the PC-side evaluator computes `measurements` +
`pass_fail` from them. Sanity bounds are set generously (several times
the physical limit) so they only trip on measurement corruption, never
on legitimate design points.

```yaml
signals:
  - name: Vdiff
    kind: Vdiff
    paths: ["/Vout_p", "/Vout_n"]
    bounds: {max_abs: 4.0, ptp_max: 8.0}
  - name: Vcm
    kind: Vsum_half
    paths: ["/Vout_p", "/Vout_n"]
    bounds: {max_abs: 3.0}
  - name: Vout_p
    kind: V
    path: "/Vout_p"
    bounds: {max_abs: 3.0}
  - name: Vout_n
    kind: V
    path: "/Vout_n"
    bounds: {max_abs: 3.0}
  - name: I_tail
    kind: I
    path: "/I0/M2/D"
    bounds: {max_abs: 0.05}

windows:
  full:    [1.0e-7, 2.0e-7]
  late:    [1.5e-7, 2.0e-7]
  early:   [7.5e-8, 1.25e-7]
  startup: [0.0,    2.0e-7]

metrics:
  - {name: f_osc_GHz, signal: Vdiff, window: full, stat: freq_Hz,
     scale: 1.0e-9, pass: [39.5, 40.5], sanity: [1.0, 200.0]}

  - {name: V_diff_pp_V, signal: Vdiff, window: late, stat: ptp,
     pass: [0.30, null], sanity: [0.0, 8.0]}

  - {name: V_cm_V, signal: Vcm, window: late, stat: mean,
     pass: [0.55, 0.80], sanity: [0.0, 3.0]}

  - {name: duty_cycle_pct, signal: Vdiff, window: late, stat: duty_pct,
     pass: [47, 53], sanity: [0, 100]}

  - name: amp_hold_ratio
    compound: ratio
    numerator:   {signal: Vdiff, window: late,  stat: rms}
    denominator: {signal: Vdiff, window: early, stat: rms}
    pass: [0.85, null]
    sanity: [0.0, 10.0]

  - name: t_startup_ns
    compound: t_cross_frac
    signal: Vdiff
    frac: 0.45
    ref: {signal: Vdiff, window: late, stat: ptp}
    window: startup
    direction: rising
    use_abs: true
    scale: 1.0e9
    pass: [null, 20]
    sanity: [0.0, 500.0]

  - {name: I_core_uA, signal: I_tail, window: late, stat: mean_abs,
     scale: 1.0e6, pass: [null, 400], sanity: [0.0, 50000.0]}
```

---

## 3. Design variables the LLM may adjust

| Var | Role (device) | Range | Priority |
|---|---|---|---|
| `Ibias` | M3 diode-mirror ref current | 100 µA – 1.5 mA | P1 |
| `nfin_neg` | M0/M1 cross-coupled NMOS fingers | 4 – 32 | P1 |
| `nfin_cc` | M4/M9 MOS-varactor fingers | 4 – 40 | P1 |
| `nfin_mirror` | M3 diode-mirror fingers | 4 – 32 | P2 |
| `nfin_tail` | M2 tail fingers | 4 – 32 | P2 |
| `R` | Tank Q-damping R (shared R0/R1) | 5 k – 100 k | P2 |
| `C` | Tank fixed MIM (shared C0/C1) | 1 f – 80 f | P1 |
| `L` | Tank differential inductor (shared L0/L1) | 80 p – 600 p | P1 |

SafeBridge `allowed_params` whitelist: `r, c, w, l, nf, m, multi, wf,
nfin, fingers, idc, vdc` (case-insensitive).

**Maestro prerequisite**: all 8 vars above must exist in the Maestro
"Design Variables" pane with numeric defaults (starting point guess
for 40 GHz: `Ibias=400u, nfin_neg=8, nfin_cc=16, nfin_mirror=8,
nfin_tail=8, R=20k, C=5f, L=300p`). Missing defaults → `SFE-1997`
fatal errors.

**Discovered analyses in Maestro session**: (scaffold reported 0; the
testbench must declare `tran` before the agent can run — use the same
session the 20 GHz demo uses, just update `stop` time if needed)

---

## 4. Startup convergence aids

Unstable-equilibrium circuits (oscillators, latches, Schmitt triggers)
using `skipdc=yes` + `ic` suffer broken bias networks when spectre
silently zeros non-IC'd nodes. Plan Auto reads spectre's `spectre.fc`,
learns equilibrium values, and patches `ic` so every non-output node
is seeded with its bias value while `perturb_nodes` get an asymmetric
kick. Requires `--auto-bias-ic` AND this block; absent either, no-op.

```yaml
startup:
  warm_start: auto               # auto | none
  perturb_nodes:
    - {name: Vout_n, offset_mV: +5}
    - {name: Vout_p, offset_mV: -5}
  v_cm_hint_V: 0.78              # fallback if fc parse fails (near VDD=0.8V)
  netlist_path: null             # null = reuse --scs-path
```

---

## 5. Honest caveats

- **40 GHz on ideal L/C/R is aggressive, but the metrics are
  concept-level, not silicon-realistic.** PN / FoM numbers here would
  be 10–15 dB better than silicon; do not publish phase-noise-like
  figures from this spec.
- **R0/R1 is the Q knob, not a bias-feed.** Lowering R lowers tank Q
  and directly raises the Ibias needed to start/sustain — the
  optimizer should keep R on the high side to minimize power.
- **Power-first objective:** `I_core_uA` has a hard upper bound but no
  lower bound, so the LLM should gravitate toward the lowest I_tail
  that still passes `V_diff_pp ≥ 0.30` and `amp_hold_ratio ≥ 0.85`.
  If the 400 µA ceiling proves infeasible after ~6 iterations, relax
  to 600 µA; if comfortably met, tighten to 300 µA.
- **Late-window choice** (150–200 ns of a 200 ns sim): at 40 GHz this
  is ~2000 periods of steady state — skips the 50–100 ns startup
  ringing seen in low-Q corners.
- **`t_startup_ns ≤ 20` is looser than the 20 GHz demo's 10 ns** —
  with lower Ibias we expect slower amplitude buildup; 20 ns gives
  the optimizer room without accepting a truly stuck circuit.
- **No PN / FoM / tuning-range metrics** — tran-only spec. Adding
  them requires switching to PSS + PNOISE (different `--analysis`)
  and is out of scope for this first power-minimization pass.
