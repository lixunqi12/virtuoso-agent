# Spec Authoring Workflow — Phase Details

Detailed walk-through of Phases 1, 2, 5 and the full author checklist.
Phases 3 (metrics mapping) and 4 (pass-band calibration) live in their
own reference docs because they are denser.

> **Shape applicability**: Phase 1 (DUT characterization) and Phase 2 (goal enumeration)
> below are shape-agnostic. Phases 3-5 and the structural / per-metric / cross-document
> / validation checklists at the bottom of this file are written for **Shape A** unless
> a row is explicitly marked Shape B. Shape B authors: substitute `mt_indices` / `eval_rows`
> / `op: linregress` references with `signals` / `windows` / metric `signal/window/stat`
> / `compound`, and substitute `HspiceConfigError` with `spec_evaluator` runtime errors.

---

## Phase 1 — Characterize the DUT

Produces **§1 of spec.md**. The reader (an LLM that has never seen this
circuit) must be able to answer "what am I optimizing, what drives it,
and where do I look?" from §1 alone.

### Required content for §1

1. **Library / cell name** of the toplevel testbench (post-scrub
   placeholder is fine — the agent fills it from `config/.private.yaml`).
2. **View** (HSpice netlist via `hspiceD`, Spectre netlist, etc.) and
   process node + nominal VDD.
3. **Topology — bottom-up cell list**. For each cell:
   - One-line description of role
   - Key parameters (especially anything the LLM will tune — flag
     these explicitly even though §4 also lists them)
   - The DUT sub-block (the cell whose parameters get tuned) gets a
     dedicated table listing per-instance variable mapping.
4. **Stimulus**. Walk every `V*`/`I*` source and PWL definition; name
   the file/line and explain what each pulse does and which params
   gate it.
5. **Probe nodes**. List every toplevel net that downstream metrics
   read. Use the post-scrub canonical name.
6. **What the test characterises**. A 2-3 sentence prose statement of
   the physical phenomenon under test, ending with "**The optimisation
   goal is …**" — this becomes the bridge to Phase 2.

### Common pitfalls

- **Don't paste the raw netlist.** The §1 description is a guide for
  reasoning, not an authoritative copy of the netlist (which the agent
  reads separately).
- **Probe nodes must survive PDK scrub.** If the node name contains a
  PDK identifier (rare but happens for foundry analog blocks), pick the
  closest scrub-survivable parent net.
- **Stimulus polarity matters for Phase 3.** If a single param (like
  `hinvoltage` in cobi) flips PWL polarity, call it out in §1 with
  enough detail that Phase 3 can derive which mt-files belong to which
  polarity half.

---

## Phase 2 — Enumerate optimization goals

