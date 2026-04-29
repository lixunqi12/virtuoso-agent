# Default Spec — Worked LC-Tank Oscillator Example @ 20 GHz

> This file is the **default spec** shipped with the repo and serves
> as a worked example of the 5-section spec format. Copy it to
> `projects/<your-project>/constraints/spec.md` and edit the fields
> below for your DUT. The numbers describe a generic LC-tank
> oscillator at 20 GHz; they are not tied to any specific design.
>
> **Platform contract**: circuit-agent reads the active spec and passes
> its content to the LLM as the target-spec prompt. Every number below
> is authoritative for whichever spec is loaded.
>
> Supporting docs (generic, not circuit-specific):
> `docs/spec_authoring_rules.md` (pass-range rules) ·
> `docs/llm_protocol.md` (response format, iteration flow, stop conditions).

---

## 1. Design under test

- Library / Cell: `<your-lib> / <your-cell>`; Testbench cell: `<your-lib> / <your-tb-cell>`
- Process: 16nm FinFET (PDK placeholder), ideal L/C/R, PDK MOS; VDD = 0.8 V
- Topology: cross-coupled NMOS (M0/M1) + tail (M2) + diode mirror (M3) +
  MOS varactors (M4/M9) + on-chip differential inductor L_diff (506 pH)
- Differential outputs: `/Vout_p`, `/Vout_n`; tail drain probe: `/I0/M2/D`
- Target oscillation frequency: **20.0 GHz ± 0.5 GHz**

---

## 2. Machine-readable eval block

Authoritative structured form for `src/spec_evaluator.py`. The agent
executes against this block; `safeOceanDumpAll` collects per-signal /
per-window stats, and the PC-side evaluator computes `measurements` +
`pass_fail` from them. See `docs/spec_authoring_rules.md` for
tolerance / sanity-bound rules.

```yaml
signals:
  - name: Vdiff
    kind: Vdiff
    paths: ["/Vout_p", "/Vout_n"]
    bounds: {max_abs: 4.0, ptp_max: 8.0}
  - name: Vcm
    kind: Vsum_half
    paths: ["/Vout_p", "/Vout_n"]
    bounds: {max_abs: 1.6}
  - name: Vout_p
    kind: V
    path: "/Vout_p"
    bounds: {max_abs: 1.6}
  - name: Vout_n
    kind: V
    path: "/Vout_n"
    bounds: {max_abs: 1.6}
  - name: I_tail
    kind: I
    path: "/I0/M2/D"
    bounds: {max_abs: 0.01}

windows:
  full:    [1.0e-7, 2.0e-7]
  late:    [1.5e-7, 2.0e-7]
  early:   [7.5e-8, 1.25e-7]
  startup: [0.0,    2.0e-7]

metrics:
  - {name: f_osc_GHz, signal: Vdiff, window: full, stat: freq_Hz,
     scale: 1.0e-9, pass: [19.5, 20.5], sanity: [0.1, 100.0]}
  - {name: V_diff_pp_V, signal: Vdiff, window: late, stat: ptp,
     pass: [0.40, null], sanity: [0.0, 8.0]}
  - {name: V_cm_V, signal: Vcm, window: late, stat: mean,
     pass: [0.70, 0.81], sanity: [0.0, 1.6]}
  - {name: duty_cycle_pct, signal: Vdiff, window: late, stat: duty_pct,
     pass: [48, 52], sanity: [0, 100]}
  - name: amp_hold_ratio
    compound: ratio
    numerator:   {signal: Vdiff, window: late,  stat: rms}
    denominator: {signal: Vdiff, window: early, stat: rms}
    pass: [0.95, null]
    sanity: [0.0, 3.0]
  - name: t_startup_ns
    compound: t_cross_frac
    signal: Vdiff
    frac: 0.45
    ref: {signal: Vdiff, window: late, stat: ptp}
    window: startup
    direction: rising
    use_abs: true
    scale: 1.0e9
    pass: [null, 10]
    sanity: [0.0, 200.0]
  - {name: I_core_uA, signal: I_tail, window: late, stat: mean_abs,
     scale: 1.0e6, pass: [null, 800], sanity: [0.0, 10000.0]}
```

---

## 3. Design variables the LLM may adjust

| Var | Role (device) | Range | Priority |
|---|---|---|---|
| `Ibias` | M3 diode-mirror ref current | 100 µA – 2 mA | P1 |
| `nfin_neg` | M0/M1 cross-coupled NMOS fingers | 4 – 32 | P1 |
| `nfin_cc` | M4/M9 MOS-varactor fingers | 4 – 40 | P1 |
| `nfin_mirror` | M3 diode-mirror fingers | 4 – 32 | P2 |
| `nfin_tail` | M2 tail fingers | 8 – 32 | P2 |
| `R` | Bias isolation R (shared R0/R1) | 1 k – 100 k | P3 |
| `C` | Tank fixed MIM (shared C0/C1) | 10 f – 200 f | P2 |
| `L` | Tank differential inductor (shared L0/L1) | 100 p – 2 n | P1 |

SafeBridge `allowed_params` whitelist: `r, c, w, l, nf, m, multi, wf,
nfin, fingers, idc, vdc` (case-insensitive).

**Maestro prerequisite**: all 8 vars above must exist in the Maestro
"Design Variables" pane with numeric defaults (e.g. `Ibias=500u,
nfin_neg=16, nfin_cc=20, nfin_mirror=16, nfin_tail=16, R=10k, C=50f,
L=506p`). Missing defaults → `SFE-1997` fatal errors.

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
  v_cm_hint_V: 0.75              # fallback if fc parse fails
  netlist_path: null             # null = reuse --scs-path
```

---

## 5. Honest caveats

- Ideal L/C/R makes PN/FoM 10–15 dB better than silicon — concept
  validation only; do not publish numbers.
- `V_diff_pp` late-window (last 50 ns) avoids 50–75 ns startup ringing.
- `t_startup` needs non-zero `Vout_p`/`Vout_n` mismatch — use Plan Auto
  perturb_nodes or hand-set `IC=0.001` on one rail.
