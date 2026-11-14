# Contributing to Handoff Reconciler

Thanks for your interest. Handoff Reconciler freezes a structured
handoff at the end of an AI coding session and, at the next session,
checks every claim in it against git and test ground truth. A drift
report people can't trust is worse than no report, so contributions
that protect that trust are very welcome.

## The rules of the house

1. **Standard library only.** The tool runs on a stock Python install
   with zero dependencies, no network access, no account and no token.
   PRs that add a dependency will be declined.
2. **No LLM inside the tool.** The checker is deterministic code, not
   a model call. The handoff format is agent-neutral plain text
   (markdown plus a JSON header block); nothing in the format or the
   checker is tied to one vendor's agent.
3. **Deterministic.** Same handoff plus same repository state, same
   report, byte for byte. No clocks, no randomness, no
   environment-dependent behaviour.
4. **Check claims, never trust them.** Every line of a drift report
   comes from comparing the frozen handoff with ground truth (git
   state, file contents, the verify command's real exit code) — never
   from the handoff's own say-so.
5. **Informational by design.** The GitHub Action never fails the job;
   drift is reported, not gated on. Please don't change that without
   discussing it in an issue first.

## Getting started

```bash
python run_tests.py        # the full suite (51 tests), or:
python -m unittest discover -s tests
```

Run the suite before and after your change; it must be green both
times. New behaviour starts as a test or a benchmark scenario that
fails before your change and passes after, plus trap cases — handoffs
that look drifted but must **not** change verdicts, and the reverse.

Issues labelled `good first issue` are scoped to be finishable in one
sitting, with the checks spelled out in the issue itself.
