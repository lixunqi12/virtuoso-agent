# v3 → v4 Asset Migration Proposal (await leader ack before moving)

> **Status (2026-05-16):** v3 (`paper/`) **frozen**, v4 (`paper/v4/`)
> sole active. Listing v3 assets worth carrying to v4. **No file
> moves performed.** Each item shows intended destination + carry
> rationale + open questions. Leader ack required per Round-19+1
> instruction *"list 出来等我 ack 再搬，不要无声搬运"*.

## A. Tables (6) — **all carry, with reframing**

| ID | Source | v4 destination | Carry rationale | Reframe scope |
|---|---|---|---|---|
| Table 1 | `sec/related.tex:47-86` (Differentiation matrix) | `v4/sec/related.tex` §2.4 | Cross-system axis matrix is the §2 closer; needed for C3 cohort-uniqueness claim | Caption: drop "SP2" framing, recast around "C3 cohort"; **add column** to align with §6.3 Table 7 head-to-head per Q3 hybrid ruling |
| Table 2 | `sec/methodology.tex:35-...` (LC_VCO spec contract) | `v4/sec/methodology.tex` §5.2 | Spec contract is the metric-list definitional table | Carry verbatim; check filenames if `spec.md:61--88` line refs still hold post-pivot |
| Table 3 | `sec/methodology.tex:122-...` (Metric defs) | `v4/sec/methodology.tex` §5.2 | Sibling of Table 2 | Carry verbatim |
| Table 4 | `sec/platform.tex:117-...` (Trust-boundary tier status) | `v4/sec/platform.tex` §4.5 | Three-axis audit methodology landing table | Carry verbatim; potentially expand a column for §9 reproducibility cross-link |
| Table 5 | `sec/results.tex:61-94` (Per-LLM benchmark, D6 33/33 PASS) | `v4/sec/results.tex` §6.1 | **Core C4 evidence table.** D6 data complete | Carry verbatim (Plan C/D compaction already applied); revert `\arraystretch 0.88` + `\tabcolsep 3pt` since acmsmall journal layout has more room than acmart sigconf |
| Table 6 | `sec/failures.tex:54-...` (Failure-bucket × LLM matrix) | `v4/sec/failures.tex` §7.2 | **Core C5 evidence table.** Currently 89 `\tbdcell` markers (D8 blocked) | Carry skeleton; data-fill awaits D8 near-saturation probe spec |

**Open question for leader:** Plan D vertical compaction (`\arraystretch 0.88` + `\tabcolsep 3pt`) was a page-budget hack for MLCAD 6pp. **Roll back in v4** since acmsmall allows looser typesetting? Recommend: **yes, revert to acmart defaults** in v4.

## B. Figures (3 candidates → 2 carry, 1 deprecate)

