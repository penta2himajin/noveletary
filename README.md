# noveletary

[日本語](./README.ja.md)

**novel + secretary** — a constraint-maintained narrative knowledge base and MCP server that checks the internal consistency of fiction, with first-class support for Japanese prose.

## What

A local [MCP](https://modelcontextprotocol.io) server an LLM (Claude Code, Claude.ai Projects) calls while you write or import a novel. It tracks story facts in an append-only operation log, gates contradictions at write time, branches parallel plot drafts, and routes the questions it cannot decide to you — the author.

- **Construction = checked mutation.** Adding a fact runs hard-constraint checks (a dead character acting, a monotone ledger decreasing, a temporal cycle, an orphaning delete) and rejects contradictions with the conflicting fact set.
- **Verification = the same engine, batch mode.** Audit a whole branch; hard violations are certain, optional semantic checks (NLI) become author questions.
- **Story branches** are first-class: parallel drafts (A-plot / B-plot) audited independently, merged with structural conflict detection, rolled back without losing history.
- **The author is the oracle.** Unresolved aliases, merge conflicts, and semantic doubts go to the author, not to LLM guesswork. Answers persist and propagate through later checks.

## Why

LLM-driven consistency checking is wasteful and unreliable when the LLM both writes and self-grades. noveletary keeps a deterministic constraint engine and the author as the trusted core, and treats the LLM as a fallible translator with no authority. Most "contradictions" in fiction are structurally decidable (state machines, numeric invariants, temporal constraints) and need no semantics; the semantic residual is the only part a language model judges, and even then the verdict is a question, not a gate.

## Status

Early (v0.1). Core engine, store, tri-temporal facts (valid interval / discourse / transaction), branching, merge, audit, outline beats + foreshadow ledger, and the MCP server are implemented and tested. The Japanese NLP extraction layer (KWJA zero-anaphora + full noun-phrase reconstruction, GiNZA fallback) is the standard path but stays advisory — `propose_canon_facts` drafts canon from prose for author curation; it never gates. KWJA needs Python < 3.14 and self-seeds its checkpoint cache on first use. Not yet deployed remotely (Cloudflare Workers + D1 is a known migration path).

## Install

```bash
pip install -e ".[dev]"          # core + tests
pip install -e ".[dev,nlp]"      # add Japanese NLP (GiNZA, KWJA)
```

## Run as an MCP server

```bash
noveletary-mcp                                   # stdio
claude mcp add noveletary -- noveletary-mcp      # register with Claude Code
```

SQLite state persists in `data/narrative.db` (run from the repo root; override with `NARRATIVE_DB`).

## Tools (LLM-facing)

Facts are **tri-temporal**: `chapter` is valid-time as an interval `[chapter, valid_to)` (when a
fact is true in the story), `narrated_in` is discourse-time (which chapter reveals it — for
foreshadowing / flashbacks), and the op-log is transaction-time. Each tool's description is
prefixed with its category and carries read-only / destructive annotations.

| Category | Tools | Purpose |
|---|---|---|
| **read** | `get_state`, `chapter_brief`, `get_log` | state before writing (valid- or discourse-time sliced); `chapter_brief` bundles characters / world / constraints / open questions / open foreshadow / recent + the chapter beat in one call |
| **fact** | `add_fact`, `add_facts`, `retag_fact`, `delete_fact`, `import_facts` | register facts (hard-gated, atomic batches) / move-or-relabel in place / delete (orphan check) / bulk-load an existing work (ungated → `audit`) |
| **branch** | `create_branch`, `delete_branch`, `rollback_branch`, `merge_branches`, `list_branches` | parallel drafts, structural merge, non-destructive rollback, cleanup |
| **constraint** | `list_constraints`, `add_constraint`, `set_constraint`, `check_constraints` | work-specific hard rules as data, versioned per branch |
| **question** | `list_open_questions`, `answer_question`, `link_entities` | the author-oracle channel; `link_entities` declares two names same/distinct |
| **verify** | `audit` | hard violations always; `include_soft=True` adds NLI-based author questions |
| **outline** | `set_beat`, `get_outline`, `add_setup`, `resolve_setup` | outline-first beats and a Chekhov ledger (foreshadow with overdue tracking) |
| **nlp** | `reconcile_facts`, `propose_canon_facts` | mechanism prose extraction — draft canon facts from a chapter, or cross-check the LLM's self-report |

To drive the tools from unattended agents / Claude Code subagents, allowlist the whole server
(`mcp__noveletary`) rather than individual tools — see `AGENTS.md`.

## License

MIT. See [LICENSE](./LICENSE).
