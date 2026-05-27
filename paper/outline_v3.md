# MLCAD 2026 — Paper Outline (v3 DRAFT, post-pivot to Platform+Benchmark)

> **Status (2026-05-12, post-user-D2-ruling):** **v3 is now the
> adopted re-layout.** User ruled to pivot to **paper-class =
> benchmark + platform** with an **11-checkpoint matrix (6 US + 5
> China across 7 vendor families)**, Gemini upgraded from optional
> → mandatory, DeepSeek
> V4-pro + V4-flash added. Safe-Bridge is preserved as a full §3
> Platform section (not compressed) — per user explicit note
> *"我们的 safe-bridge 这些也应该提到"* — only its *positioning*
> changes from "first NDA-safe headline" to "verifiable substrate
> that makes the benchmark possible." v2 baseline files
> (`outline_v2.md`, `sec1_intro.md`, `sec2_related_v2.md`,
> `sec4_safe_bridge_threat_model.md`) are archived for diff reference.

**Proposed working titles (user pick one or supply alternate):**

1. *An Eleven-LLM Benchmark for Industrial-Node Analog Sizing:
   Cost, Quality, and Cross-Vendor Domestic-China Viability*
2. *Six US + Five China Frontier LLMs vs. an Industrial Analog
   Sizing Task: A Cost-Quality Pareto Study*
3. *Safe-Bridge: a Verifiable NDA-Safe Platform and Eleven-LLM
   Benchmark for Industrial Analog Circuit Design*

**Venue:** MLCAD 2026 (Workshop on ML for CAD).
**Format (locked 2026-05-12):** 6 pages, double-column ACM (acmart
sigconf, US Letter, 9-10 pt). References + appendix don't count.
**Double-blind.**
**Deadline:** abstract reg 2026-05-16, full paper 2026-05-23.
**Paper class:** **benchmark + platform** (was: system).

---

## Selling-point re-layout (post-pivot)

- **SP1 (was headline → now enabling infra).** Safe-Bridge:
  verifiable NDA-safe platform with four scrub layers + code-anchored
  invariants. Cited as the *substrate* that makes SP2 possible on a
  real foundry PDK; not the headline contribution.
- **SP2 (NEW HEADLINE).** **First multi-LLM benchmark for industrial-
  node analog sizing.** **Eleven checkpoints across seven vendor
  families — 6 US + 5 China.** All checkpoints driven on identical
  Safe-Bridge / spec / prompt; only `--llm` flag differs. Domestic-
  China frontier coverage spans **four vendors** (Moonshot Kimi,
  MiniMax, Xiaomi MiMo, DeepSeek), the largest such cross-vendor
  China-LLM cohort on any industrial-EDA workload to our knowledge.
- **SP3.** **Cost-quality Pareto.** Per-iteration token cost (with
  separate reasoning-token accounting) vs. spec-pass rate; per-budget
  operating-point recommendation.
- **SP4.** **Failure-mode taxonomy + cross-vendor domestic-China LLM
  viability on a single industrial-PDK RF testbench.** 8 buckets
  reported per-LLM across the **11 × 1-failure-spec matrix**;
  explicit go/no-go for China-domestic deployment with a **four-
  vendor cross-vendor pattern** (Kimi + MiniMax + MiMo + DeepSeek
  pro/flash) — strictly stronger than any single-anecdote story.
  Scoped honestly to the LC_VCO_20GHz workload (with an optional
  OpAmp addendum if D6-D7 time permits).

**What dropped vs. v2:** the "we are FIRST on NDA" framing. SP1 is
now stated as "verifiable + multi-layer" only.

---

## Checkpoint list (user-locked, 11 mandatory)

