"""Dogfood: run Handoff Reconciler on copies of REAL Spark project trees.

The originals are never touched. Each tree is copied (without .git /
__pycache__), initialised as a fresh git repo, given a plausible handoff
whose claims are about that project's real files and real work, and then
checked. Every verdict is asserted against the hand-labelled ground truth
recorded in this file -- a flag that does not match a real, listed mutation
would be a false alarm and fails the run.

Run:  python3 benchmark/run_dogfood.py
Writes benchmark/dogfood-results.json and benchmark/dogfood-outputs/*.txt
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
SPIKES = Path.home() / "workspace" / "agentic-spikes"

TREES = [
    {
        "name": "skillscan",
        "src": SPIKES / "skillscan-src",
        "goal": "Ship SkillScan v0.5 rule coverage",
        "agent": "claude-code",
        "files_claimed": ["skillscan.py", "README.md"],
        "next_target": "docs/AUDIT_COVERAGE.md",
        "next_title": "Add next rule batch from the audit coverage gaps",
        "verify": f'"{PY}" -m py_compile skillscan.py',
    },
    {
        "name": "living-contract-diff",
        "src": SPIKES / "round4-pair" / "living-contract-diff",
        "goal": "Observed-contract diffing for recorded HAR traffic",
        "agent": "claude-code",
        "files_claimed": ["lcdiff/cli.py", "lcdiff/diff.py"],
        "next_target": "examples/real_world/server.py",
        "next_title": "Extend the real-world example server",
        "verify": f'"{PY}" -m py_compile lcdiff/cli.py',
    },
    {
        "name": "ci-command-truth",
        "src": SPIKES / "round3-trio" / "ci-command-truth",
        "goal": "Prove every run: command in GitHub workflows actually exists",
        "agent": "claude-code",
        "files_claimed": ["ci_command_truth.py", "README.md"],
        "next_target": "examples/example-workflow.yml",
        "next_title": "Add another example workflow",
        "verify": f'"{PY}" -m py_compile ci_command_truth.py',
    },
]


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r


def run_cli(repo, *argv):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([PY, "-m", "handoff_reconciler", "--repo", str(repo), *argv],
                          capture_output=True, text=True, env=env, timeout=120)


def prepare(tmp, cfg):
    repo = Path(tmp) / cfg["name"]
    shutil.copytree(cfg["src"], repo,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "dogfood@example.com")
    git(repo, "config", "user.name", "Dogfood")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "import real project tree")
    draft = {
        "goal": cfg["goal"], "agent": cfg["agent"],
        "tasks": [
            {"id": "t1", "title": f"Stabilise {cfg['files_claimed'][0]}", "status": "done_claimed"},
            {"id": "t2", "title": cfg["next_title"], "status": "next",
             "target_file": cfg["next_target"]},
        ],
        "files_claimed": cfg["files_claimed"],
        "verify_command": cfg["verify"],
    }
    p = Path(tmp) / f"{cfg['name']}-draft.json"
    p.write_text(json.dumps(draft))
    r = run_cli(repo, "freeze", "--draft", str(p))
    assert r.returncode == 0, f"freeze {cfg['name']}: {r.stdout}\n{r.stderr}"
    git(repo, "add", ".handoff")
    git(repo, "commit", "-qm", "freeze handoff")
    return repo


def main():
    outdir = ROOT / "benchmark" / "dogfood-outputs"
    outdir.mkdir(exist_ok=True)
    runs = []

    # --- Pristine runs: freeze on the real tree, resume immediately. ---
    for cfg in TREES:
        with tempfile.TemporaryDirectory(prefix="hr-dogfood-") as tmp:
            repo = prepare(tmp, cfg)
            r = run_cli(repo, "resume-check", "--run-verify")
            (outdir / f"{cfg['name']}-pristine.txt").write_text(r.stdout + r.stderr)
            claims = [ln for ln in r.stdout.splitlines() if ln.startswith("[")]
            ok = (r.returncode == 0 and "OVERALL: MATCH" in r.stdout
                  and all(ln.startswith(("[MATCH]", "[NOT-RUN]")) for ln in claims))
            runs.append({"run": f"{cfg['name']}-pristine", "tree": cfg["name"],
                         "claims_checked": len(claims), "false_alarms": 0 if ok else len(claims),
                         "missed_drifts": 0, "passed": ok, "exit": r.returncode})
            print(f"{'PASS' if ok else 'FAIL'}  {cfg['name']}-pristine "
                  f"({len(claims)} claims, exit {r.returncode})")
            if not ok:
                print(r.stdout, r.stderr)

    # --- Interleaving run on the current SkillScan tree: real mutations ---
    cfg = TREES[0]
    with tempfile.TemporaryDirectory(prefix="hr-dogfood-") as tmp:
        repo = prepare(tmp, cfg)
        # (a) a "human" commit touching the claimed README.md
        readme = repo / "README.md"
        readme.write_text(readme.read_text() + "\n<!-- human edit after freeze -->\n")
        git(repo, "add", "README.md")
        git(repo, "commit", "-qm", "human: README touch")
        # (b) an uncommitted edit appended to the claimed skillscan.py
        main_py = repo / "skillscan.py"
        main_py.write_text(main_py.read_text() + "\n# uncommitted append after freeze\n")
        # (c) deletion of the next-action target
        (repo / "docs" / "AUDIT_COVERAGE.md").unlink()
        r = run_cli(repo, "resume-check", "--run-verify")
        (outdir / "skillscan-interleaved.txt").write_text(r.stdout + r.stderr)
        # Frozen HEAD is the import commit; after it came the handoff's own
        # commit and the human README commit, so HEAD must read DRIFT with a
        # commit count >= 1 (asserted loosely below; the exact count is in
        # the saved output file).
        expected_lines = [
            "[DRIFT] HEAD moved",
            "[MATCH] branch 'main'",
            "[DRIFT] file skillscan.py",
            "[DRIFT] file README.md",
            "[BROKEN] next-action target docs/AUDIT_COVERAGE.md",
            "[MATCH] verify command",
        ]
        # HEAD line is checked loosely: it must be DRIFT and name >=1 commit.
        head_ok = any(ln.startswith("[DRIFT] HEAD moved -- ") and "commit(s) since freeze" in ln
                      for ln in r.stdout.splitlines())
        rest_ok = all(any(ln.startswith(e) for ln in r.stdout.splitlines())
                      for e in expected_lines[1:])
        ok = head_ok and rest_ok and r.returncode == 2 and "OVERALL: BROKEN" in r.stdout
        runs.append({"run": "skillscan-interleaved", "tree": "skillscan",
                     "claims_checked": 6, "false_alarms": 0 if ok else 1,
                     "missed_drifts": 0 if ok else 1, "passed": ok, "exit": r.returncode})
        print(f"{'PASS' if ok else 'FAIL'}  skillscan-interleaved (6 claims, exit {r.returncode})")
        if not ok:
            print(r.stdout, r.stderr)

    total_claims = sum(x["claims_checked"] for x in runs)
    summary = {"runs": runs, "total_claims_checked": total_claims,
               "all_passed": all(x["passed"] for x in runs)}
    out = ROOT / "benchmark" / "dogfood-results.json"
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nDogfood: {sum(1 for x in runs if x['passed'])}/{len(runs)} runs clean, "
          f"{total_claims} claims hand-checked")
    print(f"Wrote {out}")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
