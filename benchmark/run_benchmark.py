"""Planted-drift corpus benchmark for Handoff Reconciler.

Every scenario builds a REAL temporary git repository, freezes a handoff
through the real CLI, then either leaves ground truth alone (accurate
handoffs -- any DRIFT/BROKEN there is a false alarm) or plants exactly one
labelled kind of drift, or crafts exactly one false claim directly into the
handoff file (simulating an agent whose summary was never true).

Scoring is objective: a scenario passes only if resume-check's overall
verdict, its process exit code, AND its key per-claim line all match the
hand-labelled expectation recorded here before the run.

Run:  python3 benchmark/run_benchmark.py
Writes benchmark/results.json and prints a per-kind table.
Stdlib only, deterministic (fixed timestamps are not needed: verdicts do
not depend on them).
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run_cli(repo, *argv):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([PY, "-m", "handoff_reconciler", "--repo", str(repo), *argv],
                          capture_output=True, text=True, env=env, timeout=60)


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check:
        assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r


class Repo:
    def __init__(self, tmp):
        self.dir = Path(tmp) / "repo"
        self.dir.mkdir(parents=True)
        git(self.dir, "init", "-q", "-b", "main")
        git(self.dir, "config", "user.email", "bench@example.com")
        git(self.dir, "config", "user.name", "Bench")
        (self.dir / "app.py").write_text("print('v1')\n")
        (self.dir / "util.py").write_text("X = 1\n")
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-qm", "initial")

    def draft(self, tmp, **overrides):
        d = {
            "goal": "Ship the auth refactor",
            "agent": "claude-code",
            "tasks": [
                {"id": "t1", "title": "Refactor app.py", "status": "done_claimed"},
                {"id": "t2", "title": "Drop the legacy helper", "status": "rejected",
                 "reason": "still imported by util.py"},
                {"id": "t3", "title": "Wire the webhook in util.py", "status": "next",
                 "target_file": "util.py"},
            ],
            "files_claimed": ["app.py"],
            "verify_command": "",
        }
        d.update(overrides)
        p = Path(tmp) / "draft.json"
        p.write_text(json.dumps(d))
        return p

    def freeze(self, tmp, **overrides):
        r = run_cli(self.dir, "freeze", "--draft", str(self.draft(tmp, **overrides)))
        assert r.returncode == 0, f"freeze failed: {r.stdout}\n{r.stderr}"
        return r

    def commit_file(self, name, content, msg):
        (self.dir / name).write_text(content)
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-qm", msg)

    def handoff_json(self):
        text = (self.dir / ".handoff" / "HANDOFF.md").read_text()
        block = text.split("```json", 1)[1].split("```", 1)[0]
        return json.loads(block)

    def rewrite_handoff_json(self, data):
        text = (self.dir / ".handoff" / "HANDOFF.md").read_text()
        head, rest = text.split("```json", 1)
        _, tail = rest.split("```", 1)
        new = head + "```json\n" + json.dumps(data, indent=2, sort_keys=True) + "\n```" + tail
        (self.dir / ".handoff" / "HANDOFF.md").write_text(new)


# ---------------------------------------------------------------- scenarios
# Each scenario: (id, kind, label, setup_fn, run_verify, expected_overall,
#                 expected_exit, must_contain)
# expected_overall "ERROR" means a clean tool ERROR on stderr, exit 2.

def s_pristine(repo, tmp):
    repo.freeze(tmp)


def s_multi_claimed(repo, tmp):
    repo.freeze(tmp, files_claimed=["app.py", "util.py"],
                tasks=[{"id": "t1", "title": "Refactor app.py", "status": "done_claimed"},
                       {"id": "t2", "title": "Drop the legacy helper", "status": "rejected",
                        "reason": "still imported by util.py"},
                       {"id": "t3", "title": "Docs pass", "status": "next"}])


def s_handoff_commit_only(repo, tmp):
    repo.freeze(tmp)
    git(repo.dir, "add", ".handoff")
    git(repo.dir, "commit", "-qm", "freeze handoff")


def s_dirty_at_freeze_unchanged(repo, tmp):
    (repo.dir / "app.py").write_text("print('work in progress, frozen as-is')\n")
    repo.freeze(tmp)


def s_verify_passes(repo, tmp):
    repo.freeze(tmp, verify_command=f'"{PY}" -c "import sys; sys.exit(0)"')


def s_verify_not_run_by_default(repo, tmp):
    # The command WOULD fail if run; by design it must not run without the flag.
    repo.freeze(tmp, verify_command=f'"{PY}" -c "import sys; sys.exit(3)"')


def s_subdir_claimed(repo, tmp):
    (repo.dir / "src").mkdir()
    (repo.dir / "src" / "mod.py").write_text("Y = 2\n")
    git(repo.dir, "add", "-A")
    git(repo.dir, "commit", "-qm", "add src/mod.py")
    repo.freeze(tmp, files_claimed=["src/mod.py"])


def s_feature_branch(repo, tmp):
    git(repo.dir, "checkout", "-qb", "feature-x")
    repo.freeze(tmp)


def s_no_claimed_files(repo, tmp):
    repo.freeze(tmp, files_claimed=[],
                tasks=[{"id": "t1", "title": "Plan the refactor", "status": "decided"},
                       {"id": "t2", "title": "GraphQL rewrite", "status": "on_hold"}])


def s_spaced_filename(repo, tmp):
    (repo.dir / "my file.py").write_text("Z = 3\n")
    git(repo.dir, "add", "-A")
    git(repo.dir, "commit", "-qm", "add spaced file")
    repo.freeze(tmp, files_claimed=["my file.py"])


def s_dirty_edit(repo, tmp):
    repo.freeze(tmp)
    (repo.dir / "app.py").write_text("print('uncommitted edit')\n")


def s_committed_edit(repo, tmp):
    repo.freeze(tmp)
    repo.commit_file("app.py", "print('v2')\n", "someone rewrote app.py")


def s_deleted(repo, tmp):
    repo.freeze(tmp)
    (repo.dir / "app.py").unlink()


def s_renamed_committed(repo, tmp):
    repo.freeze(tmp)
    git(repo.dir, "mv", "app.py", "main_app.py")
    git(repo.dir, "commit", "-qm", "rename app.py")


def s_renamed_staged(repo, tmp):
    repo.freeze(tmp)
    git(repo.dir, "mv", "app.py", "main_app.py")


def s_gitignored_edit(repo, tmp):
    (repo.dir / ".gitignore").write_text("ignored.log\n")
    (repo.dir / "ignored.log").write_text("line1\n")
    git(repo.dir, "add", ".gitignore")
    git(repo.dir, "commit", "-qm", "ignore the log")
    repo.freeze(tmp, files_claimed=["ignored.log"])
    (repo.dir / "ignored.log").write_text("line1\nline2 silently appended\n")


def s_assume_unchanged_edit(repo, tmp):
    repo.freeze(tmp)
    git(repo.dir, "update-index", "--assume-unchanged", "app.py")
    (repo.dir / "app.py").write_text("print('invisible to git status')\n")


def s_claimed_edit_never_happened(repo, tmp):
    repo.freeze(tmp)
    h = repo.handoff_json()
    ghost = hashlib.sha256(b"print('the edit the agent claimed but never made')\n").hexdigest()
    h["git"]["file_hashes"]["app.py"] = ghost
    repo.rewrite_handoff_json(h)


def s_one_of_two_claims_false(repo, tmp):
    repo.freeze(tmp, files_claimed=["app.py", "util.py"],
                tasks=[{"id": "t1", "title": "Refactor app.py", "status": "done_claimed"},
                       {"id": "t3", "title": "Docs pass", "status": "next"}])
    h = repo.handoff_json()
    ghost = hashlib.sha256(b"X = 999  # never actually written\n").hexdigest()
    h["git"]["file_hashes"]["util.py"] = ghost
    repo.rewrite_handoff_json(h)


def s_head_forward(repo, tmp):
    repo.freeze(tmp)
    repo.commit_file("notes.txt", "human work\n", "human commit 1")
    repo.commit_file("notes.txt", "more human work\n", "human commit 2")


def s_head_backward(repo, tmp):
    repo.commit_file("notes.txt", "extra\n", "second commit")
    repo.freeze(tmp)
    git(repo.dir, "reset", "-q", "--hard", "HEAD~1")


def s_history_rewritten(repo, tmp):
    repo.commit_file("notes.txt", "v1 notes\n", "notes commit")
    repo.freeze(tmp)
    (repo.dir / "notes.txt").write_text("v1 notes, amended\n")
    git(repo.dir, "add", "-A")
    git(repo.dir, "commit", "-q", "--amend", "-m", "notes commit (amended)")


def s_claimed_commit_missing(repo, tmp):
    repo.freeze(tmp)
    h = repo.handoff_json()
    h["git"]["head"] = "a" * 40
    repo.rewrite_handoff_json(h)


def s_branch_switched(repo, tmp):
    repo.freeze(tmp)
    git(repo.dir, "checkout", "-qb", "feature-x")


def s_branch_and_head(repo, tmp):
    repo.freeze(tmp)
    git(repo.dir, "checkout", "-qb", "feature-x")
    repo.commit_file("notes.txt", "branch work\n", "branch commit")


def s_verify_now_fails(repo, tmp):
    repo.freeze(tmp, verify_command=f'"{PY}" -c "import sys; sys.exit(3)"')


def s_verify_breaks_with_code(repo, tmp):
    repo.freeze(tmp, files_claimed=["app.py"],
                verify_command=f'"{PY}" -m py_compile util.py')
    (repo.dir / "util.py").write_text("X = (\n")  # syntax error, util.py is not claimed


def s_next_target_deleted(repo, tmp):
    repo.freeze(tmp)
    (repo.dir / "util.py").unlink()


def s_next_target_renamed(repo, tmp):
    repo.freeze(tmp)
    git(repo.dir, "mv", "util.py", "webhook.py")
    git(repo.dir, "commit", "-qm", "rename util.py")


def s_next_target_subdir_deleted(repo, tmp):
    (repo.dir / "docs").mkdir()
    (repo.dir / "docs" / "PLAN.md").write_text("# plan\n")
    git(repo.dir, "add", "-A")
    git(repo.dir, "commit", "-qm", "add plan")
    repo.freeze(tmp, tasks=[
        {"id": "t1", "title": "Refactor app.py", "status": "done_claimed"},
        {"id": "t3", "title": "Execute the plan", "status": "next",
         "target_file": "docs/PLAN.md"}])
    (repo.dir / "docs" / "PLAN.md").unlink()


SCENARIOS = [
    # --- accurate handoffs: any DRIFT/BROKEN here is a FALSE ALARM ---
    ("A01", "accurate", "pristine resume, single claimed file", s_pristine, False,
     "MATCH", 0, ["[MATCH] file app.py", "[MATCH] branch 'main'"]),
    ("A02", "accurate", "pristine resume, two claimed files", s_multi_claimed, False,
     "MATCH", 0, ["[MATCH] file app.py", "[MATCH] file util.py"]),
    ("A03", "accurate", "only the handoff itself was committed after freeze", s_handoff_commit_only, False,
     "MATCH", 0, ["[MATCH] HEAD moved (handoff commit only)"]),
    ("A04", "accurate", "file dirty at freeze, identically dirty at resume", s_dirty_at_freeze_unchanged, False,
     "MATCH", 0, ["[MATCH] file app.py"]),
    ("A05", "accurate", "verify command passes (run with --run-verify)", s_verify_passes, True,
     "MATCH", 0, ["[MATCH] verify command"]),
    ("A06", "accurate", "verify would fail but is NOT run by default", s_verify_not_run_by_default, False,
     "MATCH", 0, ["[NOT-RUN] verify command"]),
    ("A07", "accurate", "claimed file in a subdirectory, unchanged", s_subdir_claimed, False,
     "MATCH", 0, ["[MATCH] file src/mod.py"]),
    ("A08", "accurate", "frozen and resumed on the same feature branch", s_feature_branch, False,
     "MATCH", 0, ["[MATCH] branch 'feature-x'"]),
    ("A09", "accurate", "no files claimed, decided/on_hold tasks only", s_no_claimed_files, False,
     "MATCH", 0, ["OVERALL: MATCH"]),
    ("A10", "accurate", "claimed file with a space in its name, unchanged", s_spaced_filename, False,
     "MATCH", 0, ["[MATCH] file my file.py"]),
    ("A11", "accurate", "pristine resume, default draft (repeat determinism check)", s_pristine, False,
     "MATCH", 0, ["OVERALL: MATCH"]),
    ("A12", "accurate", "handoff-commit-only, two claimed files", s_multi_claimed, False,
     "MATCH", 0, ["[MATCH] file util.py"]),

    # --- planted drift: file content ---
    ("D01", "file-content", "claimed file edited, uncommitted", s_dirty_edit, False,
     "DRIFT", 1, ["[DRIFT] file app.py"]),
    ("D02", "file-content", "claimed file edited by a later commit", s_committed_edit, False,
     "DRIFT", 1, ["[DRIFT] file app.py -- content differs from freeze; changed by commits after freeze"]),
    ("D03", "file-content", "claimed file deleted", s_deleted, False,
     "BROKEN", 2, ["[BROKEN] file app.py -- claimed file is missing/deleted"]),
    ("D04", "file-content", "claimed file renamed (committed)", s_renamed_committed, False,
     "DRIFT", 1, ["[DRIFT] file app.py -- renamed to main_app.py"]),
    ("D05", "file-content", "claimed file renamed (staged git mv)", s_renamed_staged, False,
     "DRIFT", 1, ["[DRIFT] file app.py -- renamed to main_app.py"]),
    ("D06", "file-content", "gitignored claimed file silently edited", s_gitignored_edit, False,
     "DRIFT", 1, ["[DRIFT] file ignored.log"]),
    ("D07", "file-content", "assume-unchanged claimed file edited (invisible to git status)",
     s_assume_unchanged_edit, False,
     "DRIFT", 1, ["[DRIFT] file app.py"]),

    # --- planted false claims (crafted into the handoff itself) ---
    ("D08", "false-claim", "claimed file edit that never happened (hash of ghost content)",
     s_claimed_edit_never_happened, False,
     "DRIFT", 1, ["[DRIFT] file app.py -- content differs from freeze"]),
    ("D09", "false-claim", "one of two claimed files carries a false hash", s_one_of_two_claims_false, False,
     "DRIFT", 1, ["[DRIFT] file util.py", "[MATCH] file app.py"]),

    # --- planted drift: commits / git state ---
    ("D10", "commit-state", "HEAD advanced by 2 unrelated commits", s_head_forward, False,
     "DRIFT", 1, ["[DRIFT] HEAD moved -- 2 commit(s) since freeze"]),
    ("D11", "commit-state", "HEAD moved backward (rollback)", s_head_backward, False,
     "DRIFT", 1, ["HEAD moved backward"]),
    ("D12", "commit-state", "history rewritten (amended freeze commit)", s_history_rewritten, False,
     "BROKEN", 2, ["[BROKEN] HEAD moved -- history rewritten"]),
    ("D13", "commit-state", "claimed frozen commit does not exist", s_claimed_commit_missing, False,
     "ERROR", 2, []),
    ("D14", "commit-state", "branch switched after freeze", s_branch_switched, False,
     "DRIFT", 1, ["[DRIFT] branch changed -- main -> feature-x"]),
    ("D15", "commit-state", "branch switched AND HEAD advanced", s_branch_and_head, False,
     "DRIFT", 1, ["[DRIFT] branch changed", "[DRIFT] HEAD moved"]),

    # --- planted drift: verify command (claimed tests pass, they fail) ---
    ("D16", "verify", "verify command fails at resume (claimed pass is false)", s_verify_now_fails, True,
     "BROKEN", 2, ["[BROKEN] verify command -- exit 3"]),
    ("D17", "verify", "non-claimed file broken after freeze, verify catches it", s_verify_breaks_with_code, True,
     "BROKEN", 2, ["[BROKEN] verify command", "[MATCH] file app.py"]),

    # --- planted drift: next-action targets ---
    ("D18", "next-action", "next-action target deleted", s_next_target_deleted, False,
     "BROKEN", 2, ["[BROKEN] next-action target util.py"]),
    ("D19", "next-action", "next-action target renamed (committed)", s_next_target_renamed, False,
     "DRIFT", 1, ["[DRIFT] next-action target util.py -- renamed to webhook.py"]),
    ("D20", "next-action", "next-action target in a subdirectory deleted", s_next_target_subdir_deleted, False,
     "BROKEN", 2, ["[BROKEN] next-action target docs/PLAN.md"]),
]

# A12 needs the handoff-commit step as well; wrap it.
def _a12(repo, tmp):
    s_multi_claimed(repo, tmp)
    git(repo.dir, "add", ".handoff")
    git(repo.dir, "commit", "-qm", "freeze handoff")
_idx_a12 = next(i for i, s in enumerate(SCENARIOS) if s[0] == "A12")
SCENARIOS[_idx_a12] = ("A12", "accurate", "handoff-commit-only, two claimed files", _a12, False,
                       "MATCH", 0, ["[MATCH] file util.py",
                                    "[MATCH] HEAD moved (handoff commit only)"])


def main():
    results = []
    for sid, kind, label, fn, run_verify, exp_overall, exp_exit, must in SCENARIOS:
        with tempfile.TemporaryDirectory(prefix="hr-bench-") as tmp:
            repo = Repo(tmp)
            fn(repo, tmp)
            argv = ["resume-check"] + (["--run-verify"] if run_verify else [])
            r = run_cli(repo.dir, *argv)
            if exp_overall == "ERROR":
                ok = (r.returncode == exp_exit and "ERROR:" in r.stderr
                      and "Traceback" not in r.stderr and "Traceback" not in r.stdout)
                got = "ERROR"
            else:
                ok = (r.returncode == exp_exit
                      and f"OVERALL: {exp_overall}" in r.stdout
                      and all(m in r.stdout for m in must)
                      and "Traceback" not in r.stdout and "Traceback" not in r.stderr)
                got = next((ln.split("OVERALL: ")[1].strip() for ln in r.stdout.splitlines()
                            if ln.startswith("OVERALL:")), f"exit {r.returncode}")
            results.append({"id": sid, "kind": kind, "label": label,
                            "expected": exp_overall, "expected_exit": exp_exit,
                            "got": got, "exit": r.returncode, "passed": ok})
            print(f"{'PASS' if ok else 'FAIL'}  {sid}  [{kind}] {label} "
                  f"(expected {exp_overall}/{exp_exit}, got {got}/{r.returncode})")
            if not ok:
                print("----- stdout -----\n" + r.stdout + "\n----- stderr -----\n" + r.stderr)

    kinds = {}
    for res in results:
        kinds.setdefault(res["kind"], []).append(res)
    print("\nPer-kind:")
    summary = {}
    for kind, rs in sorted(kinds.items()):
        passed = sum(1 for x in rs if x["passed"])
        summary[kind] = {"total": len(rs), "passed": passed}
        print(f"  {kind}: {passed}/{len(rs)}")
    accurate = kinds.get("accurate", [])
    false_alarms = [x for x in accurate if not x["passed"]]
    drift = [x for x in results if x["kind"] != "accurate"]
    caught = [x for x in drift if x["passed"]]
    summary_out = {
        "total_scenarios": len(results),
        "accurate_total": len(accurate),
        "accurate_passed": len(accurate) - len(false_alarms),
        "false_alarm_rate": (len(false_alarms) / len(accurate)) if accurate else None,
        "drift_total": len(drift),
        "drift_caught": len(caught),
        "drift_catch_rate": (len(caught) / len(drift)) if drift else None,
        "per_kind": summary,
        "results": results,
    }
    out = ROOT / "benchmark" / "results.json"
    out.write_text(json.dumps(summary_out, indent=2) + "\n")
    print(f"\nAccurate: {len(accurate) - len(false_alarms)}/{len(accurate)} clean "
          f"(false-alarm rate {summary_out['false_alarm_rate']:.1%})")
    print(f"Drift: {len(caught)}/{len(drift)} caught ({summary_out['drift_catch_rate']:.1%})")
    print(f"Wrote {out}")
    return 0 if all(x["passed"] for x in results) else 1


if __name__ == "__main__":
    sys.exit(main())
