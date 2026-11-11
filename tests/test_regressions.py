"""Regression tests for the adversarial-review fix pass.

Every test here maps to a numbered reviewer finding (see ANALYSIS.md,
"Adversarial review & fix pass") and would have FAILED on the pre-fix code.
Same harness style as test_handoff.py: real temp git repos, real CLI.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_cli(repo, *argv, expect=None, extra_env=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([sys.executable, "-m", "handoff_reconciler", "--repo", str(repo), *argv],
                       capture_output=True, text=True, env=env, timeout=60)
    if expect is not None:
        assert r.returncode == expect, f"{argv} -> {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


class Regression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hr-regr-")
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

    def draft_md(self, text):
        p = Path(self.tmp) / "draft.md"
        p.write_text(text)
        return p

    def freeze(self, **overrides):
        return run_cli(self.repo, "freeze", "--draft", str(self.draft(**overrides)), expect=0)

    def tamper(self, mutator):
        hp = self.repo / ".handoff" / "HANDOFF.md"
        text = hp.read_text()
        m = re.search(r"```json\n(.*?)```", text, re.DOTALL)
        data = json.loads(m.group(1))
        mutator(data)
        hp.write_text("# Session Handoff\n\n```json\n" + json.dumps(data, indent=2) + "\n```\n")

    def assert_clean_error(self, r, code=2):
        self.assertEqual(r.returncode, code, f"{r.stdout}\n{r.stderr}")
        self.assertIn("ERROR", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("Traceback", r.stdout)

    # ---- #1 git argv injection + strict snapshot validation
    def test_r01_git_argv_injection_head_rejected(self):
        self.freeze()
        evil = Path(self.tmp) / "injected.txt"
        self.tamper(lambda d: d["git"].__setitem__("head", f"--output={evil}"))
        r = run_cli(self.repo, "resume-check")
        self.assert_clean_error(r)
        self.assertIn("git.head", r.stderr)
        self.assertFalse(evil.exists(), "git argv injection wrote a file!")

    def test_r01b_snapshot_unknown_field_and_bad_types_rejected(self):
        self.freeze()
        self.tamper(lambda d: d["git"].__setitem__("surprise", "x"))
        self.assert_clean_error(run_cli(self.repo, "resume-check"))
        self.freeze()
        self.tamper(lambda d: d["git"].__setitem__("dirty_files", "app.py"))
        self.assert_clean_error(run_cli(self.repo, "resume-check"))
        self.freeze()
        self.tamper(lambda d: d["git"].__setitem__("branch", 42))
        self.assert_clean_error(run_cli(self.repo, "resume-check"))

    # ---- #2 content hashes
    def test_r02_dirty_at_freeze_then_reverted_is_drift(self):
        (self.repo / "app.py").write_text("print('work in progress')\n")  # dirty at freeze
        self.freeze()
        git(self.repo, "checkout", "--", "app.py")  # work silently reverted/lost
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] file app.py", r.stdout)
        self.assertIn("content differs from freeze", r.stdout)
        self.assertIn("may have been lost", r.stdout)

    def test_r02b_gitignored_claimed_file_rewritten_is_drift(self):
        (self.repo / ".gitignore").write_text("ignored.log\n")
        (self.repo / "ignored.log").write_text("original\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-qm", "ignore file")
        self.freeze(files_claimed=["ignored.log"])
        (self.repo / "ignored.log").write_text("rewritten\n")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] file ignored.log", r.stdout)

    def test_r02c_assume_unchanged_overwrite_is_drift(self):
        self.freeze()
        git(self.repo, "update-index", "--assume-unchanged", "app.py")
        (self.repo / "app.py").write_text("print('overwritten invisibly')\n")
        # git status reports nothing for app.py, but the hash must catch it
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] file app.py", r.stdout)

    def test_r02d_missing_claimed_file_at_freeze_errors(self):
        r = run_cli(self.repo, "freeze", "--draft",
                    str(self.draft(files_claimed=["nope.py"])))
        self.assert_clean_error(r)
        self.assertIn("does not exist at freeze", r.stderr)
        self.assertFalse((self.repo / ".handoff" / "HANDOFF.md").exists())

    # ---- #3 git -z parsing
    def test_r03_filename_with_spaces_changed_by_commit(self):
        (self.repo / "my file.py").write_text("A = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "spaced file")
        self.freeze(files_claimed=["my file.py"])
        r = run_cli(self.repo, "resume-check", expect=0)
        self.assertIn("[MATCH] file my file.py", r.stdout)
        (self.repo / "my file.py").write_text("A = 2\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "change spaced file")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] file my file.py", r.stdout)

    def test_r03b_file_in_untracked_directory_drifts(self):
        sub = self.repo / "newdir"
        sub.mkdir()
        (sub / "inner.py").write_text("N = 1\n")  # untracked, inside a new dir
        self.freeze(files_claimed=["newdir/inner.py"])
        (sub / "inner.py").write_text("N = 2\n")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] file newdir/inner.py", r.stdout)

    # ---- #4 path canonicalisation
    def test_r04_dot_slash_claim_normalised(self):
        self.freeze(files_claimed=["./app.py"])
        r = run_cli(self.repo, "resume-check", expect=0)
        self.assertIn("[MATCH] file app.py", r.stdout)

    def test_r04b_dot_claim_rejected(self):
        r = run_cli(self.repo, "freeze", "--draft", str(self.draft(files_claimed=["."])))
        self.assert_clean_error(r)

    def test_r04c_directory_claim_rejected_at_freeze(self):
        (self.repo / "somedir").mkdir()
        r = run_cli(self.repo, "freeze", "--draft",
                    str(self.draft(files_claimed=["somedir"])))
        self.assert_clean_error(r)
        self.assertIn("directory", r.stderr)

    # ---- #5 ancestry
    def test_r05_history_rewrite_is_broken(self):
        self.freeze()
        (self.repo / "app.py").write_text("print('rewritten root')\n")
        git(self.repo, "add", "app.py")
        git(self.repo, "commit", "--amend", "-qm", "rewritten initial")
        r = run_cli(self.repo, "resume-check", expect=2)
        self.assertIn("[BROKEN] HEAD moved", r.stdout)
        self.assertIn("history rewritten", r.stdout)

    def test_r05b_head_moved_backward_is_drift(self):
        (self.repo / "notes.txt").write_text("second commit\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "second")
        self.freeze()  # frozen HEAD = second commit
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("HEAD moved backward", r.stdout)

    # ---- #6 dirty-at-freeze baseline
    def test_r06_dirty_at_freeze_unchanged_is_match(self):
        (self.repo / "app.py").write_text("print('dirty but finished')\n")
        self.freeze()
        r = run_cli(self.repo, "resume-check", expect=0)
        self.assertIn("OVERALL: MATCH", r.stdout)
        self.assertIn("[MATCH] file app.py", r.stdout)

    # ---- #7 handoff's own commit
    def test_r07_handoff_commit_only_is_match(self):
        self.freeze()
        git(self.repo, "add", ".handoff/HANDOFF.md")
        git(self.repo, "commit", "-qm", "handoff")
        r = run_cli(self.repo, "resume-check", expect=0)
        self.assertIn("OVERALL: MATCH", r.stdout)
        self.assertIn("only .handoff/", r.stdout)

    # ---- #8 symlink escape
    def test_r08_symlinked_handoff_dir_refused(self):
        outside = Path(self.tmp) / "outside"
        outside.mkdir()
        sentinel = outside / "HANDOFF.md"
        sentinel.write_text("SENTINEL - do not overwrite\n")
        os.symlink(outside, self.repo / ".handoff")
        r = run_cli(self.repo, "freeze", "--draft", str(self.draft()))
        self.assert_clean_error(r)
        self.assertIn("symlink", r.stderr)
        self.assertEqual(sentinel.read_text(), "SENTINEL - do not overwrite\n")

    # ---- #9 error discipline
    def test_r09_handoff_md_is_directory_clean_error(self):
        self.freeze()
        hp = self.repo / ".handoff" / "HANDOFF.md"
        hp.unlink()
        hp.mkdir()
        self.assert_clean_error(run_cli(self.repo, "resume-check"))
        self.assert_clean_error(run_cli(self.repo, "show"))

    def test_r09b_deeply_nested_json_clean_error(self):
        deep = Path(self.tmp) / "deep.json"
        deep.write_text('{"a":' * 200 + "1" + "}" * 200)
        r = run_cli(self.repo, "freeze", "--draft", str(deep))
        self.assert_clean_error(r)
        self.assertIn("too deep", r.stderr)

    def test_r09c_invalid_utf8_draft_clean_error(self):
        bad = Path(self.tmp) / "bad.json"
        bad.write_bytes(b"\xff\xfe{not utf-8")
        r = run_cli(self.repo, "freeze", "--draft", str(bad))
        self.assert_clean_error(r)

    # ---- #10 renames
    def test_r10_committed_rename_is_drift_not_broken(self):
        self.freeze()
        git(self.repo, "mv", "app.py", "renamed.py")
        git(self.repo, "commit", "-qm", "rename app")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] file app.py -- renamed to renamed.py", r.stdout)

    def test_r10b_staged_rename_is_drift_not_broken(self):
        self.freeze()
        git(self.repo, "mv", "app.py", "renamed.py")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] file app.py -- renamed to renamed.py", r.stdout)

    def test_r10c_next_target_renamed_is_drift(self):
        self.freeze()
        git(self.repo, "mv", "util.py", "renamed_util.py")
        git(self.repo, "commit", "-qm", "rename util")
        r = run_cli(self.repo, "resume-check", expect=1)
        self.assertIn("[DRIFT] next-action target util.py -- renamed to renamed_util.py",
                      r.stdout)

    # ---- #11 verify command
    def test_r11_verify_failure_includes_output_tail(self):
        cmd = (f'"{sys.executable}" -c "import sys; '
               f"sys.stderr.write('BOOM-MARKER\\n'); sys.exit(3)\"")
        self.freeze(verify_command=cmd)
        r = run_cli(self.repo, "resume-check", "--run-verify", expect=2)
        self.assertIn("[BROKEN] verify command -- exit 3", r.stdout)
        self.assertIn("BOOM-MARKER", r.stdout)
        self.assertIn("verify output tail", r.stdout)

    def test_r11b_verify_timeout_kills_process_group(self):
        marker = Path(self.tmp) / "child-survived.txt"
        spawner = self.repo / "spawner.py"
        spawner.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', \"import time, pathlib; "
            f"time.sleep(2.5); pathlib.Path({str(marker)!r}).write_text('x')\"])\n"
            "time.sleep(30)\n")
        self.freeze(verify_command=f'"{sys.executable}" spawner.py')
        r = run_cli(self.repo, "resume-check", "--run-verify",
                    "--verify-timeout", "1", expect=2)
        self.assertIn("timed out after 1s", r.stdout)
        time.sleep(3.5)
        self.assertFalse(marker.exists(), "timeout left a grandchild process alive!")

    def test_r11c_verify_timeout_out_of_range_is_usage_error(self):
        self.freeze()
        for bad in ("0", "3601", "-5", "abc"):
            r = run_cli(self.repo, "resume-check", "--verify-timeout", bad)
            self.assertEqual(r.returncode, 2, bad)
            self.assertNotIn("Traceback", r.stderr)

    def test_r11d_whitespace_verify_command_treated_as_empty(self):
        self.freeze(verify_command="   ")
        r = run_cli(self.repo, "resume-check", "--run-verify", expect=0)
        self.assertIn("[NOT-RUN] verify command", r.stdout)
        self.assertNotIn("About to run", r.stdout)

    # ---- #12 markdown drafts
    def test_r12_multiple_json_blocks_rejected(self):
        d = json.loads(self.draft().read_text())
        block = "```json\n" + json.dumps(d) + "\n```\n"
        r = run_cli(self.repo, "freeze", "--draft",
                    str(self.draft_md("# Draft\n\n" + block + "\n" + block)))
        self.assert_clean_error(r)
        self.assertIn("exactly one", r.stderr)

    def test_r12b_uppercase_json_fence_accepted(self):
        d = json.loads(self.draft().read_text())
        md = self.draft_md("# Draft\n\n```JSON\n" + json.dumps(d) + "\n```\n")
        run_cli(self.repo, "freeze", "--draft", str(md), expect=0)

    def test_r12c_jsonc_fence_rejected_with_explanation(self):
        d = json.loads(self.draft().read_text())
        md = self.draft_md("# Draft\n\n```jsonc\n" + json.dumps(d) + "\n```\n")
        r = run_cli(self.repo, "freeze", "--draft", str(md))
        self.assert_clean_error(r)
        self.assertIn("plain json", r.stderr)

    # ---- #13 duplicates / types
    def test_r13_duplicate_files_claimed_rejected(self):
        for files in (["app.py", "app.py"], ["app.py", "./app.py"]):
            r = run_cli(self.repo, "freeze", "--draft",
                        str(self.draft(files_claimed=files)))
            self.assert_clean_error(r)
            self.assertIn("duplicate", r.stderr)

    def test_r13b_duplicate_task_ids_rejected(self):
        tasks = [{"id": "t1", "title": "a", "status": "next"},
                 {"id": "t1", "title": "b", "status": "next"}]
        r = run_cli(self.repo, "freeze", "--draft", str(self.draft(tasks=tasks)))
        self.assert_clean_error(r)
        self.assertIn("duplicate task id", r.stderr)

    def test_r13c_reason_wrong_type_rejected_for_any_status(self):
        tasks = [{"id": "t1", "title": "a", "status": "done_claimed", "reason": 123}]
        r = run_cli(self.repo, "freeze", "--draft", str(self.draft(tasks=tasks)))
        self.assert_clean_error(r)
        self.assertIn("reason must be a string", r.stderr)

    # ---- #14 diff errors never silently MATCH
    def test_r14_nonexistent_frozen_commit_is_tool_error_not_match(self):
        self.freeze()
        self.tamper(lambda d: d["git"].__setitem__("head", "a" * 40))
        r = run_cli(self.repo, "resume-check")
        self.assert_clean_error(r)
        self.assertNotIn("[MATCH] file app.py", r.stdout)

    # ---- #15 re-freeze preserves previous + atomic
    def test_r15_refreeze_preserves_previous(self):
        self.freeze()
        self.freeze(goal="Second goal")
        prev = self.repo / ".handoff" / "HANDOFF.prev.md"
        cur = self.repo / ".handoff" / "HANDOFF.md"
        self.assertTrue(prev.exists())
        self.assertIn("Ship the auth refactor", prev.read_text())
        self.assertIn("Second goal", cur.read_text())
        leftovers = list((self.repo / ".handoff").glob(".HANDOFF.tmp.*"))
        self.assertEqual(leftovers, [])

    # ---- LOW: unborn HEAD, bare-date frozen_at, git env hygiene
    def test_r16_unborn_head_clean_error(self):
        empty = Path(self.tmp) / "empty"
        empty.mkdir()
        git(empty, "init", "-q", "-b", "main")
        r = run_cli(empty, "freeze", "--draft", str(self.draft()))
        self.assert_clean_error(r)
        self.assertIn("no HEAD", r.stderr)

    def test_r17_bare_date_frozen_at_rejected(self):
        r = run_cli(self.repo, "freeze", "--draft",
                    str(self.draft(frozen_at="2026-11-10")))
        self.assert_clean_error(r)
        self.assertIn("frozen_at", r.stderr)
        self.assertIn("time component", r.stderr)

    def test_r18_git_dir_env_does_not_redirect_ground_truth(self):
        bogus = str(Path(self.tmp) / "bogus-git-dir")
        r = run_cli(self.repo, "freeze", "--draft", str(self.draft()),
                    expect=0, extra_env={"GIT_DIR": bogus})
        self.assertIn("froze handoff", r.stdout)


if __name__ == "__main__":
    unittest.main()