| #  | Vendor    | Country | Checkpoint        | Reasoning model? | Tier / notes                           |
|----|-----------|---------|-------------------|------------------|----------------------------------------|
| 1  | Anthropic | US      | Opus 4.7          | yes              | US frontier                            |
| 2  | Anthropic | US      | Sonnet 4.6        | yes              | mid-tier (intra-vendor cost ladder)    |
| 3  | Anthropic | US      | Haiku 4.5         | yes              | low-tier (intra-vendor cost floor)     |
| 4  | OpenAI    | US      | GPT-5.5           | yes              | US frontier                            |
| 5  | OpenAI    | US      | GPT-5.4-mini      | yes              | cost-optimized                         |
| 6  | Google    | US      | Gemini 2.x        | yes              | US frontier (exact id verified by mlcad_runner) |
| 7  | Moonshot  | China   | Kimi K2.5         | yes              | China frontier; `reasoning_content`    |
| 8  | MiniMax   | China   | MiniMax M2.7      | yes              | China frontier; `reasoning_content`    |
| 9  | Xiaomi    | China   | MiMo v2.5-pro     | TBD              | China frontier; endpoint TBD by mlcad_runner |
| 10 | DeepSeek  | China   | DeepSeek V4-pro   | yes              | China frontier; MoE, 1.6T params       |
| 11 | DeepSeek  | China   | DeepSeek V4-flash | yes              | China cost-optimized; same family as #10 |

**Headline framing:** **"10+1 跨 7 厂商前沿 LLM benchmark"** — 6 US +
5 China across **seven vendor families** (Anthropic, OpenAI, Google,
Moonshot, MiniMax, Xiaomi, DeepSeek). The 6-vs-5 split is the
geopolitical-economic story; the 4-China-frontier-vendor cohort
(Kimi / MiniMax / MiMo / DeepSeek-pro+flash) makes SP4 a cross-
vendor pattern rather than a single anecdote. Anthropic-3-tier + 1
OpenAI-mini + 1 DeepSeek-flash give intra-vendor cost-ladder points.

**Open questions to resolve before §4 numbers fill:** (see the
authoritative Open-issues list at end-of-file; the §4.3 checkpoint
table already encodes the resolved reasoning-channel shapes per
mlcad_runner D2/D3-pre verification — MiMo and DeepSeek both
implement `reasoning_content`; Anthropic / Gemini reasoning-token
accounting is unavailable on the current `_normalize_usage` schema
and flagged for the §8.2 schema-extension future-work item).
- **MiMo v2.5-pro** — live-API smoke verification pending
  (`mlcad_d1_d11.md` D5).
- **Gemini 2.x checkpoint id** — mlcad_runner verifying which 2.x
  variant is latest at submission time.
- **DeepSeek V4-pro / V4-flash** — `reasoning_content` channel
  implemented in client; remaining work is live smoke + cost-floor
  pricing-accounting verification in the appendix.

---

## Page budget (6.0 pages, 0.5p buffer)

| § | Title                            | Pages | Headline figure/table       |
|---|----------------------------------|-------|-----------------------------|
| 1 | Introduction                     | 0.50  | —                           |
| 2 | Related Work                     | 0.50  | Table 1 (multi-LLM matrix)  |
| 3 | Platform: Safe-Bridge            | 0.75  | Fig. 1 (arch)               |
| 4 | Benchmark Methodology            | 1.00  | Table 2 (metric defs)       |
| 5 | Results: Cost-Quality Pareto     | 1.50  | Fig. 2 (Pareto) + Table 3 (per-LLM data) |
| 6 | Failure-Mode Taxonomy            | 0.50  | Table 4 (bucket × LLM)      |
| 7 | Discussion: Domestic-China viability + Take-aways | 0.50 | —          |
| 8 | Conclusion + Future Work         | 0.25  | —                           |
|   | **Total**                        | **5.50** | (refs + appendix not counted) |

The 0.5p buffer is intentional — pivot adds risk on §5 (figure
density), and reviewers usually want one extra paragraph on threat-
to-validity in a benchmark paper. Buffer absorbs that.

---

## §1 Introduction — 0.5 page (~300 words)

- **Gap statement (sharpened post-pivot).** Existing LLM-analog
  systems demonstrate viability on *one* LLM at a time, on *open*
  PDKs. Two questions remain unanswered for industrial deployment:
  **(i) which LLM should you pick under a token budget?** —
  no published study compares >2 frontier checkpoints on the same
  spec under identical conditions. **(ii) Is domestic-China LLM
  inference industrially viable for analog sizing?** — to our
  knowledge, zero published work evaluates Kimi / MiniMax / Xiaomi
  MiMo / DeepSeek on this workload.
- **Concurrent work.** AnalogAgent [Bao et al., 2026] addresses the
  NDA constraint with on-prem tiny models (Qwen3 1.7B-14B); we
  address the *deployment-economics* constraint with an 11-
  checkpoint cost-quality Pareto on an industrial PDK. Complementary.
