# Metrics Cookbook — Phase 3 Mapping Reference

How to translate a numbered goal from Phase 2 into one or more
`metrics:` entries in §3 of spec.md. Copy-paste the patterns; tune
the names, columns, and pass bands.

> **Scope of this cookbook**: HSpice `.mt` reduce flow (Shape A in
> SKILL.md Phase 0) only. The pattern library below assumes you have
> `.mt<k>` files from `.alter` blocks and want to reduce with
> `linregress` / `mean` / `std` / `diff_paired`. If you're writing a
> Spectre/OCEAN (Shape B) spec, this cookbook does NOT apply — use
> `projects/lc_vco_base/constraints/spec.md` as a template instead.

## Table of contents

1. The 7 building-block ops in three families: `linregress`, scalar
   reducers (`mean` / `max` / `min` / `std` / `range`), and `diff_paired`
2. Subset selectors (`mt_indices`, `eval_rows`, `source.expr`)
3. The two traps in detail
4. Pattern library — one canonical YAML per goal type
5. YAML anchor conventions
6. Rejected ops and why

---

## 1. Building-block ops

All ops live under `reduce.op:` of a metric. The cross-link to
`docs/hspice_backend.md` is currently incomplete — `mt_indices`,
`eval_rows`, `source.expr`, and `diff_paired` are documented inline
in this skill instead. The authoritative contract is `src/hspice_resolver.py`
`_REDUCE_OPS`, `_validate_expr_node`, and the `mt_indices`/`eval_rows`
handling inside `_evaluate_reduce_metric` (around L501) and supporting
helpers `_coerce_eval_rows` (L247) and `_filter_basenames_by_indices`
(L799); reduce-op dispatch is in `_apply_reduce_op` (L896). Here is
the working summary.

| op | Inputs needed | What it returns | Use for |
|---|---|---|---|
| `linregress` | `x: [list]` (length = mt-axis or `mt_indices` length); `output: slope \| slope_abs \| intercept \| r_squared` | scalar per row | DAC linearity (slope + R² as two metrics) |
| `mean` / `max` / `min` / `std` / `range` | none | scalar per row reducing across mt-axis | Floor (`max` + `min` pair), CM drift (`std`), spread (`range`) |
| `diff_paired` | `pairs: [[a, b], ...]`; `output: signed_diff` (single pair) or `max_abs_diff` (multi-pair) | scalar per row | SIGN/polarity symmetry checks |

**`reduce.across:` is always `mt_files`** for the platform's standard
sweep model. Time-axis reduction uses a different mechanism (out of
scope here — see backend doc).

---

## 2. Subset selectors

Three knobs control what rows the metric actually evaluates:

### `mt_indices: [...]` (T8.7a)

Restricts the mt-axis subset the reducer sees. **Length must equal
the matching `x:` array length** for `linregress`. Use to:

- Pick one polarity half (cobi: `[0,1,2,3]` for SIGN=0)
- Skip a known-bad alter (e.g. corner sweep where one alter blew up)
- Compare overlapping subsets in different metrics

### `eval_rows: [...]` (T8.7b, top-level metric field)

Restricts which TRAN-sweep rows count toward the aggregate PASS/FAIL.
The metric still computes per row; rows outside `eval_rows` are
emitted to the log but ignored by the verdict.

### `source: {expr: "h_t* - v_t*"}` (T8.7c)

Derives a synthetic column from raw `.mt<k>` columns via a whitelisted
AST. The whitelist is **strict**: Names, `+ − * /`, unary minus,
numeric constants. **No function calls, no comparisons, no attribute
access.** If you need more, extend the resolver — don't try to bend
YAML around it.

If `source:` is omitted, the metric reads the column whose name
matches `name:` (so `name: v_tphl` reads the `v_tphl` column).

---

## 3. The two traps in detail

### Trap 1 — Subset trap (column-event swap)

**Symptom**: `linregress` slope drifts toward 0 even though the
underlying physical effect is large; or R² drops dramatically when
you "add more data".

