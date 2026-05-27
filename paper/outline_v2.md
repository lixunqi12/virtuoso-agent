# MLCAD 2026 — Paper Outline (v2, 6-page ACM)

**Working title:** *Safe-Bridge: NDA-Compliant Closed-Loop LLM Agents for
Industrial Analog Circuit Design*

**Venue:** MLCAD 2026 (Workshop on Machine Learning for CAD).
**Format (locked 2026-05-12):** **6 pages, double-column ACM** (acmart
sigconf, US Letter, 9-10 pt). References + appendix do not count
against the page limit. **Double-blind** — strip author block from
camera-ready until acceptance.
**Deadline:** abstract reg 2026-05-16, full paper 2026-05-23.
**Paper class:** system paper.

**Four selling points (referenced as SP1–SP4 below):**

- **SP1.** **Industrial-grade NDA-safe closed-loop** for analog
  sizing against a real foundry PDK — via a Safe-Bridge architecture
  whose foundry-content non-egress is enforced by **four verifiable
  scrub layers** (Python whitelist, return-path scrubber, SKILL
  whitelist, reasoning-trace scrub) and audited by code-line-anchored
  invariants in §4. Concurrent AnalogAgent [2026] addresses the same
  NDA constraint via the orthogonal on-prem-tiny-model path; our
  contribution is the *verifiable filter* path that admits any cloud
  LLM, including state-of-the-art reasoning models.
- **SP2.** Failure-mode taxonomy across ~N runs (4 projects × M LLMs ×
  K iterations) — we report what breaks, not only what works.
- **SP3.** Cost Pareto — per-iteration token cost vs. spec-pass
  accuracy curves, with a per-LLM operating-point recommendation.
- **SP4.** Open-LLM / domestic-China LLM evaluation — Kimi K2.5,
  MiniMax M2.7 as industrially-deployable alternatives to Claude / GPT
  / Gemini, including reasoning-token accounting. **Unique to us**
  per the verification scan: no current competitor evaluates a
  China-domestic LLM.

---

## Page budget (6 pages, references not counted)

| § | Title                              | Pages |
|---|------------------------------------|-------|
| 1 | Introduction                       | 0.75  |
| 2 | Related Work (incl. Table 1)       | 0.75  |
| 3 | System Architecture (incl. Fig. 1) | 1.00  |
| 4 | Safe Bridge & NDA (incl. Table 2)  | 1.25  |
| 5 | Failure-Mode Taxonomy (incl. Table 3) | 1.00 |
| 6 | Cost Pareto (incl. Fig. 2)         | 0.50  |
| 7 | Open-LLM Eval (incl. Fig. 3)       | 0.50  |
| 8 | Conclusion + Future Work           | 0.25  |
|   | **Total**                          | **6.00** |

