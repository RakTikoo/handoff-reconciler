"""Handoff Reconciler core + CLI. Stdlib only, deterministic, no LLM.

Commands:
  freeze       validate a draft handoff, fill the git snapshot, write .handoff/HANDOFF.md
  resume-check READ-ONLY reconciliation of the frozen handoff against git ground truth
  show         print a short summary of the frozen handoff

Exit codes (resume-check): 0 = all MATCH, 1 = DRIFT present, 2 = BROKEN or tool error.
Exit codes (freeze/show):  0 = ok, 2 = validation / tool error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

STATUSES = {"done_claimed", "decided", "rejected", "next", "on_hold"}
MATCH, DRIFT, BROKEN, NOTRUN = "MATCH", "DRIFT", "BROKEN", "NOT-RUN"
_SEVERITY = {MATCH: 0, NOTRUN: 0, DRIFT: 1, BROKEN: 2}
_HEAD_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_DEPTH = 100
_VERIFY_TAIL_BYTES = 4096
_VERIFY_TAIL_LINES = 20
_VERIFY_CAPTURE_BYTES = 65536


class HandoffError(Exception):
    pass


# ---------------------------------------------------------------- validation
def validate_rel_path(value, field):
    """Canonicalise and validate a relative path claim.

    - './x' normalises to 'x'; inner '.' components are removed ('a/./b' -> 'a/b').
    - '.' alone (or e.g. './.') is rejected: it is a directory, not a file claim.
    - Absolute paths, '..' components and empty components are rejected.
    """
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f"{field}: path must be a non-empty string, got {value!r}")
    if "\x00" in value:
        raise HandoffError(f"{field}: path contains NUL: {value!r}")
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise HandoffError(f"{field}: absolute path rejected: {value!r}")
    parts = re.split(r"[/\\]", value)
    if any(p == ".." for p in parts):
        raise HandoffError(f"{field}: path containing '..' rejected: {value!r}")
    if any(p == "" for p in parts):
        raise HandoffError(f"{field}: path contains an empty component: {value!r}")
    filtered = [p for p in parts if p != "."]
    if not filtered:
        raise HandoffError(f"{field}: path '.' is not a file claim (it is the repo directory): {value!r}")
    return "/".join(filtered)


def _validate_git_snapshot(g, files_claimed):
    if not isinstance(g, dict):
        raise HandoffError("git: snapshot must be an object")
    allowed_git = {"branch", "head", "dirty_files", "file_hashes"}
    unknown = set(g) - allowed_git
    if unknown:
        raise HandoffError(f"git: unknown fields in snapshot: {sorted(unknown)}")
    head = g.get("head")
    if not isinstance(head, str) or not _HEAD_RE.match(head):
        raise HandoffError(
            f"git.head: must be a full commit hash (40-64 lowercase hex chars), got {head!r}")
    branch = g.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        raise HandoffError(f"git.branch: must be a non-empty string, got {branch!r}")
    if "\x00" in branch or "\n" in branch or branch.startswith("-"):
        raise HandoffError(f"git.branch: invalid branch value: {branch!r}")
    dirty = g.get("dirty_files")
    if not isinstance(dirty, list) or any(not isinstance(x, str) for x in dirty):
        raise HandoffError("git.dirty_files: must be a list of strings")
    if any("\x00" in x for x in dirty):
        raise HandoffError("git.dirty_files: entries must not contain NUL")
    out = {"branch": branch, "head": head, "dirty_files": list(dirty)}
    if "file_hashes" in g:
        fh = g["file_hashes"]
        if not isinstance(fh, dict):
            raise HandoffError("git.file_hashes: must be an object mapping path -> sha256 hex")
        for k, v in fh.items():
            if not isinstance(k, str) or not isinstance(v, str) or not _SHA256_RE.match(v):
                raise HandoffError(
                    f"git.file_hashes[{k!r}]: must map a path string to a sha256 hex string")
        # Keys are canonicalised the same way as files_claimed.
        canon = {}
        for k, v in fh.items():
            canon[validate_rel_path(k, "git.file_hashes key")] = v
        if set(canon) != set(files_claimed):
            raise HandoffError(
                "git.file_hashes: keys must exactly match files_claimed "
                f"(hashes for {sorted(canon)}, claims {sorted(files_claimed)})")
        out["file_hashes"] = canon
    return out


def validate_handoff(data, *, fill_defaults=False):
    """Strictly validate a handoff dict. Returns a normalised copy."""
    if not isinstance(data, dict):
        raise HandoffError("handoff must be a JSON object")
    allowed = {"goal", "tasks", "files_claimed", "verify_command", "frozen_at",
               "agent", "git"}
    unknown = set(data) - allowed
    if unknown:
        raise HandoffError(f"unknown top-level fields: {sorted(unknown)}")
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise HandoffError("goal: required non-empty string")
    agent = data.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        raise HandoffError("agent: required non-empty string (agent name)")
    tasks_in = data.get("tasks", [])
    if not isinstance(tasks_in, list):
        raise HandoffError("tasks: must be a list")
    tasks = []
    seen_ids = set()
    for i, t in enumerate(tasks_in):
        if not isinstance(t, dict):
            raise HandoffError(f"tasks[{i}]: must be an object")
        t_unknown = set(t) - {"id", "title", "text", "status", "reason",
                              "target_file", "file"}
        if t_unknown:
            raise HandoffError(f"tasks[{i}]: unknown fields: {sorted(t_unknown)}")
        title = t.get("title", t.get("text"))
        if not isinstance(title, str) or not title.strip():
            raise HandoffError(f"tasks[{i}]: title/text required non-empty string")
        status = t.get("status")
        if status not in STATUSES:
            raise HandoffError(
                f"tasks[{i}]: unknown status {status!r}; allowed: {sorted(STATUSES)}")
        # reason is type-checked whenever present, not only for rejected tasks.
        if "reason" in t and t["reason"] is not None and not isinstance(t["reason"], str):
            raise HandoffError(f"tasks[{i}]: reason must be a string, got {t['reason']!r}")
        reason = t.get("reason") or ""
        if not isinstance(reason, str):
            raise HandoffError(f"tasks[{i}]: reason must be a string, got {reason!r}")
        if status == "rejected" and not reason.strip():
            raise HandoffError(f"tasks[{i}]: status=rejected requires a non-empty reason")
        task = {"title": title, "status": status}
        if t.get("id") is not None:
            if not isinstance(t["id"], str) or not t["id"].strip():
                raise HandoffError(f"tasks[{i}]: id must be a non-empty string")
            if t["id"] in seen_ids:
                raise HandoffError(f"tasks[{i}]: duplicate task id {t['id']!r}")
            seen_ids.add(t["id"])
            task["id"] = t["id"]
        if reason:
            task["reason"] = reason
        target = t.get("target_file", t.get("file"))
        if target is not None:
            task["target_file"] = validate_rel_path(target, f"tasks[{i}].target_file")
        tasks.append(task)
    files_in = data.get("files_claimed", [])
    if not isinstance(files_in, list):
        raise HandoffError("files_claimed: must be a list of relative paths")
    files = [validate_rel_path(p, f"files_claimed[{i}]") for i, p in enumerate(files_in)]
    if len(set(files)) != len(files):
        dupes = sorted({p for p in files if files.count(p) > 1})
        raise HandoffError(f"files_claimed: duplicate entries: {dupes}")
    verify = data.get("verify_command", "")
    if not isinstance(verify, str):
        raise HandoffError("verify_command: must be a single string (may be empty)")
    if not verify.strip():
        verify = ""
    frozen_at = data.get("frozen_at")
    if frozen_at is None:
        if not fill_defaults:
            raise HandoffError("frozen_at: required (ISO 8601)")
        frozen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not isinstance(frozen_at, str):
        raise HandoffError("frozen_at: must be an ISO 8601 string")
    if "T" not in frozen_at and "t" not in frozen_at:
        raise HandoffError(
            f"frozen_at: must be a full timestamp with a time component "
            f"(bare dates are not accepted): {frozen_at!r}")
    try:
        datetime.fromisoformat(frozen_at.replace("Z", "+00:00"))
    except ValueError:
        raise HandoffError(f"frozen_at: not valid ISO 8601: {frozen_at!r}")
    out = {"goal": goal, "tasks": tasks, "files_claimed": files,
           "verify_command": verify, "frozen_at": frozen_at, "agent": agent}
    if "git" in data:
        out["git"] = _validate_git_snapshot(data["git"], files)
    return out


def _check_json_depth(data, source):
    """Iterative depth guard (avoids recursion in the guard itself)."""
    stack = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_JSON_DEPTH:
            raise HandoffError(f"{source}: JSON nesting too deep (limit {_MAX_JSON_DEPTH})")
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)


def extract_json(text, source):
    """Accept a bare JSON document or markdown containing exactly one ```json block."""
    stripped = text.strip()
    if stripped.startswith("{"):
        raw = stripped
    else:
        fence_re = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
        matches = list(fence_re.finditer(text))
        json_blocks = [m for m in matches if m.group(1).strip().lower() == "json"]
        if len(json_blocks) > 1:
            raise HandoffError(
                f"{source}: found {len(json_blocks)} ```json fenced blocks; "
                f"keep exactly one ```json block in the document")
        if len(json_blocks) == 1:
            raw = json_blocks[0].group(2)
        elif matches:
            infos = ", ".join(repr(m.group(1).strip() or "(no language)") for m in matches)
            raise HandoffError(
                f"{source}: found a fenced block ({infos}) but it isn't plain json; "
                f"use a single ```json fenced block")
        else:
            raise HandoffError(f"{source}: no JSON object and no ```json fenced block found")
    try:
        data = json.loads(raw)
    except RecursionError:
        raise HandoffError(f"{source}: JSON nesting too deep (parser recursion limit)")
    except json.JSONDecodeError as e:
        raise HandoffError(f"{source}: malformed JSON: {e}")
    _check_json_depth(data, source)
    return data


