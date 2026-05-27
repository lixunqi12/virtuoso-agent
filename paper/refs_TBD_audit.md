# refs.bib TBD-field Audit (v4 idle-time deliverable, 2026-05-16)

> **Purpose.** Catalog every TBD/missing field in `paper/refs.bib`, by
> entry, so the camera-ready pass can resolve them in one batch
> against canonical DOI / arXiv / PDF sources. **No values invented
> in this pass** — only gap identification.

## Group A — TODAES LLM4EDA anchors (5 entries) — **✓ ALL RESOLVED 2026-05-16**

Leader resolved all 5 via Crossref API and batch-filled the canonical
metadata into `refs.bib`. All fields below are now populated; this
table is retained for camera-ready cross-check.

| cite-key | DOI | author | year | vol/iss/pp | status |
|---|---|---|---|---|---|
| `autochip` | 10.1145/3723876 | Blocklove / Thakur / Tan / Pearce / Garg / Karri | 2025 | 30(6):1--26 | ✓ resolved |
| `hlsrewriter` | 10.1145/3749986 | Xu / Zhang / Yin / Zhuo / Schlichtmann / Li | **2026** | 31(4):1--21 | ✓ resolved |
| `c2hlsc` | 10.1145/3734524 | Collini / Garg / Karri | 2025 | 30(6):1--24 | ✓ resolved |
| `lhs` | 10.1145/3734523 | Reddy / Bhattacharyya / Sarmah / Nongpoh / Maddala / Karfa | 2025 | 30(6):1--27 | ✓ resolved |
| `confibench` | 10.1145/3773087 | Qiu / Zhang / Drechsler / Ho / Schlichtmann / Li | 2025 | Just Accepted, art \textnumero~3773087 | ✓ resolved (Just Accepted) |

**R1 RISK CLOSED.** Canonical titles differ from my inferred placeholders
but **cite-strings in prose ("AutoChip", "HLSRewriter", "C2HLSC", "LHS",
"ConfiBench") remain valid as system-name short forms.** No v4 prose
edits required. Leader's specific note:
- `autochip` canonical title = *"Automatically Improving LLM-based Verilog Generation using EDA Tool Feedback"* — "AutoChip" is the system name, not in the title. Short-cite *"AutoChip~\cite{autochip}"* survives.
- Other 4 have system name at title-start — short-cites trivially survive.

**Bonus observation (leader, 2026-05-16):** 4 of 5 (autochip / c2hlsc / lhs / confibench-pre-finalize) are TODAES vol 30 issue 6 = LLM4EDA special issue. Strengthens C1 positioning ("first analog LLM4EDA in TODAES") because TODAES editorial has demonstrably opened to this line of work.

**bibtex build status (2026-05-16):** 4 of 5 anchors produce **zero warnings**; `confibench` produces the expected "no number and no volume" warning, suppressed by the `note = {Just Accepted, article \textnumero~3773087}` workaround. `\textnumero` resolves cleanly (acmart-loaded textcomp).

## Group B — v3-imported entries (9 entries, pre-Round-19)

All have author={**Surname**, TBD} or fully `{{TBD}}`; most lack
DOI.

### B1. Designer cluster
| cite-key | known | missing |
|---|---|---|
| `analogcoder` | title (verified v3), AAAI 2025 venue | full author list, DOI, pages |
| `analogcoderpro` | **✓ RESOLVED 2026-05-16** via leader WebSearch+arXiv | — (full Lai et al. author list, arXiv:2508.02518, 2025 — restored to refs.bib option-c re-ruling) |

### B2. Optimizer / sizer cluster
| cite-key | known | missing |
|---|---|---|
| `adollm` | "Yin" first-author surname, ICCAD 2024 | rest of author list, DOI, pages, publisher (acmart wants ACM/IEEE), address |
| `eesizer` | "Liu" surname, NEWCAS 2025, cites 8 commercial LLMs | rest of authors, DOI, pages, publisher field, address field |
| `anaflow` | "Ahmadzadeh + Gielen" authors, ICCAD 2025, BSIM 45nm | first-author given name, DOI, pages |
| `ledro` | "Kochar" surname, ICLAD 2025 | rest of authors, DOI, pages |
| `autosizer` | "Cheng" surname, arXiv:2602.02849, 2026, 24 circuits | rest of authors, full title verification |
| `analogsage` | "Wang" surname, arXiv:2512.22435, 2025 | rest of authors, full title verification |

### B3. Benchmark cluster
| cite-key | known | missing |
|---|---|---|
| `analoggym` | "Li" surname, ICCAD 2024, open-PDK YAML idiom | rest of authors, DOI, pages |

### B4. NDA-constrained cluster
| cite-key | known | missing |
|---|---|---|
| `analogagent` | "Bao" surname, arXiv:2603.23910, 2026, Qwen3 1.7B–14B | rest of authors, full title verification, venue (if accepted somewhere by submission time) |

## Resolution plan (camera-ready pass)

1. **Group A (5 TODAES DOIs)** — pull each from `https://dl.acm.org/doi/<doi>` and copy: full author list (BibTeX format with proper LaTeX escaping of accented names), canonical title, volume, issue, articleno, numpages. Cross-check year against DOI metadata.
2. **Group B (9 v3 entries)** — split sub-tasks:
   - **arXiv-anchored** (`autosizer`, `analogsage`, `analogagent`): pull canonical author list + title from arXiv abstract page.
   - **Venue-anchored** (`analogcoder`, `adollm`, `eesizer`, `anaflow`, `ledro`, `analoggym`): pull from proceedings DOI if available, else read original PDF.
   - **Unverified** (`analogcoderpro`): confirm with leader whether this is a real cite or v3 placeholder slated for removal.
3. **Prose-side check** — once canonical titles are in, grep v4/sec/*.tex for inline mentions ("AutoChip", "HLSRewriter", etc.) and confirm they match the published titles' short form.

## Risk flags

- **R1.** ~~Title strings for Group A are *inferred placeholders*~~ **CLOSED 2026-05-16** — leader resolved all 5 canonical titles via Crossref. Cite-strings remain valid as system-name short forms; no prose edits required.
- **R2.** `analogcoderpro` resolution: **CLOSED 2026-05-16 (option c)**. Audit-premise error → leader re-ruled with corrected premise → leader WebSearch+arXiv resolved the paper (Lai et al., arXiv:2508.02518) → entry restored to refs.bib with full metadata. **New downstream risk surfaced:** Pro's sizing-via-BO module overlaps our sizing work, so v3's "complementary code-generation" framing is invalid; Phase-2 §2.2 reframe logged in `phase1_carry_decisions.md`.
- **R3.** ~~`acmart` natbib pass prints warnings for missing volume/pages on all Group A~~ — **Group A CLOSED 2026-05-16**: 4/5 anchors zero warnings; confibench's Just-Accepted state covered by `\textnumero` note. Group B remains pre-camera-ready blocker.

## What was NOT done

- Did **not** modify any bib entry on this pass — only catalog.
- Did **not** attempt web lookups (offline environment assumed; leader-authorised network access would unblock most resolutions).
- Did **not** consult any PDF; would require local reference copies outside the public repo.
