# noveletary

## Overview

noveletary (novel + secretary) is a constraint-maintained narrative knowledge base and MCP server for checking the internal consistency of fiction, with first-class support for Japanese prose. It unifies **construction** (adding/updating/deleting story facts) and **verification** (contradiction checking) into a single constraint engine: hard, decidable contradictions (a dead character acting, a monotone ledger decreasing, a temporal cycle) gate writes at construction time; softer semantic contradictions surface as questions for the author. Story branches (parallel plot drafts) are first-class via an append-only operation log; each branch is audited independently and merged with structural conflict detection.

The design principle throughout: the knowledge base and the author are the trusted core; the LLM is a fallible translator with no authority. Unresolved questions go to the author (the ground-truth oracle), not to LLM guesswork.

## Project Structure

```
src/noveletary/
  engine.py        # constraint engine: delegates hard checks to the template executor; affected-subgraph; alias resolution
  constraints.py   # EC-grounded constraint template executor (forbid_after_state/monotone/acyclic/release) — rules as data
  store.py         # operation log + branches + persistence (SQLite) + gated add / import / audit / merge / questions / constraints
  server.py        # MCP server (FastMCP, stdio) exposing the store as tools
  kwja_extract.py  # KWJA PAS adapter -> generic predicate-argument records (zero-anaphora-resolved); records_to_facts
  extract.py       # GiNZA fallback producing the same generic record schema (degraded: no modality/zero-anaphora)
  reconcile.py     # generic reconcile (subject,predicate axis) of LLM self-report vs mechanism records
tests/             # pytest suite; core tests run without NLP extras
docs/              # engineering docs (English): handoff protocol, i18n policy
data/              # SQLite op-log (data/narrative.db) — generated on first run, NOT committed (only data/.gitkeep tracked)
```

## Development Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + test tooling
pip install -e ".[dev,nlp]"      # Japanese NLP (GiNZA, KWJA) — the standard extraction stack; needs Python < 3.14 for KWJA

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

NLP extraction is the **standard** path: on startup `noveletary-mcp` checks for the NLP
stack and auto-installs the `nlp` extra if missing (pip output goes to stderr so the stdio
protocol stays clean). Disable with `NOVELETARY_NLP_AUTOSETUP=0`. KWJA requires
**Python < 3.14**; on 3.14+ the auto-setup of KWJA fails and extraction falls back to GiNZA
(degraded) — use a 3.12/3.13 interpreter for the full stack. KWJA checkpoints are seeded on
first extraction by `kwja_extract.ensure_kwja_cache` (certifi → curl fallback), which works
around the Kyoto host shipping an incomplete cert chain that OpenSSL-based Python can't verify;
transformer models still download lazily from HuggingFace on first use.

The DB at `data/narrative.db` (override with `NARRATIVE_DB`) is created on first launch and
seeded with the **initial values**: the deletable default constraints (forbid-after-state /
monotone ledger / acyclic order). It is a generated artifact — not committed (see Prohibitions).

**Driving noveletary from agents.** Tools carry a `[category]` prefix in their description
(branch / read / fact / constraint / outline / question / verify / nlp) plus read-only /
destructive annotations. To let Claude Code **subagents** (or any unattended agent) call the
tools without stalling on approval prompts, allowlist the **whole server** — add
`mcp__noveletary` to `permissions.allow` in `.claude/settings.local.json`. Do **not** list
tools individually: the surface is consolidated over time and per-tool entries go stale
(observed: subagents blocked mid-run on unlisted `set_beat`/`chapter_brief`). Failure-recovery
patterns live in the tools themselves — a rejected write's `conflict` detail carries the fix
(e.g. "put the fact before the death chapter or fold it with `valid_to`").

## Development Principles

- Extraction is a **second, advisory signal**, never authoritative. `reconcile_facts` surfaces likely omissions/fabrications for human confirmation; it does not gate.
- Hard constraints gate construction; soft/semantic checks never gate — they create author questions.
- Hard constraints are **data, not code**: `constraints.py` holds work-agnostic templates; the work-specific instances (params) live in the op-log, are versioned per branch, and are author-operable (add/disable/remove). Defaults are seeded as deletable data, not privileged. Templates are EC-grounded (forbid_after_state = inertia/state-constraint; release = EC Release for per-branch resurrection).
- Every new contradiction type ships with a test that plants the contradiction and asserts it fires.
- Facts are **tri-temporal**: valid-time is the interval `[chapter, valid_to)` (when a fact is true in the story world; `valid_to=None` ⇒ `+∞`, terminated implicitly by supersession), `narrated_in` is discourse-time (which chapter reveals it; defaults to `chapter`), and the op-log `op_id` is transaction-time. Hard constraints judge on **valid-time** (forbid_after_state fires on a forbidden fluent *re-initiating* at/after a terminal, i.e. `valid_from >= td`). `get_state` slices independently: `as_of_chapter` = world state (interval contains the chapter), `as_of_narrated` = reader knowledge. A finite `valid_to` lets a biographical fluent end cleanly at death (no fake "chapter 0"). All temporal data lives in the op payload (JSON) / snapshot tuples, with defensive defaults on replay, so old ops and snapshots upgrade with no destructive migration. (`valid_from`'s true `-∞` start is still approximated by an early finite chapter — a known limitation.)

## Architectural Boundaries

- `engine.py` stays free of persistence and NLP — pure constraint logic over in-memory facts.
- `store.py` owns SQLite and the operation log; the engine never touches the database.
- NLP (`extract.py`, `kwja_extract.py`) ships as the `nlp` extra and is the **standard** extraction path (`server.py:ensure_nlp` auto-installs it at startup). It stays a separable extra: core (`engine`, `store`, `server`) must still **import and pass tests without it** — `ensure_nlp` runs only in `main()`, never at import, and extraction degrades to GiNZA / skips when absent.
- The operation log is append-only. State is derived by replay; rollback moves a branch head and never deletes operations.

## Prohibitions

1. Do not make soft/semantic checks gate construction. They produce questions only.
2. Do not add NLP imports to `engine.py` or `store.py`.
3. Do not delete operations from the log to implement rollback; move the head instead.
4. Do not commit `data/narrative.db`; only `data/.gitkeep` is tracked. The DB is a generated
   artifact: it is rebuilt on first launch and its **initial values** (the default constraints)
   are seeded in code (`store.py` `__init__` → `default_constraints()`), not shipped as a binary.
   `.gitignore` ignores `*.db` accordingly. (Authoritative over the earlier `.gitignore` comment
   that called the DB "intentionally committed", which is superseded by this rule.)

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
