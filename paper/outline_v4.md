# TODAES Long-Form — Paper Outline (v4 DRAFT, post-pivot to TODAES journal)

> **Status (2026-05-16, post-leader-Round-19-ruling):** **v4 is the
> adopted re-layout.** Leader ruled to pivot venue from MLCAD 2026
> short-conference (6-page double-column) to **ACM TODAES journal
> long-form (14-25 pages)**, positioning the work as the **first
> NDA-safe industrial-PDK 11-LLM analog sizing benchmark in
> TODAES**, extending prior LLM4EDA work (AutoChip, HLSRewriter,
> C2HLSC, LHS, ConfiBench — all digital / HLS) into the analog /
> SPICE-loop domain under realistic foundry-PDK confidentiality
> constraints. **C1 narrowed 2026-05-16 (task aa897fa4)** after
> codex_reviewer_v4 surfaced AMSnet-KG (DOI 10.1145/3736166, TODAES
> v30(6) 2025, Shi et al. He-Lei lab) as concurrent TODAES analog
> LLM4EDA work invalidating the prior "first analog LLM4EDA" claim;
> AMSnet-KG occupies the orthogonal open-PDK + KG-RAG + single-LLM
> niche. ISSCC main paper unaffected; this is a side paper.

> **Style anchors (5 TODAES LLM4EDA application papers, by
> structural similarity):**
> 1. **AutoChip** (DOI 10.1145/3723876) — closest structural match:
>    LLM + EDA-tool feedback loop. Adopt its **method → tool
>    integration → quantitative comparison table → reproducibility**
>    segmentation.
> 2. HLSRewriter (DOI 10.1145/3749986)
> 3. C2HLSC (DOI 10.1145/3734524)
> 4. LHS (DOI 10.1145/3734523)
> 5. ConfiBench (DOI 10.1145/3773087)

---

## Positioning sentence (§1 motivation closer)

> *"To our knowledge, this is the first NDA-safe, industrial-PDK,
> eleven-LLM analog sizing benchmark in a TODAES-class venue,
> extending prior LLM4EDA work [AutoChip, HLSRewriter, C2HLSC, LHS,
> ConfiBench] from the digital / HLS domain into the analog /
> SPICE-loop domain under realistic foundry-PDK confidentiality
> constraints. Concurrent TODAES analog work AMSnet-KG targets
> open-PDK netlist auto-design via knowledge-graph RAG and is
> orthogonal."*

---

## TODAES vs. MLCAD — what changes

| Dimension | MLCAD v3 (was) | TODAES v4 (target) |
|---|---|---|
| Length | 6 pp double-column | 14-25 pp |
| Method depth | Compressed (1.0 p) | Expanded (3-4 pp): each design choice carries **motivation + alternative considered + why this** |
| Evaluation depth | Compressed (1.5 p) | Expanded (4-5 pp): per-axis breakout, **quantitative head-to-head table** vs baselines |
| Related work | 0.5 p single-cluster | 1.5-2 pp **three-way split**: (i) LLM for digital EDA, (ii) classical analog optimization, (iii) PDK-safe LLM tooling |
| Reproducibility | not present | **dedicated §9** describing artifact structure (Safe-Bridge interface, PDK scrub flow, rerun recipe) |
| Tone | sharp / marketing-friendly | every claim backed by a number (runtime ms, token count, iteration count, success rate); no marketing voice |

**v3 sections retained** (Safe-Bridge / 11-LLM benchmark / Pareto /
reasoning-token premium / failure taxonomy / China-cohort
discussion) are all preserved — the change is **depth and framing**,
not removal.

---

## v4 Section structure (AutoChip-aligned)

