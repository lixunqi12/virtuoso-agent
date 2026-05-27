# Agent Workflows

This document indexes the stable workflows for maintaining `virtuoso-agent`.
It is intentionally tool-neutral: Codex, Claude Code, Cursor, humans, and other
agents should all land on the same project rules.

## Canonical Sources

| Need | Source |
| --- | --- |
| Repository-wide agent and maintainer rules | `AGENTS.md` |
| Spec authoring rules | `docs/spec_authoring_rules.md` |
| LLM JSON/protocol contract | `docs/llm_protocol.md` |
| HSpice backend workflow | `docs/hspice_backend.md` |
| Local LLM evaluation notes | `docs/local-llm-evaluation.md` |
| Temporary current-session handoff | `HANDOFF.md` |

`HANDOFF.md` is allowed for immediate context, but stable procedures should be
promoted into `docs/` or `AGENTS.md` after they prove useful.

## Standard Local Flow

1. Inspect `git status --short` before editing.
2. Read the files related to the requested behavior before assuming ownership.
3. Use `.\.venv\Scripts\python.exe` for local Python commands on Windows.
4. Run focused tests for the touched module.
5. Run `scripts/check_p0_gate.ps1` before committing or publishing.
6. Use `bash scripts/sync_to_remote.sh --push` only when the user wants the
   current state shipped outward.

## Backend-Specific Notes

For Maestro and Spectre work, treat the remote Virtuoso session, bridge daemon,
and license state as user-owned runtime state. Do not restart or kill remote
processes unless explicitly asked.

For HSpice work, keep generated `.sp`, `.mt0`, and `.lis` data behind the
scrubbing path documented in `docs/hspice_backend.md` and implemented in
`src/hspice_scrub.py`.

For swept or oscillator specs, validate both the spec parser contract and the
metric reducer behavior. Prefer focused regression tests in `tests/` over
manual-only validation.

## Tool-Specific Mirrors

`CLAUDE.md` is a Claude Code auto-load shim. `.claude/skills/*` may contain
Claude-specific UX wrappers. Neither is the canonical source for project rules.
When adding reusable process knowledge, write the tool-neutral version first,
then add only a small pointer from any tool-specific surface.

## Publish Checklist

Before a commit, push, deploy, or public PR:

- Confirm the dirty worktree contains only intended changes for the publish
  scope.
- Run focused tests relevant to the changed code.
- Run the P0 leak gate.
- Check docs and generated artifacts for real PDK names, private hostnames,
  usernames, absolute personal paths, and real email addresses.
- Use GitHub noreply author identity for public commits.