- **Contributions table (SP1-SP4).** Mirror the SP layout above:
  - **SP1** Verifiable NDA-safe substrate (Safe-Bridge, §3): four
    scrub layers + five invariants + audit methodology — *enables*
    SP2.
  - **SP2 (headline)** **First eleven-LLM industrial-node analog-
    sizing benchmark** (§4-§5): 6 US + 5 China across seven vendor
    families.
  - **SP3** Cost-quality Pareto with reasoning-token premium broken
    out (§5).
  - **SP4** Failure-mode taxonomy + **cross-vendor (4 China-
    frontier) domestic-LLM viability** (§6-§7).
- **Roadmap.** §2 related; §3 Safe-Bridge platform; §4 methodology;
  §5 Pareto; §6 failures; §7 discussion; §8 conclusion.

## §2 Related Work — 0.5 page (~300 words + Table 1)

- **Existing LLM-analog systems** group into three clusters: designer
  (AnalogCoder [AAAI 2025], AnalogCoder-Pro [2025]); optimizer/sizer
  (ADO-LLM [ICCAD 2024], EEsizer [NEWCAS 2025], AnaFlow [ICCAD 2025],
  LEDRO [ICLAD 2025], AutoSizer [arXiv 2602.02849, 2026], AnalogSAGE
  [arXiv 2512.22435, 2025]); benchmark (AnalogGym [ICCAD 2024]).
- **AnalogAgent [Bao et al., arXiv 2603.23910, 2026]** is the only
  prior work to address NDA; positioned as complementary (on-prem
  tiny vs cloud-LLM-with-filter).
- **Differentiation — re-cast around SP2 (Table 1).** Six columns:
  *# checkpoints evaluated* / *China-frontier-vendor count* /
  *cost-quality Pareto* / *industrial PDK* / *failure taxonomy* /
  *NDA-safe substrate verifiable*. Per the lit scan, no competitor
  evaluates ≥4 checkpoints on the same task; **none evaluates any
  China-domestic frontier LLM**. Our row reports **11 checkpoints,
  4 China-frontier vendors, all columns ✓**.

## §3 Platform: Safe-Bridge — 0.75 page

- **Figure 1.** Safe-Bridge architecture (existing SVG at
  `paper/figs/safe_bridge_arch.svg` reused — no redraw needed).
- **Four scrub layers** (numbered 1-4 in Fig. 1): Python whitelist
  (`allowed_params` + regex + 20-name SKILL entrypoint allow-list),
  return-path scrubber (`_FOUNDRY_LEAK_RE` + path scrub + op-point
  key whitelist), SKILL-side re-validation, reasoning-trace scrub
  (history-replay + debug-log sinks).
- **Threat model summary (1 paragraph)** — five hard invariants
  (existing `sec4_safe_bridge_threat_model.md` §4.4 prose), summarised.
- **Why this matters for SP2.** Without Safe-Bridge, the benchmark
  could not run on an industrial PDK — every LLM-bound byte would
  egress foundry content. Safe-Bridge is the substrate, not the
  headline.
- **Table 2 from v2 §4.6 (Tier × status)** — relocated here as a
  compressed reference table; full attack-tier prose moves to
  appendix.

## §4 Benchmark Methodology — 1.0 page

- **Workload.** **One industrial-node circuit**: LC_VCO_20G (20 GHz
  LC oscillator on a real foundry PDK). Boilerplate language for §1
  / §4 / abstract: *"we evaluate on LC_VCO_20GHz; an additional
  OpAmp testbench will be reported in revision/appendix if available
  before submission deadline"* — leaves the hook without committing.
  Single circuit is the honest scope of the D8 deliverable; the
  cobi_matching second case study is **dropped per user D1-evening
  pivot** (sizing-style sweeps only, no Ising/matching workload).
- **Spec contract.** YAML metric contract (signals / windows /
  metrics) — same across all 11 LLMs. Concrete metrics per the live
  spec (`projects/lc_vco_base/constraints/spec.md:61-88`):
  `f_osc_GHz`, `V_diff_pp_V`, `V_cm_V`, `duty_cycle_pct`,
  `amp_hold_ratio`, `t_startup_ns`, `I_core_uA`. Pass = all metrics
  in spec range. (K_VCO / phase-noise / power are not in the v3
  evaluator support set — see §4.2.)