| ID | Source | v4 destination | Status |
|---|---|---|---|
| Fig 1 | `figs/safe_bridge_arch.{svg,pdf}` | `v4/figs/safe_bridge_arch.{svg,pdf}` | **Carry verbatim.** Same SVG, same rasterised PDF. v4 expansion §4.2 will reuse with possibly enlarged caption |
| Fig 2 | `figs/pareto_loss_iter.tex` (TikZ Pareto) | `v4/figs/pareto_loss_iter.tex` | **Carry**, but **note**: original was D9-deferred (renderer not wired). Same blocker in v4 |
| Fig (drop) | `figs/failure_bucket_breakdown.tex` (TikZ) | **deprecate** | Replaced by Table 6 matrix; standalone TikZ no longer carries narrative weight |
| Fig 3 | (NEW for v4, currently `v4/sec/overview.tex` placeholder) | `v4/figs/system_overview.{svg,tikz}` | **To commission** per Q2 ruling once §3 prose stabilises |
| Aux | `figs/lc_vco_hier/`, `figs/lc_vco_tb_hier/`, `figs/full_opamp_hier/` | **defer** | v3 used these as schematic-hierarchy assets; not currently cited in v3 sec/*.tex. May surface in v4 §5.1 workload description for OpAmp transferability — **flag for §5 expansion** |

## C. Prose (8 §s) — **selective carry by section**

| v3 § | Source LOC | v4 destination | Carry scope |
|---|---|---|---|
| §1 Intro | `sec/intro.tex` (66L) | **already replaced** by `v4/sec/intro.tex` (this pass) | — |
| §2 Related | `sec/related.tex` (86L) | `v4/sec/related.tex` (HOLD) | **Carry prose + Table 1**; restructure into 2.1/2.2/2.3 three-way split per outline_v4. Single-cluster v3 prose → cluster B2 (classical analog opt) verbatim; add new 2.1 (LLM-for-digital-EDA = 5 TODAES anchors) + 2.3 (PDK-safe LLM tooling = AnalogAgent + Safe-Bridge framing) |
| §3 Platform | `sec/platform.tex` (151L) | `v4/sec/platform.tex` §4 | **Carry as backbone**; expand each scrub layer with motivation + alternative + why per outline_v4 §4 (target 2.5-3.0p vs v3 ~0.75p) |
| §4 Methodology | `sec/methodology.tex` (209L) | `v4/sec/methodology.tex` §5 | **Carry as backbone**; per-design-choice rationale expansion + JSON tool contract inline + threats-to-validity sub-section (target 3.0-3.5p) |
| §5 Results | `sec/results.tex` (157L) | `v4/sec/results.tex` §6 | **Carry as backbone**; add §6.3 head-to-head Table 7 + per-axis breakouts (target 4.0-5.0p). **Note:** §5.1 TL;DR + §5.5 ops-recommend were aggressively trimmed for MLCAD page-budget; **un-trim in v4** since journal layout allows |
| §6 Failures | `sec/failures.tex` (101L) | `v4/sec/failures.tex` §7 | **Carry skeleton**; await D8 data fill |
| §7 Discussion | `sec/discussion.tex` (74L) | `v4/sec/discussion.tex` §8 | **Carry** with 3-axis breakout (quality / cost / ops-maturity) per outline_v4 §8 expansion |
| §8 Conclusion | `sec/conclusion.tex` (29L) | `v4/sec/conclusion.tex` §10 | **Carry verbatim** + add explicit future-work bullets (multi-circuit, multi-PDK, reasoning-token schema ext) |

**Note on un-trimming:** v3 §5.1 TL;DR was trimmed 12→9 lines and §5.5 from 10→8 lines in Round-17/18 to hold §8 on p.6 for MLCAD. Per leader's "不必再考虑 MLCAD 6pp 限长压缩", the v4 carry should **restore the pre-trim prose** (`paper/sec5_results.md` retains the un-trimmed version as source of truth) **OR** keep the trimmed version and let §6 expansion add depth elsewhere. **Leader to decide.**

## D. Data + supporting files

| Item | v4 destination | Action |
|---|---|---|
| `data/extracted_logs.csv` | **shared via `..\data\`** from v4 | No move — `v4/main.tex` reads via relative path. Already the case for `refs.bib`; same pattern. |
| `data/benchmark_state.json` (D6 33/33 PASS) | same | Shared |
| `data/pricing_2026_05_01.yaml` | same | Shared. **Leader Q decision pending**: re-snapshot near TODAES submission or pin permanent 2026-05-01 with "snapshot frozen for reproducibility" framing |
| `failures/case_A/`, `case_B/` | **carry** to `v4/failures/` once D8 designs probe spec | Two cases — likely not enough for full 8-bucket taxonomy; leader's D8 dispatch to mlcad_runner expected to expand |

## E. Markdown source-of-truth files (`sec*_*.md`)

11 md files in `paper/` (sec1_intro through sec8_conclusion, including
versioned `_v3.md`). v3 convention was md = source of truth, tex =
generated artefact. **v4 question:** continue this convention, or
switch to tex as source of truth?

**Recommendation:** **Switch to tex as source of truth in v4.** Md
files in `paper/` (v3 sources) become **archived reference** only.
Reason: v4 long-form prose is too LaTeX-formatting-rich to maintain
in md (table layouts, math, cross-refs); md → tex translation step
becomes a chronic source of drift. **Leader to confirm.**

## F. What I will NOT carry without explicit leader ack

- `paper/outline_v2.md`, `paper/outline_v3.md` — superseded by `paper/outline_v4.md`. Leave in place as historical record.
- v3 `main.tex` (`paper/main.tex`) — already replaced by `v4/main.tex`. Leave in place; v3 tree is reference-only.
- `paper/plans/`, `paper/scripts/` directories — not yet inspected; flag for separate audit before any carry decision.

## Open questions for leader (5)

1. **Plan D revert** (table arraystretch/tabcolsep) — confirm revert to acmart defaults in v4 §6.1?
2. **§5.1 / §5.5 un-trim** — restore pre-trim prose or keep trimmed?
3. **Md → tex source-of-truth switch** for v4 — confirm?
4. **`analogcoderpro` cite-key fate** — ~~appears in `refs.bib` but never cited in any v3 tex~~ **CORRECTION:** IS cited in v3 `sec/related.tex:9` Designer-cluster paragraph alongside `analogcoder`; v4 §2.2 (classical analog opt) carry decision pending.
5. **Pricing snapshot date** — re-snapshot near submission or pin 2026-05-01?

## Ready to execute on leader ack

Once outline_v4 dual-review closes and you greenlight migration:

- **Phase 1 (mechanical carry):** Tables 2-6, Fig 1, §3-§7 prose backbones → v4 files. ~2-3 hour mechanical operation.
- **Phase 2 (TODAES expansion):** Per-§ depth expansion per outline_v4 targets. ~larger effort, gates on D8/D9/D11 data.
- **Phase 3 (NEW §9 reproducibility):** From scratch; no v3 source.
