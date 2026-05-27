# <project_name> Optimization Spec — <process> <topology> <metric_family> (<backend> backend)

> **Scope**: This scaffold is **Shape A only** (HSpice `.mt` reduce — see SKILL.md
> Phase 0). It uses `mt_indices` / `eval_rows` / `op: linregress` / `mt_files_expected`
> and an HSpice backend block. **Do NOT use this scaffold for Shape B (Spectre/OCEAN)** —
> instead copy `projects/lc_vco_base/constraints/spec.md` as your starting template, which
> uses the `signals:` / `windows:` / `metrics:` eval block consumed by `src/spec_evaluator.py`.

> **Platform contract**: circuit-agent reads this file and passes its
> content (after PDK scrub) to the LLM as the target-spec prompt.
> Every number below is authoritative.
>
> Supporting docs (generic, not circuit-specific):
> `docs/spec_authoring_rules.md` (pass-range microrules) ·
> `docs/llm_protocol.md` (response format, iteration flow, stop conditions) ·
> `docs/<backend>_backend.md` (this file's `--netlist` / `.alter` semantics).
>
> **PREREQUISITE**: this spec depends on resolver feature(s) **<T-numbers>**.
> Without them the listed metrics emit `UNMEASURABLE` and the loop idles.

---

## 1. Design under test

- **Library / Cell**: `<scrubbed> / <toplevel_cell>`
- **View**: <backend> netlist (exported from Virtuoso schematic via `<exporter>`)
- Process: <process>; VDD = <X> V
- **Topology** (bottom-up; flag whether primitives are user-authored or stdcell):
  - `<cell_1>` — <one-line role>; key params: `<param_list>`
  - `<cell_2>` — ...
  - **`<DUT_cell>` — <role>. (DUT for sizing.)** Ports:
    `<port_list>`. The transistors whose `nf` (or other tunable) is parameterised:
    | refdes | role | nf = |
    |---|---|---|
    | <inst> | <role> | `<param_name>` |

- **Stimulus** (from `<tb_filename>`):
  - `<source_1>`: <description, gating params, timing>
  - `<source_2>`: ...

- **Probe nodes** (toplevel net names, preserved through scrub):
  - `<net_1>`, `<net_2>` — <role>

- **What the test characterises**: <2-3 sentence physical statement>.
  **The optimisation goal is a <N+M> goal set** — the LLM tunes
  `<design_vars>` so that on the <signal_being_optimized>:
  1. **<Goal 1 name>** — <pass criterion in physical units>.
  2. **<Goal 2 name>** — <pass criterion>.
  3. **<Goal 3 name>** — <pass criterion>.
  4. **<Goal 4 name>** — <pass criterion>.
  *(+M informational: <description> for log/observability only.)*

  **Why <subset_choice>** (delete this paragraph if no subset trap applies):
  <prose explanation of any column-event-swap or polarity-asymmetry that
  forces optimization metrics onto a subset of the mt-axis>.

---

## 2. <Code/index> derivation (spec-author bookkeeping)

<Drop this section if your testbench has only one alter — keep if
multiple alters encode a swept code (weight, frequency, corner, ...).
Show how each `.mt<k>` maps to a physical code.>

| .mt index | label | <bit_1> | <bit_2> | ... | physical_code |
|---|---|---|---|---|---|
| mt0 | <label> | <val> | <val> | ... | <derived_value> |
| ...

---

## 3. Machine-readable eval block (v<X.Y>)

The evaluator (`src/<backend>_resolver.py`) reads `.mt<k>` columns
directly. v<X.Y> uses these resolver features:

- <T-number> `<feature_name>` — <one-line description>
- ...

Aggregate verdict = PASS iff every selected (`eval_rows`, ...) row
passes. See `docs/<backend>_backend.md` for the full contract.

```yaml
# <Sweep shape comment: N alter files × M-step sweep, what each axis represents>
# <Total metric count breakdown: optimization vs observability>

# YAML anchor — single source of truth for the <axis-name> axis.
# (Length must match `mt_indices: [...]` in every metric below.)
_<axis_name>: &<anchor_name> [<values>]

# YAML anchor — the centred TRAN row (where the compared edges nominally
# coincide). Used by the floor / common-mode / DC-offset metrics.
_row_centre: &row_centre [<centre_index>]

metrics:
  # ============================================================
  # Goal 1 — <Goal name> (<N> metrics: <description>)
  # ------------------------------------------------------------
  # <Why this metric family makes physical sense for this goal,
  # why eval_rows is what it is, why mt_indices is what it is.
  # Quote the reference value driving the pass band.>
  # ============================================================
  - name: <metric_name>
    source: { expr: "<H_col> - <V_col>" }   # or just `source: <col_name>`
    reduce:
      across: mt_files
      mt_indices: [<subset>]                # if subset trap applies
      op: <linregress|mean|max|min|std|range|diff_paired>
      x: *<x_anchor>                        # for linregress
      output: <slope_abs|r_squared|signed_diff|max_abs_diff>   # depends on op
    eval_rows: [<extremes_or_centre>]
    scale: 1.0e+12                          # s → ps (or appropriate unit conversion)
    pass:   [<lo>, <hi>]
    sanity: [<wider_lo>, <wider_hi>]
  # ... more metrics for Goal 1 ...

  # ============================================================
  # Goal 2 — <Goal name> (<N> metrics: <description>)
  # ------------------------------------------------------------
  # <Same rationale block format as Goal 1>
  # ============================================================
  - name: <metric_name>
    # ...

  # ============================================================
  # Goal 3 — ...
  # ============================================================

  # ============================================================
  # Goal 4 — ...
  # ============================================================

  # ============================================================
  # Raw delays — observability only, very wide pass for log triage
  # ============================================================
  - name: <raw_col>
    scale: 1.0e+12
    pass:   [<wide_lo>, <wide_hi>]
    sanity: [<even_wider_lo>, <even_wider_hi>]
  # ... one entry per raw column you want logged ...
```

> **Threshold provenance** (don't re-derive without
> understanding both): the <N> pass bands above are calibrated
> from (1) <reference design> (<key reference numbers>) and (2)
> baseline `<design_var>=<baseline_value>` <process> measurements
> (<key baseline numbers>). At baseline only `<failing_metric>` fails
> (<baseline_value> < <pass_floor>) — exactly the optimisation driver
> the LLM is meant to fix; the rest pass. **If you change
> `<design_var>` range or the testbench `<sweep_var>` sweep,
> re-derive these bands; do not tweak them to make a particular run
> "look greener".**

---

## 4. Design variables the LLM may adjust

`.PARAM` declarations split across <N> file(s):

| Var | File | Role | Range | Type | Priority |
|---|---|---|---|---|---|
| `<var_1>` | **<file>** L<line> | <description> | <lo>–<hi> | int/float | **P0** |
| `<var_2>` | **<file>** L<line> | <description> | <lo>–<hi> | int/float | **P0** |
| `<sweep_var>` | <file> | <swept_quantity> | <range> | <type> | held |

`.param` rewrite whitelist (case-insensitive): `<var_1>, <var_2>, ...`.
Any LLM-proposed name outside this set is rejected by
`src/sp_rewrite.py` before the simulator ever runs. The stimulus
params (`<sweep_var>`, ...) are **frozen by the spec** — they
parameterise the test, not the design.

**Physical intuition for `<design_vars>` → goals**:
- <Effect 1>
- <Effect 2>
- The LLM should <suggested search strategy>.

**Effective DOF**: <N> vars but only ~<M> strong physical DOF
(<list_of_strong_dof>). The <weaker_axis> is a weak second-order knob.
Expect LLM convergence in <X>–<Y> iterations.

---

## 5. Convergence aids

<If deterministic: "Not applicable — this is a deterministic switching
circuit. No `startup:` block needed. `.IC` is unnecessary; <backend>'s
default DC operating point converges in < 100 iterations on this
netlist.">

<If oscillator/RF: list `startup:` block, `.IC` settings, and any
`.options` that affect convergence.>

---

## 6. <Backend> backend specifics

```yaml
<backend>:
  netlist:    <netlist.sp>
  testbench:  <testbench.sp>
  topcell:    <toplevel_cell>

  param_rewrite_target: <netlist|testbench>     # which file's .PARAM block to rewrite

  lib:
    path:    "<PDK toplevel.l on backend host — set in config/.env>"
    section: <corner_section>

  options: "<exact .OPTION line, mirrored from testbench>"
  options_extra: "<additional options if any>"
  temp_C:  <T>
  vdd_V:   <V>

  tran:
    step:        <step>
    stop:        <stop>
    sweep_var:   <var>
    sweep_range: [<lo>, <hi>]
    sweep_step:  <step>

  mt_files_expected: <N>
  fetch_tr0: false                              # raw waveform — usually never fetch
```

---

## 7. Honest caveats

- **<Headline measurement subtlety>**: <prose explanation>. <Mitigation
  in the spec, e.g. "v<X.Y> keeps every optimisation metric reading
  `<col>` on `mt_indices: [...]` so the regression / floor / CM math
  sees one consistent event">.
- **Subset rationale**: <restate why the optimization metrics are on
  a subset, so a future editor doesn't widen `mt_indices` and silently
  break things>.
- **eval_rows rationale**: <restate why slope is on extremes / floor on
  centre / etc.>
- **Pass bands are calibrated, not arbitrary** — see "Threshold
  provenance" callout in §3. Re-derive when changing `<design_vars>`,
  the swept-code axis, or the `.tran` sweep — do not tune to make a
  particular run greener.
- **<Granularity caveat>**: <e.g. "Integer-only `nf` gives ~50%
  drive-strength steps from `nf=1→2`. If `<metric>` cannot reach
  `<target>` within the <range> search range, expose `<other_var>` as
  a continuous knob — that requires another netlist parameterisation
  pass and is out of scope for this spec.">
- **Simulator runtime**: <measured wall-clock for one iteration on
  the production host>; sets the LLM-loop iteration budget.
- **`source: {expr: ...}` is restricted to a small AST whitelist**
  (Names, `+ − * /`, unary minus, numeric constants). This is
  deliberate — it is *not* a general expression engine; do not
  request function calls, comparisons, or attribute access. If a
  future metric needs more, extend the resolver under <T-number>
  rather than working around it in YAML.
- **<Re-derivation triggers>**: a re-calibration of pass bands is
  mandatory if any of these change: design var range, sweep range,
  sweep step, `.measure` window or trigger condition, stimulus
  polarity / amplitude, PDK section, reference design.
