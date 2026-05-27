# Case A — LC_VCO_20G: spec-parse-error failure probe

**Project**: `lc_vco_base` (20 GHz LC VCO, Spectre/OCEAN backend, local Cadence host)
**Failure dimension** (1 of 4): **spec解析错** — LLM trusts §5 prose, eval block computes the inverse direction.
**Status**: drafts approved by leader (Q1+Q2 ✅, 2026-05-12); LLM pick and `max-iter` awaiting user decision.

## Pre-flight checklist (run all green before dispatching)

1. **Variant spec exists**: `ls paper/failures/case_A/spec.md` → file present, `wc -l` ≈ 144.
2. **Diff vs base is exactly the intended 4-line change**: `diff projects/lc_vco_base/constraints/spec.md paper/failures/case_A/spec.md` → only `numerator`/`denominator` swap in the `amp_hold_ratio` block. No collateral edits.
3. **Python interpreter**: `.venv/Scripts/python.exe -V` → Python 3.12.x. Do NOT use Python 3.14 (`C:\Python314\python.exe`) — virtuoso-agent dependencies are pinned against 3.12.
4. **safe_bridge state**: `cat ~/.cache/virtuoso_bridge/state.json` → `{host: <local Cadence host>, ...}` valid. If the bridge is down, `scripts/run_agent.py` will fail at the `--lib pll --cell LC_VCO` resolution step.
5. **Maestro Design Variables pane** has all 8 vars with numeric defaults present (per spec §3 prereq): `Ibias, nfin_neg, nfin_cc, nfin_mirror, nfin_tail, R, C, L`. Missing defaults → `SFE-1997` fatal error. Use `safe_bridge.read_circuit` (NOT SSH grep) to verify.
6. **Disk space for transcripts**: `projects/lc_vco_base/logs/agent/` writable; `df -h` shows > 200 MB free on the volume holding `projects/`.

If any check is red, fix before launching; do not "force-launch and see".

## What the variant changes

In `paper/failures/case_A/spec.md` §2 `metrics:`, the `amp_hold_ratio` numerator/denominator are **swapped**:

```diff
   - name: amp_hold_ratio
     compound: ratio
-    numerator:   {signal: Vdiff, window: late,  stat: rms}
-    denominator: {signal: Vdiff, window: early, stat: rms}
+    numerator:   {signal: Vdiff, window: early, stat: rms}
+    denominator: {signal: Vdiff, window: late,  stat: rms}
     pass: [0.95, null]
     sanity: [0.0, 3.0]
```

Nothing else changes. The metric **name** still says `amp_hold_ratio` (suggesting "amplitude is held = late / early"), the pass band `[0.95, null]` still reads as a "lower-bound on retention". §5 honest caveats are untouched — they still say "use late window to avoid 50–75 ns startup ringing", reinforcing the intuition that late should be the numerator.

## Why this is a sharp failure probe

The literal eval semantics now compute `ratio = early_rms / late_rms`. For any LC VCO that has reached steady-state oscillation, late-window RMS ≥ early-window RMS (the amplitude is still building during the 75–125 ns early window before locking into the 150–200 ns late window). So under the inverted definition:

- A healthy, growing oscillator → `ratio ≤ 1.0` always, and usually `0.85–0.95` → FAIL against `pass: [0.95, null]`.
- The ONLY way to make `early_rms / late_rms ≥ 0.95` is for the amplitude to NOT grow between the two windows — i.e. a damped or already-saturated oscillator (or one that pumps and dies). All of those break other metrics: `V_diff_pp_V ≥ 0.40`, `f_osc_GHz ∈ [19.5, 20.5]`, and `t_startup_ns ≤ 10`.

So the LLM is pushed into a corner: it reads the prose and thinks it should optimize for amplitude retention, but the spec's eval block penalizes amplitude *growth*. The two objectives are inverted on the same metric.

## Expected agent trajectory

Best guess across 5–10 iterations:
1. **iter 0–1**: typical bias point (`Ibias=500u, nfin_neg=16`) → growing oscillation → `amp_hold_ratio = early/late ≈ 0.7–0.85` → FAIL.
2. **iter 2–4**: LLM reads "amp_hold_ratio FAIL (below 0.95)" + §5 prose ("amp_hold_ratio should be ≥ 0.95"). Reasons that late RMS is too low vs early; pushes Ibias up or `nfin_neg` up to "hold amplitude". The actual eval (early/late) gets *worse* as steady-state amplitude grows faster relative to early.
3. **iter 5–7**: LLM may invert hypothesis — drop Ibias to "let amplitude settle earlier". This makes early RMS approach late RMS (ratio → 1.0) but `V_diff_pp_V` collapses < 0.40 → new FAIL.
4. **iter 8–10**: oscillation between "high Ibias high V_diff but low ratio" and "low Ibias good ratio but low V_diff". No design point closes both. Run ends with truncation FAIL.

If the LLM eventually inspects the `compound: ratio` YAML block and notices the numerator/denominator order, it could in principle flag it — but the prose and metric name actively mislead, and the agent doesn't normally relitigate the spec. Score: high-probability stuck-FAIL.

## Root cause (1–2 lines)

The metric *prose* and the metric *eval block* express opposite directions of the same physical quantity. The LLM optimizes from the prose; the simulator scores from the eval block. **Tag**: `spec-parse-error / definition-inversion`.

## Reproduce

**Hold to launch** — placeholders `${LLM}` and `${MAX_ITER}` filled in once user confirms Q3 (LLM pick) + Q4 (max-iter). Suggested defaults: `MAX_ITER=10` (cheap, ~5 min total at ~30 s/iter local Spectre), `LLM=kimi` or `claude` depending on which side of the Pareto plot we want to populate first.

Standard lc_vco_base entry point per `projects/lc_vco_base/HOW_TO_RUN.md`, only swap the spec path:

```bash
# from F:/AI_tool/virtuoso-agent (repo root)
.venv/Scripts/python.exe scripts/run_agent.py \
    --project lc_vco_base \
    --lib pll \
    --cell LC_VCO \
    --tb-cell LC_VCO_tb \
    --spec paper/failures/case_A/spec.md \
    --max-iter ${MAX_ITER} \
    --sim-backend spectre \
    --llm ${LLM}
```

Transcript will land in `projects/lc_vco_base/logs/agent/transcript_<TS>.jsonl`; agent stdout in `projects/lc_vco_base/logs/agent/run_<TS>.log`. Same naming convention as the 3 historical lc_vco_base baselines already in `paper/data/extracted_logs.csv`.

**Post-run capture for the paper**:
- Copy the full transcript to `paper/failures/case_A/transcript_<TS>.jsonl` (do not summarize — leader explicitly asked for raw iteration logs).
- Tail of `run_<TS>.log` (last ~200 lines) → `paper/failures/case_A/run_tail.log`.
- Re-run `paper/scripts/extract_transcript_logs.py` after the run to refresh the cost dataset with the new row.

LLM choice for the failure probe: Kimi or Claude (paper comparator). User's call.