| § | Title | Approx pp | TODAES-specific shift vs v3 |
|---|---|---|---|
| 1 | Introduction | 1.0 | New positioning sentence; contribution list reframed as "first analog LLM4EDA in TODAES"; explicit roadmap |
| 2 | Background + Related Work | 1.5-2.0 | 3-way split: 2.1 LLM for digital EDA (the 5 TODAES anchors), 2.2 classical analog opt (BO, evolutionary, ADO-LLM, AnaFlow…), 2.3 PDK-safe LLM tooling (AnalogAgent, on-prem complements) — **plus** Table 1 differentiation matrix retained from v3 |
| 3 | Approach Overview | 0.75-1.0 | NEW high-level section: agent loop + Safe-Bridge + spec-contract framework at a glance, before deep-dives. AutoChip places its "system overview" here as a one-figure-one-paragraph onramp |
| 4 | Safe-Bridge: NDA-Safe Substrate | 2.5-3.0 | Expanded from v3 §3 (~0.75 p) to full TODAES-depth treatment: 4.1 threat model, 4.2 architecture (Fig. 1), 4.3 four scrub layers (each with motivation + alternative considered + chosen mechanism), 4.4 five hard invariants, 4.5 three-axis scrub-audit methodology with canonical test |
| 5 | Benchmark Methodology | 3.0-3.5 | Expanded from v3 §4 (~1.0 p): 5.1 workload (LC_VCO_20G + OpAmp), 5.2 spec contract (with metric-def Table 2), 5.3 11-checkpoint matrix with cohort rationale, 5.4 protocol (Safe-Bridge config, prompt template, JSON tool contract), 5.5 design-choice discussion (per-axis motivation + alternative + why), 5.6 seed handling, 5.7 threats to validity |
| 6 | Evaluation: Cost-Quality Pareto + Reasoning-Token Premium | 4.0-5.0 | Expanded from v3 §5 (~1.5 p): 6.1 cost-quality Pareto results + Fig. 2 + Table 5 (per-LLM data), 6.2 reasoning-token premium (SP3 sharp), 6.3 **head-to-head comparison table** vs AutoChip / AnaFlow / EEsizer / AnalogAgent (NEW for TODAES — quantitative deltas on whatever axes overlap), 6.4 operating-point recommendations |
| 7 | Failure-Mode Taxonomy | 2.0-2.5 | Expanded from v3 §6 (~0.5 p): 7.1 eight buckets defined, 7.2 per-LLM × bucket matrix (Table 6), 7.3 pre-registered hypotheses (H1-H3) + results, 7.4 per-bucket case studies (1 short case per bucket where data permits) |
| 8 | Discussion: Cross-Vendor Domestic-China Viability + Take-aways | 1.5-2.0 | Expanded from v3 §7 (~0.5 p): 8.1 viability framing, 8.2 quality / 8.3 cost / 8.4 ops-maturity axes per-axis, 8.5 take-aways + reusable methodology |
| **9** | **Reproducibility / Artifact** | **1.0-1.5** | **NEW for TODAES.** 9.1 artifact structure (`safe_bridge.py`, `llm_client.py`, `spec_evaluator.py`, `run_benchmark.py` org chart), 9.2 PDK scrub flow as a reusable pattern (token list / regex / scrub call sites), 9.3 replication recipe (how to rerun against a new PDK / new LLM ckpt) |
| 10 | Conclusion + Future Work | 0.5 | v3 §8 reframed: future work explicit — multi-circuit (OpAmp + comparator + bandgap), multi-PDK / multi-node, Anthropic/Gemini reasoning-token schema extension |
| | **Total** | **17-22 pp** | within TODAES 14-25 pp window with editorial slack |

**Figures / Tables target:**
- Fig. 1: Safe-Bridge architecture (kept from v3; expand caption)
- Fig. 2: Cost-quality Pareto scatter (D9 hand-off, render pending D8)
- Fig. 3: NEW — system overview diagram for §3 (agent + Safe-Bridge + spec-contract feedback loop)
- Table 1: Differentiation matrix (kept from v3 §2)
- Table 2: Metric definitions (kept from v3 §4)
- Table 3: Spec-contract metrics (kept from v3 §4)
- Table 4: Trust-boundary tier status (kept from v3 §3)
- Table 5: Per-LLM benchmark data (kept from v3 §5)
- Table 6: Failure-bucket × LLM matrix (kept from v3 §6, D8-blocked)
- Table 7: NEW — head-to-head vs AutoChip / AnaFlow / EEsizer / AnalogAgent (§6.3)