Note: §6 + §7 collapsed from 0.75 → 0.50 each. If Cost-Pareto figure
needs more space, §7 merges into a single subsection of §6 ("Open-LLM
operating points") and §7 disappears as a standalone heading.

---

## §1 Introduction — 0.75 page (~450 words)

- LLM agents for analog design are an emerging class of tool (must-cite
  set in §2). Existing demos run on **open PDKs** (Skywater 130 /
  FreePDK 45 / PTM-MG predictive) and report optimistic single-corner
  success rates.
- **Gap.** Industrial deployment is gated by two constraints the
  open-PDK literature does not address: (i) foundry NDAs forbid
  shipping model cards, cell topologies, or process strings to
  commercial LLM APIs; (ii) the failure-mode distribution under real
  PDKs is unknown — published wins do not show the silent breakages.
- **Concurrent work.** AnalogAgent [2026] takes the orthogonal path
  (tiny on-prem Qwen3 1.7B-14B models, no cloud LLM); we take the
  *Safe-Bridge filter* path (any cloud LLM, foundry content never
  crosses the boundary). One sentence of positioning in §1.
- **Contributions.** State SP1–SP4 explicitly. Pre-announce §3 system
  design, §4 NDA defense layer, §5 failures, §6 cost, §7 open-LLM.
- **Threat-model scope.** One-sentence forward-pointer to §4: PDK
  content stays inside Cadence; only scrubbed metric values cross to
  the LLM. Out-of-scope: insider exfiltration, supply-chain.

## §2 Related Work — 0.75 page (~550 words + Table 1)

- **Cluster A. LLM-as-designer** (generates netlists from spec):
  AnalogCoder [AAAI 2025], AnalogCoder-Pro [arXiv 2508.02518, 2025].
  → Complementary: we *optimize* a foundry-bound schematic, not
  generate from open-PDK abstractions.
- **Cluster B. LLM-as-optimizer / sizer + agentic loops**
  (closest cluster): ADO-LLM [ICCAD 2024], EEsizer [arXiv 2509.25510 /
  NEWCAS 2025], AnaFlow [ICCAD 2025], LEDRO [ICLAD 2025], plus 2025–
  2026 concurrent SOTA AutoSizer [arXiv 2602.02849] and AnalogSAGE
  [arXiv 2512.22435]. → Closest to our setup; differentiators are
  SP1 industrial PDK + Safe-Bridge + SP2 fail taxonomy + SP4
  domestic-China LLM eval. **AnalogAgent [arXiv 2603.23910, 2026]**
  gets its own paragraph as the only paper contesting SP1: their NDA
  story is on-prem-tiny-models, ours is cloud-LLM-plus-filter.
- **Cluster C. Benchmark / eval infrastructure**: AnalogGym [ICCAD
  2024]. → AnalogGym-style metric contracts; binding to a real PDK is
  what's new. **No v2 successor exists** per the lit scan (drop the
  placeholder).
- **Cluster D. Adjacent EDA agent work (digital / RTL)**: ChatEDA,
  ChipNeMo. → Cited briefly for closed-loop agent design patterns;
  trim to a single sentence if §2 word count is tight.
- **Differentiation table** (Table 1, 6 columns: Industrial PDK /
  NDA defense / Failure-mode taxonomy / Per-iter cost / Open-LLM eval
  including domestic-China / Real-foundry sim). Per the lit scan, our
  row is the only all-✓ row; competitor cells get nuanced ✗ /
  *partial* per the verification report.

## §3 System Architecture — 1.0 page

- **Figure 1:** Safe-Bridge architecture diagram. Hand-drawn SVG at
  `paper/figs/safe_bridge_arch.svg` (camera-ready render); Mermaid
  source + reading-guide caption at
  `paper/figures/safe_bridge_arch.md` (kept as the LaTeX/Markdown
  description that ships alongside the SVG). Eight nodes: LLM ↔ Agent
  ↔ Python whitelist ↔ Scrubber ↔ RAMIC ↔ SKILL whitelist ↔
  Cadence DB ↔ PDK. Dashed-green trust boundary; PDK + LLM outside.
- **Components in 1 paragraph each:** CircuitAgent loop
  (`src/agent.py`), SpecEvaluator (Markdown YAML contract), OCEAN
  worker (subprocess kill on timeout), HSpice backend (SSH + `.mt`
  ingest), LLM client abstraction (Claude / Gemini / Kimi / MiniMax
  / Ollama under one interface), transcript JSONL for cost +
  reproducibility audit.
- **Iteration protocol:** the LLM's JSON schema (design_vars /
  measurements / pass_fail / reasoning), how repair-prompts handle
  contract violations, the empty-diff guard, the stuck-streak abort.
- **What the LLM never sees:** SKILL expression strings,
  raw foundry cell names, absolute filesystem paths, model card
  text, op-point keys outside the whitelist
  (`safe_bridge.py:41-56`).

## §4 Safe Bridge & NDA Compliance — 1.25 pages

- **Threat model recap.** Curious-but-passive LLM provider as the
  primary actor (precise framing in
  `paper/sec4_safe_bridge_threat_model.md`). Insider, direct PDK
  theft, MITM on localhost: explicitly out of scope. State the five
  hard invariants (see `sec4_safe_bridge_threat_model.md` §4.4).
