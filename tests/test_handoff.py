"""Fixture corpus: scripted session stories in REAL temp git repos.

Each test builds a fresh repo, freezes a handoff, mutates ground truth in a
scripted way, and asserts the objective expected verdict of resume-check.
Run:  python -m unittest discover -s tests   (or: python run_tests.py)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_cli(repo, *argv, expect=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "handoff_reconciler", "--repo", str(repo), *argv],
                       capture_output=True, text=True, env=env, timeout=60)
    if expect is not None:
        assert r.returncode == expect, f"{argv} -> {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


class Story(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hr-fixture-")
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "fixture@example.com")
        git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "app.py").write_text("print('v1')\n")
        (self.repo / "util.py").write_text("X = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "initial")

    def draft(self, **overrides):
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
        p = Path(self.tmp) / "draft.json"
        p.write_text(json.dumps(d))
        return p

    def freeze(self, **overrides):
        return run_cli(self.repo, "freeze", "--draft", str(self.draft(**overrides)), expect=0)

    def commit_change(self, name, content, msg):
        (self.repo / name).write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)

    # (1) perfect-match resume
    def test_01_perfect_match(self):
        self.freeze()
        r = run_cli(self.repo, "resume-check", expect=0)
        self.assertIn("OVERALL: MATCH", r.stdout)
        self.assertIn("[MATCH] file app.py", r.stdout)

    # (2) HEAD advanced by human commits (unrelated file)
    def test_02_head_advanced(self):
        self.freeze()
        self.commit_change("notes.txt", "human work\n", "human commit 1")
        self.commit_change("notes.txt", "more human work\n", "human commit 2")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("OVERALL: DRIFT", r.stdout)
        self.assertIn("2 commit(s) since freeze", r.stdout)
        self.assertIn("[MATCH] file app.py", r.stdout)

    # (2b) HEAD advanced by a commit touching a claimed file
    def test_02b_claimed_file_changed_by_commit(self):
        self.freeze()
        self.commit_change("app.py", "print('v2')\n", "someone rewrote app.py")
        r = run_cli(self.repo, "resume-check", expect=1)
        # INTENTIONAL CHANGE (fix pass, findings #2/#14): the per-file check is
        # now a freeze-time content-hash comparison, so the wording leads with
        # "content differs from freeze"; the commit cause is kept as a suffix.
        self.assertIn("[DRIFT] file app.py -- content differs from freeze; "
                      "changed by commits after freeze", r.stdout)

    # (3) claimed-finished file dirty
    def test_03_claimed_file_dirty(self):
        self.freeze()
        (self.repo / "app.py").write_text("print('uncommitted edit')\n")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] file app.py", r.stdout)
        self.assertIn("dirty", r.stdout)

    # (4) claimed file deleted
    def test_04_claimed_file_deleted(self):
        self.freeze()
        (self.repo / "app.py").unlink()
        r = run_cli(self.repo, "resume-check", expect=2)
        self.assertIn("OVERALL: BROKEN", r.stdout)
        self.assertIn("[BROKEN] file app.py", r.stdout)

    # (5) verify command: not-run by default; green then red with --run-verify
    def test_05_verify_command(self):
        ok = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
        self.freeze(verify_command=ok)
        r = run_cli(self.repo, "resume-check", expect=0)
        self.assertIn("[NOT-RUN] verify command", r.stdout)
        r = run_cli(self.repo, "resume-check", "--run-verify", expect=0)
        self.assertIn("About to run verify command", r.stdout)
        self.assertIn("[MATCH] verify command", r.stdout)
        # now make it red: replace the frozen verify command via a fresh freeze
        bad = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
        self.freeze(verify_command=bad)
        r = run_cli(self.repo, "resume-check", "--run-verify", expect=2)
        self.assertIn("[BROKEN] verify command -- exit 3", r.stdout)
        # and without the flag the red command is still never run
        r = run_cli(self.repo, "resume-check", expect=0)
        self.assertIn("[NOT-RUN] verify command", r.stdout)

    # (5b) shell metacharacters in other fields are data, never executed
    def test_05b_metachars_are_data(self):
        marker = Path(self.tmp) / "pwned"
        evil = f"done; touch {marker} # $(touch {marker})"
        self.freeze(goal=evil)
        r = run_cli(self.repo, "resume-check", "--run-verify", expect=0)
        self.assertFalse(marker.exists(), "metacharacters in goal were executed!")

    # (6) malformed handoffs fail loudly at freeze
    def test_06_malformed(self):
        cases = {
            "bad JSON": "{not json",
            "unknown status": None,  # built below
            "rejected without reason": None,
        }
        p = Path(self.tmp) / "bad.json"
        p.write_text(cases["bad JSON"])
        r = run_cli(self.repo, "freeze", "--draft", str(p))
        self.assertEqual(r.returncode, 2)
        self.assertIn("malformed JSON", r.stderr)
        d = json.loads(self.draft().read_text())
        d["tasks"] = [{"title": "x", "status": "maybe_done"}]
        p.write_text(json.dumps(d))
        r = run_cli(self.repo, "freeze", "--draft", str(p))
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown status", r.stderr)
        d["tasks"] = [{"title": "x", "status": "rejected"}]
        p.write_text(json.dumps(d))
        r = run_cli(self.repo, "freeze", "--draft", str(p))
        self.assertEqual(r.returncode, 2)
        self.assertIn("requires a non-empty reason", r.stderr)
        self.assertFalse((self.repo / ".handoff" / "HANDOFF.md").exists())

    # (7) branch switch
    def test_07_branch_switch(self):
        self.freeze()
        git(self.repo, "checkout", "-qb", "feature-x")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] branch changed -- main -> feature-x", r.stdout)

    # (8) freeze in a non-git directory fails clearly, writes nothing
    def test_08_freeze_non_git_fails(self):
        plain = Path(self.tmp) / "plain"
        plain.mkdir()
        r = run_cli(plain, "freeze", "--draft", str(self.draft()))
        self.assertEqual(r.returncode, 2)
        self.assertIn("not inside a git working tree", r.stderr)
        self.assertFalse((plain / ".handoff").exists())

    # (9) absolute / .. paths rejected
    def test_09_bad_paths_rejected(self):
        for bad in ["/etc/passwd", "../secret.py", "a/../../secret.py", "C:\\win\\x.py"]:
            r = run_cli(self.repo, "freeze", "--draft",
                        str(self.draft(files_claimed=[bad])))
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("path", r.stderr)
        r = run_cli(self.repo, "freeze", "--draft",
                    str(self.draft(tasks=[{"title": "n", "status": "next",
                                           "target_file": "../outside.py"}])))
        self.assertEqual(r.returncode, 2)

    # (10) next-action target deleted
    def test_10_next_target_deleted(self):
        self.freeze()
        (self.repo / "util.py").unlink()
        r = run_cli(self.repo, "resume-check", expect=2)
        self.assertIn("[BROKEN] next-action target util.py", r.stdout)
        self.assertIn("Next actions that survive:", r.stdout)
        self.assertNotIn("- Wire the webhook in util.py", r.stdout)

    # extras: markdown draft input + show + read-only guarantee
    def test_11_markdown_draft_and_show(self):
        d = json.loads(self.draft().read_text())
        md = Path(self.tmp) / "draft.md"
        md.write_text("# Draft\n\n```json\n" + json.dumps(d) + "\n```\n")
        run_cli(self.repo, "freeze", "--draft", str(md), expect=0)
        self.assertTrue((self.repo / ".handoff" / "HANDOFF.md").exists())
        r = run_cli(self.repo, "show", expect=0)
        self.assertIn("Ship the auth refactor", r.stdout)

    def test_12_resume_check_is_read_only(self):
        self.freeze()
        before = git(self.repo, "status", "--porcelain")
        run_cli(self.repo, "resume-check", expect=0)
        after = git(self.repo, "status", "--porcelain")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
