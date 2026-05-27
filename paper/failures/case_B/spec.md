# matching_test Optimization Spec - Sanitized CMOS Weight-Coupling Linearity (HSpice backend)

> **Platform contract**: circuit-agent reads this file and passes its
> content (after PDK scrub) to the LLM as the target-spec prompt.
> Every number below is authoritative.
>
> Supporting docs (generic, not circuit-specific):
> `docs/spec_authoring_rules.md` (pass-range rules) ·
> `docs/llm_protocol.md` (response format, iteration flow, stop conditions) ·
> `docs/hspice_backend.md` (this file's `--netlist` / `.alter` semantics).
>
> **PREREQUISITE**: this spec depends on resolver **T8.6** (generic
> cross-mt `reduce:` block with `op: linregress` — landed) and
> **T8.7** (subset/row selectors, derived `source.expr`, and
> `op: diff_paired` — landed; see `tests/test_spec_evaluator_hspice.py`
> classes `TestT87*`). Both features are required for v3.2 to
> resolve; without them the slope/match/symmetry metrics emit
> `UNMEASURABLE` and the loop idles.

---

## 1. Design under test

- **Library / Cell**: `<scrubbed> / matching_test`
- **View**: HSpice netlist (exported from Virtuoso schematic via `hspiceD`)
- Process: sanitized CMOS process alias (PDK names redacted at scrub time); VDD = 0.8 V
- **Topology** (bottom-up; all primitives are user-authored transistors,
  no foundry stdcell IP):
  - `INV1X` / `NAND1X` / `NOR1X` — CMOS logic primitives (l=16e-9,
    w=106e-9, multi=1, nf=1, sd=74e-9).
  - `TIEH` / `TIEL` — tie-high / tie-low cells with dummies (w=58e-9).
  - **`TRI_SVT` — 4-MOS tri-state buffer (DUT for sizing).** Ports:
    `n0 n1 p0 p1 vdd vss zn`. The 4 transistors are the only ones in
    this design whose `nf` is parameterised:
    | refdes | role             | nf =                |
    |---     |---               |---                  |
    | xm0    | NMOS top of stack| `num_finger_n0`     |
    | xm1    | NMOS bot of stack| `num_finger_n1`     |
    | xm2    | PMOS bot of stack| `num_finger_p0`     |
    | xm3    | PMOS top of stack| `num_finger_p1`     |
  - `SPIN_WEIGHT_LOGIC` — 2× INV + 2× NOR — produces (sw, swb, sbw, sbwb)
    from a single bit (s, sb, wb).
  - `12TSRAM_TRI` — 12-T SRAM cell: 1× INV storage + 4× TRI_SVT
    (write port via wwl/wwlb, dual read port via rwlh/rwlv).
  - `MEMORY_CELL` — 4× 12T-SRAM + 2× SPIN_WEIGHT_LOGIC + control INV
    chain + 2× NAND for weight-enable mask.
  - `COUPLING_LOGIC_SHORT` / `COUPLING_LOGIC_LONG` — 4× TRI_SVT each,
    polarity differs (SHORT vs LONG).
  - `DRIVER_COUPLING_COMBO` — 2× TRI_SVT delay/driver stage with
    binarised tieh/tiel inputs.
  - `UNIT_CELL` — 1× MEMORY_CELL + 6× DRIVER_COUPLING_COMBO + 4×
    COUPLING_LOGIC_SHORT + 2× COUPLING_LOGIC_LONG + TIEH/TIEL +
    H/V passthru INV1X.
  - `matching_test` (toplevel, **flat, no `.subckt`**, 5× UNIT_CELL):
    - `xi43`: input  stage  → `h_in` → `h_in_mid`
    - `xi29`: **middle stage (DUT)** → `h_in_mid → h_out_mid`,
      `v_in_mid → v_out_mid`
    - `xi42`: H return  / top neighbour
    - `xi30`: V input   / bottom neighbour, `v_in → v_in_mid`
    - `xi41`: V return  / right neighbour

- **Stimulus** (from `edge_close_new.sp`):
  - `WBL<5..8>`: PWL bit-loads gated by `SIGN`/`LSB`/`LSB2`/`MSB` —
    write 1-of-8 weight codes into the centre SRAM during a 5–6 ns window.
  - `WBL<1..4>` and `WBL<9..12>`: held at 0 V (neighbour cells).
  - `WWL<0..2>`: write-wordline pulses (3–4 ns / 3–5.5 ns).
  - `WEIGHT_EN`: held at VDD (write-enable).
  - `H_IN`: PWL pulse at 7 ns, low-rail amplitude controlled by
    `hinvoltage` (0 V or 0.8 V).
  - `V_IN`: PWL pulse offset by `delay` parameter (the swept variable
    inside `.TRAN`).
  - All other H_*/V_* rails (RETURN_IN, PASS, DUMMY) clamped to VDD.

- **Probe nodes** (toplevel net names, preserved through scrub):
  - `h_in_mid`, `h_out_mid` — H-axis input / output of `xi29`.
  - `v_in_mid`, `v_out_mid` — V-axis input / output of `xi29`.
  - `h_in`, `v_in`, `vdd`, `vss` — driven supplies / inputs.

- **What the test characterises**: the central UNIT_CELL `xi29`
  implements an Ising-machine spin-coupling block whose effective
  coupling strength is set by an 8-level weight code stored in its
  MEMORY_CELL. Each `.alter` programs one weight code; the four raw
  propagation delays (`h_tphl`, `h_tplh`, `v_tphl`, `v_tplh`) measured
  on the H/V output edges are the proxy for coupling strength.
  **The optimisation goal is a 4+1 goal set** — the LLM tunes
  `num_finger_*` so that on the H–V *matching skew*
  (`hv_match_* = h_t* − v_t*`):
  1. **DAC linearity** — `|slope|` of `hv_match` vs weight code is
     ≥ 5 ps/LSB and `R²` ≥ 0.95 over the positive-half subset
     (`mt0..3`, weight codes 0/+2/+4/+6) for both PHL and PLH edges.
  2. **Match floor** — at the centred TRAN delay row the residual
     `|hv_match|` is ≤ 5 ps (the design's H–V mismatch floor).
  3. **Common-mode behaviour** — the midpoint `(h_t* + v_t*)/2`
     should not drift > 5 ps across weight codes (any drift is a
     parasitic that LLM tuning must suppress).
  4. **POS/NEG SIGN symmetry** — for both `v_tphl` and `v_tplh`,
     the SIGN-flipped same-magnitude alters agree: mt0 (SIGN=0,
     w=0) vs mt4 (SIGN=1, w=0) within ≤ 2 ps (DC-offset check at
     centred TRAN); and the magnitude-paired alters (mt1↔mt5,
     mt2↔mt6, mt3↔mt7) agree within ≤ 2 ps at the delay extremes
     (rows 0 / 12, where the V edge is fully before / after the
     coupling event).
  *(+1 informational: raw `*_t*` delays are emitted with very wide
  pass-bands for log/observability only.)*
  **Why pos-half-only for the linearity check**: the `H_IN` PWL
  source (`edge_close_new.sp:44`) is
  `PWL (0 '0.8-hinvoltage' 7.0n '0.8-hinvoltage' 7.01n 'hinvoltage'
  9n 'hinvoltage' 9.01n '0.8-hinvoltage')`. With `hinvoltage=0`
  (SIGN=0, mt0..3) this is a normal pulse `0.8 → 0 @ 7 ns → 0.8
  @ 9 ns` (FALL at 7 ns, RISE at 9 ns); with `hinvoltage=0.8 V`
  (SIGN=1, mt4..7) the polarity inverts to `0 → 0.8 @ 7 ns → 0
  @ 9 ns` (RISE at 7 ns, FALL at 9 ns). The four `.measure` lines
  (testbench L75–78) read `h_tphl` from `v(h_in_mid) rise=1` and
  `h_tplh` from `v(h_in_mid) fall=1`, with no qualifying time
  window. After the input-stage inverter `xi43`, `h_in_mid` is the
  inverse of `H_IN`, so:
  - SIGN=0: `h_in_mid` rises at ~7 ns → `h_tphl` samples the 7-ns
    event; falls at ~9 ns → `h_tplh` samples the 9-ns event.
  - SIGN=1: `h_in_mid` falls at ~7 ns → `h_tplh` samples the 7-ns
    event; rises at ~9 ns → `h_tphl` samples the 9-ns event.

  Across the SIGN halves, the **assignment of column → physical
  event swaps** (`h_tphl` is the 7-ns delay in pos-half but the
  9-ns delay in neg-half; `h_tplh` is the reverse). Combining the
  two halves in a single `linregress` mixes those incompatible
  events and produces a meaningless slope. The pos-half subset
  (mt0..3, SIGN=0) is the consistent measurement domain for any
  *optimisation* metric that reads `h_t*`. Metrics that read only
  `v_t*` are not affected (V_IN polarity is fixed by L45 regardless
  of SIGN/hinvoltage) and may safely cover the full mt0..7 axis.
  The raw `h_tphl` / `h_tplh` observability metrics in §3 are NOT
  restricted — they span all 8 alters so the event-swap is visible
  in the log for triage.

---

## 2. Weight-code derivation (spec-author bookkeeping)

No new `.measure` lines are required in the testbench — the existing 4
raw measures (`h_tphl`, `v_tphl`, `h_tplh`, `v_tplh`) plus the alter
index are sufficient. The integer weight code per `.mt<k>` is encoded
directly into each metric's `reduce.x:` array in §3 (T8.6 resolver
treats it as an opaque numeric x-axis; the "weight" semantics live
only here in the spec).

Weight code derivation (matches the .alter blocks in
`edge_close_new.sp`):

```
weight_code(k) = (-1)**SIGN(k) * (LSB(k) + 2*LSB2(k) + 4*MSB(k))
```

| .mt index | label  | SIGN | LSB | LSB2 | MSB | weight_code |
|---        |---     |---   |---  |---   |---  |---          |
| mt0       | base   | 0    | 0   | 0    | 0   |  0          |
| mt1       | -1     | 0    | 0   | 1    | 0   | +2          |
| mt2       | -2     | 0    | 0   | 0    | 1   | +4          |
| mt3       | -3     | 0    | 0   | 1    | 1   | +6          |
| mt4       | +0     | 1    | 0   | 0    | 0   |  0          |
| mt5       | +1     | 1    | 0   | 1    | 0   | -2          |
| mt6       | +2     | 1    | 0   | 0    | 1   | -4          |
| mt7       | +3     | 1    | 0   | 1    | 1   | -6          |

Effective weight axis = {-6, -4, -2, 0 (×2), +2, +4, +6} = 7 distinct
levels, 8 samples — enough for slope + R² regression.

---

## 3. Machine-readable eval block (v3.2)

The evaluator (`src/hspice_resolver.py`) reads `.mt<k>` columns
directly. v3.2 uses four resolver features:

- **T8.6** `reduce.across: mt_files` + `op: linregress | mean | max |
  min | std | range` — fits least-squares slope/R²/intercept across
  the N `.mt` files at each TRAN row, or scalar reductions of the
  same column.
- **T8.7a** `mt_indices: [...]` — restricts the reducer's mt-axis to
  a chosen subset (e.g. `[0,1,2,3]` for the pos-half SIGN=0 alters);
  the matching `x:` array length must equal the subset length.
- **T8.7b** top-level metric field `eval_rows: [...]` — picks
  specific TRAN-sweep rows for the aggregate PASS/FAIL (e.g. only
  the centred row 6 for "match floor" checks).
- **T8.7c** `source: {expr: "h_tphl - v_tphl"}` — derives a column
  from the raw `.mt<k>` columns via a whitelisted AST (Name / +-*/
  / unary-minus / numeric constants only; no function calls).
- **T8.7d** `op: diff_paired` with `pairs: [[a,b], ...]` — pair-wise
  difference of an mt-indexed column; `output: max_abs_diff`
  returns max |a−b| over the pairs (per row), `output: signed_diff`
  is for a single-pair signed delta.

Aggregate verdict = PASS iff every selected (eval_rows, ...) row
passes. See `docs/hspice_backend.md` and
`tests/test_spec_evaluator_hspice.py` (`TestT86*` and `TestT87*`)
for the full contract.

```yaml
# Sweep shape: 8 .mt files × 13-step delay sweep (rows 0..12,
# `delay = -90p..+90p` step 15p; row 6 is the centred TRAN, where
# the H/V edges are nominally co-incident).
#
# v3.2 emits 14 optimisation-target metrics (DAC linearity ×4:
# slope+R² for PHL/PLH + match floor ×4: hi/lo for PHL/PLH +
# common-mode drift ×2 + SIGN-symmetry ×4: DC offset PHL/PLH and
# pos/neg V-match PHL/PLH) plus 4 raw observability metrics. Every
# *optimisation* metric that reads `h_t*` is restricted to
# `mt_indices: [0,1,2,3]` (SIGN=0 alters); see §1 "Why
# pos-half-only". The raw `h_tphl` / `h_tplh` observability metrics
# (bottom of this block) span the full mt0..7 axis on purpose so
# log triage can spot the SIGN=1 event-swap directly.

# YAML anchor — single source of truth for the pos-half weight axis.
# (Length must match `mt_indices: [0,1,2,3]` in every metric below.)
_x_weight_pos: &x_weight_pos [0, 2, 4, 6]

# YAML anchor — the centred TRAN row (delay = 0). Used by the match
# floor / common-mode / SIGN-symmetry metrics where the spec-author
# wants ONE meaningful evaluation point rather than the full sweep.
_row_centre: &row_centre [6]

metrics:
  # ============================================================
  # Goal 1 — DAC linearity (4 metrics: slope+R² for PHL & PLH)
  # ------------------------------------------------------------
  # Linregress of the H-V matching skew vs weight code over the
  # pos-half (mt0..3, w=0/+2/+4/+6). At centred TRAN (row 6) both
  # edges land together so |slope| ≈ 0 by construction; restricting
  # eval_rows to the two extreme rows where the slope physically
  # peaks (row 0 = -90p, row 12 = +90p) keeps the metric meaningful.
  # Pass band [5, 50] ps/LSB: 28nm post-layout reference shows
  # ~25 ps per weight-code step in the linear region (slope≈1 ps/
  # weight unit · 25 ps scale factor); 5 ps is the "noticeable
  # coupling" floor, 50 ps caps over-driven configs that saturate.
  # ============================================================
  - name: hv_match_phl_pos_slope
    source: { expr: "h_tphl - v_tphl" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: linregress
      x: *x_weight_pos
      output: slope_abs
    eval_rows: [0, 12]
    scale: 1.0e+12          # seconds/LSB → picoseconds/LSB
    pass:   [50, 200]
    sanity: [0, 5000]
  - name: hv_match_phl_pos_r2
    source: { expr: "h_tphl - v_tphl" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: linregress
      x: *x_weight_pos
      output: r_squared
    eval_rows: [0, 12]
    scale: 1.0
    pass:   [0.95, 1.0]
    sanity: [0.0, 1.0]
  - name: hv_match_plh_pos_slope
    source: { expr: "h_tplh - v_tplh" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: linregress
      x: *x_weight_pos
      output: slope_abs
    eval_rows: [0, 12]
    scale: 1.0e+12
    pass:   [50, 200]
    sanity: [0, 5000]
  - name: hv_match_plh_pos_r2
    source: { expr: "h_tplh - v_tplh" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: linregress
      x: *x_weight_pos
      output: r_squared
    eval_rows: [0, 12]
    scale: 1.0
    pass:   [0.95, 1.0]
    sanity: [0.0, 1.0]

  # ============================================================
  # Goal 2 — Match floor at centred TRAN (4 metrics: hi/lo × PHL/PLH)
  # ------------------------------------------------------------
  # At delay=0 the H and V edges should land within the design's
  # mismatch floor: residual |h_t* − v_t*| ≤ 5 ps across the pos
  # half. `op: range` is rejected here because it only measures
  # spread across weight codes — a +10 ps DC offset shared by all
  # mt0..3 would give range=0 and pass spuriously. Encoding floor
  # as separate `op: max` and `op: min` constraints, both held in
  # `[-5, +5]` ps, catches both directions: a positive DC offset
  # blows the `_hi` ceiling, a negative DC offset blows the `_lo`
  # floor, and a 4-ps half-spread around 3 ps blows `_hi`. Threshold
  # justified by 28nm reference floor max 2.9 ps + ~2 ps margin for
  # 16nm process noise.
  # ============================================================
  - name: match_floor_phl_pos_hi
    source: { expr: "h_tphl - v_tphl" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: max
    eval_rows: *row_centre
    scale: 1.0e+12
    pass:   [-0.5, 0.5]
    sanity: [-500, 500]
  - name: match_floor_phl_pos_lo
    source: { expr: "h_tphl - v_tphl" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: min
    eval_rows: *row_centre
    scale: 1.0e+12
    pass:   [-0.5, 0.5]
    sanity: [-500, 500]
  - name: match_floor_plh_pos_hi
    source: { expr: "h_tplh - v_tplh" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: max
    eval_rows: *row_centre
    scale: 1.0e+12
    pass:   [-0.5, 0.5]
    sanity: [-500, 500]
  - name: match_floor_plh_pos_lo
    source: { expr: "h_tplh - v_tplh" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: min
    eval_rows: *row_centre
    scale: 1.0e+12
    pass:   [-0.5, 0.5]
    sanity: [-500, 500]

  # ============================================================
  # Goal 3 — Common-mode drift across weight codes (2 metrics)
  # ------------------------------------------------------------
  # Midpoint (h+v)/2 should be approximately invariant w.r.t. weight
  # code; any drift indicates a parasitic shared-path coupling that
  # tracks weight magnitude. `op: std` over pos-half at row 6 ≤ 5 ps.
  # 28nm reference shows ~5 ps V-shape symmetric around w=0 (Midpoint
  # block); 16nm baseline measured 1.5–2.2 ps stddev — well inside.
  # ============================================================
  - name: cm_phl_pos_drift
    source: { expr: "(h_tphl + v_tphl) / 2" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: std
    eval_rows: *row_centre
    scale: 1.0e+12
    pass:   [0, 5]
    sanity: [0, 500]
  - name: cm_plh_pos_drift
    source: { expr: "(h_tplh + v_tplh) / 2" }
    reduce:
      across: mt_files
      mt_indices: [0, 1, 2, 3]
      op: std
    eval_rows: *row_centre
    scale: 1.0e+12
    pass:   [0, 5]
    sanity: [0, 500]

  # ============================================================
  # Goal 4 — SIGN-symmetry on V path (4 metrics: PHL & PLH)
  # ------------------------------------------------------------
  # `v_t*` should be approximately independent of the SIGN bit at
  # times when the H↔V coupling is NOT actively perturbing the V
  # edge — i.e. at row 6 for w=0 (no coupling), and at the
  # delay-extreme rows 0/12 for non-zero |weight| (V transitions
  # before / after the coupling moment). At row 6 with non-zero
  # weight V is mid-coupling and signed-weight pairs *physically*
  # diverge by tens of ps — that is design behaviour, not a fail,
  # so do NOT evaluate symmetry there. Each edge (PHL / PLH)
  # gets one DC-offset metric (single pair at w=0) and one
  # magnitude-pair metric (three pairs at delay extremes):
  #   sign_dc_offset_{phl,plh} — pair (mt0 ↔ mt4); signed diff at
  #     row 6 captures any DC offset from SIGN-routing parasitics.
  #     ≤ 2 ps. Baseline -0.76 / -0.73 ps (PASS).
  #   pos_neg_v_t{phl,plh}_match — pairs (mt1↔mt5, mt2↔mt6,
  #     mt3↔mt7) at rows 0 / 12; max |Δ| ≤ 2 ps.
  #     Baseline 0.26 / 0.54 ps (PASS).
  # V-path metrics span the full mt0..7 axis — V_IN polarity is
  # fixed by `edge_close_new.sp:45` independent of SIGN, so v_t*
  # in mt4..7 is a clean measurement of the same physical event
  # as mt0..3 (only coupling sign flips with weight sign).
  # ============================================================
  - name: sign_dc_offset_phl
    source: v_tphl
    reduce:
      across: mt_files
      op: diff_paired
      pairs: [[0, 4]]
      output: signed_diff
    eval_rows: *row_centre
    scale: 1.0e+12
    pass:   [-2, 2]
    sanity: [-500, 500]
  - name: sign_dc_offset_plh
    source: v_tplh
    reduce:
      across: mt_files
      op: diff_paired
      pairs: [[0, 4]]
      output: signed_diff
    eval_rows: *row_centre
    scale: 1.0e+12
    pass:   [-2, 2]
    sanity: [-500, 500]
  - name: pos_neg_v_tphl_match
    source: v_tphl
    reduce:
      across: mt_files
      op: diff_paired
      pairs: [[1, 5], [2, 6], [3, 7]]
      output: max_abs_diff
    eval_rows: [0, 12]
    scale: 1.0e+12
    pass:   [0, 2]
    sanity: [0, 500]
  - name: pos_neg_v_tplh_match
    source: v_tplh
    reduce:
      across: mt_files
      op: diff_paired
      pairs: [[1, 5], [2, 6], [3, 7]]
      output: max_abs_diff
    eval_rows: [0, 12]
    scale: 1.0e+12
    pass:   [0, 2]
    sanity: [0, 500]

  # ============================================================
  # Raw delays — observability only, very wide pass for log triage
  # ============================================================
  - name: h_tphl
    scale: 1.0e+12
    pass:   [1, 5000]      # 1 ps – 5 ns; intentionally loose
    sanity: [0.1, 50000]
  - name: h_tplh
    scale: 1.0e+12
    pass:   [1, 5000]
    sanity: [0.1, 50000]
  - name: v_tphl
    scale: 1.0e+12
    pass:   [1, 5000]
    sanity: [0.1, 50000]
  - name: v_tplh
    scale: 1.0e+12
    pass:   [1, 5000]
    sanity: [0.1, 50000]
```

> **Threshold provenance** (don't re-derive without
> understanding both): the fourteen pass bands above are calibrated
> from (1) a 28nm post-layout reference (Excel
> `COBIFIXED28 4S 3L swaped I IB2`: floor max 2.9 ps, slope ~1
> ps/weight unit in the linear region, midpoint V-shape ~5 ps) and
> (2) baseline `num_finger_*=1` 16nm measurements (hv_match slope
> 7.4 / 3.7 ps/LSB at extremes / pos-half, R² 0.998, floor 0.92 ps,
> CM stddev 1.5–2.2 ps). At baseline only `hv_match_*_pos_slope`
> fails (3.7 < 5) — exactly the optimisation driver the LLM is
> meant to fix; the rest pass. **If you change `num_finger_*` range
> or the testbench `delay` sweep, re-derive these bands; do not
> tweak them to make a particular run "look greener".**

---

## 4. Design variables the LLM may adjust

`.PARAM` declarations split across **two files** — the rewrite engine
(T8.3 `sp_rewrite.py`) needs to know which file each var lives in:

| Var               | File                | Role                               | Range          | Type    | Priority |
|---                |---                  |---                                 |---             |---      |---       |
| `num_finger_n0`   | **netlist.sp** L6   | TRI_SVT NMOS top-stack `nf`        | 1 – 32         | int     | **P0**   |
| `num_finger_n1`   | **netlist.sp** L6   | TRI_SVT NMOS bot-stack `nf`        | 1 – 32         | int     | **P0**   |
| `num_finger_p0`   | **netlist.sp** L6   | TRI_SVT PMOS bot-stack `nf`        | 1 – 32         | int     | **P0**   |
| `num_finger_p1`   | **netlist.sp** L6   | TRI_SVT PMOS top-stack `nf`        | 1 – 32         | int     | **P0**   |
| `delay`           | edge_close_new.sp   | TRAN-sweep base offset             | -200p – +200p  | time    | held     |
| `hinvoltage`      | edge_close_new.sp   | H_IN low-rail (0 / 0.8 V)          | held by .alter | volt    | held     |
| `SIGN/LSB/LSB2/MSB`| edge_close_new.sp  | weight-bit codes (8 alter blocks)  | held by .alter | binary  | held     |

`.param` rewrite whitelist (case-insensitive): `num_finger_n0,
num_finger_n1, num_finger_p0, num_finger_p1`. Any LLM-proposed name
outside this set is rejected by `src/sp_rewrite.py` before HSpice ever
runs. The stimulus params (`delay`, `hinvoltage`, `SIGN`, `LSB`,
`LSB2`, `MSB`) are **frozen by the spec** — they parameterise the
test, not the design.

**Physical intuition for `num_finger_*` → linearity**:
- **Total drive (n0+n1, p0+p1)** sets the slope: weak drivers → small
  delay difference per LSB; over-strong drivers saturate the output
  edge against the load and also flatten the response → both extremes
  reduce slope. Optimum is mid-range.
- **Stack balance (n0 vs n1, p0 vs p1)** sets R²: large mismatch
  causes weight-dependent shoot-through asymmetry that bends the
  delay curve away from a straight line.
- The LLM should sweep symmetric configurations first (n0=n1, p0=p1)
  to maximise slope, then break symmetry only if R² is already > 0.95.

**Effective DOF**: 4 vars but only ~2 strong physical DOF
(total-N strength, total-P strength). The stack-internal split
(n0 vs n1, p0 vs p1) is a weak second-order knob. Expect LLM
convergence in 5–15 iterations.

> **Cross-cell scope note (informational)**: TRI_SVT is also used inside
> `12TSRAM_TRI` (4 instances per SRAM bit), so changing `num_finger_*`
> globally also resizes the SRAM write/read ports. SRAM access timing
> is not a concern for this test (the design tolerates a wide write
> window and the storage INV1X keeper holds against read-port loading
> at 16nm).

---

## 5. Convergence aids

Not applicable — this is a deterministic switching circuit. No
`startup:` block needed.

`.IC` is unnecessary; HSpice's default DC operating point converges in
< 100 iterations on this netlist.

---

## 6. HSpice backend specifics

```yaml
hspice:
  # Files under spec_root (resolved by hspice_resolver.py):
  netlist:    netlist.sp                       # subckt definitions + .PARAM num_finger_*
  testbench:  edge_close_new.sp                # top-level .tran + .alter
  topcell:    matching_test                    # name in netlist header

  # T8.3 (sp_rewrite.py): the LLM-driven rewrite engine targets the
  # FIRST .PARAM block in this file each iteration. Set to `netlist`
  # for matching_test because the four tunable vars (num_finger_n0,
  # num_finger_n1, num_finger_p0, num_finger_p1) live in netlist.sp
  # at line 6 -- they parameterise TRI_SVT instance geometry, NOT
  # the testbench stimulus. (Default for new specs is `testbench`;
  # see config/delay_test_spec.md.)
  param_rewrite_target: netlist

  # PDK include — ABSOLUTE path, scrubbed to <path> before LLM sees it.
  # The private side keeps the real path; LLM never reads this line raw.
  lib:
    path:    "<PDK toplevel.l on COBI — set in config/.env or .private.yaml>"
    section: top_tt                            # corner / process section

  # Mirrors the LIVE .OPTION line at edge_close_new.sp:3 verbatim.
  # NOTE: line 2 of the testbench is a COMMENTED-OUT (`*.OPTION ...`)
  # alternative that includes `PROBE=0 MARCH=2`; that line is dead
  # code and is intentionally NOT mirrored here. The testbench is
  # authoritative — do not edit either side without editing the
  # other.
  options: "INGOLD=2 ARTIST=2 PSF=2 MEASOUT=1 PARHIER=LOCAL ACCURACY=1 POST RUNLVL=5 probe=1"
  options_extra: "tmiage=0"                    # second .OPTION line (line 7)
  temp_C:  27
  vdd_V:   0.8

  # .TRAN sweep parameters — captured here for evaluator awareness
  # (the simulator reads them from the .sp directly).
  tran:
    step:        5p
    stop:        10n
    sweep_var:   delay
    sweep_range: [-90p, +90p]
    sweep_step:  15p

  # HSpice produces N+1 .mt<k> files for N alters in this testbench:
  #   sim.mt0 = baseline (.PARAM block at top of edge_close_new.sp)
  #   sim.mt1..mt7 = .alter blocks -1, -2, -3, +0, +1, +2, +3
  mt_files_expected: 8
  fetch_tr0: false                             # 500+ MB raw waveform — NEVER fetch

  # Note: there is no "weight_code_map" under hspice: — the x-axis for
  # `op: linregress` lives inside each metric's `reduce.x:` (§3 anchor
  # `&x_weight_pos`). The resolver is task-agnostic; "weight code" is
  # spec-author bookkeeping (§2), not a resolver concept.
```

---

## 7. Honest caveats

- **SIGN=1 alters swap which physical event each `h_t*` column
  samples.** `H_IN` is `PWL (0 '0.8-hinvoltage' 7.0n
  '0.8-hinvoltage' 7.01n 'hinvoltage' 9n 'hinvoltage' 9.01n
  '0.8-hinvoltage')` — at `hinvoltage=0` (SIGN=0) it falls at 7 ns
  / rises at 9 ns; at `hinvoltage=0.8 V` (SIGN=1) the polarity
  inverts to rises at 7 ns / falls at 9 ns. The `.measure h_tphl`
  trigger is `v(h_in_mid) rise=1` and `h_tplh` is `fall=1`, with
  no qualifying time window. After the input inverter
  `h_in_mid = ~H_IN`, so SIGN=0 puts the 7-ns event into `h_tphl`
  and the 9-ns event into `h_tplh`, while SIGN=1 swaps the two:
  `h_tphl` becomes the 9-ns delay, `h_tplh` becomes the 7-ns
  delay. Same column name → different event across the alter axis.
  v3.2 keeps every *optimisation* metric that reads `h_t*` on
  `mt_indices: [0,1,2,3]` (SIGN=0 only) so the regression /
  floor / CM math sees one consistent event. Raw observability
  `h_tphl` / `h_tplh` are intentionally unrestricted in §3 so the
  swap is visible in logs. If a future revision narrows the
  `.measure` time window or normalises the H_IN polarity, every
  linearity / floor / CM metric must be revisited. Metrics that
  read only `v_t*` (`sign_dc_offset_*`, `pos_neg_v_t*_match`) may
  safely span mt0..7 because V_IN polarity is fixed by L45
  regardless of SIGN.
- **Slope thresholds are calibrated on the pos-half subset.** The
  baseline 3.7 ps/LSB number that drives the 5 ps/LSB pass-floor
  is the slope of `linregress` over `mt_indices: [0,1,2,3]` and
  `x: [0,2,4,6]`; that is *not* the same as the slope you would
  get by including the SIGN=1 alters (which sample a different
  event — see previous bullet). Don't relax the floor based on a
  full-axis baseline; recalibrate via the same pos-half slice.
- **`hv_match_*_pos_slope` collapses at the centred TRAN row.** At
  `delay = 0` (row 6) the H and V edges coincide, so `(h_t* − v_t*)`
  is ≈ 0 across every weight code — slope is mathematically
  meaningless there. v3.2 restricts the slope/R² metrics to
  `eval_rows: [0, 12]` (the two extremes where slope physically
  peaks). If you change the testbench `delay` sweep range or step,
  the centred row index moves and these `eval_rows` must be
  re-derived alongside.
- **Pass bands are calibrated, not arbitrary** — see "Threshold
  provenance" callout in §3. Re-derive when changing
  `num_finger_*` range, the weight-code axis, or the `.tran`
  sweep — do not tune to make a particular run greener.
- **Integer-only `nf`** gives coarse granularity (~50% drive-strength
  steps from nf=1→2). If `hv_match_*_slope` cannot reach 5 ps/LSB
  or `r_squared` cannot reach 0.95 within the 1–32 search range,
  expose `w` as a continuous knob (currently hard-coded `w=106e-9`
  inside TRI_SVT) — that requires another netlist parameterisation
  pass and is out of scope for this spec.
- **`.alter` blocks share the same `.measure` directives.** HSpice
  re-runs the full transient for each alter. 8 alters × 13-step
  delay sweep ≈ 104 transient rows per raw metric per iteration; one
  full LLM-loop iteration on COBI is currently ~20 min of HSpice CPU
  (measured 2026-04-26: 1252 s for `RUNLVL=5 probe=1`).
- **`source: {expr: ...}` is restricted to a small AST whitelist**
  (Names, `+ − * /`, unary minus, numeric constants). This is
  deliberate — it is *not* a general expression engine; do not
  request function calls, comparisons, or attribute access. If a
  future metric needs more, extend the resolver under T8.7c rather
  than working around it in YAML.