**Root cause**: a single column header (e.g. `h_tphl`) measures
*physically different events* across the mt-axis because a stimulus
polarity bit flipped between alters.

**Cobi worked example**:
- `H_IN` PWL polarity is gated by `hinvoltage`
- For SIGN=0 (mt0..3), `hinvoltage=0` → `H_IN` falls at 7 ns, rises at 9 ns
- For SIGN=1 (mt4..7), `hinvoltage=0.8 V` → `H_IN` rises at 7 ns, falls at 9 ns
- `.measure h_tphl` triggers on `v(h_in_mid) rise=1` with no time window
- After the input inverter `h_in_mid = ~H_IN`, so:
  - SIGN=0: `h_tphl` samples the 7-ns event
  - SIGN=1: `h_tphl` samples the 9-ns event
- A `linregress` over mt0..7 mixes 7-ns and 9-ns delays in one fit → meaningless

**Fix**: every optimization metric reading `h_t*` is restricted to
`mt_indices: [0, 1, 2, 3]`. Raw observability metrics on `h_t*` are
deliberately left unrestricted (so the SIGN-flip is visible in logs).

**How to recognise it in your testbench**:
- Any param that appears in a `PWL` source AND varies across `.alter`
- Any param that gates `.measure trig_at` / `targ_at` time windows
- Any `if` branch in `.measure` that conditions on a `.param`

If you find one, audit each `.measure` column for whether the
trigger semantics depend on the param. For columns where they do,
the column splits into 2+ physically different signals along the
mt-axis — use `mt_indices` to isolate one.

### Trap 2 — Edge-row vs centre-row dichotomy

**Symptom**: a metric that should "obviously work" returns slope ≈ 0
or std ≈ 0 every iteration; the LLM has no signal to optimize against.

**Root cause**: the metric is being evaluated at a TRAN-sweep row
where the underlying physical quantity is 0 by construction.

**The dichotomy**:

| TRAN sweep row | What it physically represents | Slope/derivative measured here | Floor/offset measured here |
|---|---|---|---|
| centre row (e.g. `delay = 0`) | the two compared edges nominally coincide | ~0 by construction | the actual residual mismatch |
| extreme rows (e.g. `delay = ±90 ps`) | edges maximally separated, coupling effect peaks | actual physical slope | dominated by the offset, not informative |

**Rule**:

- Slope / linearity → `eval_rows: [0, N-1]` (extremes only)
- Floor / DC offset / common-mode → `eval_rows: [centre]`
- SIGN-symmetry pair-diff at non-zero weight → `eval_rows: [0, N-1]`
  (at centre with non-zero weight, the V edge is mid-coupling and pairs
  physically diverge — that is design behaviour, not a fail)
- SIGN-symmetry pair-diff at zero weight (DC offset only) → `eval_rows: [centre]`

Use a YAML anchor for `[centre]` — it appears in many metrics:

```yaml
_row_centre: &row_centre [6]      # for a 13-row sweep, centre is index 6
```

---

## 4. Pattern library — copy-paste templates

### 4a. Linearity (slope + R²) — one goal, two metrics

```yaml
_x_axis: &x_axis [0, 2, 4, 6]   # the swept code values for the chosen subset

- name: <signal>_pos_slope
  source: { expr: "<H_col> - <V_col>" }
  reduce:
    across: mt_files
    mt_indices: [0, 1, 2, 3]    # match length of x_axis above
    op: linregress
    x: *x_axis
    output: slope_abs           # |slope|; use `slope` if sign matters
  eval_rows: [0, <N-1>]         # extremes; trap 2
  scale: 1.0e+12                # s/code → ps/code
  pass:   [<min_slope>, <max_slope>]
  sanity: [0, <very_loose_top>]
- name: <signal>_pos_r2
  source: { expr: "<H_col> - <V_col>" }
  reduce:
    across: mt_files
    mt_indices: [0, 1, 2, 3]
    op: linregress
    x: *x_axis
    output: r_squared
  eval_rows: [0, <N-1>]
  scale: 1.0
  pass:   [0.95, 1.0]
  sanity: [0.0, 1.0]
```

