"""Foreign-handoff test: handoffs NOT written in this tool's house style.

Question: does the checker generalise beyond handoffs it froze itself, and
-- just as important -- does it fail LOUDLY instead of guessing when a
handoff is in a format it does not understand?

Cases:
  F1  Plain prose markdown handoff (the classic Claude Code /handoff
      output: headings and bullets, no machine-readable block).
  F2  YAML-frontmatter handoff (Session-Glue style: --- frontmatter ---
      plus markdown body).
  F3  A single ```json block, but with foreign extra top-level fields
      (summary / context / session_id) the schema does not know.
  F4  A DIFFERENT agent's draft (agent="codex") written as markdown with
      one ```json block, using the schema's alias keys (text / file).
  F5  A different agent's bare-JSON draft (agent="cursor").

Honest expectation, set before running: F1-F3 are clean ERRORs (exit 2, no
traceback, nothing executed, nothing silently trusted); F4-F5 freeze and
reconcile fully. The tool does NOT claim to parse arbitrary prose.

Run:  python3 benchmark/run_foreign.py
Writes benchmark/foreign-results.json
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r


def run_cli(repo, *argv):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([PY, "-m", "handoff_reconciler", "--repo", str(repo), *argv],
                          capture_output=True, text=True, env=env, timeout=60)


def base_repo(tmp):
    repo = Path(tmp) / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "foreign@example.com")
    git(repo, "config", "user.name", "Foreign")
    (repo / "app.py").write_text("print('v1')\n")
    (repo / "util.py").write_text("X = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo


def place_handoff(repo, text):
    d = repo / ".handoff"
    d.mkdir(exist_ok=True)
    (d / "HANDOFF.md").write_text(text)


PROSE = """# Handoff — auth refactor

## What was done
- Refactored app.py, everything works
- Tests all pass

## Next steps
- Wire the webhook in util.py

## Notes
Branch main, everything committed. Trust me.
"""

YAML_FM = """---
goal: Ship the auth refactor
agent: session-glue
branch: main
files: [app.py]
---

# Session summary

Refactored app.py. Next: wire the webhook in util.py.
"""

FOREIGN_JSON_EXTRA = """# Handoff

```json
{
  "goal": "Ship the auth refactor",
  "agent": "some-other-tool",
  "summary": "Refactored app.py",
  "context": {"session_id": "abc-123", "model": "unknown"},
  "tasks": [{"id": "t1", "title": "Refactor app.py", "status": "done_claimed"}],
  "files_claimed": ["app.py"],
  "verify_command": ""
}
```
"""


def main():
    cases = []

    # F1 / F2 / F3: foreign formats placed directly as the frozen handoff.
    for cid, label, text, expect_fragment in [
        ("F1", "plain prose markdown handoff (no machine-readable block)",
         PROSE, "no JSON object and no ```json fenced block found"),
        ("F2", "YAML-frontmatter handoff (Session-Glue style)",
         YAML_FM, "no JSON object and no ```json fenced block found"),
        ("F3", "json block with foreign extra top-level fields",
         FOREIGN_JSON_EXTRA, "unknown top-level fields"),
    ]:
        with tempfile.TemporaryDirectory(prefix="hr-foreign-") as tmp:
            repo = base_repo(tmp)
            place_handoff(repo, text)
            r = run_cli(repo, "resume-check")
            ok = (r.returncode == 2 and "ERROR:" in r.stderr
                  and expect_fragment in r.stderr
                  and "Traceback" not in r.stderr and "Traceback" not in r.stdout)
            cases.append({"id": cid, "label": label, "outcome": "clean-error" if ok else "UNEXPECTED",
                          "exit": r.returncode, "passed": ok,
                          "detail": r.stderr.strip()[:300]})
            print(f"{'PASS' if ok else 'FAIL'}  {cid}  {label} -> exit {r.returncode}: "
                  f"{r.stderr.strip()[:120]}")

    # F4: another agent's markdown draft using alias keys (text / file).
    with tempfile.TemporaryDirectory(prefix="hr-foreign-") as tmp:
        repo = base_repo(tmp)
        draft = {
            "goal": "Ship the auth refactor", "agent": "codex",
            "tasks": [
                {"id": "t1", "text": "Refactor app.py", "status": "done_claimed"},
                {"id": "t2", "text": "Wire the webhook", "status": "next", "file": "util.py"},
            ],
            "files_claimed": ["app.py"], "verify_command": "",
        }
        p = Path(tmp) / "draft.md"
        p.write_text("# Codex session handoff\n\n```json\n" + json.dumps(draft) + "\n```\n")
        rf = run_cli(repo, "freeze", "--draft", str(p))
        rr = run_cli(repo, "resume-check") if rf.returncode == 0 else rf
        ok = rf.returncode == 0 and rr.returncode == 0 and "OVERALL: MATCH" in rr.stdout
        cases.append({"id": "F4", "label": "codex markdown draft, alias keys (text/file)",
                      "outcome": "full-match" if ok else "UNEXPECTED",
                      "exit": rr.returncode, "passed": ok, "detail": rr.stdout.strip()[:300]})
        print(f"{'PASS' if ok else 'FAIL'}  F4  codex markdown draft (alias keys) -> "
              f"freeze {rf.returncode}, resume {rr.returncode}")

    # F5: another agent's bare-JSON draft.
    with tempfile.TemporaryDirectory(prefix="hr-foreign-") as tmp:
        repo = base_repo(tmp)
        draft = {
            "goal": "Ship the auth refactor", "agent": "cursor",
            "tasks": [{"id": "t1", "title": "Refactor app.py", "status": "done_claimed"}],
            "files_claimed": ["app.py"], "verify_command": "",
        }
        p = Path(tmp) / "draft.json"
        p.write_text(json.dumps(draft))
        rf = run_cli(repo, "freeze", "--draft", str(p))
        rr = run_cli(repo, "resume-check") if rf.returncode == 0 else rf
        ok = rf.returncode == 0 and rr.returncode == 0 and "OVERALL: MATCH" in rr.stdout
        cases.append({"id": "F5", "label": "cursor bare-JSON draft",
                      "outcome": "full-match" if ok else "UNEXPECTED",
                      "exit": rr.returncode, "passed": ok, "detail": rr.stdout.strip()[:300]})
        print(f"{'PASS' if ok else 'FAIL'}  F5  cursor bare-JSON draft -> "
              f"freeze {rf.returncode}, resume {rr.returncode}")

    summary = {"cases": cases, "all_passed": all(c["passed"] for c in cases),
               "clean_errors": sum(1 for c in cases if c["outcome"] == "clean-error"),
               "full_matches": sum(1 for c in cases if c["outcome"] == "full-match")}
    out = ROOT / "benchmark" / "foreign-results.json"
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nForeign: {summary['clean_errors']} clean loud errors, "
          f"{summary['full_matches']} full cross-agent matches, 0 silent misreads")
    print(f"Wrote {out}")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
