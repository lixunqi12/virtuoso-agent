---
name: spec-planner
description: Author a circuit-optimization `spec.md` for the virtuoso-agent platform — the YAML-laced markdown contract that drives the LLM optimization loop (HSpice `.mt` reduce or Spectre via OCEAN `safeOceanDumpAll` flow — see Phase 0). Use when the user asks to write/draft/plan a spec for a new circuit, says they want to optimize a parameter (sizing, bias, topology choice) on a specific DUT, refers to "the spec" of a project, or needs to add new metrics/goals to an existing spec. The skill walks a 5-phase authoring workflow (characterize DUT → enumerate goals → map to metrics → calibrate pass bands → document caveats), warns about subset/eval-row traps that quietly poison the regression math, and ships a canonical Shape A 7-section scaffold (modelled on cobi_matching v3.2) and points Shape B authors at the lc_vco_base spec as a starting template.
---

# spec-planner

Author a circuit-optimization `spec.md` that the **virtuoso-agent** consumes as the
authoritative target-spec prompt for an LLM-driven optimization loop. A good
spec turns vague intent ("make the linearity better") into a machine-readable
PASS/FAIL contract that converges in a predictable number of LLM iterations.

This skill encodes the meta-process and the load-bearing patterns from the
gold-standard reference: `projects/cobi_matching/constraints/spec.md` v3.2
(13 iterations of refinement). Don't reinvent that work.

## Where the spec lives

A spec lives at `projects/<project_name>/constraints/spec.md` inside the
virtuoso-agent repo. It is read by `circuit-agent` → scrubbed for PDK names
→ passed verbatim to the LLM as the optimization target.

## ⚠ Hard rule — do NOT guess metric numbers

A spec's `pass:` / `sanity:` bands, `mt_indices`, `eval_rows`, sweep ranges
and design-var ranges become a binding contract on the LLM optimization
loop. **Wrong numbers don't error — they silently mislead the LLM for
hundreds of iterations.**

When you don't have first-hand data to back a number, **STOP and ask the
user**. Do not fill in plausible-looking placeholders, do not extrapolate
from a different circuit, do not invent a baseline. Acceptable behaviours:

- Ask the user for the missing measurement / reference design / silicon datum
- Ask which mt-file index corresponds to which physical code if the
  testbench bookkeeping isn't obvious
- Ask which sub-block is the DUT if §1 admits ambiguity
- Add a `# TODO(confirm user): ...` YAML **comment** immediately above
  the missing field (or above the whole metric / backend block) and
  leave the field absent, OR omit the affected block entirely until the
  user confirms the value — and flag the open question in your reply

> **Never put `<TODO: ...>` text inside a live YAML scalar.** Unquoted,
> the `<...>` parses as a flow mapping → YAML scanner error; quoted, it
> becomes a string and downstream `src/hspice_resolver.py` rejects it
> at runtime (`HspiceConfigError`) because `pass` / `sanity` / `scale` /
> `eval_rows` / `mt_indices` / `x` / `mt_files_expected` / sweep bounds
> / design-var ranges must be numeric. Use a YAML comment above the
> field, or omit the block. **A spec with any unresolved TODO is
> draft-only — do not run the optimizer against it, do not claim
> validation.**

Things you must never invent without confirmation:

- `pass` / `sanity` band edges
- `mt_indices` polarity-half assignment
- `eval_rows` choice when the centre/extreme dichotomy is ambiguous
- design-var ranges
- weight-code ↔ alter index mapping
- "reference design" numbers in the Threshold provenance callout
- `scale` factor (unit conversion is a physics statement)
- `source` column name and `source.expr` (these name physical signals)
- `.measure` column semantics (what event each column samples)
- `reduce.op` choice and `output:` selector (linregress vs mean vs std
  is a metric-design decision)
- `x:` axis values and YAML anchor contents (these are the swept-code
  physical values)
