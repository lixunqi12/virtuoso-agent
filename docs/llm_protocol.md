# LLM Response Protocol

At each iteration, the LLM must emit exactly **one fenced JSON block**
with four top-level keys: `measurements`, `pass_fail`, `reasoning`,
`design_vars` (plus optional `iteration`). The agent parses the block,
enforces a schema (unknown keys rejected, design-var names checked
against the spec whitelist), and applies the delta to the accumulated
design-variable dict.

## Format

```json
{
  "iteration": 3,
  "measurements": { "<metric_name>": <number> },
  "pass_fail":    { "<metric_name>": "PASS | FAIL (...) | UNMEASURABLE (...)" },
  "reasoning": "<one paragraph diagnosing the pass/fail pattern>",
  "design_vars": { "<var_name>": "<value_with_engineering_suffix>" }
}
```

## Contract

- Keys in `design_vars` MUST be a subset of the spec's design-variable
  table (the `Design variables` section). The agent accumulates them
  iteration over iteration; an omitted key keeps its previous value
  (baseline or prior turn).
- `pass_fail` is authoritative for convergence: the agent breaks when
  every value in it starts with `"PASS"` (case-insensitive prefix).
- `measurements` is advisory — the platform recomputes it every turn
  from the authoritative `safeOceanDumpAll` output. Keep the field so
  the LLM can sanity-check.
- Values in `design_vars` must use **engineering suffixes** only
  (`500u`, `1.5f`, `10n`, `3k`). Physical units (`mA`, `pF`, `nH`,
  `V`, `GHz`) are rejected by the contract-violation check.

## Verdict three-state semantics

- **`PASS`** — value inside the spec's pass range. Do nothing for this
  metric.
- **`FAIL (...)`** — value outside pass range but inside the physical
  sanity envelope. The circuit genuinely misses target; propose
  `design_vars` that move this metric toward pass.
- **`UNMEASURABLE (...)`** — value could not be computed (dump missing,
  SKILL helper error, t_cross found no crossing) OR is outside the
  sanity envelope (suspect). Do NOT tune `design_vars` to fix an
  UNMEASURABLE metric — the measurement chain or spec math is broken,
  not the circuit. Report it in `reasoning`; keep other `design_vars`
  focused on real FAIL metrics.

## Anti-hallucination rules

1. Do NOT fabricate or guess measurement values. `measurements` is
   ignored by the platform — put 0 or null for anything you cannot
   derive from the data shown.
2. When ANY metric is FAIL, you MUST propose at least one change in
   `design_vars`. An empty or unchanged `design_vars` after a FAIL
   wastes an iteration.
3. Do NOT copy `design_vars` verbatim from a previous iteration when
   metrics are still failing — the agent detects identical diffs and
   force-perturbs a current-source design variable (e.g. the key
   beginning with `Ibias`) ×2 as a last-resort exploration kick.

## Iteration flow

The agent loops up to `max_iter` times:

1. Agent merges the LLM's `design_vars` with the accumulated dict.
2. Agent calls `bridge.run_ocean_sim(lib, cell, tb_cell, design_vars,
   analyses)` — OCEAN executes the pre-configured Maestro analyses.
3. Agent calls the generic `safeOceanDumpAll` to collect per-signal /
   per-window statistics over the spec's `signals × windows`.
4. Agent computes `measurements` + `pass_fail` on the PC side from the
   spec's YAML eval block (authoritative).
5. Agent requests a best-effort waveform display for operator visibility.
6. Agent feeds the computed metrics, raw dump stats, per-device DC
   op-point table, and running history into the next-turn prompt.
7. LLM returns a new JSON block; parsing proceeds to step 1.

After the loop, the agent pushes the converged `design_vars` back into
Maestro via `bridge.write_and_save_maestro`.

## Stop conditions

- **SUCCESS** — every metric PASSes in a single iteration.
- **MAX_ITER** — `max_iter` iterations without PASS → abort, print
  best-so-far history.
- **SAFEGUARD** — `amp_hold_ratio < 0.3` for 3 consecutive iterations →
  abort (circuit not oscillating; no point tweaking further).
- **STUCK_IDENTICAL_VARS** — identical `design_vars` for 2 consecutive
  iterations while metrics still fail → abort.
- **CONTRACT_VIOLATION** — LLM emits unknown top-level keys or
  out-of-whitelist `design_vars` names twice in a row → abort.
- **NO_CHANGES** — first iteration produces no baseline AND no LLM
  proposal → abort.

## Per-device DC op-point feedback

Every next-turn prompt carries a per-device DC operating-point table
(`tranOp` snapshot at t=0). Columns: `region`, `vgs`, `vds`, `vov`
(= `vgs − vth`), `id`, `gm`, `gds`, `vth`, `vdsat`.

**Region enum** (SKILL `asiGetInstOpPointValue region` integer):

| Code | Label        | Meaning for a CMOS device                        |
|------|--------------|--------------------------------------------------|
| 0    | cutoff       | Off; `id ≈ 0`; `gm ≈ 0`.                         |
| 1    | triode       | `vds < vdsat`; linear-region; `gds >> gm`.       |
| 2    | saturation   | `vds > vdsat`, `vgs > vth`; normal amplifier.    |
| 3    | subthreshold | `vgs < vth`, weak inversion.                     |
| 4    | breakdown    | `vds > Vdsbreak`; never expected.                |

The LLM must read the op-point table BEFORE proposing `design_vars` and
cite which device's region / vov motivated the chosen delta in its
`reasoning` field. Devices intended as current sources or amplifying
transistors should be in `saturation(2)`; capacitor-like devices
(e.g. MOS varactors with gate-bulk tied) can legitimately sit in
`cutoff(0)` or `triode(1)` with `id ≈ 0` — consult the spec's §1
topology section for per-device intent.
