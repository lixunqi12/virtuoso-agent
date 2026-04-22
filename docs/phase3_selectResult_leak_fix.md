# Phase 3 Follow-up — Cross-Iter `selectResult('tran)` Handle Leak

**Status**: DRAFT rev 1 — awaiting dual-reviewer (Claude authored, Codex pending)
**Filed**: 2026-04-20 by Claude (post Phase 3 e2e test)
**Scope**: `src/agent.py` only (one-line insertion + comment)
**Risk**: low (idempotent SKILL call, best-effort, no behavioral change on success path)

---

## 1. Motivation — evidence from `run_detached_20260420_143108.log`

Phase 3 (OceanWorker subprocess) ran its first 5-iter e2e test. Result:

| Iter | OceanWorker outcome | Wall clock |
|------|---------------------|------------|
| 1 | ✅ rc=0, 7 metrics returned | 12.4 s |
| 2 | ❌ wall-clock timeout → kill -9 | 60.0 s |
| 3 | ❌ wall-clock timeout → kill -9 | 60.0 s |
| 4 | ❌ wall-clock timeout → kill -9 | 60.0 s |
| 5 | ❌ wall-clock timeout → kill -9 | 60.0 s |

Iters 2–5 **did oscillate** — `safeOceanTCross("Vdiff" ...)` returned a valid
period (44-byte TCP response, not NIL), and the LLM's own reasoning for iter 5
quotes concrete waveform numbers (`V_diff_pp=3.06V`, `f_osc=21.34 GHz`). So
"degenerate PSF" is **not** the right label — the new OCEAN subprocess is
hanging on waveforms that are perfectly well-formed.

### The time-ordering that matters

Log grep `tCross|selectResult|plot\(|OceanWorker|Iteration`:

```
Iter 1 (OK):
  14:32:41  OceanWorker spawn    ← main session has NO prior plot() this run
  14:32:54  subprocess rc=0 in 12.4 s
  14:32:55  safeOceanTCross ...
  14:32:55  selectResult('tran) plot(VT("/Vout_p") - VT("/Vout_n"))
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
            (first call — leaks a live selectResult handle)

Iter 2 (TIMEOUT):
  14:36:52  OceanWorker spawn    ← stale plot handle still alive
  14:37:52  wall-clock timeout   ← subprocess openResults() hangs
  14:37:54  selectResult('tran) plot(...) AGAIN

Iter 3/4/5: same pattern.
```

Iter 1 is the **only** iter where the main SKILL session had no stale
`selectResult('tran)` handle when OceanWorker spawned. That iter succeeded in
12 s. Every subsequent iter had a leftover handle and every one timed out.

## 2. Root cause

`_display_waveform` (`src/agent.py:797`) calls

```skill
selectResult('tran) plot(VT("/Vout_p") - VT("/Vout_n"))
```

`selectResult('tran)` binds the main SKILL session to the PSF at
`~/simulation/LC_VCO_tb/spectre/schematic`. **That binding is process-global
and never freed.** On the next iter:

1. `run_ocean_sim` overwrites the PSF files in place (same dir).
2. Main session's `selectResult` is now pointing at overwritten files — it
   still holds whatever OS-level locks / mmap regions Cadence attaches to an
   opened result.
3. OceanWorker subprocess calls `openResults(psfDir)` on that same dir → the
   Cadence result-open path contends with main session's stale mapping →
   hangs until wall-clock kill.

This is a known pitfall, previously recorded in memory
`reference_ocean_skill_pitfalls.md` entry 4
("**Viva plot() before dumpAll deadlocks selectResult handle**"). The
pre-Phase-3 fix (`src/agent.py:481-490` comment) moved `_display_waveform`
from **before** to **after** `safeOceanDumpAll` within the same iter. That
solved the intra-iter ordering but left the **cross-iter** state intact
because nothing ever drops the handle between iters.

## 3. Proposed fix

Insert one `unselectResult()` call at the **start of each iter**, before
anything touches the PSF. Idempotent on the first iter (no handle yet; the
call is a no-op).

### Diff (against current `src/agent.py`)

```python
# At ~line 457 (after run_ocean_sim succeeds, before Plan Auto patch)
            sim_result = self.bridge.run_ocean_sim(
                lib=lib,
                cell=cell,
                tb_cell=tb_cell,
                design_vars=accumulated_vars,
                analyses=analyses_for_run,
            )
+
+           # Stage 1 rev 13 (2026-04-20): Drop any lingering
+           # selectResult('tran) handle left over from the
+           # previous iter's _display_waveform call. Cadence binds
+           # selectResult process-globally to the PSF dir; when the
+           # next safeOceanRun overwrites that dir in place, a
+           # downstream OceanWorker subprocess openResults() hangs
+           # on the stale mapping. Fixing at iter START covers all
+           # downstream consumers (OceanWorker, read_op_point,
+           # safeOceanTCross) without having to police each one.
+           # Idempotent: a no-op on the first iter (no prior
+           # selectResult). Best-effort: swallow any error so a
+           # SKILL-side glitch does not abort the optimization.
+           try:
+               self.bridge.client.execute_skill("unselectResult()")
+           except Exception as exc:  # noqa: BLE001 — best-effort
+               logger.debug(
+                   "unselectResult() cleanup failed (%s); continuing.",
+                   type(exc).__name__,
+               )

            # Per-iter diagnostic surface (Bug 0/2/4, rev 11 2026-04-20).
            diagnostic = IterationDiagnostic()
```

## 4. Alternatives considered

| Option | Why not |
|--------|---------|
| Call `unselectResult()` inside `_display_waveform` after `plot()` | Plot window still references the result; unselecting immediately would close it and defeat the debug-visualization purpose. |
| Delete `_display_waveform` entirely | User wants live waveform visibility on remote host per spec §6 step 3. |
| Only unselect before `ocean_worker.dump_all` | Does not protect `read_op_point_after_tran` (line 500) and `safeOceanTCross` calls that also touch the tran result; iter-start placement covers all. |
| Gate unselect on iter index > 0 | Saves one ~100 ms TCP round-trip on iter 1 at the cost of extra control flow. Not worth the clarity hit; `unselectResult()` on empty state is documented as safe. |

## 5. Risks

1. **`unselectResult()` with no active selection** — per Cadence docs and
   observed behavior, this is a harmless no-op returning `t`. Verified by
   existing `_display_waveform` calling `selectResult('tran)` afresh each time
   without cleanup and not erroring.
2. **Race with concurrent plot() window** — the plot window's Viva renderer
   keeps its own copy of the waveform data once painted; unselect does not
   close the window, just drops the SKILL-level handle. User still sees the
   last iter's plot until the next `_display_waveform` call.
3. **Worse error path than before** — if `unselectResult()` itself errors
   (unlikely), we `logger.debug` and continue. Optimization run is unaffected.

## 6. Test plan

Re-run the same 5-iter e2e with the same spec and the RAMIC Bridge already up:

```powershell
cd <repo-root>
.\scripts\launch_agent_detached.ps1
```

**Expected**: all 5 iters reach OceanWorker and **none** hit 60 s wall-clock
kill unless the PSF is genuinely degenerate. Success metric: at least 3 of 5
iters return `rc=0` within 20 s (LLM picks a mix of working / borderline
designs; no more systemic timeouts).

**Regression check** — confirm waveform plot window still opens on remote host
Virtuoso after each iter (visual inspection by user).

## 7. Reviewer sign-off

- [ ] **Claude** — draft author, self-review: agreed root-cause, agreed minimal fix
- [ ] **Codex** — pending second review (dual-reviewer rule, memory
      `feedback_dual_reviewer.md`)