- **Defense layers (numbered crossings 1–6 from Figure 1):** Python
  whitelist (`allowed_params` per project + 17-name SKILL
  entrypoint allow-list); return-path scrub (`_FOUNDRY_LEAK_RE`,
  abs/UNC path scrub, op-point key whitelist); SKILL-side
  re-validation; reasoning-content scrub (closes the Kimi K2.5 /
  MiniMax / Ollama replay + debug-log gap — patch (i) + Ollama
  debug-log rework, both gated on dual re-review).
- **Red-team eval (probe results from D1–D3, owned by mlcad_runner):**
  *X / Y* probes successfully exfiltrated PDK tokens
  → *0 / Y* after the closing patches, *X' / Y* abort or refuse
  → discuss the *X'* refusals as the desired failure mode.
- **Honest limitations.** Cost of the defense: ~M ms per round trip;
  hard-rejects ~Z% of legitimate metric reads that have
  PDK-overlapping names — call out the false-positive trade-off.

## §5 Failure-Mode Taxonomy — 1.0 page

- **Data source.** Transcripts from 4 projects × M LLMs; aggregated
  by `paper/scripts/extract_transcript_logs.py` into
  `paper/data/extracted_logs.csv` (T3 deliverable from mlcad_runner).
- **5–7 failure-mode buckets** (filled from mlcad_runner D3 runs):
  - empty-diff loop (LLM stuck on identical design vars)
  - contract-violation repair-loop terminations
  - dump-status `UNMEASURABLE` saturation (spec / sim chain wrong, not
    circuit)
  - topology-induced sanity-range FAILs (LLM converges on a
    physically implausible point)
  - PDK-content refusal correctness (red-team probes)
  - reasoning_content re-leak via replay + debug-log (closed by
    patch (i) + Ollama debug-log rework)
  - rate-limit / 429 cascade
- **Frequency + impact table (Table 3)** (1 row per bucket × per LLM).
- **Tier-3 finding cross-link to §4.** Pre-patch, ~X% of Kimi K2.5
  runs replayed reasoning_content verbatim; post-patch, 0%.

## §6 Cost Pareto — 0.5 page

- **Axes.** Per-iteration token cost (input + output + reasoning,
  USD-normalized) on x-axis; aggregated spec-pass-rate on y-axis.
- **Sources.** `extract_transcript_logs.py` (D1-2 T3) plus the new
  `usage` block in JSONL transcripts (post-D3-am patch).
- **Scatter (Fig. 2):** one point per (project, LLM, run) tuple.
  Pareto frontier highlighted.
- **Operating-point recommendation.** "If you have $X / run budget,
  pick LLM L. If you need spec-pass-rate > Y%, pick LLM L'."
- **Reasoning-token premium.** Kimi K2.5 / MiniMax M2.7: reasoning
  tokens contribute ~Z% of the per-iter cost; under-reported by
  pricing models that quote completion tokens alone.

## §7 Open-LLM Evaluation — 0.5 page

- **Why this section exists (1 sentence).** Industrial deployment
  cares about whether you can run the loop on an open-weights or
  domestic-China LLM. We compare Claude (commercial baseline), Kimi
  K2.5 (Moonshot, China), MiniMax M2.7 (China), Gemini (commercial
  alt), Ollama (local-only fallback).
- **Setup parity.** Same Safe-Bridge / spec / prompt; only the LLM
  client differs (`--llm` flag at `src/llm_client.py`).
- **Three findings (compressed into 1 paragraph + Fig. 3 bars):**
  Kimi K2.5 vs Claude within ε% spec-pass at ~F× cost; MiniMax M2.7
  reasoning-content reliance (confirms Tier-3 replay was real
  pre-patch); Gemini/Ollama outliers as one-sentence asides.

## §8 Conclusion + Future Work — 0.25 page