- `.alter` count and `mt_files_expected` (must match the testbench)
- sweep step / range / stop (these are the testbench, not the spec)
- simulator options, PDK section, `temp_C`, `vdd_V` (operating point,
  must come from user)
- `param_rewrite_target` and `.param` rewrite whitelist (security
  boundary)
- baseline simulator runtime (sets the LLM iteration budget — measured,
  not guessed)

**Shape B-specific fields (same rule applies — do NOT guess any of these):**

- `signals[*].kind` (`V` / `I` / `Vdiff` / `Vsum_half`) — testbench-derived; no other values supported (see `src/spec_evaluator.py:34`)
- `signals[*].path` / `signals[*].paths` — exact OCEAN signal path strings (singular for single-net V/I; plural for multi-net derived signals like Vdiff/Vsum_half); one wrong path = UNMEASURABLE
- `signals[*].bounds.max_abs` / `bounds.ptp_max` — physical/sanity limits; wrong values silently demote real metrics to UNMEASURABLE
- `windows:` time intervals (e.g. `late: [1.5e-7, 2.0e-7]`) — testbench-derived; wrong window measures transient noise instead of steady-state
- metric `stat:` choice (`freq_Hz` / `ptp` / `rms` / `mean` / `mean_abs` / `min` / `max` / `duty_pct`) — wrong stat silently measures wrong quantity
- `compound: ratio` numerator/denominator (signal, window, stat) tuple — wrong tuple = meaningless ratio
- `compound: t_cross_frac` `frac:` / `direction:` / `use_abs:` / `ref:` — each is a physics statement
- `startup.perturb_nodes` / `startup.warm_start` / `startup.v_cm_hint_V` — physical/circuit choices

If the user pushes back on this with "just make a reasonable guess", remind
them that pass-band drift compounds across iterations and a 5% wrong floor
can cost a full re-derivation cycle. Bias toward asking.

## Phase 0 — Pick the spec shape (do this FIRST)

Two distinct spec shapes are supported in this repo, plus a third
(Shape C) the skill defers to the user. They use different resolvers,
different YAML structures, and different metric vocabularies. Pick one
before writing anything.

### Shape A — HSpice `.mt` reduce (transient / .alter sweep)
- **When**: testbench produces `.mt0..mtN` measurement files via `.alter`,
  you reduce across mt_files with `linregress` / `mean` / `std` /
  `diff_paired`, your physical signal is edge timing or a static value
  that varies by alter/sweep code.
- **Resolver**: `src/hspice_resolver.py`
- **Backend doc**: `docs/hspice_backend.md`
- **Gold-standard reference**: `projects/cobi_matching/constraints/spec.md`
- **Cookbook applies**: yes — use this skill's `references/metrics-cookbook.md`
  for `mt_indices`, `eval_rows`, subset/edge-row traps, ops, anchors.

### Shape B — Spectre via OCEAN `safeOceanDumpAll` (signals/windows/metrics)
- **When**: testbench is a Cadence Spectre simulation invoked via OCEAN /
  Maestro, you dump signals over time windows and reduce per-window with
  `stat: freq_Hz | ptp | rms | mean | mean_abs | duty_pct | ...` plus
  `compound: ratio` / `compound: t_cross_frac` for derived metrics.
- **Evaluator**: `src/spec_evaluator.py`
- **Gold-standard reference**: `projects/lc_vco_base/constraints/spec.md`
  (also `projects/lc_vco_40g/constraints/spec.md` for the same circuit
  at a different operating point)
- **Cookbook applies**: NO — this skill's `references/metrics-cookbook.md`
  is HSpice-`.mt`-only. For Shape B, copy the YAML structure from the
  LC_VCO spec.md and adapt: `signals:` block lists Vdiff/Vcm/V/I probes
  with `bounds:`; `windows:` block names time intervals; `metrics:` block
  is one entry per measurement with `signal:`, `window:`, `stat:`, and
  optional `compound:` / `scale:` / `pass:` / `sanity:`.