- **Protocol.** For each (LLM, run-seed) tuple: same Safe-Bridge
  config, same prompt, same JSON contract; only `--llm` differs.
  Each run: up to N iterations or until first-pass / stuck-streak
  abort.
- **Repetition + variance.** ≥3 runs per LLM (more for the noisy
  LLMs identified in pilot). Report median + IQR, not just best.
- **Metric definitions (Table 2).**
  - **Success rate**: fraction of runs reaching all-metric-pass.
  - **Iter-to-converge**: iteration count of first pass (∞ if never).
  - **Token cost**: input + output + **reasoning** tokens summed
    per run, separately for input/output/reasoning so reasoning
    premium is visible.
  - **USD cost per run**: tokens × public pricing as of 2026-05-01;
    pricing table in appendix.
  - **Wall-clock per iteration**: median over completed iterations.
- **Threats to validity.** Single circuit; single spec; pricing
  drift; reasoning-token accounting differences per vendor; rate-
  limit incidents inflating wall-clock. Each gets a sentence in §4.

## §5 Results: Cost-Quality Pareto — 1.5 pages

- **Figure 2 (HEADLINE FIGURE).** Cost-quality Pareto scatter:
  x-axis USD/run (log), y-axis success rate. One point per (LLM,
  run-seed) — 11 LLMs × ≥3 seeds; per-LLM median highlighted;
  Pareto frontier in bold; annotate the China-frontier-4 (Kimi /
  MiniMax / MiMo / DeepSeek-pro) + US-frontier-3 (Opus 4.7 /
  GPT-5.5 / Gemini) by name. Marker shape encodes country (US ○ /
  China ●); colour encodes vendor family.
- **Table 3.** Per-LLM data — 11 rows × columns: success rate
  (median, IQR), iter-to-converge median, input-tokens median,
  output-tokens median, reasoning-tokens median, USD/run median.
- **Findings (filled at D8 from real data — placeholder narrative).**
  Sketch only; no fabricated numbers in skeleton:
  - US frontier (Opus 4.7 / GPT-5.5 / Gemini): top success rate,
    highest cost.
  - Anthropic intra-vendor cost ladder (Opus / Sonnet / Haiku).
  - Kimi K2.5 / MiniMax M2.7 / DeepSeek V4-pro vs Opus 4.7:
    spec-pass within ε% at ~F× lower cost (TBD-from-D6).
  - DeepSeek V4-flash vs MiMo v2.5-pro vs GPT-5.4-mini: cost-floor
    comparison.
  - MiMo v2.5-pro: viability point — does it pass at all?
- **Reasoning-token premium subsection (SP3 sharp evidence).** Vendors
  that hide reasoning tokens in completion-token bills systematically
  under-report cost. We quantify: reasoning tokens contribute Z-W%
  of per-iter spend on Kimi/MiniMax/MiMo (and the Anthropic
  thinking-token variant). This is the one numerically *sharp* item
  the cost-Pareto headline carries — call it out as a labeled
  subsection, not a footnote.
- **Operating-point recommendations.** "If your budget is $X/run,
  pick L; if you need ≥Y% success, pick L'."

## §6 Failure-Mode Taxonomy — 0.5 page

- **Reframe (post-pivot).** Not "2 case studies" anymore. This is an
  **11 LLM × 1 failure-probe spec bucket matrix** (~55 runs total,
  11 LLMs × ≥5 repeats; mlcad_runner D8 deliverable). One probe spec
  exercises the LC_VCO_20G sizing loop deliberately near saturation
  / contract-violation boundaries so failures surface across LLMs
  comparably.
- **8 buckets** (refined from v2 §5 list; LC_VCO only — cobi_matching
  Case B fully dropped per user pivot):
  - empty-diff loop
  - contract-violation repair termination
  - dump-status `UNMEASURABLE` saturation
  - topology-induced sanity-range FAIL
  - PDK-content refusal (red-team probe — orthogonal to the
    benchmark but cross-link to §3)
  - reasoning-content replay (closed by patch; cross-link)
  - rate-limit / 429 cascade
