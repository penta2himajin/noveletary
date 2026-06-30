# noveletary

## Overview

noveletary (novel + secretary) is a constraint-maintained narrative knowledge base and MCP server for checking the internal consistency of fiction, with first-class support for Japanese prose. It unifies **construction** (adding/updating/deleting story facts) and **verification** (contradiction checking) into a single constraint engine: hard, decidable contradictions (a dead character acting, a monotone ledger decreasing, a temporal cycle) gate writes at construction time; softer semantic contradictions surface as questions for the author. Story branches (parallel plot drafts) are first-class via an append-only operation log; each branch is audited independently and merged with structural conflict detection.

The design principle throughout: the knowledge base and the author are the trusted core; the LLM is a fallible translator with no authority. Unresolved questions go to the author (the ground-truth oracle), not to LLM guesswork.

## Project Structure

```
src/noveletary/
  engine.py        # constraint engine: hard-constraint checks, affected-subgraph, alias resolution
  store.py         # operation log + branches + persistence (SQLite) + gated add / import / audit / merge / questions
  server.py        # MCP server (FastMCP, stdio) exposing the store as tools
  extract.py       # prose -> fact extraction (GiNZA) + reconciliation against LLM self-report
  kwja_extract.py  # KWJA PAS adapter (zero-anaphora-resolved extraction); pending external model host
tests/             # pytest suite; core tests run without NLP extras
docs/              # engineering docs (English): handoff protocol, i18n policy
data/              # SQLite operation log persisted in-repo (data/narrative.db)
```

## Development Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + test tooling
pip install -e ".[dev,nlp]"      # add Japanese NLP (GiNZA, KWJA) for extract/reconcile

# Pre-push hook (ruff format / lint).
git config core.hooksPath git-hooks
```

## Build & Test

```bash
pytest -q                        # core suite (no NLP deps required)
ruff format --check . && ruff check .
```

Running the MCP server locally:

```bash
noveletary-mcp                   # stdio; register with: claude mcp add noveletary -- noveletary-mcp
```

## Development Principles

- Extraction is a **second, advisory signal**, never authoritative. `reconcile_facts` surfaces likely omissions/fabrications for human confirmation; it does not gate.
- Hard constraints (use-after-free, monotone break, temporal cycle, orphan-on-delete) gate construction. Soft/semantic checks never gate — they create author questions.
- Every new contradiction type ships with a test that plants the contradiction and asserts it fires.

## Architectural Boundaries

- `engine.py` stays free of persistence and NLP — pure constraint logic over in-memory facts.
- `store.py` owns SQLite and the operation log; the engine never touches the database.
- NLP (`extract.py`, `kwja_extract.py`) is an **optional extra**; core (`engine`, `store`, `server`) must import and pass tests without it.
- The operation log is append-only. State is derived by replay; rollback moves a branch head and never deletes operations.

## Prohibitions

1. Do not make soft/semantic checks gate construction. They produce questions only.
2. Do not add NLP imports to `engine.py` or `store.py`.
3. Do not delete operations from the log to implement rollback; move the head instead.
4. Do not commit a populated `data/narrative.db`; only `data/.gitkeep` is tracked.

## Git Conventions

Conventional Commits with optional scope, e.g. `feat(store):`, `test(engine):`, `feat(mcp):`.

## Session Handoff

Long-running workstreams use GitHub issues for cross-session continuity. See `docs/handoff-protocol.md`.

- Label: `session-handoff`
- One issue per workstream (not per session)
- On session start, read the relevant handoff issue and confirm the **Next action** with the user before executing.

## Internationalisation

Follows `docs/i18n-policy.md`: translations are suffix files (`README.ja.md` next to `README.md`); only `README.md` and the user-facing introduction tier of `docs/` are in scope; each translated file carries a `> Source: <name>.md @ <sha>` header; PRs are never blocked on translation parity.

---

<!-- Common rules below this line apply to every project. -->

## Common Development Rules

### TDD (Red -> Green -> Refactor)

1. **Red**: write a failing test that captures the intended behaviour.
2. **Green**: write the minimum code that makes the test pass.
3. **Refactor**: tidy up while keeping tests green.

When a test fails, fix the production code — do not delete, skip, or weaken the test.

### Git Conventions

- **Conventional Commits**: `feat:` `fix:` `docs:` `refactor:` `test:` `ci:` `chore:`.
- **Branch naming**: short prefix for the agent or author plus a topic, e.g. `claude/<topic>`, `human/<topic>`.
- **Pre-push hook**: install via `git config core.hooksPath git-hooks`. Runs format / lint before every push.

### Pull Requests

- **Always ready for review.** Never open as drafts.
- **One PR per workstream**, matching the handoff issue. Reference it with `Closes #N`.

### Common Prohibitions

1. Do not delete, skip, or comment out existing tests.
2. Do not modify CI configuration without explicit instruction.
3. Do not weaken production code merely to make tests pass.
4. Do not commit credentials, API keys, signed URLs, or anything in `.env*`.
