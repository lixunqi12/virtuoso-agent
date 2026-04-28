# HSpice backend — resolver contract

The HSpice path of `src/spec_evaluator.py` delegates to
`src/hspice_resolver.py:evaluate_hspice`. This document captures the
spec-author-facing contract for the resolver's two evaluation modes
(legacy column lookup vs. `reduce:` block) introduced in T8.6.

## 1. Legacy mode — `name` is the `.mt` column name

When a metric has no `reduce:` key, `name` must appear verbatim as a
column in at least one `.mt<k>` table:

```yaml
- name: f_osc_GHz       # this name is the literal .mt column
  scale: 1.0
  pass:   [19.5, 20.5]
  sanity: [0.1, 100.0]
```

- `evaluate_hspice` reads `name` from each `.mt` table that contains
  it, multiplies by `scale` (default `1.0`), and runs each value
  through `_verdict`.
- Multi-`.mt`/multi-row runs flatten in basename-natural order; the
  aggregate verdict is `PASS` iff every row passes.
- A `name` that matches no column anywhere raises
  `HspiceMetricNotFoundError`. A `name` present in some but not all
  tables is fine — values are collected only from tables that have it.

## 2. Reduce mode — `name` is an output label, `source` is the column

When a metric has a `reduce:` block, `name` becomes a free-form output
label and `source` names the `.mt` column the reducer reads:

```yaml
- name: coupling_sensitivity_h_phl   # output label only
  source: h_tphl                     # the .mt column
  reduce:
    across: mt_files
    op: linregress
    x: [0, 2, 4, 6, 0, -2, -4, -6]
    output: slope_abs
  scale: 1.0e+12                     # applied to source y BEFORE the reducer
  pass:   [10, 200]
  sanity: [0, 5000]
```

### Schema

| key | required | type | notes |
| --- | --- | --- | --- |
| `name` | yes | string | output label |
| `source` | yes | string | `.mt` column to feed the reducer |
| `reduce.across` | yes | enum | `mt_files`, `sweep_rows`, `all` |
| `reduce.op` | yes | enum | `linregress`, `mean`, `max`, `min`, `std`, `range` |
| `reduce.output` | only for `linregress` | enum | `slope`, `slope_abs`, `r_squared`, `intercept` |
| `reduce.x` | only for `linregress` | list of finite numbers | length must equal #mt files for `across: mt_files`; ignored (and not validated) for `mean`/`max`/`min`/`std`/`range` |
| `scale` | optional | finite number | applied to source y **before** the reducer |
| `pass` / `sanity` | as usual | range | applied to the reducer output |

### `scale` order

`scale` is applied to the per-sample y values **before** the reducer
runs. For ops that are linear in y (`mean`, `max`, `min`, `range`,
`std`, `slope`, `intercept`), this scales the output by the same
factor; for `r_squared` it cancels out, so r² is scale-invariant.

### Op semantics

| op | output | UNMEASURABLE when |
| --- | --- | --- |
| `mean` | mean of finite y | 0 finite samples |
| `max` | max of finite y | 0 finite samples |
| `min` | min of finite y | 0 finite samples |
| `std` | population std (`ddof=0`) | <2 finite samples (single-sample std collapses to 0.0 and would silently pass a "spread ≤ X" gate) |
| `range` | `max − min` of finite y | <2 finite samples (same trap as `std`) |
| `linregress slope` | least-squares slope (`numpy.polyfit` deg 1) | <2 finite samples; x has zero variance |
| `linregress slope_abs` | `\|slope\|` | same as `slope` |
| `linregress intercept` | least-squares intercept | same as `slope` |
| `linregress r_squared` | `1 − SS_res / SS_tot` | <2 finite samples; x has zero variance; y has zero variance (`SS_tot = 0`); exactly 2 finite samples (perfect fit, statistically meaningless) |

### Output shape per `across:`

| `across:` | output length | notes |
| --- | --- | --- |
| `mt_files` | `M` (= TRAN sweep rows per `.mt` file; must agree across files) | one scalar per sweep row, reducing N samples (one per `.mt` file) |
| `sweep_rows` | reserved as `N` (one per `.mt` file), reducing M sweep rows per file | **not implemented** — raises `NotImplementedError` |
| `all` | reserved as `[scalar]` (single value over all NM samples) | **not implemented** — raises `NotImplementedError` |

For `mt_files`, every input table must contain `source` and have the
same row count; otherwise the resolver raises `HspiceConfigError`
(missing column) or `HspiceShapeError` (row-count disagreement).

### Errors

| exception | when |
| --- | --- |
| `HspiceMetricNotFoundError` | legacy mode only; `name` does not match any column |
| `HspiceConfigError` | unknown `across` / `op` / `output` enum; `x` length ≠ #mt files; `x` contains NaN/inf; `source` missing in any input `.mt`; `source`/`name` empty/non-string |
| `HspiceShapeError` | row counts disagree across `.mt` files |

`HspiceConfigError` and `HspiceShapeError` are spec/netlist-author
bugs — surface them eagerly so a human fixes the YAML or netlist
rather than the LLM tuning design vars around a mis-wired metric.

### Per-row UNMEASURABLE handling

Each output row goes through `_verdict(value, pass, sanity, reason)`.
A row whose reducer returns `None` (degenerate) is recorded with
verdict `UNMEASURABLE (<reason>)` and a `NaN` placeholder in
`measurements[name]` so per-row alignment is preserved. The aggregate
verdict surfaces the first FAIL row, then the first UNMEASURABLE row
if no FAIL exists.

## 3. Out of scope (intentional, not yet planned)

- `reduce.op`: `median`, `percentile`, `first`, `last`, `argmax`,
  `argmin`
- `reduce.output` for `linregress`: `stderr`, `p_value`
- `reduce.across`: `sweep_rows`, `all` (schema reserved, execution
  raises)
- CSV / per-row dump of reducer inputs
- "Best-row" selection (e.g. pick the sweep row with the highest R²)
- `scipy` is intentionally not a dependency — `numpy.polyfit` covers
  the linregress need.