# ---------------------------------------------------------------- git helpers
def _git_env():
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        env.pop(k, None)
    return env


def _git(repo, *args):
    """Run git with an argv list only -- handoff field values are never shell input.

    Deterministic decoding (utf-8/replace) and ground-truth env hygiene:
    GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE from the caller's environment are
    stripped so they cannot redirect the ground-truth queries.
    FileNotFoundError (git missing) and TimeoutExpired propagate to main(),
    which maps them to a clean ERROR + exit 2.
    """
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30, env=_git_env())


def git_root(repo):
    r = _git(repo, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        raise HandoffError(f"not inside a git working tree: {repo} "
                           f"({r.stderr.strip() or 'git rev-parse failed'})")
    return Path(r.stdout.strip())


def _current_head(root):
    r = _git(root, "rev-parse", "HEAD")
    if r.returncode != 0:
        raise HandoffError("git repo has no HEAD commit yet (unborn HEAD); "
                           "commit something before freezing / checking")
    return r.stdout.strip()


def _current_branch(root):
    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if r.returncode != 0:
        raise HandoffError(f"git rev-parse --abbrev-ref failed: {r.stderr.strip()}")
    branch = r.stdout.strip()
    if branch == "HEAD":  # detached
        branch = "DETACHED"
    return branch


def git_snapshot(root):
    head = _current_head(root)
    branch = _current_branch(root)
    return {"branch": branch, "head": head,
            "dirty_files": dirty_files(root)}


def _parse_status_z(stdout):
    """Parse `git status --porcelain=v1 -z` output.

    Returns (dirty_paths_sorted, rename_map) where rename_map maps the OLD
    path to the NEW path. With -z, a rename entry is 'XY <new>\\0<old>\\0'
    (destination first, source as the following bare field).
    """
    dirty = []
    renames = {}
    tokens = stdout.split("\x00")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        if len(tok) < 4:
            continue
        xy, path = tok[:2], tok[3:]
        if "R" in xy or "C" in xy:
            old = tokens[i] if i < len(tokens) else ""
            i += 1
            if old:
                renames[old] = path
            dirty.append(path)
        else:
            dirty.append(path)
    return sorted(dirty), renames


def _status(root):
    r = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if r.returncode != 0:
        raise HandoffError(f"git status failed: {r.stderr.strip()}")
    return _parse_status_z(r.stdout)


def dirty_files(root):
    dirty, _ = _status(root)
    return dirty


def _is_ancestor(root, anc, desc):
    r = _git(root, "merge-base", "--is-ancestor", anc, desc)
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    raise HandoffError(f"git merge-base --is-ancestor failed: {r.stderr.strip()}")


def _commits_between(root, old, new):
    r = _git(root, "rev-list", "--count", f"{old}..{new}")
    if r.returncode != 0:
        raise HandoffError(f"git rev-list failed: {r.stderr.strip()}")
    return int(r.stdout.strip())


def _files_changed_between(root, old, new):
    """Net tree diff between two commits. Errors PROPAGATE (never silent)."""
    r = _git(root, "diff", "--name-only", "-z", old, new)
    if r.returncode != 0:
        raise HandoffError(
            f"git diff between {old[:12]} and {new[:12]} failed: {r.stderr.strip()}")
    return {p for p in r.stdout.split("\x00") if p}


def _renames_between(root, old, new):
    """Committed renames old->new via `git diff --name-status -z -M`.

    With -z the layout is '<status>\\0<old>\\0<new>\\0' for R/C entries and
    '<status>\\0<path>\\0' otherwise. Errors propagate.
    """
    r = _git(root, "diff", "--name-status", "-z", "-M", old, new)
    if r.returncode != 0:
        raise HandoffError(
            f"git diff --name-status between {old[:12]} and {new[:12]} failed: "
            f"{r.stderr.strip()}")
    renames = {}
    tokens = r.stdout.split("\x00")
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if not status:
            continue
        if status[0] in ("R", "C"):
            old_p = tokens[i] if i < len(tokens) else ""
            new_p = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
            if old_p and new_p:
                renames[old_p] = new_p
        else:
            i += 1  # skip the single path field
    return renames


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- rendering
def render_markdown(h):
    lines = ["# Session Handoff", "",
             "Machine-readable handoff (do not edit the JSON block by hand; re-freeze instead):",
             "", "```json", json.dumps(h, indent=2, sort_keys=True), "```", "",
             f"**Goal:** {h['goal']}", "",
             f"**Agent:** {h['agent']}  ", f"**Frozen at:** {h['frozen_at']}", ""]
    g = h.get("git", {})
    if g:
        lines += [f"**Git at freeze:** branch `{g.get('branch')}`, HEAD `{g.get('head')}`", ""]
    lines += ["## Tasks", ""]
    for t in h["tasks"]:
        suffix = f" — reason: {t['reason']}" if t.get("reason") else ""
        target = f" (target: `{t['target_file']}`)" if t.get("target_file") else ""
        lines.append(f"- [{t['status']}] {t['title']}{suffix}{target}")
    lines += ["", "## Files claimed", ""]
    lines += [f"- `{p}`" for p in h["files_claimed"]] or ["- (none)"]
    lines += ["", "## Verify command", "",
              f"`{h['verify_command']}`" if h["verify_command"] else "(none recorded)", ""]
    return "\n".join(lines)


def load_handoff(root):
    path = root / ".handoff" / "HANDOFF.md"
    if not path.exists():
        raise HandoffError(f"no frozen handoff found at {path}")
    data = extract_json(path.read_text(encoding="utf-8"), str(path))
    h = validate_handoff(data)
    g = h.get("git")
    if not isinstance(g, dict) or not g.get("head") or not g.get("branch"):
        raise HandoffError("handoff has no git snapshot; it was not written by `freeze`")
    return h


# ---------------------------------------------------------------- commands
def _check_no_symlink_escape(root, target_dir, target, prev):
    for p, label in ((target_dir, ".handoff"), (target, ".handoff/HANDOFF.md"),
                     (prev, ".handoff/HANDOFF.prev.md")):
        if p.is_symlink():
            raise HandoffError(f"refusing to freeze: {label} is a symlink ({p}); "
                               f"freeze will not follow symlinks out of the repo")
        if p.exists():
            resolved = p.resolve()
            root_resolved = root.resolve()
            if resolved != root_resolved and root_resolved not in resolved.parents:
                raise HandoffError(f"refusing to freeze: {label} resolves outside the "
                                   f"repo root ({resolved})")


def cmd_freeze(args):
    repo = Path(args.repo).resolve()
    root = git_root(repo)  # fails clearly in a non-git directory; nothing written
    draft_path = Path(args.draft)
    if not draft_path.exists():
        raise HandoffError(f"draft not found: {draft_path}")
    data = extract_json(draft_path.read_text(encoding="utf-8"), str(draft_path))
    if "git" in data:
        raise HandoffError("draft must not contain a 'git' snapshot; the tool fills it at freeze time")
    h = validate_handoff(data, fill_defaults=True)
    h["git"] = git_snapshot(root)  # first: unborn HEAD etc. fail before file checks
    # Claimed files must exist as regular files at freeze time: freeze records a
    # sha256 content hash for each, and a claim about a missing/directory path
    # can never be reconciled honestly. Missing claimed file = freeze ERROR.
    file_hashes = {}
    for p in h["files_claimed"]:
        fp = root / p
        if fp.is_dir():
            raise HandoffError(f"files_claimed: claimed path is a directory at freeze time: {p!r}")
        if not fp.exists():
            raise HandoffError(f"files_claimed: claimed file does not exist at freeze time: {p!r}")
        if not fp.is_file():
            raise HandoffError(f"files_claimed: claimed path is not a regular file at freeze time: {p!r}")
        file_hashes[p] = _file_sha256(fp)
    for t in h["tasks"]:
        tp = t.get("target_file")
        if tp and (root / tp).is_dir():
            raise HandoffError(f"tasks: target_file is a directory at freeze time: {tp!r}")
    h["git"]["file_hashes"] = file_hashes
    target_dir = root / ".handoff"
    target = target_dir / "HANDOFF.md"
    prev = target_dir / "HANDOFF.prev.md"
    _check_no_symlink_escape(root, target_dir, target, prev)
    target_dir.mkdir(exist_ok=True)
    _check_no_symlink_escape(root, target_dir, target, prev)
    # Atomic write: temp file in the same directory + os.replace; the previous
    # handoff is preserved as HANDOFF.prev.md before the new one lands.
    content = render_markdown(h)
    fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), prefix=".HANDOFF.tmp.", suffix=".md")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if target.exists():
            os.replace(target, prev)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    print(f"froze handoff -> {target}")
    print(f"branch={h['git']['branch']} head={h['git']['head'][:12]} "
          f"dirty_at_freeze={len(h['git']['dirty_files'])} tasks={len(h['tasks'])} "
          f"files_claimed={len(h['files_claimed'])}")
    return 0


