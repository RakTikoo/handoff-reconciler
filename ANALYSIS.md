# ANALYSIS — Handoff Reconciler v0.1.0

Built 2026-11-10. Python 3.12, stdlib only, deterministic, no LLM in the tool.
Test command: `python run_tests.py` (equivalently `python -m unittest discover -s tests`).

## Corpus design

The fixture corpus is not canned JSON: every test constructs a **real temporary
git repository** (`git init -b main`, fixture-local `user.email`/`user.name`),
commits real files, freezes a handoff through the real CLI
(`python -m handoff_reconciler`), then mutates ground truth with real git
operations (commits, branch switches, uncommitted edits, deletions) and asserts
the objective expected verdict of `resume-check` — per-claim lines, the overall
verdict, **and the process exit code** (0 MATCH / 1 DRIFT / 2 BROKEN).

The shared story: goal "Ship the auth refactor"; `app.py` claimed finished;
one `rejected` task with a reason; one `next` task targeting `util.py`.

## Measured test results

**At v0.1.0 build: 14 tests run, 14 passed, 0 failed** (`Ran 14 tests in 4.510s — OK`).

**After the adversarial-review fix pass (see the fix-pass section below):
51 tests run, 51 passed, 0 failed** (`python run_tests.py` — the original 14
fixture stories plus 37 regression tests in `tests/test_regressions.py`).
One original test was intentionally updated: #2b now expects the hash-first
wording `content differs from freeze; changed by commits after freeze`
(findings #2/#14 changed what the per-file check *is*).

| # | Fixture | Expected verdict | Actual |
|---|---|---|---|
| 1 | Perfect-match resume | MATCH, exit 0 | MATCH, exit 0 ✓ |
| 2 | HEAD advanced by 2 human commits (unrelated file) | DRIFT "2 commit(s)", claimed file still MATCH, exit 1 | as expected ✓ |
| 2b | Claimed file changed by a later commit | per-file DRIFT "changed by commits after freeze", exit 1 | as expected ✓ |
| 3 | Claimed-finished file dirty in working tree | per-file DRIFT, exit 1 | as expected ✓ |
| 4 | Claimed file deleted | per-file BROKEN, exit 2 | as expected ✓ |
| 5 | Verify command green, then red (safe `python -c` exit 0 / exit 3) | default NOT-RUN; `--run-verify` green = MATCH exit 0; red = BROKEN "exit 3" exit 2; without flag the red command is still never run | as expected ✓ |
| 5b | Shell metacharacters (`; touch …`, `$(…)`) in the `goal` field, run with `--run-verify` | marker file never created; fields treated as data | marker absent ✓ |
| 6 | Malformed handoffs: bad JSON / unknown status `maybe_done` / `rejected` without reason | freeze fails exit 2 with a specific error each; no HANDOFF.md written | as expected ✓ |
| 7 | Branch switch after freeze | DRIFT "branch changed main -> feature-x", exit 1 | as expected ✓ |
| 8 | Freeze in a non-git directory | exit 2, "not inside a git working tree", nothing written | as expected ✓ |
| 9 | Absolute (`/etc/passwd`, `C:\…`) and `..` paths in `files_claimed` and in a task target | freeze rejects each, exit 2 | as expected ✓ |
| 10 | Next-action target file deleted | BROKEN for that target; task absent from "Next actions that survive", exit 2 | as expected ✓ |
| 11 | Markdown draft input (fenced ```json block) + `show` | freeze succeeds, show prints the goal | as expected ✓ |
| 12 | `resume-check` read-only guarantee | `git status --porcelain` identical before/after | identical ✓ |

## Real-world run (hand-labelled)

Setup: a **copy** of the real SkillScan source tree
(`~/workspace/agentic-spikes/skillscan-src`, 86 files, 720K) was placed at
`realworld/skillscan-copy/` — the original was never modified. In the copy:
`git init`, one import commit, then a plausible session handoff was frozen
(goal: extend SkillScan rule coverage; `files_claimed = [skillscan.py, README.md]`;
next task targeting `docs/AUDIT_COVERAGE.md`; verify command
`python3 -m py_compile skillscan.py`). Scripted ground-truth mutations after
freeze: (a) commit the handoff itself; (b) a "human" commit touching the claimed
`README.md`; (c) an uncommitted edit appended to the claimed `skillscan.py`;
(d) deletion of the next-action target `docs/AUDIT_COVERAGE.md`. Full output:
`realworld/resume-check-output.txt` (run with `--run-verify`).

Reported verdicts vs hand-labelled truth — **6 of 6 correct, 0 false positives,
0 missed drifts (labelled precision 6/6 = 100% on this single run)**:

| Claim | Reported | Hand label |
|---|---|---|
| HEAD moved, 2 commits since freeze | DRIFT | TRUE — 2 commits exist (the handoff commit itself + the README commit; see limitation 1) |
| Branch `main` unchanged | MATCH | TRUE |
| `skillscan.py` claimed-finished but dirty | DRIFT | TRUE — uncommitted append present |
| `README.md` changed by commits after freeze | DRIFT | TRUE — human commit touched exactly it |
| Next-action target `docs/AUDIT_COVERAGE.md` missing | BROKEN | TRUE — file deleted |
| Verify command `py_compile skillscan.py` exit 0 | MATCH | TRUE — dirty append was a comment; file still compiles |

Overall BROKEN, exit 2 — correct: the sole next action's target is gone, so the
corrected resume pack correctly lists **no** surviving next actions.

A baseline `resume-check` immediately after freezing (before mutations b–d)
reported all file claims MATCH and only the HEAD drift from the handoff's own
commit — see limitation 1. *(Update, fix pass: that limitation is FIXED —
commits touching only `.handoff/` now report HEAD as MATCH with an
informational note, finding #7; and this run's per-file verdicts were produced
by the pre-fix git-state logic, whereas the current checker compares freeze-time
content hashes, finding #2. The hand-labelled verdicts above remain correct for
the mutations performed.)*

## Known limitations (honest; post-fix-pass)

1. **No handoff signing.** Integrity relies on the handoff living in git
   history (edits are visible there) plus strict format validation of the
   snapshot. Anyone who can rewrite both the handoff and the repo history can
   forge a baseline. Signing is out of v0 scope (see README security section).
2. Content hashes cover `files_claimed` only. A next-action *target's* contents
   are not hashed (its existence / rename state is checked), and semantic truth
   ("the refactor is actually complete") remains out of scope by design — that
   is what the single verify command is for, and it is opt-in.
3. **Legacy handoffs without `file_hashes`** (written before the fix pass) load
   with a weaker fallback: per-file verdicts fall back to git dirty/changed
   state only, so git-invisible edits (gitignored, `assume-unchanged`) are not
   caught for those handoffs. Re-freeze to get hash baselines.
4. One handoff per repo (`.handoff/HANDOFF.md`, plus the preserved previous
   copy `HANDOFF.prev.md`); no chain, no per-branch handoffs, no merging of
   concurrent handoffs.
5. `--run-verify` uses a shell (the verify command is a single user-supplied
   string by design). It is gated, printed first, timed out, process-group
   contained and output-bounded, but it remains the one intentionally sharp
   edge — see README security section.
6. Windows path checks use `PureWindowsPath` heuristics on any platform. A
   claimed path that is itself a symlink is followed for content hashing (the
   *content* is what is checked); the link target path is not canonicalised
   into the claim. (The `.handoff/` write path, by contrast, refuses symlinks
   outright — finding #8.)
7. HEAD ancestry cannot say *why* history was rewritten: a rebase onto an
   unrelated base and a wholesale history replacement both read as BROKEN
   "history rewritten" — the checker now distinguishes forward / backward /
   neither-ancestor precisely, but not intent.

## Adversarial review & fix pass

Three independent reviews of v0.1.0 (red team, false-positive tester, code
auditor) produced 16 numbered findings (3 critical/high clusters with proven
reproductions). All were addressed in one fix pass; every fix carries a
regression test in `tests/test_regressions.py` (37 new tests; suite 14 → 51,
all passing). Before → after, per cluster:

| # | Finding (before) | After |
|---|---|---|
| 1 | **Git argv injection**: the `git` snapshot passed load validation unchecked; a crafted `git.head = "--output=/tmp/x"` reached `git diff` as an *option* and a read-only `resume-check` overwrote `/tmp/x`. | Strict snapshot validation at load: `head` must match `^[0-9a-f]{40,64}$`, `branch` a non-empty string, `dirty_files` a list of strings, `file_hashes` a path→sha256 map matching the claims, unknown `git` fields rejected; any violation → clean `ERROR`, exit 2, nothing executed. Integrity story documented honestly (git history + format validation; no signing in v0). Tests: `test_r01_*`, `test_r01b_*`. |
| 2 | **No content hashes**: freeze recorded only filenames, so work dirty at freeze then reverted, a rewritten gitignored file, and an overwritten `assume-unchanged` file all read **MATCH** (work silently lost / invisible edits). | Freeze records a sha256 content hash per claimed file and **errors (exit 2)** if a claimed file is missing or a directory at freeze time (chosen over warn: a claim about a nonexistent file cannot be reconciled). Resume compares hashes: differ → DRIFT "content differs from freeze", missing → BROKEN. All three invisible-edit repros now DRIFT. Tests: `test_r02_*` … `test_r02d_*`. |
| 3 | **Git parsing**: `git diff --name-only` split on whitespace (spaces in filenames → false MATCH); porcelain parsing stripped quotes naively (quoted/newline names → false MATCH); untracked directories collapsed (claimed `newdir/inner.py` invisible). | `-z` output everywhere: `status --porcelain=v1 -z --untracked-files=all`, `diff --name-only -z`, `diff --name-status -z -M`; NUL-separated parsing including rename pairs (status `-z`: new-then-old; diff `-z`: old-then-new — both verified against real git). Tests: `test_r03_*`, `test_r03b_*`, plus spaced-name assertions in `test_r03`. |
| 4 | **Path canonicalisation**: `files_claimed: ["./app.py"]` or `["."]` froze fine then never matched git paths (permanent false MATCH / nonsense claim). | `./x` normalises to `x` (inner `.` components removed); `.` alone rejected; a claimed path that is a directory at freeze time is a clear freeze error. Tests: `test_r04_*` … `test_r04c_*`. |
| 5 | **Ancestry**: `rev-list --count` alone could not detect a history rewrite (a non-ancestor read as DRIFT "1 commit") and HEAD moving *backward* read as "0 commit(s)". | `git merge-base --is-ancestor` both directions: frozen ⊏ HEAD → DRIFT "N commit(s) since freeze"; HEAD ⊏ frozen → DRIFT "HEAD moved backward … rollback"; neither → BROKEN "history rewritten / frozen commit not an ancestor". Tests: `test_r05_*`, `test_r05b_*`. |
| 6 | **Dirty-at-freeze baseline ignored**: the snapshot's `dirty_files` was recorded but never used; a zero-change resume with a dirty claimed file flagged DRIFT. | Hash baseline resolves the main case (dirty-at-freeze and identically dirty at resume → hash equal → MATCH, test `test_r06_*`); the snapshot dirty list is also consulted for wording — dirty at freeze but clean at resume with a different hash reads DRIFT "…work present at freeze may have been lost" (test `test_r02_*`). |
| 7 | **The handoff's own commit**: committing `.handoff/HANDOFF.md` made every pristine resume DRIFT. (Was limitation 1.) | If every commit between frozen HEAD and current HEAD touches only `.handoff/`, the HEAD claim is MATCH with an informational note. Test: `test_r07_*`; also re-verified by hand on a scratch repo (pristine resume now exit 0). |
| 8 | **Symlink escape**: freeze followed a symlinked `.handoff/` and overwrote a file outside the repo. | Freeze refuses (exit 2) if `.handoff`, `HANDOFF.md`, or `HANDOFF.prev.md` is a symlink or resolves outside the repo root; checked before and after `mkdir`. Test: `test_r08_*` (sentinel file outside the repo is byte-identical after the refused freeze). |
| 9 | **Error discipline**: `UnicodeDecodeError`, `IsADirectoryError`, `FileNotFoundError` (git missing), `TimeoutExpired` from git calls, `RecursionError` on deeply nested JSON, and tampered-type `TypeError`s leaked tracebacks with exit 1. | `main()` maps `OSError` / `UnicodeDecodeError` / `TimeoutExpired` / `RecursionError` / `TypeError` / `ValueError` to clean `ERROR: …` + exit 2; a JSON nesting-depth guard (iterative, limit 100) runs on every parsed draft/handoff, and parser `RecursionError` is converted. Tests: `test_r09_*` … `test_r09c_*`, and `assert_clean_error` (no "Traceback" anywhere) is asserted across the regression suite. |
| 10 | **Renames**: a renamed claimed file (committed or staged `git mv`) reported BROKEN "missing/deleted". | Rename detection from `diff --name-status -z -M` (committed) and status `-z` rename entries (working tree): verdict DRIFT "renamed to \<new path\>", for claimed files and next-action targets alike. Tests: `test_r10_*` … `test_r10c_*`. |
| 11 | **Verify command**: (a) failures reported only "exit N"; (b) timeout killed only the shell (grandchildren survived); (c) captured output unbounded in memory; (d) `--verify-timeout` unvalidated; whitespace-only command ran. | (a) Failure/timeout reports include a bounded tail (last 20 lines / 4KB, stderr first, then stdout). (b) Verify runs with `start_new_session=True`; on timeout the whole process group is SIGKILLed — regression test proves a spawned grandchild that would write a marker file 2.5s later never does. (c) Output streams to temp files; only the last 64KB per stream is read back. (d) Timeout is an argparse-validated integer in 1..3600 (usage error, exit 2, also re-checked in the command); whitespace-only `verify_command` normalises to empty / NOT-RUN. Tests: `test_r11_*` … `test_r11d_*`. |
| 12 | **Markdown drafts**: `extract_json` silently took the *first* ```` ```json ```` block (an embedded example would be frozen). | More than one ```` ```json ```` block → error telling the author to keep exactly one; the fence tag is recognised case-insensitively (```` ```JSON ```` works); other fence tags (e.g. `jsonc`) → an error saying a fenced block was found but isn't plain json. Tests: `test_r12_*` … `test_r12c_*`. |
| 13 | **Duplicates/types**: duplicate `files_claimed` entries and duplicate task ids accepted; `reason` type-checked only for `rejected` tasks. | Duplicates rejected with clear errors — including duplicates that only appear *after* canonicalisation (`app.py` + `./app.py`); duplicate task ids rejected; `reason` must be a string whenever present. Tests: `test_r13_*` … `test_r13c_*`. |
| 14 | **Diff errors swallowed**: `_files_changed_between` returned an empty set on any git error → false per-file MATCH; wording implied a surveillance-style check. | Git diff failures now propagate as a tool error (clean `ERROR`, exit 2 — e.g. a well-formed but nonexistent frozen commit hash, test `test_r14_*`); per-file wording leads with the honest semantics, "content differs from freeze" (the net hash/tree comparison), with causes appended. Original test #2b was updated to the new wording (intentional change, noted above). |
| 15 | **Re-freeze destructive**: a second freeze silently overwrote the handoff, and the write was non-atomic (a crash could truncate it). | The previous handoff is preserved as `.handoff/HANDOFF.prev.md`, and the new handoff is written via a temp file in the same directory + `os.replace` (atomic on the same filesystem); no temp files remain after success or failure. Documented in README. Test: `test_r15_*`. |
| 16 | **Action/workflow honesty**: `action.yml` exits 0 with little explanation; the example workflow piped the checker through `tee`, silently replacing its exit code with `tee`'s; the README sample showed a verify MATCH for a command shown *without* `--run-verify`. | Informational design kept and now stated plainly in README ("the job never fails; drift is reported in the summary") and in comments in both YAML files; the example workflow uses `pipefail` + `PIPESTATUS[0]` so the real code reaches the summary; the README sample is labelled illustrative and its verify line now correctly reads `NOT-RUN` for the command shown. |
| LOW | HEAD/branch probes ignored git return codes (unborn HEAD produced garbage/crashes); branch normalisation was duplicated; `frozen_at` accepted a bare date; git subprocesses inherited `GIT_DIR`/`GIT_INDEX_FILE`/`GIT_WORK_TREE` and locale-dependent decoding. | Probes check return codes and fail cleanly on unborn HEAD (test `test_r16_*`); a single `_current_branch` helper; **`frozen_at` now requires a full timestamp with a time component** — a bare date is rejected (chosen over documenting acceptance; test `test_r17_*`); git calls strip `GIT_DIR`/`GIT_INDEX_FILE`/`GIT_WORK_TREE` from the environment and decode as utf-8 with `errors="replace"` (test `test_r18_*` proves a bogus `GIT_DIR` no longer redirects ground truth). |

**Intentional behaviour changes** (anything a user of v0.1.0 would notice):
freeze now *errors* on claimed files that are missing or directories (#2/#4);
`./x` claims are normalised instead of stored verbatim (#4); duplicate claims /
task ids and non-string `reason`s are rejected (#13); a bare-date `frozen_at`
is rejected (LOW); multiple ```` ```json ```` blocks in a draft are an error
instead of first-block-wins (#12); re-freeze keeps `HANDOFF.prev.md` (#15);
per-file drift wording leads with "content differs from freeze" (#14); a
pristine resume after committing the handoff itself is now MATCH, not DRIFT
(#7); renamed claimed files/targets are DRIFT, not BROKEN (#10); HEAD rollback
and history rewrite have their own verdicts (#5).

**Reviewer repros re-run by hand after the fix** (in addition to the automated
regressions): the `--output=` head injection (clean `ERROR: git.head …`,
exit 2, target file never created) and the pristine handoff-commit flow
(MATCH, exit 0) were both re-run manually on a scratch repo and behave as
specified; the remaining repros (spaced filename, `./path`, dirty-vanish,
symlink, rename) are covered by the named regression tests above, all passing.

## Expansion ladder (kernel stays: schema + reconciler)

1. Timestamped handoff chain under `.handoff/` (seq-linked, previous-head
   cross-checks) — additive files, same schema.
2. ~~`.handoff/`-only HEAD-drift refinement~~ — DONE in the fix pass (finding #7).
3. One leaf adapter per additional agent (Codex, Cursor): each only *writes the
   same draft schema*; the reconciler does not change.
4. GitHub Action hardening: optional fail-on-BROKEN mode; PR comment with the
   corrected resume pack.
5. Team layer (much later, separate product surface): shared ledger search and
   drift review gates over many repos' `.handoff/` histories.

## GitHub integration story

`action.yml` is a composite action: check-only `resume-check` (never
`--run-verify`), drift report appended to `GITHUB_STEP_SUMMARY`, informational
exit 0 so drift informs rather than blocks until a team opts into gating.
`.github/workflows/example-resume-check.yml` shows the equivalent inline form
with `fetch-depth: 0` (the checker needs history back to the freeze commit).
Nothing in the Action is claimed beyond what the CLI already does locally.

## Break-risk guardrails implemented (from the Round 4 "complexity without collapse" patterns)

- **Local-first plain files only** — one markdown file in-repo; no daemon,
  server, database, or network. Git is the sync/backup layer.
- **Snapshot baseline + diff, never surveillance** — freeze stores a compact
  snapshot (branch, HEAD, dirty list); all intelligence is the diff at resume.
- **Oracle-command contract** — one verify command string answers pass/fail;
  the tool never tries to understand the user's build system.
- **One agent in v0 scope, agent-neutral schema** — Claude Code is the usage
  target; the format is plain JSON/markdown any agent can produce.
- **CLI first, no GUI.**
- **Deterministic kernel, LLM excluded** — the agent writes prose at the edge
  (the draft); validation and reconciliation are pure stdlib code.
- **Strict-by-default validation** — unknown fields/statuses fail loudly
  instead of being silently dropped, so schema drift surfaces immediately.

## Incumbent re-verification (searched 2026-11-10, web index reads only)

Searches run for this section, 2026-11-10: `Session Glue handoff plugin Claude
Code resume session`; `Claude Code /resume native session resume feature
documentation`; `agent handoff plugin claude-handoff thepushkarp handoff session`.

- **Session Glue** (`realproject7/session-glue`, repo created 2026-07-01;
  also on PyPI): a session-continuity protocol/CLI — freezes a validated,
  repo-local markdown handoff (`.agent-history/`, YAML frontmatter) and
  generates a resume prompt; its README describes the receiving agent checking
  branch/HEAD/working-tree drift. This is the closest incumbent found: it owns
  *capture + prompt-side resume guidance*. What the index reads did **not**
  show: a standalone deterministic reconciler that scores every claim
  MATCH/DRIFT/BROKEN against git with exit codes, or a verify-command safety
  model. (Feature comparison from its own README/PyPI pages only — unverified
  beyond those pages.)
- **thepushkarp/handoff** (GitHub, created 2026-01-05): a Claude Code plugin
  (`/handoff:create`, `/handoff:resume`) that captures context to
  `docs/handoff/HANDOFF.md` as an appended timestamped log. Capture/restore
  only in what was read; no verification layer seen.
- **claude-handoff skills/plugins** (several, e.g. Matt Pocock's skills set;
  also `@neoacar/claude-handoff`, which packages Claude Code transcripts for
  sharing via git): summarise/launch or transport session context. Again
  capture/transport, not claim-by-claim reconciliation.
- **Claude Code native `/resume`, `--continue`, `--resume`** (vendor behaviour
  as described in multiple secondary write-ups read today; sessions stored as
  JSONL under `~/.claude/projects/`): restores the *same vendor's own*
  conversation. Single-vendor continuity, no cross-agent handoff, and no
  checking of the resumed context's claims against current repo state.

Consistent with the Round 4 report (2026-11-10, Candidate 1): capture is
commoditised by free skills/plugins and native resume owns single-vendor
continuity; **no product with pricing, a team tier, or meaningful traction was
found doing verified, claim-by-claim reconciliation** in these searches. That
is a "searched and found none" statement bounded by the three queries above,
not a proof of non-existence. Traction numbers for Session Glue / handoff
plugins could not be verified from the pages read.