### 4b. Floor — one goal, two metrics (hi + lo)

`op: range` looks tempting but is wrong here — a +10 ps DC offset
shared by every code gives `range = 0` and passes spuriously.
Use a separate `max` and `min`, both held in symmetric bounds:

```yaml
- name: floor_<signal>_pos_hi
  source: { expr: "<H_col> - <V_col>" }
  reduce:
    across: mt_files
    mt_indices: [0, 1, 2, 3]
    op: max
  eval_rows: *row_centre
  scale: 1.0e+12
  pass:   [-5, 5]
  sanity: [-500, 500]
- name: floor_<signal>_pos_lo
  source: { expr: "<H_col> - <V_col>" }
  reduce:
    across: mt_files
    mt_indices: [0, 1, 2, 3]
    op: min
  eval_rows: *row_centre
  scale: 1.0e+12
  pass:   [-5, 5]
  sanity: [-500, 500]
```

### 4c. Common-mode drift — one metric per edge

```yaml
- name: cm_<signal>_pos_drift
  source: { expr: "(<H_col> + <V_col>) / 2" }
  reduce:
    across: mt_files
    mt_indices: [0, 1, 2, 3]
    op: std
  eval_rows: *row_centre
  scale: 1.0e+12
  pass:   [0, 5]
  sanity: [0, 500]
```

### 4d. SIGN-symmetry — DC offset (single pair) + magnitude pairs (multi)

```yaml
- name: sign_dc_offset_<edge>
  source: <V_col>                         # raw column, no expr needed
  reduce:
    across: mt_files
    op: diff_paired
    pairs: [[0, 4]]                       # the two zero-weight alters
    output: signed_diff
  eval_rows: *row_centre
  scale: 1.0e+12
  pass:   [-2, 2]
  sanity: [-500, 500]
- name: pos_neg_v_<edge>_match
  source: <V_col>
  reduce:
    across: mt_files
    op: diff_paired
    pairs: [[1, 5], [2, 6], [3, 7]]       # same-magnitude pairs
    output: max_abs_diff                  # max over pairs
  eval_rows: [0, <N-1>]                   # NOT centre — see Trap 2
  scale: 1.0e+12
  pass:   [0, 2]
  sanity: [0, 500]
```

### 4e. Raw observability (very wide pass)

```yaml
- name: <col>
  scale: 1.0e+12
  pass:   [1, 5000]                       # 1 ps – 5 ns; intentionally loose
  sanity: [0.1, 50000]
```

No `reduce`, no `mt_indices`, no `eval_rows` — the resolver emits one
value per (mt, row) into the log. Use these for triage; never for
optimization signal.

---

## 5. YAML anchor conventions

Use anchors for any value that appears in 2+ metrics. Cobi's anchors:

```yaml
_x_weight_pos: &x_weight_pos [0, 2, 4, 6]
_row_centre:   &row_centre   [6]
```

Naming: leading underscore (so anchors don't get parsed as metric
names if a future resolver scans top-level keys), descriptive
suffix, anchor name matches the variable name.

If you have multiple x-axes (e.g. pos-half and neg-half), name them
distinctly: `&x_weight_pos`, `&x_weight_neg`. Don't reuse one anchor
across semantically different axes — defeats the purpose.

---

## 6. Rejected ops and why

| What you might want | Why it fails | Use instead |
|---|---|---|
| `op: range` for floor | 0-spread around a non-zero offset passes spuriously | `op: max` + `op: min` pair |
| `op: linregress` over full mt-axis on a polarity-asymmetric column | mixes incompatible events (Trap 1) | `mt_indices` to one polarity half |
| `op: linregress` at centre row | slope is 0 by construction (Trap 2) | `eval_rows: [extremes]` |
| `source.expr` with `abs()` or `if` | not in AST whitelist | put the abs into `output: slope_abs` if `linregress`, otherwise compute as separate `_hi` / `_lo` metrics |
| Single metric for "slope and R²" | needs two `output:` values; one metric returns one scalar | two metrics sharing the same `mt_indices` / `x:` / `eval_rows` |
