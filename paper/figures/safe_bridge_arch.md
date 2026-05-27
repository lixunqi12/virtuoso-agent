# Figure 1 — Safe-Bridge Architecture

Mermaid source for paper Figure 1. Renders directly on GitHub; final
PDF/PNG export will be regenerated with `mermaid-cli` (`mmdc -i
safe_bridge_arch.mmd -o safe_bridge_arch.pdf -w 1200`) during the
camera-ready pass.

The diagram has eight nodes plus a trust-boundary line. The
trust-boundary cuts between the LLM provider (untrusted, top) and
everything downstream; the PDK sits intentionally **outside** that
trusted region too — it is what the trusted region defends.

## Diagram

```mermaid
%%{init: {"flowchart": {"htmlLabels": true, "curve": "linear"}, "themeVariables": {"fontSize": "14px"}}}%%
flowchart TB
    classDef untrusted fill:#fff0f0,stroke:#c33,stroke-width:2px,color:#000
    classDef trusted fill:#f0fff4,stroke:#1a7f37,stroke-width:2px,color:#000
    classDef scrub fill:#fff7d6,stroke:#9a6700,stroke-width:2px,color:#000
    classDef cad fill:#e7f0ff,stroke:#1f4eaa,stroke-width:2px,color:#000

    LLM["<b>LLM provider</b><br/>(Anthropic / OpenAI / Kimi)<br/><i>receives every prompt + tool result</i>"]:::untrusted

    AGENT["<b>Agent</b> (<code>src/agent.py</code>)<br/>Python optimisation loop"]:::trusted

    PYWL["<b>Python whitelist layer</b><br/><code>safe_bridge.py</code><br/>• <code>allowed_params</code> + regex (L2300-2346)<br/>• <code>_NAME_RE</code> (L34) on every lib/cell<br/>• <code>_ALLOWED_SKILL_ENTRYPOINTS</code> (L125-176)<br/>• Inline-upload contained to <code>skill/</code> (L2021-2024)"]:::trusted

    SCRUB["<b>Return-path scrubber</b><br/><code>_scrub</code> (L255-277)<br/>• <code>_FOUNDRY_LEAK_RE</code> → <code>&lt;redacted&gt;</code> (L80-83)<br/>• abs/UNC paths → <code>&lt;path&gt;</code> (L84-95)<br/>• <code>_strip_model_info</code> (L2282-2291)<br/>• Op-point key whitelist (L41-56)"]:::scrub

    RAMIC["<b>RAMIC TCP bridge</b><br/>localhost:65061<br/>(<code>virtuoso-bridge-lite</code>)"]:::trusted

    SKILLWL["<b>SKILL whitelist layer</b><br/><code>skill/safe*.il</code> wrappers<br/>• <code>safeReadSchematic[Deep]</code><br/>• <code>safeReadOpPoint[AfterTran]</code><br/>• <code>safeOceanRun</code> / <code>safeOceanDumpAll</code> / <code>safeOceanTCross</code><br/>• <code>safeMaeWriteAndSave</code> / <code>safeGenerateSpecScaffold</code><br/>• <code>safeSetParam</code> / <code>safePatchNetlistIC</code>"]:::trusted

    CDB["<b>Cadence DB + Maestro</b><br/>(Virtuoso IC23.1 session)"]:::cad

    PDK["<b>Foundry PDK</b><br/>BSIM model cards<br/>process stack<br/>proprietary cell library<br/><i>NDA-bound; never traverses the boundary</i>"]:::untrusted

    LLM -- "tool call (JSON)" --> AGENT
    AGENT -- "validated call" --> PYWL
    PYWL -- "scrubbed result" --> SCRUB
    SCRUB -- "redacted JSON" --> AGENT
    AGENT -- "redacted result string" --> LLM

    PYWL -- "allow-listed SKILL expr<br/>(<code>_execute_skill_json</code>)" --> RAMIC
    RAMIC -- "TCP payload" --> SKILLWL
    SKILLWL -- "read-only / scoped writes" --> CDB
    CDB -- "session-scoped query" --> PDK
    PDK -. "model card + cell names<br/>(content stays in Cadence proc)" .-> CDB
    CDB -- "raw result" --> SKILLWL
    SKILLWL -- "filtered JSON<br/>(strips model + foundry names)" --> RAMIC
    RAMIC -- "JSON response" --> PYWL

    %% trust boundary (rendered as a labelled subgraph)
    subgraph TRUST [" "]
        direction TB
        AGENT
        PYWL
        SCRUB
        RAMIC
        SKILLWL
        CDB
    end
    style TRUST fill:none,stroke:#1a7f37,stroke-width:3px,stroke-dasharray:8 4
```