def _tail_text(data_bytes):
    """Bounded tail for reports: last 4KB, last 20 lines."""
    tail = data_bytes[-_VERIFY_TAIL_BYTES:]
    text = tail.decode("utf-8", errors="replace")
    lines = text.splitlines()[-_VERIFY_TAIL_LINES:]
    return "\n".join(lines).strip()


def _run_verify(command, cwd, timeout):
    """Run the (explicitly opted-in) verify command with bounded capture.

    - Runs in its own process group (start_new_session); on timeout the whole
      group is SIGKILLed, not just the shell.
    - Output goes to temp files, and only the last 64KB of each stream is ever
      read back into memory, so a noisy command cannot OOM the checker.
    Returns (timed_out, returncode, stdout_tail_bytes, stderr_tail_bytes).
    """
    with tempfile.TemporaryFile() as outf, tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(command, shell=True, cwd=str(cwd),
                                stdout=outf, stderr=errf, start_new_session=True)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            proc.wait()

        def _read_tail(f):
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _VERIFY_CAPTURE_BYTES))
            return f.read()

        return timed_out, proc.returncode, _read_tail(outf), _read_tail(errf)


def cmd_resume_check(args):
    # Validate timeout range even when verify will not run (usage error, exit 2).
    if not (1 <= args.verify_timeout <= 3600):
        raise HandoffError(f"--verify-timeout must be between 1 and 3600 seconds, "
                           f"got {args.verify_timeout}")
    root = git_root(Path(args.repo).resolve())
    h = load_handoff(root)
    snap = h["git"]
    snap_dirty = set(snap.get("dirty_files", []))
    file_hashes = snap.get("file_hashes", {})
    results = []  # (claim, verdict, detail)

    # HEAD (ancestry-aware, both directions)
    cur_head = _current_head(root)
    changed = set()
    committed_renames = {}
    if cur_head != snap["head"]:
        changed = _files_changed_between(root, snap["head"], cur_head)
        committed_renames = _renames_between(root, snap["head"], cur_head)
    if cur_head == snap["head"]:
        results.append(("HEAD unchanged", MATCH, cur_head[:12]))
    else:
        forward = _is_ancestor(root, snap["head"], cur_head)
        if forward:
            n = _commits_between(root, snap["head"], cur_head)
            if changed and all(p == ".handoff" or p.startswith(".handoff/")
                                for p in changed):
                results.append(("HEAD moved (handoff commit only)", MATCH,
                                f"{n} commit(s) since freeze touch only .handoff/ "
                                f"({snap['head'][:12]} -> {cur_head[:12]}); "
                                f"informational: this is the handoff's own commit"))
            else:
                results.append(("HEAD moved", DRIFT, f"{n} commit(s) since freeze "
                                f"({snap['head'][:12]} -> {cur_head[:12]})"))
        elif _is_ancestor(root, cur_head, snap["head"]):
            n = _commits_between(root, cur_head, snap["head"])
            results.append(("HEAD moved", DRIFT,
                            f"HEAD moved backward: current HEAD is {n} commit(s) behind "
                            f"the frozen HEAD (rollback) "
                            f"({snap['head'][:12]} -> {cur_head[:12]})"))
        else:
            results.append(("HEAD moved", BROKEN,
                            "history rewritten / frozen commit not an ancestor of "
                            "current HEAD (and current HEAD is not an ancestor of the "
                            "frozen commit)"))
    # Branch
    cur_branch = _current_branch(root)
    if cur_branch == snap["branch"]:
        results.append((f"branch '{snap['branch']}'", MATCH, "unchanged"))
    else:
        results.append(("branch changed", DRIFT, f"{snap['branch']} -> {cur_branch}"))
    # Working-tree state
    dirty_list, status_renames = _status(root)
    dirty = set(dirty_list)
    rename_map = dict(committed_renames)
    rename_map.update(status_renames)
    # Per claimed file: the freeze-time content hash is the baseline.
    for p in h["files_claimed"]:
        fp = root / p
        if not fp.exists():
            new = rename_map.get(p)
            if new and (root / new).exists():
                results.append((f"file {p}", DRIFT, f"renamed to {new}"))
            else:
                results.append((f"file {p}", BROKEN, "claimed file is missing/deleted"))
            continue
        if fp.is_dir():
            results.append((f"file {p}", BROKEN, "claimed file is now a directory"))
            continue
        frozen_hash = file_hashes.get(p)
        if frozen_hash is not None:
            try:
                cur_hash = _file_sha256(fp)
            except OSError as e:
                results.append((f"file {p}", BROKEN, f"claimed file could not be read: {e}"))
                continue
            if cur_hash == frozen_hash:
                results.append((f"file {p}", MATCH,
                                "present, content unchanged since freeze (content hash matches)"))
            else:
                reasons = ["content differs from freeze"]
                if p in dirty:
                    reasons.append("working tree is dirty in a file claimed finished")
                if p in changed:
                    reasons.append("changed by commits after freeze")
                if p in snap_dirty and p not in dirty and p not in changed:
                    reasons.append("was dirty at freeze but is now clean with different "
                                   "content — work present at freeze may have been lost")
                elif p in snap_dirty:
                    reasons.append("was already dirty at freeze and its content has "
                                   "changed since freeze")
                results.append((f"file {p}", DRIFT, "; ".join(reasons)))
        else:
            # Legacy handoff without hashes: fall back to git state only.
            if p in dirty:
                results.append((f"file {p}", DRIFT, "working tree is dirty in a file claimed finished"))
            elif p in changed:
                results.append((f"file {p}", DRIFT, "content differs from freeze; changed by commits after freeze"))
            else:
                results.append((f"file {p}", MATCH, "present, clean, unchanged since freeze"))
    # Next-action targets
    for t in h["tasks"]:
        if t["status"] == "next" and t.get("target_file"):
            tp = t["target_file"]
            if (root / tp).exists():
                results.append((f"next-action target {tp}", MATCH, f"still exists (task: {t['title']})"))
            else:
                new = rename_map.get(tp)
                if new and (root / new).exists():
                    results.append((f"next-action target {tp}", DRIFT,
                                    f"renamed to {new} (task: {t['title']})"))
                else:
                    results.append((f"next-action target {tp}", BROKEN,
                                    f"target no longer exists (task: {t['title']})"))
    # Verify command -- NEVER run by default
    verify = h["verify_command"]
    if not verify or not verify.strip():
        results.append(("verify command", NOTRUN, "no verify_command recorded in handoff"))
    elif not args.run_verify:
        results.append(("verify command", NOTRUN,
                        "not run (pass --run-verify to run it); command is data, not executed"))
    else:
        print(f"About to run verify command from handoff: {verify}")
        print("(This command came from the handoff file; running it only because --run-verify was given.)")
        timed_out, rc, out_tail, err_tail = _run_verify(verify, root, args.verify_timeout)
        if timed_out:
            detail = f"timed out after {args.verify_timeout}s: {verify}"
            tail = _tail_text(err_tail) or _tail_text(out_tail)
            if tail:
                detail += f"\n--- verify output tail ---\n{tail}\n--- end tail ---"
            results.append(("verify command", BROKEN, detail))
        elif rc == 0:
            results.append(("verify command", MATCH, f"exit 0: {verify}"))
        else:
            detail = f"exit {rc}: {verify}"
            tail = _tail_text(err_tail) or _tail_text(out_tail)
            if tail:
                detail += f"\n--- verify output tail ---\n{tail}\n--- end tail ---"
            results.append(("verify command", BROKEN, detail))

    overall = max((v for _, v, _ in results), key=lambda v: _SEVERITY[v], default=MATCH)
    print("# Handoff drift report")
    print(f"Goal: {h['goal']}")
    print(f"Frozen: {h['frozen_at']} by {h['agent']} on branch {snap['branch']} @ {snap['head'][:12]}")
    print()
    for claim, verdict, detail in results:
        print(f"[{verdict}] {claim} -- {detail}")
    print()
    print(f"OVERALL: {overall}")
    print()
    print("## Corrected resume pack")
    still_true = [c for c, v, _ in results if v in (MATCH, NOTRUN)]
    drifted = [(c, d) for c, v, d in results if v == DRIFT]
    broken = [(c, d) for c, v, d in results if v == BROKEN]
    print("Still true:")
    for c in still_true:
        print(f"- {c}")
    if not still_true:
        print("- (nothing verified as still true)")
    print("Drifted (usable with care):")
    for c, d in drifted:
        print(f"- {c}: {d}")
    if not drifted:
        print("- (none)")
    print("Broken (do not trust the claim):")
    for c, d in broken:
        print(f"- {c}: {d}")
    if not broken:
        print("- (none)")
    surviving_next = [t["title"] for t in h["tasks"] if t["status"] == "next"
                      and (not t.get("target_file") or (root / t["target_file"]).exists())]
    print("Next actions that survive:")
    for title in surviving_next:
        print(f"- {title}")
    if not surviving_next:
        print("- (none)")
    return _SEVERITY[overall]