---

## Headline contribution list (TODAES-framed)

Replaces MLCAD-framed SP1-SP4 (kept structurally but re-pitched):

- **C1.** **First NDA-safe, closed-loop, industrial-PDK analog
  sizing benchmark comparing 11 LLM checkpoints under a scrubbed
  Safe-Bridge interface in ACM TODAES**, extending the AutoChip /
  HLSRewriter / C2HLSC / LHS / ConfiBench digital-EDA precedents
  into the analog / SPICE-loop domain. Concurrent TODAES analog
  work AMSnet-KG [DOI 10.1145/3736166] targets open-PDK netlist
  auto-design via knowledge-graph RAG and is orthogonal along the
  NDA-PDK, scrub-substrate, and cross-vendor-cohort axes.
- **C2.** **Safe-Bridge: a verifiable NDA-safe substrate** for
  cloud-LLM-driven analog design on real foundry PDK — code-anchored
  four-scrub-layer architecture with three-axis audit methodology.
  Reusable pattern (§9 reproducibility) for any LLM4EDA effort that
  must respect foundry NDA.
- **C3.** **Eleven-checkpoint industrial-node benchmark** spanning
  7 vendor families across 2 countries (6 US + 5 China), the only
  cross-vendor cohort to evaluate frontier domestic-China LLM
  inference (Moonshot / MiniMax / Xiaomi / DeepSeek) on industrial
  analog sizing.
- **C4.** **Cost-quality Pareto + labelled reasoning-token premium**
  — the sharpest single quantitative finding: practitioner under-
  budgeting ratio of $r/(1{-}r)$ for reasoning-billing vendors,
  measured across 11 checkpoints.
- **C5.** **Eight-bucket failure-mode taxonomy** with per-LLM cross-
  vendor data — failure-pattern characterization beyond
  pass/fail headlines.
  > **Contingency note (baked 2026-05-16, Phase-1 task 91240545).**
  > C5 quantitative cells (Table~\ref{tab:failmatrix} 8×11 frequency
  > matrix) are contingent on D8 data collection; D8 expected by
  > **[DATE_TBD]**. If D8 slips past camera-ready cut, C5 degrades
  > to **pre-registered-hypothesis-only** framing (H1/H2/H3 with
  > rationale; matrix replaced by qualitative bucket-occurrence
  > discussion). Leader to confirm camera-ready cutoff at [DATE_TBD].
- **C6.** **Reproducibility / artifact contribution** — full software
  artifact, scrub-rule format, and rerun recipe in §9, enabling
  future analog LLM4EDA evaluations on closed industrial PDKs.
  > **Contingency note (baked 2026-05-16, Phase-1 task 91240545).**
  > C6 artifact public-release is contingent on the foundry-NDA
  > review decision for the `safe_bridge.py` PDK-regex content;
  > expected resolution by **[DATE_TBD]**. If NDA review forbids
  > public release, C6 degrades to **on-request artifact** (scrub-
  > flow doc + regex-format spec + rerun-recipe ships publicly; the
  > regex-content YAML ships only to NDA-cleared reviewers). §9
  > artifact-boundary caveat covers either branch.

---

## Down-stream work after outline approval

1. **§1-§2 rewrite** (intro.tex + related.tex):
   - §1 motivation re-anchored to TODAES positioning sentence
   - §1 contribution list moved from SP1-SP4 to C1-C6
   - §2 split into 2.1 / 2.2 / 2.3 three-way clusters
   - §2 Table 1 retained but caption reframed for TODAES audience
2. **§3 Approach Overview** (NEW file `sec/overview.tex`):
   - One-figure-one-paragraph system overview onramp
   - Fig. 3 system-overview diagram (NEW figure to commission)
3. **§4 Safe-Bridge expansion** (platform.tex → 2.5-3.0 pp):
   - Each scrub layer: motivation paragraph + alternative considered + why this
   - Three-axis audit methodology: canonical test walkthrough
4. **§5 Methodology expansion** (methodology.tex → 3.0-3.5 pp):
   - Per-design-choice motivation + alternative + why
   - Cohort selection rationale expanded
   - JSON tool contract documented inline
