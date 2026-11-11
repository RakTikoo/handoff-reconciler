# BENCHMARK RESULTS — Handoff Reconciler v0.1

Measured 2026-11-10 (America/Los_Angeles) on the v0.1 tree, before the
v0.1 tag. Python 3.12, stdlib only. All numbers are this tool's own rates
on its own corpora, measured by the scripts in `benchmark/` — anyone can
re-run them:

```bash
python3 benchmark/run_benchmark.py   # planted-drift corpus (CI)
python3 benchmark/run_foreign.py     # foreign handoffs (CI)
python3 benchmark/run_dogfood.py     # real project trees (local only, see below)
python3 run_tests.py                 # 51 unit + regression tests
```

## Headline numbers

| Measure | Result |
|---|---|
| Planted-drift catch rate | **20 / 20 (100%)** |
| False alarms on accurate handoffs | **0 / 12 (0.0%)** |
| Dogfood on real project trees | **4 / 4 runs, 24 / 24 claims correct, 0 false alarms** |
| Foreign handoffs | **3 clean loud errors, 2 full cross-agent matches, 0 silent misreads** |
| Unit + adversarial regression tests | **51 / 51** |

## 1. Planted-drift corpus (`benchmark/run_benchmark.py`, `benchmark/results.json`)

Every scenario builds a real temporary git repository, freezes a handoff
through the real CLI, then either leaves ground truth alone or plants
exactly one labelled drift — or crafts exactly one false claim directly
into the handoff file, simulating an agent whose summary was never true.
A scenario passes only if the overall verdict, the exit code, **and** the
key per-claim line all match the label recorded before the run.

| Kind | Caught / total | What was planted |
|---|---|---|
| file-content | 7 / 7 | uncommitted edit; edit by a later commit; deletion; rename (committed); rename (staged `git mv`); silent edit of a gitignored claimed file; edit hidden by `assume-unchanged` |
| false-claim | 2 / 2 | a claimed file edit that never happened (handoff hash describes ghost content, file and git state untouched); one false hash among two claimed files (the true one still reads MATCH) |
| commit-state | 6 / 6 | HEAD advanced by 2 unrelated commits; HEAD moved backward (rollback); history rewritten (amended freeze commit); a claimed frozen commit that does not exist (clean tool `ERROR`, exit 2, no traceback — counted as caught because the claim is never trusted); branch switch; branch switch + HEAD advance together |
| verify | 2 / 2 | verify command fails at resume although the handoff claims success; a non-claimed file broken after freeze, caught only by the verify command (claimed file still MATCH) |
| next-action | 3 / 3 | target deleted; target renamed (committed); target in a subdirectory deleted |
| **accurate (no drift planted)** | **12 / 12 clean** | pristine resumes (single/multiple/subdirectory/spaced-name claims), handoff-commit-only resumes, dirty-at-freeze file identically dirty at resume, verify passes under `--run-verify`, a failing verify command correctly **not** run by default |

The false-alarm number is the one we weight most, same discipline as a
benign false-BLOCK count: a checker that cries drift on honest handoffs
will be ignored within a week. On this corpus it is **0%**.

## 2. Dogfood on real handoffs (`benchmark/run_dogfood.py`, `benchmark/dogfood-results.json`)

Copies of three real project trees from the same workspace — SkillScan,
Living Contract Diff, CI Command Truth — were initialised as fresh git
repos (originals never touched), given handoffs claiming their real files
and real next steps, and checked. (Local-only: the source trees are not
part of this repo, so this script does not run in CI. Full outputs are in
`benchmark/dogfood-outputs/`.)

- **Pristine runs (3):** freeze, commit only the handoff, resume with
  `--run-verify`. All 3 read **MATCH on every claim** — HEAD, branch, both
  claimed files, next-action target, verify command — 18 / 18 claims,
  **0 false alarms** on real, messy project trees.
- **Interleaved run (SkillScan, current tree):** after freezing, a
  "human" commit touched the claimed `README.md`, an uncommitted edit was
  appended to the claimed `skillscan.py`, and the next-action target
  `docs/AUDIT_COVERAGE.md` was deleted. The checker reported exactly
  those: HEAD DRIFT (2 commits), `skillscan.py` DRIFT (dirty),
  `README.md` DRIFT (changed by commits), target BROKEN, branch MATCH,
  verify MATCH (the append was a comment; the file still compiles) —
  **6 / 6 hand-verified correct, 0 false alarms, 0 misses.** Overall
  BROKEN, exit 2, and the corrected resume pack correctly listed no
  surviving next actions.
- The earlier hand-labelled real-world run on a SkillScan copy
  (`realworld/`, documented in `ANALYSIS.md`) also scored 6 / 6; this
  dogfood round repeats that pattern on the current tree and adds two
  more real projects.

## 3. Foreign handoffs (`benchmark/run_foreign.py`, `benchmark/foreign-results.json`)

Handoffs **not** written in this tool's format, placed in real repos:

| Case | Result |
|---|---|
| Plain prose markdown handoff (classic agent `/handoff` output, no machine-readable block) | Clean `ERROR`, exit 2: "no JSON object and no ```json fenced block found" |
| YAML-frontmatter handoff (Session-Glue style) | Clean `ERROR`, exit 2, same message |
| One ```json block but with foreign extra top-level fields (`summary`, `context`) | Clean `ERROR`, exit 2: "unknown top-level fields: ['context', 'summary']" |
| A different agent's draft (`agent: codex`), markdown with one ```json block, alias keys (`text` / `file`) | Freezes and reconciles fully — **MATCH** |
| A different agent's bare-JSON draft (`agent: cursor`) | Freezes and reconciles fully — **MATCH** |

Conclusion, stated honestly: the format is agent-neutral and any agent
that writes the documented schema (or embeds exactly one JSON block using
it) reconciles fully, whoever wrote it. The tool does **not** parse
arbitrary prose or other tools' native formats — by design it fails
loudly instead of guessing, so a foreign handoff is never silently
trusted and never silently misread. **0 silent misreads in 5 cases.**

## 4. Live GitHub Actions (stranger run)

The published Action is exercised the way a stranger would use it — a
separate consumer repo whose workflow references the published Action by
tag (`uses: RakTikoo/handoff-reconciler@v0.1`), not a vendored copy.
This section is completed with the live run's identifier and verdict
immediately after the v0.1 tag exists (the run depends on the tag);
until then, the Action is covered locally by the 51-test suite and the
corpora above, and has **not** yet run in live GitHub Actions.

## What this does not prove

- Semantic truth ("the refactor is actually complete") is out of scope
  by design; that is what the single opt-in verify command is for.
- The corpus is ours: 32 labelled scenarios built to cover every drift
  kind the README claims to catch. Kinds the tool does not claim (e.g.
  prose-handoff parsing) are tested as loud failures, not catches.
- No comparison with any other tool is made or claimed. These are our
  own rates on our own corpus only.
