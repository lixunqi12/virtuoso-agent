# Case B — cobi_matching: convergence-failure probe

**Project**: `cobi_matching` (sanitized Ising spin-coupling block, HSpice backend on COBI)
**Failure dimension** (1 of 4): **收敛失败** — pass band is tightened beyond the physical reach of the design DOF.
**Status**: drafts approved by leader (Q1+Q2 ✅, 2026-05-12); LLM pick and `max-iter` awaiting user decision. **Cost ≈ 22 min/iter HSpice on COBI → 4 h at max-iter=10.**

## Pre-flight checklist (run all green before dispatching)

1. **Variant spec exists**: `ls paper/failures/case_B/spec.md` → file present, `wc -l` ≈ 650.
2. **Diff vs base is exactly the intended 6 pass-band edits**: `diff projects/cobi_matching/constraints/spec.md paper/failures/case_B/spec.md` → only `hv_match_phl_pos_slope.pass`, `hv_match_plh_pos_slope.pass`, and 4× `match_floor_*.pass` lines. No collateral edits to prose, anchors, or other metrics.
3. **Python interpreter**: `.venv/Scripts/python.exe -V` → Python 3.12.x. Do NOT use Python 3.14.
4. **safe_bridge state**: `cat ~/.cache/virtuoso_bridge/state.json` → `{host: "<remote-host>", ...}` valid + recent heartbeat (< 24 h old). If stale, refresh the bridge before launching — every HSpice round-trip needs it up.
5. **COBI SSH tunnel up**: bridge `ping`-equivalent passes (do NOT manually SSH — that's the policy violation route). If bridge says COBI is unreachable, fix the tunnel before launching.
6. **Remote spec root reachable**: `--remote-spec-root /project/<group>/<user>/<process>/simulation_example` per the local-only HOW_TO_RUN note. Confirm via the bridge that this path still has the testbench `edge_close_new.sp` and netlist `netlist.sp` from the 2026-04-29 baseline run.
7. **Remote HSpice license available**: `hspice/2023.03` on COBI (HOW_TO_RUN.md). License starvation will manifest as `rc=255` zombie deaths at ~0.5 s — different from spec-driven FAIL we're trying to capture, so check first.
8. **Disk space on COBI**: `projects/cobi_matching/logs/hspice/` is ~10 MB per transcript; remote scratch on COBI consumes ~500 MB per `.tr0` (`fetch_tr0: false` keeps it from coming home, but the COBI side still needs the room).

If any check is red, fix before launching. **NO SSH grep, NO `kinit`-hacks — use the bridge.**

## Why this circuit, not "amp/OTA"

`projects/` contains 4 specs: `lc_vco_base` (20 G LC VCO), `lc_vco_40g` (40 G LC VCO), `cobi_delay` (28 nm delay element, placeholder pass bands), `cobi_matching` (16 nm Ising coupling DAC linearity). **There is no amp/OTA spec on disk.** Among the remaining three, `cobi_matching` is the strongest convergence-fail probe because:

1. The optimization signal is real and quantitative (14 calibrated metrics, ~12 ps/LSB physical slope ceiling at `nf=32`).
2. We already have 7 historical transcripts at MiniMax-M2.7 → established baseline for "what convergence looks like when the spec is reasonable" (`hv_match_*_pos_slope` ~3.5–5.7 ps/LSB across iter 1–5 on the 2026-04-28 run, per `cobi_matching/HOW_TO_RUN.md` last-trajectory table).
3. The design DOF is small (4 integer fingers, 1–32) so the physical bound on each metric is easy to argue from the spec's own §7 caveats.

If the leader wants this on a separate amp/OTA, that would require a **new project** (new netlist + spec + HOW_TO_RUN) — out of scope for D1–3.

## What the variant changes

In `paper/failures/case_B/spec.md` §3 `metrics:`, two metric families are tightened:

```diff
   - name: hv_match_phl_pos_slope
     ...
-    pass:   [5, 50]      # ps/LSB
+    pass:   [50, 200]    # ← 10× over the documented ~12 ps/LSB ceiling

   - name: hv_match_plh_pos_slope
     ...
-    pass:   [5, 50]
+    pass:   [50, 200]

   - name: match_floor_phl_pos_hi   (also _phl_pos_lo, _plh_pos_hi, _plh_pos_lo)
     ...
-    pass:   [-5, 5]      # ps
+    pass:   [-0.5, 0.5]  # ← sub-ps floor, below 16nm process noise of ~0.9 ps
```

All other metric pass bands (`hv_match_*_pos_r2`, `cm_*_drift`, `sign_dc_offset_*`, `pos_neg_v_t*_match`, raw observability bands) are **unchanged**. Spec prose, §7 caveats, weight-code table, and §6 hspice backend block are unchanged — only the pass numbers move.

## Why this is physically unreachable

Two independent walls:

**Slope wall.** §3 threshold provenance: "baseline `num_finger_*=1` 16nm measurements show hv_match slope **7.4 / 3.7 ps/LSB** at extremes / pos-half". §7 honest caveat: "Integer-only `nf` gives coarse granularity (~50% drive-strength steps from nf=1→2). If `hv_match_*_slope` cannot reach **5 ps/LSB** or `r_squared` cannot reach 0.95 within the 1–32 search range, expose `w` as a continuous knob (currently hard-coded `w=106e-9` inside TRI_SVT) — that requires another netlist parameterisation pass." The spec itself documents that the slope ceiling within the LLM's allowed DOF is in the 5–15 ps/LSB band; asking for **50 ps/LSB minimum** is a factor of ~5× past the physical ceiling and would require structural changes the LLM is not allowed to make.

**Match-floor wall.** The original `[-5, 5]` ps band was set with "28nm reference floor max 2.9 ps + ~2 ps margin for 16nm process noise" (§3 inline comment). Tightening to `[-0.5, 0.5]` ps puts the requirement *below* the documented 16nm process noise floor (~0.9 ps); even the perfectly matched design has worse-than-this `max - min` from HSpice fp jitter alone.

Joint: even if the LLM somehow found a `nf` config that gave maximum slope (it cannot exceed ~12 ps/LSB), the match-floor metric would still fail because that's an orthogonal axis driven by parasitic asymmetry, not by total drive strength.

## Expected agent trajectory

5–10 iterations on cobi_matching (HSpice on COBI ~22 min/iter — about 4 h end-to-end at max_iter=10):

1. **iter 1–3**: symmetric sweep `(1,1,1,1) → (4,4,4,4) → (8,8,8,8)`. Slope rises from ~0.4 to ~5 ps/LSB. R² stays 0.95+. Fail reason will be dominated by `hv_match_phl_pos_slope:FAIL(below 50)`, `match_floor_*:FAIL`.
2. **iter 4–6**: LLM pushes `nf` toward the upper end `(16,16,16,16) → (32,32,32,32)`. Slope plateaus near ~12 ps/LSB. Still ~4× under the 50 ps/LSB floor. Match floor metrics still FAIL because increasing finger count doesn't reduce parasitic floor.
3. **iter 7–10**: LLM tries asymmetric stack splits (n0≠n1, p0≠p1, e.g. `(32,4,32,4)`). R² collapses from the imbalance; slope swings wildly but never reaches 50. Match floor stays FAIL.

Run ends at max_iter with the same FAIL set across every iter. No design point in the 4-d search hypercube can pass.

## Root cause (1–2 lines)

Pass band exceeds the physical reach of the allowed DOF; the spec is internally inconsistent because §7 caveats explicitly document the ceiling that the §3 pass band violates. **Tag**: `convergence-failure / unreachable-target`. Closely related to "spec author didn't read §7 before writing §3" — a real-world spec-authoring mistake the paper can showcase.

## Reproduce

**Hold to launch** — placeholders `${LLM}` and `${MAX_ITER}` filled in once user confirms Q3 (LLM pick) + Q4 (max-iter). Suggested defaults: `MAX_ITER=10` (parity with the 2026-04-28 baseline trajectory in HOW_TO_RUN.md; ≈ 4 h COBI HSpice CPU end-to-end). If user wants a cheaper probe to verify the FAIL signature first, suggest `MAX_ITER=5` (≈ 2 h) — enough to see slope plateau near 12 ps/LSB and confirm the FAIL pattern without burning full budget.

Standard cobi_matching HSpice entry point per `projects/cobi_matching/HOW_TO_RUN.md`, only swap the spec path:

```bash
# from F:/AI_tool/virtuoso-agent (repo root)
.venv/Scripts/python.exe scripts/run_agent.py \
    --project cobi_matching \
    --testbench edge_close_new.sp \
    --spec paper/failures/case_B/spec.md \
    --sim-backend hspice \
    --hspice-loop \
    --remote-spec-root /project/<group>/<user>/<process>/simulation_example \
    --max-iter ${MAX_ITER} \
    --llm ${LLM}
```

Common flag traps (verbatim from HOW_TO_RUN.md):
- `--testbench` is the **remote filename only** (`edge_close_new.sp`) — passing a local path causes double-concat and `rc=1` at 0.6 s.
- `--hspice-loop` is required — without it the agent dispatches OCEAN-style single-shot which demands `signals/windows/metrics` blocks; this spec uses `mt_files`/`eval_rows`/`reduce` instead.

Transcript will land in `projects/cobi_matching/logs/hspice/hspice_transcript_<TS>.jsonl`; agent stdout in `projects/cobi_matching/logs/agent/run_<TS>.log`. Same naming as the 7 historical baselines in `paper/data/extracted_logs.csv`.

**Post-run capture for the paper**:
- Copy the full transcript to `paper/failures/case_B/transcript_<TS>.jsonl` (raw, no summary).
- Tail of `run_<TS>.log` (last ~200 lines) → `paper/failures/case_B/run_tail.log`.
- Re-run `paper/scripts/extract_transcript_logs.py` after the run to extend the cost dataset.

LLM choice: Kimi or Claude. User's call.