- **Table 4.** Bucket × LLM frequency matrix (rows = buckets above,
  cols = 11 checkpoints). Surfaces per-vendor failure-distribution
  differences (e.g. mini/haiku rate-limit cascades vs Opus saturation
  vs Kimi reasoning-replay vs DeepSeek-flash MoE-routing failures).
- **One cross-link.** Pre-patch vs post-patch Kimi/MiniMax/Ollama
  reasoning-replay rates from v2 §5 (cite the rework).

## §7 Discussion: Domestic-China Viability + Take-aways — 0.5 page

- **The viability question (cross-vendor, post-pivot).** Can you ship
  an industrial analog sizing flow that depends on China-domestic
  LLMs? With **four China-frontier vendors** (Kimi K2.5, MiniMax
  M2.7, MiMo v2.5-pro, DeepSeek V4-pro/flash) we answer this as a
  *cross-vendor pattern*, not a single-LLM anecdote. Three paragraphs
  based on §5 data:
  - **Quality.** Within ε% on success rate vs US frontier (Opus 4.7
    / GPT-5.5 / Gemini)? Is the pattern consistent across the 4
    China vendors, or vendor-specific?
  - **Cost.** Material savings vs US frontier? Where does the
    Pareto frontier sit?
  - **Operational.** Rate-limit / latency / reasoning-token
    accounting reliability — per-vendor, with attention to the
    DeepSeek MoE routing and MiMo new-entrant operational maturity.
- **One-paragraph take-away per audience.**
  - For practitioners: cost-quality recommendations + China-domestic
    deployment story (cross-vendor confident).
  - For researchers: methodology (multi-LLM benchmark on industrial
    PDK is the reusable contribution).

## §8 Conclusion + Future Work — 0.25 page