## Reading guide for the paper caption

> **Fig. 1 — Safe-Bridge architecture.** Untrusted nodes (red) bound
> the trust region (dashed green). The LLM provider sees only the
> agent's JSON tool calls and the scrubbed JSON responses; the PDK
> content stays inside the Cadence process and is filtered at two
> levels before any byte crosses back into Python — first by the SKILL
> wrappers (`safeOceanDumpAll` etc., which strip model and foundry
> names before serialising), then by `_scrub` (`safe_bridge.py:255-277`,
> regex-redacts any residual foundry tokens or absolute paths).
> Parameter names proposed by the LLM are validated against the
> per-project `allowed_params` whitelist before any SKILL invocation;
> SKILL expressions themselves are restricted to a 17-name entrypoint
> allow-list. The dashed PDK → Cadence arrow is intentional: the PDK
> content is *referenced* by Cadence during simulation but never
> serialised to the bridge.

## Numbered trust-boundary crossings (for the §3 prose to reference)

1. **LLM → Agent** — JSON tool call. The only thing the LLM is
   permitted to ship into the trust region. Validated by the Python
   whitelist layer before any SKILL call is constructed.
2. **Python WL → RAMIC** — allow-listed SKILL expression as a string.
   Constructed by `_execute_skill_json`
   (`safe_bridge.py:2139-2202`). All identifier and atom arguments
   passed through `_validate_name` / `_format_param_value`.
3. **RAMIC → SKILL WL** — TCP payload to localhost:65061. The wrappers
   (`skill/safe*.il`) re-validate arguments on the Cadence side.
4. **SKILL WL → Cadence DB** — session-scoped query. No file IO
   outside the Maestro / OCEAN working dirs; no `hiOpenLib` exposed.
5. **Cadence ↔ PDK** — internal to the Cadence process. Confidential
   PDK bytes are loaded into the simulator's memory but never
   serialised back through RAMIC.
6. **SKILL WL → RAMIC → Python WL → Scrub → Agent → LLM** — return
   path. SKILL strips model info and foundry-cell-name keys on
   the Cadence side; `_scrub` redacts anything that slips through on
   the Python side.

## Open issue noted by the threat model (cross-link)

Tier 3 of §4.3 covers the `reasoning_content` re-entry path: when the
LLM provider returns a thinking block alongside its content (Kimi K2.5,
MiniMax M2.7, o-series, etc.), the bridge previously did **not** scrub
it before `agent.py:610-612` re-appended the full response to the
conversation history. As of 2026-05-12 (task `e750189c`, dual-approved
by `claude_reviewer_v2` and `codex_reviewer_v2`), this gap is closed:
`KimiClient.chat`, `MiniMaxClient.chat`, and `OllamaClient.chat` now
return `scrub(reasoning)` / `scrub(thinking)` at
`llm_client.py:335, 427, 497`. The first-emission leg (the bytes
the model sends to its own provider before the agent ever sees them)
remains out of scope per §4.5; that distinction is what the
"replay" vs "first-emission" rows of Table 2 capture.

## Mermaid → PDF render notes (delete before submission)

- Run `mmdc -i safe_bridge_arch.mmd -o safe_bridge_arch.pdf -w 1200 -H 900`
  for the camera-ready PDF.
- For ACM single-column figure width, use `-w 800`.
- The `subgraph TRUST [" "]` trick gives us the dashed-green trust
  boundary without a label; if mermaid-cli renders the empty title
  badly, swap the `" "` for the literal word "trusted" and accept the
  label.
- If reviewers ask for a more conventional security-architecture style
  (data-flow lines crossing a vertical bar), we can swap in a TikZ
  redraw post-acceptance; for the submission deadline the Mermaid
  render is the right cost/benefit point.