5. **§6 Evaluation expansion** (results.tex → 4.0-5.0 pp):
   - Head-to-head comparison table vs prior LLM4EDA work (NEW Table 7)
   - Per-axis breakout on cost / quality / token / iteration
6. **§9 Reproducibility / Artifact** (NEW file `sec/reproducibility.tex`):
   - Artifact org chart
   - Scrub flow as a reusable pattern
   - Replication recipe (how to rerun)

---

## Open questions for leader (before §1-§2 rewrite)

1. **AutoChip structure deep-dive** — do you want me to read the
   AutoChip PDF in full and pull a structural-mirror outline (down to
   subsection granularity), or is the section-level alignment in this
   v4 draft sufficient to proceed?
2. **Fig. 3 system overview** — commission a new diagram (TikZ inline
   or external SVG) or defer to a placeholder pending leader sketch?
3. **Table 7 head-to-head** — TODAES expects quantitative deltas. The
   prior LLM4EDA works (AutoChip / AnaFlow / EEsizer / AnalogAgent)
   each report on different metrics; do we (a) define a common metric
   set and re-extract from their reported numbers, (b) qualitative
   comparison only with explicit "metrics differ" disclaimer, or
   (c) hybrid — quantitative on overlapping axes (success rate,
   iteration count where reported), qualitative elsewhere?
4. **MLCAD 2026 abandon timeline** — current v3 LaTeX build is intact
   and ships on MLCAD timeline; do you want me to keep it
   build-able as a fallback, or freeze v3 and start v4 LaTeX scaffold
   immediately? Leader said *"等我确认是否完全放弃后再清掉相关
   task"* — interpret as: keep v3 intact, parallel-build v4 in
   `paper/v4/` directory tree?
5. **OpAmp / multi-circuit** — TODAES long-form increases pressure
   on the "single-circuit threat to validity"; is mlcad_runner spun
   up on the OpAmp testbench, or does v4 release-target push OpAmp
   from "in-revision hook" to "must-have"?

---

## D-task remapping (MLCAD D-codes → TODAES roadmap)

| MLCAD D-code | Status | TODAES re-interpretation |
|---|---|---|
| D1-D5 | done | done — runner setup / 11-ckpt list / Gemini ID / DeepSeek live smoke |
| D6 | **done 2026-05-16** | 33/33 PASS data ingested into tab:percellm + §5.1 TL;DR + §5.4 r-values + §5.5 ops |
| D7 | pending | Pricing yaml cross-check (pre-camera-ready) |
| D8 | not ready | Near-saturation probe spec design + 8-bucket × 11-LLM failure matrix data |
| D9 | not ready | Pareto figure render |
| D10 | not ready (MLCAD) | TODAES does not have MLCAD's 2026-05-23 deadline; D10 timeline detached |
| **D11 (NEW)** | not started | OpAmp testbench end-to-end (now must-have per multi-circuit threat) |
| **D12 (NEW)** | not started | Reproducibility artifact section (§9): repo cleanup, scrub-flow doc, rerun recipe |

---

## Open issues — TODAES-specific

1. **Author affiliation / venue identity.** TODAES submission is not
   double-blind by default (varies by editor); confirm current
   anonymous-build scaffold (`acmart sigconf,anonymous,nonacm,review`)
   needs to switch to `acmart acmsmall,review` (TODAES short
   submission class). Build chain stays identical; just class option
   swap.
2. **Pricing snapshot date.** v3 pinned 2026-05-01. TODAES timeline
   is months out; either re-snapshot near submission or pin permanent
   2026-05-01 with "snapshot frozen for reproducibility" framing.
   Leader to decide.
3. **Five TODAES anchor citations.** Need bib entries for AutoChip /
   HLSRewriter / C2HLSC / LHS / ConfiBench added to `refs.bib`.
4. **§9 artifact public-vs-NDA boundary.** Reproducibility section
   describes how to rerun, but actual `safe_bridge.py` ships only on
   request (PDK regex needs to be redacted). Clarify in §9 caveat.