Produces the goal-set inside §1 ("**The optimisation goal is a N+M goal
set**"). The N goals are PASS/FAIL drivers; the M are observability
metrics for log triage.

### Pattern — discrete numbered goals

Each goal is **one physical property** the LLM must drive. Don't
collapse two properties into one goal "for compactness" — the LLM
needs to attribute its iteration's win or loss to a specific axis.

Cobi worked example below — copy the **structure** (a small numbered
goal-set with non-degeneracy guards), NOT the goal names. A comparator
/ LDO / oscillator / SAR will have entirely different physical goals.

1. **Linearity** — slope + R² of the optimization-target signal vs
   the swept code, over the measurement-clean subset.
2. **Floor** — residual mismatch at the nominally aligned operating
   point (where the signal is theoretically zero).
3. **Common-mode behaviour** — the average of the differential pair
   shouldn't drift across the swept code.
4. **Symmetry** — pair-wise checks that should be invariant under a
   sign / polarity flip (uses different metric op family — `diff_paired`).
5. **(+1 informational)** — raw signals with very wide pass bands for
   logging only.

### Anti-patterns

- **"Make it good"** — not a goal. Each goal must be a specific
  physical quantity with a finite pass band.
- **Goals that can be satisfied trivially together** (e.g. "low offset"
  AND "low gain" — both satisfied by a degenerate zero-output config).
  Add a Goal that prevents the trivial solution (cobi adds a slope
  *floor* of 5 ps/LSB precisely so the LLM can't win by killing all
  coupling).
- **More than ~6 goals** — each goal becomes one or more YAML metrics;
  the LLM has to balance them in its iteration prompt. Above ~6, the
  LLM gets lost. If you have more, group physically related goals into
  one with multiple sub-metrics.

### Output of Phase 2

A numbered prose list inside §1 that explicitly states pass thresholds
in physical units. Example structure:

*[Example only — replace every name and number with your circuit's:]*

> 1. **DAC linearity** — `|slope|` of `hv_match` vs weight code is
>    ≥ 5 ps/LSB and `R²` ≥ 0.95 over [pos-half subset].
> 2. **Match floor** — at the centred TRAN delay row the residual
>    `|hv_match|` is ≤ 5 ps.
> ...

Phase 3 then translates each numbered goal into 1+ YAML metrics. Keep
the numbering consistent across §1 prose and §3 metric comments
(cobi's §3 has `# Goal 1 — DAC linearity` headers tying back to §1).

---

## Phase 5 — Document caveats

Produces **§7 of spec.md**. The Caveats section is non-negotiable —
every spec must end with one. It is read by the next person (or LLM)
modifying the spec, and prevents quiet regressions.

### Required Caveats topics — Shape A

1. **Measurement subtleties** the metric block silently relies on.
   *Example caveat (cobi):* "SIGN=1 alters swap which physical event
   each `h_t*` column samples" — without this caveat, a future editor
   widening `mt_indices` to `[0..7]` would silently break the slope
   metric. Your spec needs caveats specific to *your* circuit, not
   these.
2. **Subset rationale**. If any metric uses `mt_indices` to restrict
   to a subset, restate why — readers won't re-derive it from the
   testbench.
3. **eval_rows rationale**. Same for any non-trivial `eval_rows`
   choice (especially "centre row only" for floor/CM, "extremes only"
   for slope).
4. **Pass-band calibration provenance**. Repeat the §3 callout in
   prose: "bands calibrated from X reference + Y baseline; do not
   relax to make a particular run greener".
5. **Granularity limits of design vars**. *Example (cobi):* integer-only
   `nf` → ~50% drive-strength steps from `nf=1→2`. State the implication
   ("if metric Z can't reach pass within the search range, expose W as
   continuous before tightening the spec"). For your circuit, identify
   your own granularity-limited tunable parameter.
6. **Simulator runtime**. Measured wall-clock for one HSpice/Spectre
   iteration — sets the LLM-loop iteration budget.
7. **Re-derivation triggers**. List what *must* trigger a band
   re-derivation: change of variable range, sweep range, sweep step,
   `.measure` window, stimulus polarity, or PDK section.
8. **AST / schema limits**. If §3 uses `source.expr`, repeat the AST
   whitelist (`+ − * /`, unary minus, names, numeric constants — no
   function calls). Saves a future editor 30 minutes of debugging.

### Required Caveats topics — Shape B (Spectre/OCEAN)

1. **Signal-path subtleties** — for any `signals[*].path` / `paths` that depends on net-naming conventions of the Spectre netlist or the OCEAN dump path; rationale that future readers won't re-derive from the testbench.
2. **Window rationale** — for any `windows:` interval, why those start/stop times (e.g. "after PLL lock", "first 5 cycles after startup kick", "post-stimulus settle").
3. **stat / compound choices** — for any non-obvious `stat:` (e.g. `duty_pct` instead of `mean`) or `compound:` (`ratio` vs `t_cross_frac`), why this stat captures the metric vs alternatives.
4. **bounds rationale** — for `signals[*].bounds.max_abs` / `bounds.ptp_max` / `bounds.min` / `bounds.max`, what physical limit each sets and why values outside trigger UNMEASURABLE.
5. **startup block rationale** (oscillators only) — `perturb_nodes`, `warm_start`, `v_cm_hint_V` choices and why; cross-link to `src/plan_auto.py` since `spec_evaluator.py` doesn't consume this block.
6. **Pass-band calibration provenance** — same as Shape A; bands calibrated from X reference + Y baseline.
7. **Granularity limits of design vars** — same as Shape A.
8. **Simulator runtime** — measured wall-clock for one Spectre/OCEAN iteration; includes startup-kick overhead if applicable.
9. **Re-derivation triggers** — list what *must* trigger band re-derivation: change of stimulus, window edges, signal path scrub, PDK section, etc.

### Tone

Caveats are written in second person to a future editor. Be honest
about what is fragile. Never use Caveats to hide a weak metric — if
something is wrong, fix the metric in §3 instead of hand-waving in §7.

---

## Full author checklist

Run before declaring spec.md done. Each item is a hard stop —
don't ship until all pass.

### Structural

- [ ] §1 names library/cell, process, VDD, topology bottom-up,
      stimulus, probe nodes, and ends with the goal-set
- [ ] §2 weight-code (or equivalent) bookkeeping table is present and
      indices match the `.alter` blocks in the testbench (Shape A only —
      Shape B has no equivalent)
- [ ] §3 metrics block parses as YAML by extracting and parsing it manually:

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
- [ ] §3 has a "Threshold provenance" callout naming baseline + reference
- [ ] §4 design-vars table lists File/Role/Range/Type/Priority for
      every tunable; held vars are flagged "held"
- [ ] §6 backend block lists every file path the resolver needs
      (netlist, testbench, lib path, options) and the exact mt-file count
      (Shape A only — Shape B has no equivalent)
- [ ] §7 Caveats section is present and covers all 8 required topics

### Per-metric

For every metric in §3:

- [ ] Both `pass` and `sanity` are present, `sanity` strictly wider
- [ ] `pass` width > `max(2 × fp_noise, 1% target)` —
      see `docs/spec_authoring_rules.md`
- [ ] `eval_rows` matches the metric family per the SKILL.md Trap 2
      table
- [ ] If reading a polarity-dependent column, `mt_indices` restricts
      to one polarity half
- [ ] `scale` converts units (seconds → ps, etc.) so that `pass`
      reads in physical units
- [ ] If multiple metrics share an axis (e.g. `x:` for linregress, or
      `eval_rows:` for centre), they reference a YAML anchor not a
      duplicated literal
- [ ] The `name:` is unique and self-describes what is measured (a
      reader scanning `name`s alone should reconstruct the goal-set)

**For Shape B metrics (Spectre/OCEAN), substitute the per-metric checks above with:**

- [ ] Each metric has `signal:`, `window:`, `stat:` (or `compound:` for derived metrics)
- [ ] All `signal:` references resolve to a name declared in the top-level `signals:` block
- [ ] All `window:` references resolve to a name declared in the top-level `windows:` block
- [ ] If `compound: ratio`, both numerator and denominator (signal, window, stat) tuples are well-formed
- [ ] If `compound: t_cross_frac`, `frac:`, `direction:`, `use_abs:`, `ref:` all set deliberately (each is a physics statement)
- [ ] `signals[].bounds.max_abs` / `bounds.ptp_max` are set from physical limits, not guessed
- [ ] If oscillator, `startup:` block exists in a separate YAML fence (NOT inside the `signals/windows/metrics` fence) and lists `perturb_nodes:` etc.
- [ ] `pass:` / `sanity:` present, `sanity` strictly wider; if a `scale:` factor is used (optional but common — see `lc_vco_base/spec.md`), `pass` and `sanity` are read in the post-`scale` units

### Cross-document

- [ ] spec.md cross-links to `docs/spec_authoring_rules.md`,
      `docs/llm_protocol.md`, and the backend doc — does NOT inline
      their rules
- [ ] The PREREQUISITE callout at the top names every resolver
      feature (T-number) the spec depends on; if any are unlanded,
      mark the spec as draft

### Validation

- [ ] Metrics fence parses as YAML by extracting and parsing it manually:

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

      (No standalone `python -m src.spec_validator` CLI exists yet — that's
      planned but unimplemented; the runtime resolver / evaluator is the
      authoritative checker.)
- [ ] Run one baseline iteration end-to-end (`HOW_TO_RUN.md` instructions)
      and confirm the metric block emits real numbers (no UNMEASURABLE),
      and the baseline measured numbers fall inside `sanity` for every
      metric (some failing `pass` is expected — that's the optimization
      driver). Shape A: structural / type bugs surface as
      `HspiceConfigError`. (Shape B: the equivalent runtime check is
      `spec_evaluator` raising on missing/malformed `signals`/`windows`/`metrics`
      — see `src/spec_evaluator.py:112-148`.)
