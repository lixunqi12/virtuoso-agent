# Pass-Band Calibration — Phase 4 Reference

How to set the `pass: [lo, hi]` numbers in §3 metrics so they reflect
**physical reality**, not wishful thinking. Wrong bands are the most
common reason an LLM optimization loop converges to a "winner" that
isn't actually better than the baseline.

> **Cobi numbers warning**: every numeric value in this doc (5 ps/LSB,
> 25 ps reference, 28 nm post-layout, etc.) is from the cobi worked
> example. Do NOT transfer these numbers to another circuit. The
> *recipe* is generic; the *values* are not.

> Pass-band **microrules** (fp-noise margins, one-sided bounds,
> ε-collapse) live in `docs/spec_authoring_rules.md` in the
> virtuoso-agent repo. Do NOT duplicate them here. This doc covers
> **macrorules** — where the numbers come from in the first place.

## Inputs you need before Phase 4

You cannot calibrate without:

1. **Baseline measurement**: at least one full simulation with the
   design vars in the middle of their declared range (or at the
   "nominal" sizing if the design has one). The baseline tells you
   the *current* metric values and which goals fail / pass at start.
2. **Reference design data** (one or more of):
   - Prior post-layout sim of the same/related circuit
   - Measured silicon
   - An older PDK functional view with hand-tuned parameters
   - A published paper or characterization report
   The reference tells you *what good looks like* — physically
   achievable values, not just "what we got today".
3. **Simulator fp noise floor**. Run the same baseline twice with the
   only difference being a benign reordering (or RUNLVL bump). The
   metric-to-metric delta is the noise floor for that backend +
   accuracy setting. Cobi at HSpice RUNLVL=5 measures ~1e-5 relative.

## The 4-rule calibration recipe

For each metric in §3, set `pass: [lo, hi]` by walking these rules in order:

### Rule 1 — Anchor the centre of the band

The centre of `pass` should be where the **reference design** sits, not
where the **baseline** sits. Reference = "what good looks like". Baseline
= "where we start". The LLM is supposed to move the metric from baseline
toward (and across) the pass band.

Example: cobi reference (28nm post-layout) shows ~25 ps per weight-code
step in the linear region → ~12.5 ps/LSB scale. Scaled down for 16nm
device-size and weight-code unit conventions, the "physically achievable"
slope target is ~5–10 ps/LSB. So `pass: [5, 50]` for slope —
5 is the floor below which there isn't enough coupling; 50 caps over-driven.

### Rule 2 — Set the floor of `pass` at the "noticeable" threshold

For metrics where one direction is "good" (slope wants high, error wants
low), set the *binding* edge of `pass` at the smallest value that still
delivers physical utility. For cobi slope: 5 ps/LSB is "the smallest
slope that gives the Ising loop enough dynamic range to anneal".

If you have a confirmed reference-design value but no separately
justified utility floor, use reference * 0.5 as a *provisional* draft
value, label it provisional in the Threshold provenance callout, and
ask the user to confirm before running optimization. If no confirmed
reference exists, STOP and ask the user — do not invent a floor.

### Rule 3 — Set the loose edge of `pass` to cap saturation

Every metric has a "too much of a good thing" failure mode:
- Slope too high → over-driven cell saturating against the load,
  R² collapses
- Floor "too negative" → cell has flipped polarity, design broken
- CM std "too low" → likely a degenerate metric (signal is constant
  for an unphysical reason)

Pick the loose edge to catch these. Cobi: slope `pass: [5, 50]` — 50 is
~10× the floor, well above any healthy design but below the saturation
regime starting around 80–100 ps/LSB.

### Rule 4 — Verify width against fp noise

Confirm `pass` width > `max(2 × fp_noise, 1% of pass-centre)`. If this
fails, your pass band will be killed by simulator jitter on every run.
This is the main rule covered in `docs/spec_authoring_rules.md` —
re-read that doc after applying Rules 1-3 and adjust width if needed.

## Set `sanity: [lo, hi]` separately

`sanity` is **not** "looser pass". It is the **plausibility envelope**:
values outside `sanity` mean the measurement chain is broken, not that
the design is wrong.

- `sanity` should be ~50-100× wider than `pass` for absolute-value
  metrics (ps delays, pA currents)
- For ratio / probability / R² metrics, `sanity` is `[0, 1]` (or `[0,
  N]` for counts) — the mathematical range of the quantity
- A value outside `sanity` triggers an `UNMEASURABLE (...)` log
  entry; the LLM is told **not** to tune `design_vars` to fix it.
  It's a spec/measurement bug, not a design bug.

## The "Threshold provenance" callout — non-negotiable

After the YAML metrics block in §3, every spec MUST have a callout
naming, for each pass band, where the numbers came from. Cobi's
template (copy-paste with substitution):

```markdown
> **Threshold provenance** (don't re-derive without
> understanding both): the <N> pass bands above are calibrated
> from (1) <reference design> (<key reference numbers>) and (2)
> baseline `<design_var>=<baseline_value>` <process> measurements
> (<key baseline numbers>). At baseline only `<failing_metric_name>`
> fails (<baseline_value> < <pass_floor>) — exactly the optimisation
> driver the LLM is meant to fix; the rest pass. **If you change
> `<design_var>` range or the testbench `<sweep_var>` sweep,
> re-derive these bands; do not tweak them to make a particular run
> "look greener".**
```

The "don't tweak to look greener" line is critical. The most insidious
spec rot is: an LLM iteration produces a near-pass result, the author
relaxes the band by 5% to call it a PASS, and nobody catches the slow
drift across iterations. The provenance callout exists to make future
authors stop and think.

## Calibration anti-patterns

- **Tuning bands to the baseline so "everything passes at iter 0"**.
  Then there is nothing for the LLM to optimize against. The whole
  point is that 1+ goals fail at baseline so the LLM has a target.
- **Setting bands "to round numbers"**. 5 ps and 50 ps are fine if
  the reference says ~25 ps physical scale; setting them to 10 and
  100 because "it looks cleaner" detaches the spec from physics.
- **Asymmetric bands without justification**. `pass: [5, 50]` is
  fine if rationale is "5 ps is the noticeable floor, 50 is the
  saturation cap"; `pass: [5, 47]` with no comment is a red flag —
  the 47 came from somewhere ad hoc and won't survive a re-derivation.
- **Same band for `_hi` and `_lo` floor metrics with different
  centring**. If `_hi` is `pass: [-5, 5]` and `_lo` is `pass: [-3, 3]`
  the metric is silently asymmetric. Either justify in the comment
  or use the same bounds.

## When to re-calibrate (Caveats §7 should list this)

A re-calibration is mandatory if any of these change:
- Design var range (e.g. `nf: 1-32` → `nf: 1-64`)
- Sweep variable range or step (`delay -90p..+90p` → `-200p..+200p`)
- `.measure` time window or trigger condition
- Stimulus polarity / amplitude
- Reference design changes (e.g. you moved from a 28nm reference to a
  measured 16nm reference)
- PDK process section (TT → SS, or different temperature)

A re-calibration means: rerun baseline, re-anchor centres, re-set
floors per Rule 2, re-update the provenance callout. Do not merely
nudge numbers.
