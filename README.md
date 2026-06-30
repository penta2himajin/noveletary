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

Early (v0.1). Core engine, store, branching, merge, audit, and MCP server are implemented and tested. The Japanese NLP extraction layer (GiNZA reconciliation; KWJA zero-anaphora adapter) is optional and advisory; the KWJA path awaits its upstream model host. Not yet deployed remotely (Cloudflare Workers + D1 is a known migration path).

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

| Tool | Purpose |
|---|---|
| `get_state` / `get_log` | state before writing (chapter-sliced, subject-focused), history |
| `add_fact` / `add_facts` | register facts (hard-gated) — writing from scratch |
| `import_facts` | bulk-load an existing work (not gated) — then `audit` surfaces issues |
| `update_fact` / `delete_fact` | supersession (+retcon check) / delete (orphan check) |
| `audit` | hard violations always; `include_soft=True` adds NLI-based author questions |
| `create_branch` / `merge_branches` / `rollback_branch` | parallel drafts, structural merge, non-destructive rollback |
| `list_open_questions` / `answer_question` | the author-oracle channel |
| `extract_facts` / `reconcile_facts` | independent prose extraction; cross-check the LLM's self-report |

## License

MIT. See [LICENSE](./LICENSE).