def cmd_show(args):
    root = git_root(Path(args.repo).resolve())
    h = load_handoff(root)
    g = h["git"]
    print(f"Goal: {h['goal']}")
    print(f"Agent: {h['agent']} | Frozen: {h['frozen_at']}")
    print(f"Git at freeze: branch {g['branch']} @ {g['head'][:12]}, "
          f"{len(g.get('dirty_files', []))} dirty file(s) at freeze")
    counts = {}
    for t in h["tasks"]:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    print("Tasks: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"))
    print(f"Files claimed: {len(h['files_claimed'])} | Verify: {h['verify_command'] or '(none)'}")
    return 0


def _verify_timeout_type(value):
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--verify-timeout must be an integer, got {value!r}")
    if not (1 <= ivalue <= 3600):
        raise argparse.ArgumentTypeError(
            f"--verify-timeout must be between 1 and 3600 seconds, got {ivalue}")
    return ivalue


def main(argv=None):
    p = argparse.ArgumentParser(prog="handoff_reconciler",
                                description="Freeze and verify AI coding-session handoffs against git ground truth.")
    p.add_argument("--repo", default=".", help="repo directory (default: current directory)")
    sub = p.add_subparsers(dest="command", required=True)
    pf = sub.add_parser("freeze", help="validate a draft and write .handoff/HANDOFF.md")
    pf.add_argument("--draft", required=True, help="draft handoff: JSON file or markdown with a ```json block")
    pf.set_defaults(func=cmd_freeze)
    pr = sub.add_parser("resume-check", help="read-only drift report vs current git state")
    pr.add_argument("--run-verify", action="store_true",
                    help="actually run the handoff's verify_command (default: never run it)")
    pr.add_argument("--verify-timeout", type=_verify_timeout_type, default=120,
                    help="verify command timeout in seconds, 1..3600 (default 120)")
    pr.set_defaults(func=cmd_resume_check)
    ps = sub.add_parser("show", help="print a summary of the frozen handoff")
    ps.set_defaults(func=cmd_show)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except HandoffError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired,
            RecursionError, TypeError, ValueError) as e:
        # Error discipline: tool/environment failures are a clean ERROR + exit 2,
        # never a traceback. (UnicodeDecodeError is a ValueError subclass; it is
        # listed explicitly for clarity.)
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