- Restate SP1–SP4 with measured numbers.
- Safe-Bridge generalizes: any closed-loop EDA agent hitting industrial
  tooling can adopt the same trust-boundary topology.
- **Future work.** Tier 4 timing side-channel hardening; multi-PDK
  abstraction; tighter ABNF for `design_vars`; Verilog-A / DSPF
  extension to Safe-Bridge scope; on-prem inference (Ollama path) to
  close the Tier-3 first-emission leg.

---

## Figures + tables budget

| # | What | Source | §  |
|---|------|--------|----|
| Fig. 1 | Safe-Bridge architecture | `paper/figs/safe_bridge_arch.svg` (+ Mermaid source at `paper/figures/safe_bridge_arch.md`) | §3 |
| Fig. 2 | Cost-Pareto scatter | `paper/data/extracted_logs.csv` (TBD) | §6 |
| Fig. 3 | Per-LLM spec-pass-rate bars | same source | §7 |
| Table 1 | Differentiation matrix | hand-built in §2 | §2 |
| Table 2 | Threat-model summary (4 tiers × defenses × status) | from `paper/sec4_safe_bridge_threat_model.md` §4.6 | §4 |
| Table 3 | Failure-mode bucket × LLM frequencies | from D3 runs (mlcad_runner) | §5 |

---

## Open issues / dependencies

1. **§2 lit scan — RESOLVED 2026-05-12 evening.** mlcad_runner (the
   renamed `lc_vco_researcher`) delivered a tightened verification
   scan: 2 author/venue corrections (EEsizer → Liu et al., 2025;
   AnalogGym → Li et al., ICCAD 2024) + 1 venue confirmation
   (AnalogCoder → AAAI 2025) + 3 new must-cites
   (AnalogAgent 2026, AutoSizer 2026, AnalogSAGE 2025) + cell flips
   in Table 1 (mostly ✗ → *partial* for SP3/SP4 on competitors;
   AnaFlow gains a *partial* SP2). AnaFlow + LEDRO confirmed as real
   peer-reviewed cites, not informal. Drop the "AnalogGym v2"
   placeholder — no successor exists.

2. **§5 + §6 + §7 numbers** — depend on D3–D8 runs. Outline lands the
   structure; numbers fill at D8 draft consolidation. T3 cost-
   extraction owned by mlcad_runner (task `ee4396a6`).

3. **§3 / §4 split — resolved.** File moved to
   `paper/sec4_safe_bridge_threat_model.md` with §3.x → §4.x labels.
   See item 5 below for the Tier-3 status caveat.

4. **Format — RESOLVED 2026-05-12.** **6-page double-column ACM**
   (acmart sigconf, US Letter, 9-10 pt), references + appendix not
   counted. **Double-blind.** Previous 8-page IEEE assumption was a
   misread of the CFP; correction confirmed by Claude Code. Page
   budget rebalanced above.

5. **§4 Table 2 Tier 3 status — RESOLVED 2026-05-12** (R2 dual
   APPROVE: `claude_reviewer_v2` task `e38b2c5d` +
   `codex_reviewer_v2` task `2d15ccd5`). Bundled patch (i) +
   Ollama-debug-log rework closes both the history-replay sink
   (`llm_client.py:335, 427, 497`) and the debug-log sink
   (`llm_client.py:489`). Regression test
   `test_debug_log_scrubs_thinking_pdk_tokens` in
   `tests/test_llm_client.py` (three-axis: records-present /
   forbidden-tokens-absent / scrub-marker-present; 11/11 passing).
   Tier 3 in `sec4_safe_bridge_threat_model.md` §4.6 now
   **enforced**. R2 reviewer observations on type-narrowed live
   paths + multi-axis tests captured as new §4.7 audit-methodology
   subsection.

6. **mlcad_runner rename.** Previously referred to as
   `lc_vco_researcher` in task descriptions (e.g. `9e5a68ad`,
   `ee4396a6`); the agent is now `mlcad_runner`. Cross-references in
   §5 / §6 data-source bullets updated.