- Recap SP2 headline + numbers.
- Future: multi-circuit expansion (the hand-built OpAmp testbench
  if it doesn't land in revision; comparator + reference); Gemini
  family addition; tighter ABNF for `design_vars`; on-prem inference
  for Tier-3 first-emission closure (Ollama served-locally path).

---

## Figures + tables budget (post-pivot)

| # | What                                  | Source                                                                 | § |
|---|---------------------------------------|------------------------------------------------------------------------|---|
| Fig. 1 | Safe-Bridge architecture           | `paper/figs/safe_bridge_arch.svg` (reused, no redraw)                  | §3 |
| Fig. 2 | **HEADLINE — Cost-Quality Pareto** | `paper/data/extracted_logs.csv` (mlcad_runner D6-D8)                   | §5 |
| Table 1 | Differentiation (multi-LLM-cast)  | hand-built in §2 (revamp of v2 Table 1)                                | §2 |
| Table 2 | Metric definitions + tier summary | mixed: methodology + sec4 §4.6 compressed                              | §3-§4 |
| Table 3 | Per-LLM benchmark data            | mlcad_runner D6-D8 + transcript JSONL usage block                      | §5 |
| Table 4 | Failure-mode bucket × LLM         | mlcad_runner D3-D8                                                     | §6 |

---

## Migration plan v2 → v3 — execution status

| v2 artifact                              | v3 disposition                                                                                                        | Status (2026-05-12) |
|------------------------------------------|------------------------------------------------------------------------------------------------------------------------|---------------------|
| `outline_v2.md`                          | Superseded by `outline_v3.md` (this file). v2 retained as archive for diff reference.                                  | **done** |
| `sec1_intro.md` (~450 w, SP1-headline)   | Rewritten as `sec1_intro_v3.md` (~300 w, SP2-headline + Bao positioning + SP1-4 contribution list).                    | **done — Groups 1+2 fixed; re-review requested** |
| `sec2_related_v2.md` (~560 w)            | Rewritten as `sec2_related_v3.md` (~310 w + recast Table 1 with `# checkpoints` / `China-frontier count` axes).        | **done — Groups 1+2 fixed; re-review requested** |
| `sec4_safe_bridge_threat_model.md`       | Compressed to `sec3_platform.md` (~445 w, 0.75p): four scrub layers + five invariants summary + Tier table + Fig 1. Tier-by-tier full prose moves to supplementary appendix. | **done — Groups 1+2 + Group 3+4 R1 fixes applied** |
| `paper/figs/safe_bridge_arch.svg`        | **Reused unchanged** — same diagram, caption shifts away from "first NDA-safe" framing to "verifiable substrate."     | **done (no edit)**  |
| Bao et al. AnalogAgent paragraph         | **Reused verbatim** in `sec2_related_v3.md`.                                                                          | **done** |
| Three-axis regression-test methodology   | Moved to `sec4_methodology.md` §4.6 ("how we audit scrub coverage").                                                  | **done — anchors updated for e750189c** |
| §4 Methodology                            | Authored as `sec4_methodology.md` (~600 w, 1.0p): workload + spec (live-spec metrics) + protocol + seed=label + repetition + 3-axis audit + Table 2 metric defs + 8 threats-to-validity. | **done — Groups 3+4 R1 fixes applied** |
| §5 Results                                | Authored as `sec5_results.md` (~750 w skeleton, 1.5p): TL;DR + Fig 2 spec + Table 3 (11 rows × 7 cols, **all TBD-from-D6**) + Pareto findings + reasoning-token premium + operating-points. | **done — Groups 3 R1 fixes applied; re-review requested** |
| §6 Failure-Mode Taxonomy                 | Authored as `sec6_failures.md` (~310 w, 0.5p): 11 × 1-spec bucket matrix; 8 buckets (incl. wandering); Table 4 (8×11, **all TBD-from-D8**); pre-registered hypotheses. | **done — Groups 3 R1 fixes applied; re-review requested** |
| §7 Discussion                            | Authored as `sec7_discussion.md` (~310 w, 0.5p): cross-vendor viability frame + 3 paragraphs (quality / cost-pinned-to-pricing-snapshot / operational) + 2 take-aways. | **done — Group 4 R1 fixes applied; re-review requested** |
| §8 Conclusion + Future                   | Authored as `sec8_conclusion.md` (~180 w, 0.25p): recap + 5 future items (multi-circuit, multi-PDK, Anthropic/Gemini schema extension, Tier-3 closure, ABNF tightening). | **done — Group 4 R1 fixes applied; re-review requested** |
| Case B failure case study (cobi_matching / Ising) | **REMOVED per user D1-evening pivot.** No references to cobi/matching survive v3; §6 is LC_VCO-only.            | **done** |

## Open issues (post-pivot)

1. ~~User D2 morning ruling~~ — **RESOLVED 2026-05-12: v3 adopted,
   benchmark+platform, 11 checkpoints, Gemini+DeepSeek mandatory.**
2. **mlcad_runner client/circuit enum still in flight.** SP2 numbers
   depend on this; v3 §4-§5 are skeleton until D6-D8 runs land.
3. **MiMo v2.5-pro endpoint shape** — client implementation assumes
   OpenAI-compatible shape with `reasoning_content` fallback
   (`src/llm_client.py:628-636`); live-API smoke verification by
   mlcad_runner D5 pending.
4. **Gemini exact checkpoint id** — mlcad_runner verifying which 2.x
   variant is the latest at submission time.
5. **DeepSeek V4-pro / V4-flash reasoning-token accounting** —
   client implements `reasoning_content` per
   `src/llm_client.py:659-662, 734-742`; remaining work is live
   smoke + cost-floor pricing accounting in the appendix.
6. **Single-circuit honesty.** §4 must state the LC_VCO_20G-only
   limitation up front; reviewers will otherwise call it out.
7. **Threat-to-validity paragraph in §4.** Reviewers expect a
   benchmark paper to be candid about pricing-drift, rate-limit
   noise, reasoning-token-accounting per-vendor differences.

## v2 baseline files — archived for diff reference

Following the D2 ruling these v2 files are **preserved unchanged**
as archive copies so reviewers (and future-us) can diff the v2 SP1-
headline framing against the v3 SP2-headline rewrite:

- `paper/outline_v2.md`
- `paper/sec1_intro.md`
- `paper/sec2_related_v2.md`
- `paper/sec4_safe_bridge_threat_model.md`
- `paper/figs/safe_bridge_arch.svg` (re-used unchanged, *not* archived)

Other state unchanged:

- Task `e750189c` (completed; working tree staged for user commit).
- Task `ddda51de` (Kimi smoke followup; mlcad_runner owner).
- The Safe-Bridge code itself.