- **Oscillator-class circuits also need a separate `startup:` YAML fence** in its
  own section (see `projects/lc_vco_base/constraints/spec.md` §4) listing
  `perturb_nodes:`, `warm_start:`, `v_cm_hint_V:` etc. for the unstable-equilibrium
  kick. **This block is in a SEPARATE fence, NOT inside the `signals`/`windows`/`metrics`
  fence**, and is consumed by `src/plan_auto.py` (`--auto-bias-ic` path), not by
  `src/spec_evaluator.py`.
- **What still applies from this skill in Shape B**:
  Phase 1 (DUT characterization), Phase 2 (goal enumeration), Phase 4
  (pass-band calibration), Phase 5 (caveats), AND the "do NOT guess"
  hard rule (it's about epistemic discipline, not YAML shape).
- **What does NOT apply**: the metrics-cookbook patterns, the
  `mt_indices` / `eval_rows` traps, the YAML anchors for `_x_axis` /
  `_row_centre`. None of these concepts exist in Shape B.

### Shape C — Anything else (AC / PSRR / phase noise / Monte Carlo / DC stability)
**This skill does NOT currently cover Shape C.** Examples that fall here:
LDO PSRR vs frequency, oscillator phase noise vs offset, comparator
input-referred offset Monte Carlo, opamp open-loop gain/phase margin.
None of these are transient `.measure` table reduction or
`safeOceanDumpAll` time-window stats — they need different evaluator
contracts that don't exist yet. STOP and ask the user how they want to
proceed; do not fake-author a Shape A or Shape B spec for a Shape C
problem.

## Workflow — 5 phases (after Phase 0)

Run these in order. Each phase has a dedicated reference doc — load only the
ones you need for the current step.

| Phase | What you produce | Reference doc |
|---|---|---|
| 1. Characterize DUT | spec §1: cell list, stimulus, probe nodes, what is being characterised | `references/workflow-phases.md` (Phase 1 section) |
| 2. Enumerate goals | A small discrete numbered goal-set (cobi happens to use 4+1 as an example) — each goal is one physical property the LLM must drive | `references/workflow-phases.md` (Phase 2) |
| 3. Map goals to YAML metrics | spec §3 (Shape A) / §2 eval block (Shape B): metrics fence | Shape A: see `references/metrics-cookbook.md` (uses `reduce` / `mt_indices` / `eval_rows` / anchors).<br>Shape B: copy-and-adapt the `signals:` / `windows:` / `metrics:` block from `projects/lc_vco_base/constraints/spec.md` §2; schema lives in `src/spec_evaluator.py` (`_REQUIRED_KEYS`, `_SIMPLE_STATS`, `_COMPOUND_KINDS`). |
| 4. Calibrate pass bands | spec §3 "Threshold provenance" callout — every pass band tied to a baseline run + reference design | `references/pass-band-calibration.md` |
| 5. Document caveats | spec §7: measurement subtleties, granularity limits, what triggers re-derivation | `references/workflow-phases.md` (Phase 5) |

> Section numbers above match the Shape A scaffold (`templates/spec_scaffold.md` /
> cobi). Shape B specs (e.g. `lc_vco_base`) condense to ~5 sections — Phase 5 caveats
> typically land in §5 there, not §7. Use the section names ("DUT", "metrics", "design
> vars", "caveats") rather than the numbers when adapting to Shape B.

**Sections 2 / 4 / 5 / 6** of the canonical spec (sweep / index
bookkeeping — e.g. weight-code in cobi, design vars, convergence aids,
backend specifics) are mostly fill-in boilerplate — the scaffold at
`templates/spec_scaffold.md` has slots for them.

## Two traps that silently poison metrics

Both detailed in `references/metrics-cookbook.md`. Shape A: read the
cookbook before writing the §3 metrics block. Shape B: the cookbook
does not apply (see Phase 0); the gold-standard reference for the
`signals:` / `windows:` / `metrics:` block is
`projects/lc_vco_base/constraints/spec.md` itself.

### Trap 1 — Subset trap (column-event swap)

When the testbench has a stimulus parameter (polarity bit, mode select,
trigger-window switch, ...) that varies across `.alter` AND changes
which physical event a `.measure` column samples, a `linregress` over
the full mt-axis mixes incompatible events and produces a meaningless
slope. See `references/metrics-cookbook.md` §3 Trap 1 for a worked
example (cobi `H_IN` polarity flip swapping `h_tphl` between the 7-ns
and 9-ns events across SIGN halves).

→ **Restrict every optimization metric to one consistent
parameter-value half via `mt_indices: [...]`**. Raw observability
metrics may span the full axis on purpose so log triage can spot the
swap.

### Trap 2 — Edge-row vs centre-row dichotomy

The TRAN sweep has a centred row (where the two compared edges
nominally coincide) and extreme rows (where the relative offset
peaks). Different metric families need different rows:

| Metric family | Correct `eval_rows` | Why |
|---|---|---|
| Slope / linearity | extremes only `[0, N-1]` | At centre, the differential signal ≈ 0 for every swept-code / axis value → slope is mathematically zero |
| Floor / DC offset / common-mode | centre only `[centre]` | Floor is the residual at the nominally aligned point |
| SIGN-symmetry pair-diff at non-zero weight | extremes `[0, N-1]` | At centre with non-zero weight the V edge is mid-coupling and pairs physically diverge — that is design behaviour, not a fail |

Picking the wrong row is the most common spec bug — the metric runs
fine but tests something physically meaningless.

## Spec-author checklist (run before declaring spec.md done)

The full checklist with rationale lives in
`references/workflow-phases.md` (bottom). The hard-stop items:

1. Every optimization metric has BOTH `pass` and `sanity` bands; `sanity`
   is strictly wider than `pass`.
2. Every metric reading a column whose physical event depends on a
   stimulus polarity bit is restricted to one polarity half via
   `mt_indices`.
3. Every metric's `eval_rows` matches the metric family per Trap 2 table.
4. `pass: [lo, hi]` width exceeds `max(2 × simulator_fp_noise, 1% of
   target)` — see `docs/spec_authoring_rules.md` (already in the
   virtuoso-agent repo, do NOT duplicate its rules here).
5. The §3 metrics block has a "Threshold provenance" callout naming
   the baseline run + reference design + which numbers are calibrated
   from which source.
6. §7 Caveats lists every known measurement subtlety and what changes
   trigger re-derivation of the bands (variable range, sweep range,
   stimulus polarity, .measure window).
7. YAML anchors (`&x_axis`, `&row_centre`, ...) are used for any
   value that appears in 2+ metrics — single source of truth.
8. The spec validates structurally and runs end-to-end at least once.
   There is no standalone pre-flight validator command yet. Validate by:
   1. YAML-parsing the metrics fence manually by extracting and parsing it:

      ````python
      import pathlib, re, yaml
      text = pathlib.Path('spec.md').read_text(encoding='utf-8')
      fences = re.findall(r'```(?:yaml|yml)\s*\n(.*?)\n```', text, re.DOTALL)
      eval_blocks = []
      for body in fences:
          data = yaml.safe_load(body)
          if isinstance(data, dict) and 'metrics' in data.keys():
              eval_blocks.append(data)
      assert len(eval_blocks) == 1, f'expected exactly one metrics-bearing YAML fence, got {len(eval_blocks)}'
      data = eval_blocks[0]
      keys = set(data.keys())
      if keys >= {'signals', 'windows', 'metrics'}:
          print('Shape B eval block found:', keys)
      else:
          print('Shape A metrics block found:', keys)
      ````

   2. Running one baseline iteration end-to-end via the project's
      `HOW_TO_RUN.md` instructions; the agent's runtime resolver
      surfaces structural / type bugs as `HspiceConfigError` (Shape A)
      or `spec_evaluator` runtime errors (Shape B), or `UNMEASURABLE`.
   A real `python -m src.spec_validator <file>` CLI is planned but not
   yet implemented — do not document it as if it works.

## Cross-references (do NOT duplicate their content here)

These are companion docs that already live in the virtuoso-agent repo —
the spec.md should `>` cross-link to them, not inline their rules:

- `docs/spec_authoring_rules.md` — pass-band tolerance microrules
  (fp-noise margins, one-sided vs two-sided bounds, ε-collapse traps)
- `docs/llm_protocol.md` — LLM response format, iteration flow, stop conditions
- `docs/hspice_backend.md` — `.alter` / `.mt<k>` semantics (NOTE: this doc is currently
  incomplete — `mt_indices`, `eval_rows`, `source.expr`, and `diff_paired` are
  documented inline in this skill instead. The authoritative contract is `src/hspice_resolver.py`
  `_REDUCE_OPS`, `_validate_expr_node`, and the `mt_indices`/`eval_rows` handling inside
  `_evaluate_reduce_metric` (around L501) and supporting helpers `_coerce_eval_rows` (L247)
  and `_filter_basenames_by_indices` (L799); reduce-op dispatch is in `_apply_reduce_op` (L896).)

- `docs/spec_authoring_rules.md` and `docs/llm_protocol.md` apply to **both shapes**.
- `docs/hspice_backend.md` is **Shape A only**. For Shape B there is no equivalent
  backend doc yet — the gold-standard reference is `projects/lc_vco_base/constraints/spec.md`
  itself; cross-link to that file from your spec's §6 (or whichever section names your
  backend) in place of `docs/hspice_backend.md`. See Phase 0 for the full shape distinction.

## How to start

1. Ask the user 4 things (don't author until you have them — see the
   "do NOT guess" rule above; if any of these are missing, ask, don't infer):
   - **DUT**: library + cell name + which sub-block is being optimized
   - **Stimulus / testbench file**: where the `.tran` / `.alter` /
     `.measure` directives live and what they sweep
   - **Optimization target in plain English**: "I want X to be more linear",
     "I want Y to match Z", etc. — translate to the goal-set in Phase 2
   - **Reference data**: prior post-layout sim, measured silicon, an
     older PDK functional view — anything to anchor pass bands against.
     **No reference data → no pass bands. Stop and ask.**
2. **Pick your starting template by shape (Phase 0)**:
   - Shape A → copy `templates/spec_scaffold.md` to `projects/<name>/constraints/spec.md`
   - Shape B → copy `projects/lc_vco_base/constraints/spec.md` to `projects/<name>/constraints/spec.md` (and adapt §1 DUT, §2 eval block signals/windows/metrics, §3 design vars, §4 startup if oscillator)
   - Shape C → STOP, ask user

   > **NOTE (Shape B)**: after first baseline run confirm the agent log shows the
   > generic eval block loaded (look for `spec_evaluator` output, not the legacy
   > LLM-judged fallback path in `src/agent.py:239-244`). If you see the fallback,
   > the YAML fence is missing one of `signals` / `windows` / `metrics` and the run
   > is silently bypassing the evaluator.

3. Walk Phase 1 → 5, loading reference docs as needed.
   At every phase, if you reach a number / index / range you cannot back
   with user-provided data or a file you've actually read, **add a
   `# TODO(confirm user): ...` comment above the affected YAML field
   (or omit the metric / backend block entirely) and surface the
   question in your next reply** rather than picking a "reasonable"
   value. Never place `<TODO: ...>` text inside a YAML scalar — see the
   "do NOT guess" section above for why it breaks the parser /
   resolver.
4. Run the checklist (item 8 covers structural + end-to-end
   validation — there is no `python -m src.spec_validator` CLI yet,
   that is planned but unimplemented; do not invoke it).
