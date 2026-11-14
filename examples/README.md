# Examples

Two worked examples: a minimal starter draft, and a real handoff from
real work, with the drift report it produced at the next session.

## Start here: `draft.json`

`draft.json` is the smallest useful draft: one file claimed finished
(`app.py`), one task rejected with its reason, one next action pointing
at `util.py`, and a verify command. An outgoing agent writes a draft
like this; the tool does the rest:

```bash
# 1. Freeze it in the project it describes (the tool fills the git
#    snapshot itself — branch, HEAD, and a content hash per claimed file):
python -m handoff_reconciler freeze --draft draft.json

# 2. At the start of the next session, before trusting the summary:
python -m handoff_reconciler resume-check
```

## The real-world example: `realworld-draft.json` and `realworld-resume-check-output.txt`

These two files also live in `../realworld/`; they are copied here so
this folder is self-contained.

- `realworld-draft.json` is a real draft, written during real work on
  the SkillScan project: two files claimed finished, one rule rejected
  with its reason, and one next action aimed at
  `docs/AUDIT_COVERAGE.md`.
- `realworld-resume-check-output.txt` is the recorded `resume-check`
  report from the next session (run with `--run-verify`, so the verify
  command was executed). The frozen summary claimed a clean stopping
  point; the check found that HEAD had moved 2 commits, one claimed
  file was dirty, another had changed after the freeze, and the
  next-action target no longer existed — overall verdict BROKEN, with a
  corrected resume pack listing what was still true.

## The three steps, on your own project

1. **Freeze the draft** in the tree it describes (`freeze` above). The
   tool fills in the git snapshot itself, so the snapshot cannot be
   forged by the draft's author.
2. **Run `resume-check`** at the next session, before acting on the
   summary.
3. **Compare against the recorded report.** Your report will have the
   same shape as `realworld-resume-check-output.txt`: one line per
   claim (MATCH / DRIFT / BROKEN), an overall verdict, and a corrected
   resume pack — what is still true, what drifted, and which next
   actions survive.
