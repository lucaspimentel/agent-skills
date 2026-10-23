#!/usr/bin/env python3
"""Inspect, plan, and apply safe removal of linked Git worktrees and local branches.

This helper backs the explicitly invoked ``git-clean-worktrees`` skill. It is a
single importable module with a guarded command line entry point. Subprocess
execution, parsing, discovery, preservation decisions, plan validation, and
action execution live in separate, testable functions.

Three phases, always in order:

* ``inspect`` discovers one repository, refreshes the remote default branch, and
  writes a versioned snapshot with eligibility decisions and evidence.
* ``plan`` turns explicit target selections into a canonical plan and prints the
  SHA-256 digest a human must approve.
* ``apply`` revalidates everything, then performs only the approved actions.

Only Git and optionally ``gh`` are invoked, always with argument arrays and
``shell=False``. Python 3.11+ and its standard library are the only runtime
requirements. Nothing here deletes a directory recursively, unlocks or prunes a
worktree, repairs history, or mutates a remote branch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_SNAPSHOT = "git-clean-worktrees/snapshot/1"
SCHEMA_PLAN = "git-clean-worktrees/plan/1"
SCHEMA_PLAN_FILE = "git-clean-worktrees/plan-file/1"
SCHEMA_RESULT = "git-clean-worktrees/result/1"
TOOL_VERSION = "1"

DEFAULT_TIMEOUT = 60.0
DEFAULT_FETCH_TIMEOUT = 300.0
DEFAULT_GH_TIMEOUT = 90.0

# Environment variables that could redirect repository, index, configuration, or
# object lookup for the Git commands this helper runs. Clearing them keeps every
# command bound to the repository the caller named.
GIT_ENV_CLEAR_EXACT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
)
GIT_ENV_CLEAR_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

# Git's documented ordinary ``ls-files -v`` index tags. Anything else (notably
# lowercase tags for assume-unchanged and ``S``/``s`` for skip-worktree) can hide
# tracked changes and is treated as a blocker.
SAFE_LS_FILES_TAGS = frozenset("HMRCK")

# Repository-local configuration that can hide tracked or untracked changes from
# the file-protection checks. Overriding these for the checks keeps them
# independent of the repository's own configuration; this is a per-invocation
# `-c` override, not a change to global Git configuration.
FILE_STATE_CONFIG_OVERRIDES = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.ignoreStat=false",
)

IN_PROGRESS_GIT_FILES = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "AM_HEAD",
)

MAX_DIAGNOSTIC_CHARS = 300


class HelperError(Exception):
    """A global, fail-closed error. Nothing was applied."""

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.details = details


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    error: str | None = None


class SubprocessRunner:
    """Bounded, noninteractive subprocess execution with a sanitized Git env."""

    def __init__(
        self,
        env_overrides: Mapping[str, str] | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
    ):
        env = dict(os.environ)
        for key in list(env):
            if key in GIT_ENV_CLEAR_EXACT or key.startswith(GIT_ENV_CLEAR_PREFIXES):
                env.pop(key, None)
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
                "LANG": "C",
            }
        )
        if env_overrides:
            env.update(env_overrides)
        self.env = env
        self.default_timeout = default_timeout

    def run(
        self,
        args: Sequence[str],
        cwd: str | None = None,
        timeout: float | None = None,
        input_bytes: bytes | None = None,
    ) -> Result:
        argv = [str(a) for a in args]
        effective_timeout = self.default_timeout if timeout is None else timeout
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=self.env,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=effective_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return Result(
                -1,
                stdout=exc.stdout or b"",
                stderr=exc.stderr or b"",
                timed_out=True,
                error=f"command timed out after {effective_timeout:.1f}s",
            )
        except OSError as exc:
            return Result(127, stderr=str(exc).encode("utf-8", "surrogateescape"), error=str(exc))
        return Result(proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------
# Text, JSON, and path helpers
# ---------------------------------------------------------------------------


def decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def display(value: Any) -> str:
    """Render a repository-controlled string as escaped data for consoles."""
    return json.dumps(str(value), ensure_ascii=False)[1:-1]


def bounded(value: Any, limit: int = MAX_DIAGNOSTIC_CHARS) -> str:
    text = display(value).replace("\\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def canonical(path: str) -> str:
    """Case-normalized, symlink-resolved absolute identity for a path."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_within(parent: str, child: str) -> bool:
    parent_id = canonical(parent)
    child_id = canonical(child)
    if child_id == parent_id:
        return True
    return child_id.startswith(parent_id.rstrip(os.sep) + os.sep)


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def plan_digest(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(plan)).hexdigest()


def normalize_digest(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise HelperError(f"invalid plan digest: {display(value)}")
    return text


def write_json_file(path: str, obj: Any) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=True, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)


def read_json_file(path: str) -> Any:
    if not os.path.isfile(path):
        raise HelperError(f"file not found: {display(path)}")
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError as exc:
            raise HelperError(f"invalid JSON in {display(path)}: {exc}") from exc


# ---------------------------------------------------------------------------
# Git plumbing helpers
# ---------------------------------------------------------------------------


def run_git(runner: Any, args: Sequence[str], *, cwd: str | None = None, timeout: float | None = None) -> Result:
    return runner.run(["git", *args], cwd=cwd, timeout=timeout)


def run_git_checked(
    runner: Any,
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    what: str,
) -> Result:
    result = run_git(runner, args, cwd=cwd, timeout=timeout)
    if result.timed_out:
        raise HelperError(f"{what} timed out", details=bounded(result.error or ""))
    if result.returncode != 0:
        raise HelperError(f"{what} failed (exit {result.returncode}): {bounded(decode(result.stderr))}")
    return result


def git_stdout(runner: Any, args: Sequence[str], *, cwd: str | None = None, timeout: float | None = None) -> str:
    what = f"git {args[0]}"
    return decode(run_git_checked(runner, args, cwd=cwd, timeout=timeout, what=what).stdout).strip()


def absolute_git_dir(runner: Any, path: str, *, timeout: float | None = None) -> str | None:
    result = run_git(runner, ["-C", path, "rev-parse", "--absolute-git-dir"], timeout=timeout)
    if result.returncode != 0 or result.timed_out:
        return None
    text = decode(result.stdout).strip()
    return text or None


def rev_parse_commit(runner: Any, repo_root: str, ref: str, *, timeout: float | None = None) -> str | None:
    result = run_git(
        runner,
        ["-C", repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        timeout=timeout,
    )
    if result.returncode != 0 or result.timed_out:
        return None
    text = decode(result.stdout).strip()
    return text or None


# ---------------------------------------------------------------------------
# Origin identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OriginIdentity:
    host: str
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_origin_url(url: str) -> tuple[OriginIdentity | None, str | None]:
    """Resolve a Git remote URL to a GitHub repository identity.

    Returns ``(identity, None)`` on success or ``(None, reason)`` when the URL is
    not a safely resolvable github.com repository.
    """
    text = url.strip()
    if not text:
        return None, "origin URL is empty"

    host = ""
    path = ""
    scp_like = re.match(r"^(?P<user>[^/@:]+@)?(?P<host>[^/:]+):(?P<path>.+)$", text)
    if "://" in text:
        scheme, _, remainder = text.partition("://")
        if scheme.lower() not in {"http", "https", "git", "ssh"}:
            return None, f"origin URL scheme {display(scheme)} is not supported"
        if "@" in remainder.split("/", 1)[0]:
            remainder = remainder.split("@", 1)[1]
        host_part, _, path = remainder.partition("/")
        host = host_part.split(":", 1)[0]
    elif scp_like and not text.startswith("/"):
        host = scp_like.group("host")
        path = scp_like.group("path")
    else:
        return None, f"origin URL {display(text)} is not a remote repository URL"

    host = host.lower()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return None, f"origin URL path {display(path)} is not owner/repository"
    owner, repo = parts
    if not _OWNER_REPO_RE.match(owner) or not _OWNER_REPO_RE.match(repo):
        return None, f"origin URL {display(text)} is not a valid GitHub repository path"
    if host != "github.com":
        return None, f"origin host {display(host)} is not github.com"
    return OriginIdentity(host=host, owner=owner, repo=repo), None


# ---------------------------------------------------------------------------
# Repository resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Repository:
    input_path: str
    root: str
    common_dir: str
    git_dir: str
    main_branch: str
    main_head: str
    origin_url: str
    origin: OriginIdentity | None
    origin_reason: str | None


def _absolute_from(root: str, value: str) -> str:
    return os.path.abspath(os.path.join(root, value))


def resolve_repository(runner: Any, repository_path: str | None = None, *, timeout: float | None = None) -> Repository:
    """Validate that the input belongs to the main, non-bare worktree of a repo."""
    if not repository_path:
        repository_path = os.getcwd()
    if not os.path.exists(repository_path):
        raise HelperError(f"repository path does not exist: {display(repository_path)}")
    if not os.path.isdir(repository_path):
        raise HelperError(f"repository path is not a directory: {display(repository_path)}")

    top = run_git(runner, ["-C", repository_path, "rev-parse", "--show-toplevel"], timeout=timeout)
    if top.returncode != 0 or top.timed_out:
        raise HelperError(f"not a Git repository: {display(repository_path)}")
    root = os.path.realpath(_absolute_from(repository_path, decode(top.stdout).strip()))

    bare = run_git(runner, ["-C", root, "rev-parse", "--is-bare-repository"], timeout=timeout)
    if bare.returncode != 0:
        raise HelperError(f"cannot inspect repository at {display(root)}: {bounded(decode(bare.stderr))}")
    if decode(bare.stdout).strip() == "true":
        raise HelperError(f"repository is bare: {display(root)}")

    git_dir = os.path.realpath(_absolute_from(root, git_stdout(runner, ["-C", root, "rev-parse", "--git-dir"], cwd=None, timeout=timeout)))
    common_dir = os.path.realpath(
        _absolute_from(root, git_stdout(runner, ["-C", root, "rev-parse", "--git-common-dir"], cwd=None, timeout=timeout))
    )

    if canonical(git_dir) != canonical(common_dir):
        main_guess = os.path.dirname(common_dir.rstrip(os.sep)) or common_dir
        raise HelperError(
            "input resolves to a linked worktree, not the main working tree: "
            f"{display(root)}. Run the helper against the main worktree {display(main_guess)}."
        )

    head_ref = run_git(runner, ["-C", root, "symbolic-ref", "--quiet", "--short", "HEAD"], timeout=timeout)
    if head_ref.returncode != 0 or head_ref.timed_out:
        raise HelperError(f"the main worktree has a detached HEAD: {display(root)}")
    main_branch = decode(head_ref.stdout).strip()
    if not main_branch:
        raise HelperError(f"the main worktree has a detached HEAD: {display(root)}")

    main_head = rev_parse_commit(runner, root, f"refs/heads/{main_branch}", timeout=timeout)
    if main_head is None:
        raise HelperError(f"the main worktree branch {display(main_branch)} has no commits (unborn HEAD)")

    _reject_rewritten_history(runner, root, common_dir, timeout=timeout)

    origin_result = run_git(runner, ["-C", root, "config", "--get", "remote.origin.url"], timeout=timeout)
    origin_url = decode(origin_result.stdout).strip() if origin_result.returncode == 0 else ""
    if origin_result.returncode not in (0, 1):
        raise HelperError(f"cannot read the origin remote URL: {bounded(decode(origin_result.stderr))}")
    if not origin_url:
        raise HelperError("repository has no origin remote; cannot refresh the default branch")

    origin, origin_reason = parse_origin_url(origin_url)
    return Repository(
        input_path=os.path.abspath(repository_path),
        root=root,
        common_dir=common_dir,
        git_dir=git_dir,
        main_branch=main_branch,
        main_head=main_head,
        origin_url=origin_url,
        origin=origin,
        origin_reason=origin_reason,
    )


def _reject_rewritten_history(runner: Any, root: str, common_dir: str, *, timeout: float | None = None) -> None:
    shallow_path = _absolute_from(root, git_stdout(runner, ["-C", root, "rev-parse", "--git-path", "shallow"], timeout=timeout))
    if os.path.exists(shallow_path):
        raise HelperError("shallow history is not supported; ancestry proof cannot be established safely")
    for grafts in (os.path.join(common_dir, "info", "grafts"), os.path.join(root, ".git", "info", "grafts")):
        if os.path.exists(grafts):
            raise HelperError("replaced or grafted history is not supported; ancestry proof cannot be established safely")
    replacements = run_git(runner, ["-C", root, "for-each-ref", "--format=%(refname)", "refs/replace/"], timeout=timeout)
    if replacements.returncode == 0 and decode(replacements.stdout).strip():
        raise HelperError("replaced or grafted history is not supported; ancestry proof cannot be established safely")


# ---------------------------------------------------------------------------
# Remote default branch discovery and refresh
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteDefault:
    name: str
    tip: str
    fetched_ref: str
    fetched_url: str


def remote_head_name(runner: Any, repo: Repository, *, timeout: float | None = None) -> str:
    """Read the remote's symbolic HEAD through a fresh `ls-remote`, never cached refs."""
    result = run_git(runner, ["ls-remote", "--symref", "--", "origin", "HEAD"], cwd=repo.root, timeout=timeout)
    if result.timed_out:
        raise HelperError("refreshing the remote default branch timed out while reading remote HEAD")
    if result.returncode != 0:
        raise HelperError(f"cannot read the remote default branch: {bounded(decode(result.stderr))}")
    for line in decode(result.stdout).splitlines():
        if not line.startswith("ref: "):
            continue
        refname, _, target = line[len("ref: ") :].partition("\t")
        if target.strip() != "HEAD":
            continue
        if not refname.startswith("refs/heads/"):
            continue
        name = refname[len("refs/heads/") :]
        return name
    raise HelperError("the remote HEAD does not reference a branch; cannot determine the default branch")


def validate_branch_name(runner: Any, repo: Repository, name: str, *, timeout: float | None = None) -> None:
    result = run_git(runner, ["check-ref-format", f"refs/heads/{name}"], cwd=repo.root, timeout=timeout)
    if result.returncode != 0:
        raise HelperError(f"remote default branch name {display(name)} is not a valid ref")


def refresh_remote_default(
    runner: Any,
    repo: Repository,
    *,
    fetch_timeout: float | None = None,
) -> RemoteDefault:
    """Fetch the symbolic remote HEAD branch into its explicit origin ref.

    Fails closed: never falls back to stale remote data.
    """
    name = remote_head_name(runner, repo, timeout=fetch_timeout)
    validate_branch_name(runner, repo, name, timeout=fetch_timeout)
    if repo.main_branch != name:
        raise HelperError(
            f"the main worktree has {display(repo.main_branch)} checked out but the remote default branch is "
            f"{display(name)}; check out the default branch in the main worktree first"
        )

    refspec = f"+refs/heads/{name}:refs/remotes/origin/{name}"
    result = run_git(
        runner,
        [
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "fetch.showForcedUpdates=false",
            "fetch",
            "--no-tags",
            "--no-prune",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            "--no-progress",
            "--atomic",
            "--",
            "origin",
            refspec,
        ],
        cwd=repo.root,
        timeout=fetch_timeout,
    )
    if result.timed_out:
        raise HelperError("refreshing the remote default branch timed out during fetch")
    if result.returncode != 0:
        raise HelperError(f"cannot refresh the remote default branch: {bounded(decode(result.stderr))}")

    again = remote_head_name(runner, repo, timeout=fetch_timeout)
    if again != name:
        raise HelperError(
            f"the remote default branch changed during refresh ({display(name)} then {display(again)}); re-run later"
        )

    fetched_ref = f"refs/remotes/origin/{name}"
    tip = rev_parse_commit(runner, repo.root, fetched_ref, timeout=fetch_timeout)
    if tip is None:
        raise HelperError(f"the fetched default branch ref {display(fetched_ref)} does not resolve to a commit")
    return RemoteDefault(name=name, tip=tip, fetched_ref=fetched_ref, fetched_url=repo.origin_url)


# ---------------------------------------------------------------------------
# Worktree and branch inventory parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeRecord:
    path: str
    head: str | None
    branch_ref: str | None
    detached: bool
    locked: str | None
    prunable: str | None


def parse_worktree_porcelain(data: bytes) -> list[WorktreeRecord]:
    """Parse `git worktree list --porcelain -z` output."""
    records: list[WorktreeRecord] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if not current:
            return
        path = current.get("worktree")
        if path:
            records.append(
                WorktreeRecord(
                    path=path,
                    head=current.get("HEAD") or None,
                    branch_ref=current.get("branch") or None,
                    detached="detached" in current,
                    locked=current["locked"] if "locked" in current else None,
                    prunable=current["prunable"] if "prunable" in current else None,
                )
            )
        current.clear()

    for token in data.split(b"\0"):
        if token == b"":
            flush()
            continue
        text = decode(token)
        key, sep, value = text.partition(" ")
        current[key] = value if sep else ""
    flush()
    return records


def list_worktrees(runner: Any, repo: Repository, *, timeout: float | None = None) -> list[WorktreeRecord]:
    result = run_git_checked(
        runner,
        ["-C", repo.root, "worktree", "list", "--porcelain", "-z"],
        timeout=timeout,
        what="git worktree list",
    )
    return parse_worktree_porcelain(result.stdout)


@dataclass(frozen=True)
class BranchRecord:
    ref: str
    name: str
    tip: str
    object_type: str
    symbolic: str | None
    upstream: str | None


def parse_ref_records(data: bytes) -> list[BranchRecord]:
    """Parse the NUL-field, newline-record format used for `for-each-ref`."""
    records: list[BranchRecord] = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        parts = line.split(b"\0")
        if len(parts) < 5:
            continue
        ref, tip, obj_type, symbolic, upstream = (decode(part) for part in parts[:5])
        if not ref.startswith("refs/heads/"):
            continue
        records.append(
            BranchRecord(
                ref=ref,
                name=ref[len("refs/heads/") :],
                tip=tip,
                object_type=obj_type,
                symbolic=symbolic or None,
                upstream=upstream or None,
            )
        )
    return records


def list_local_branches(runner: Any, repo: Repository, *, timeout: float | None = None) -> list[BranchRecord]:
    result = run_git_checked(
        runner,
        [
            "-C",
            repo.root,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(symref)%00%(upstream)",
            "refs/heads/",
        ],
        timeout=timeout,
        what="git for-each-ref",
    )
    return parse_ref_records(result.stdout)


# ---------------------------------------------------------------------------
# Per-worktree file protections
# ---------------------------------------------------------------------------


def parse_status_v2(data: bytes) -> dict[str, list[dict[str, str]]]:
    """Parse `git status --porcelain=v2 -z` records into tracked/conflict buckets."""
    out: dict[str, list[dict[str, str]]] = {"changes": [], "conflicts": [], "unknown": []}
    for token in data.split(b"\0"):
        if not token:
            continue
        text = decode(token)
        kind = text[:1]
        if kind == "1":
            parts = text.split(" ", 8)
            if len(parts) < 9:
                out["unknown"].append({"raw": bounded(text)})
                continue
            out["changes"].append({"xy": parts[1], "path": parts[8]})
        elif kind == "2":
            parts = text.split(" ", 9)
            if len(parts) < 10:
                out["unknown"].append({"raw": bounded(text)})
                continue
            out["changes"].append({"xy": parts[1], "path": parts[9]})
        elif kind == "u":
            parts = text.split(" ", 10)
            if len(parts) < 11:
                out["unknown"].append({"raw": bounded(text)})
                continue
            out["conflicts"].append({"xy": parts[1], "path": parts[10]})
        else:
            out["unknown"].append({"raw": bounded(text)})
    return out


def parse_ls_files_verbose(data: bytes) -> tuple[list[str], list[str]]:
    """Split `ls-files -v -z` output into safe paths and flagged index tags."""
    safe: list[str] = []
    flagged: list[str] = []
    for token in data.split(b"\0"):
        if not token:
            continue
        text = decode(token)
        tag, sep, path = text.partition(" ")
        if not sep:
            flagged.append(f"unrecognized index record {bounded(text)}")
            continue
        if tag in SAFE_LS_FILES_TAGS:
            safe.append(path)
        else:
            flagged.append(f"{display(tag)} {display(path)}")
    return safe, flagged


def _git_bytes(runner: Any, args: Sequence[str], *, timeout: float | None) -> Result:
    return run_git(runner, args, cwd=None, timeout=timeout)


def inspect_file_state(
    runner: Any,
    worktree_path: str,
    git_dir: str,
    *,
    enumerate_ignored: bool,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Collect every file-level protection for one worktree.

    ``enumerate_ignored`` walks configured ignore rules and fingerprints the
    ignored entries. It can be expensive for large build or dependency
    directories, so callers only enable it for otherwise-removable worktrees.
    """
    state: dict[str, Any] = {
        "inspected": True,
        "tracked_changes": [],
        "conflicts": [],
        "untracked": [],
        "ignored": [],
        "ignored_count": 0,
        "ignored_fingerprint": None,
        "in_progress": [],
        "index_flags": [],
        "submodules": [],
        "sparse_checkout": False,
        "errors": [],
    }
    if os.path.islink(worktree_path):
        state["errors"].append("worktree path is a symlink")
        return state
    if not os.path.isdir(worktree_path):
        state["errors"].append("worktree path is not a directory")
        return state

    prefix = [*FILE_STATE_CONFIG_OVERRIDES, "--no-optional-locks", "-C", worktree_path]

    status = _git_bytes(
        runner,
        [
            *prefix,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=no",
            "--no-renames",
            "--ignore-submodules=none",
        ],
        timeout=timeout,
    )
    if status.timed_out or status.returncode != 0:
        state["errors"].append(f"git status failed: {bounded(decode(status.stderr) or status.error or '')}")
        return state
    parsed = parse_status_v2(status.stdout)
    state["tracked_changes"] = [f"{item['xy']} {display(item['path'])}" for item in parsed["changes"]]
    state["conflicts"] = [f"{item['xy']} {display(item['path'])}" for item in parsed["conflicts"]]
    for item in parsed["unknown"]:
        state["errors"].append(f"unrecognized status record: {item['raw']}")

    untracked = _git_bytes(
        runner,
        [*prefix, "ls-files", "--others", "--exclude-standard", "-z"],
        timeout=timeout,
    )
    if untracked.timed_out or untracked.returncode != 0:
        state["errors"].append(f"listing untracked files failed: {bounded(decode(untracked.stderr) or untracked.error or '')}")
    else:
        state["untracked"] = [display(path) for path in (decode(t) for t in untracked.stdout.split(b"\0")) if path]

    index = _git_bytes(
        runner,
        [*prefix, "ls-files", "-v", "-z"],
        timeout=timeout,
    )
    if index.timed_out or index.returncode != 0:
        state["errors"].append(f"reading index flags failed: {bounded(decode(index.stderr) or index.error or '')}")
    else:
        _, flagged = parse_ls_files_verbose(index.stdout)
        state["index_flags"] = flagged

    for marker in IN_PROGRESS_GIT_FILES:
        if os.path.exists(os.path.join(git_dir, marker)):
            state["in_progress"].append(marker)

    submodules = _git_bytes(
        runner,
        ["--no-optional-locks", "-C", worktree_path, "rev-parse", "--verify", "--quiet", "HEAD:.gitmodules"],
        timeout=timeout,
    )
    if submodules.returncode == 0 and not submodules.timed_out:
        status_result = _git_bytes(runner, ["-C", worktree_path, "submodule", "status"], timeout=timeout)
        if status_result.timed_out or status_result.returncode != 0:
            state["errors"].append(f"submodule status failed: {bounded(decode(status_result.stderr) or status_result.error or '')}")
        else:
            populated = [line for line in decode(status_result.stdout).splitlines() if line and not line.startswith("-")]
            state["submodules"] = [display(line) for line in populated]

    for key in ("core.sparseCheckout", "core.sparseCheckoutCone"):
        cfg = _git_bytes(runner, ["-C", worktree_path, "config", "--bool", "--get", key], timeout=timeout)
        if cfg.returncode == 0 and decode(cfg.stdout).strip() == "true":
            state["sparse_checkout"] = True
    if os.path.exists(os.path.join(git_dir, "info", "sparse-checkout")):
        state["sparse_checkout"] = True

    if enumerate_ignored and not state["errors"]:
        ignored = _git_bytes(
            runner,
            [*prefix, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            timeout=timeout,
        )
        if ignored.timed_out or ignored.returncode != 0:
            state["errors"].append(f"listing ignored files failed: {bounded(decode(ignored.stderr) or ignored.error or '')}")
        else:
            paths = [path for path in (decode(t) for t in ignored.stdout.split(b"\0")) if path]
            state["ignored"] = [display(path) for path in sorted(paths)]
            state["ignored_count"] = len(paths)
            fingerprint, errors = fingerprint_ignored(worktree_path, paths)
            state["ignored_fingerprint"] = fingerprint
            state["errors"].extend(errors)
    return state


def _aliased_parent(worktree_path: str, parts: Sequence[str]) -> str | None:
    """Return the first parent component that is a symlink, junction, or not a directory."""
    for depth in range(1, len(parts)):
        parent = os.path.join(worktree_path, *parts[:depth])
        try:
            info = os.lstat(parent)
        except OSError:
            return "/".join(parts[:depth])
        if not stat.S_ISDIR(info.st_mode):
            return "/".join(parts[:depth])
        if getattr(info, "st_reparse_tag", 0):
            return "/".join(parts[:depth])
    return None


def fingerprint_ignored(worktree_path: str, paths: Sequence[str]) -> tuple[str | None, list[str]]:
    """Hash ignored entries without following symlinks outside the worktree."""
    errors: list[str] = []
    digest = hashlib.sha256()
    for rel in sorted(paths):
        if os.path.isabs(rel) or rel.startswith("\\") or re.match(r"^[A-Za-z]:", rel):
            errors.append(f"ignored path is not worktree-relative: {display(rel)}")
            continue
        parts = rel.replace("\\", "/").split("/")
        if any(part in ("", "..") for part in parts):
            errors.append(f"ignored path escapes the worktree: {display(rel)}")
            continue
        aliased = _aliased_parent(worktree_path, parts)
        if aliased is not None:
            errors.append(f"ignored path traverses an aliased parent ({display(aliased)}): {display(rel)}")
            continue
        full = os.path.join(worktree_path, *parts)
        try:
            info = os.lstat(full)
        except OSError as exc:
            errors.append(f"cannot inspect ignored path {display(rel)}: {bounded(exc)}")
            continue
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"L")
            try:
                digest.update(os.readlink(full).encode("utf-8", "surrogateescape"))
            except OSError as exc:
                errors.append(f"cannot read symlink target {display(rel)}: {bounded(exc)}")
            digest.update(b"\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"F")
            digest.update(str(info.st_size).encode("ascii"))
            digest.update(b"\0")
            try:
                with open(full, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                errors.append(f"cannot read ignored file {display(rel)}: {bounded(exc)}")
            digest.update(b"\0")
        else:
            errors.append(f"ignored path has an unsupported file type: {display(rel)}")
    if errors:
        return None, errors
    return f"sha256:{digest.hexdigest()}", []


# ---------------------------------------------------------------------------
# Preservation evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preservation:
    proven: bool
    method: str | None
    reason: str
    evidence: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "proven": self.proven,
            "method": self.method,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def ancestry_preservation(
    runner: Any,
    repo: Repository,
    tip: str,
    default_tip: str,
    *,
    timeout: float | None = None,
) -> Preservation:
    result = run_git(
        runner,
        ["-C", repo.root, "merge-base", "--is-ancestor", tip, default_tip],
        timeout=timeout,
    )
    evidence = {"tip": tip, "default_tip": default_tip}
    if result.timed_out:
        return Preservation(False, "ancestry", "ancestry check timed out", evidence)
    if result.returncode == 0:
        return Preservation(True, "ancestry", "tip is an ancestor of the freshly fetched default tip", evidence)
    if result.returncode == 1:
        return Preservation(False, "ancestry", "tip is not an ancestor of the freshly fetched default tip", evidence)
    return Preservation(False, "ancestry", f"ancestry check errored: {bounded(decode(result.stderr))}", evidence)


def parse_json_documents(data: bytes) -> list[Any]:
    """Parse one or more concatenated JSON documents without external tools."""
    text = decode(data)
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise HelperError(f"gh returned malformed JSON: {exc}") from exc
        documents.append(obj)
        index = end
    return documents


def flatten_documents(documents: Sequence[Any]) -> list[Any]:
    flat: list[Any] = []
    for doc in documents:
        if isinstance(doc, list) and all(isinstance(item, list) for item in doc):
            for page in doc:
                flat.extend(page)
        elif isinstance(doc, list):
            flat.extend(doc)
        else:
            flat.append(doc)
    return flat


def evaluate_pr_evidence(documents: Sequence[Any], slug: str, tip: str) -> Preservation:
    """Decide whether a merged PR proves the exact tip, never via branch names."""
    entries = flatten_documents(documents)
    merged: list[dict[str, Any]] = []
    malformed: str | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            malformed = malformed or "gh returned a malformed PR entry"
            continue
        head = entry.get("head")
        base = entry.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            malformed = malformed or "gh returned a PR without head/base data"
            continue
        base_repo = base.get("repo")
        if not isinstance(base_repo, dict):
            malformed = malformed or "gh returned a PR without base repository data"
            continue
        head_sha = head.get("sha")
        merged_at = entry.get("merged_at")
        merge_sha = entry.get("merge_commit_sha")
        if not isinstance(head_sha, str):
            malformed = malformed or "gh returned a PR without a head SHA"
            continue
        full_name = base_repo.get("full_name")
        if not isinstance(full_name, str):
            malformed = malformed or "gh returned a PR without a base repository name"
            continue
        if full_name.lower() != slug.lower():
            continue
        if not merged_at or not merge_sha:
            continue
        if head_sha.lower() != tip.lower():
            continue
        number = entry.get("number")
        merged.append(
            {
                "number": number if isinstance(number, int) else None,
                "head_sha": head_sha,
                "merge_commit_sha": merge_sha,
                "merged_at": merged_at,
                "base_repository": full_name,
                "state": entry.get("state"),
                "title": entry.get("title"),
            }
        )
    if not merged:
        if malformed is not None:
            return Preservation(False, "pr", f"{malformed}; treated as unproven", {"slug": slug, "tip": tip, "checked": len(entries)})
        return Preservation(
            False,
            "pr",
            f"no merged PR in {display(slug)} records head SHA {display(tip)}",
            {"slug": slug, "tip": tip, "checked": len(entries)},
        )
    merged.sort(key=lambda item: (item["number"] is None, item["number"]))
    chosen = merged[0]
    return Preservation(
        True,
        "pr",
        f"merged PR #{chosen['number']} in {display(slug)} records head SHA {display(tip)}",
        chosen,
    )


@dataclass(frozen=True)
class PrLookup:
    documents: list[Any] | None
    error: str | None


class GhClient:
    """Minimal, noninteractive GitHub API client that never switches accounts."""

    def __init__(
        self,
        gh_path: str = "gh",
        runner: Any = None,
        *,
        host: str = "github.com",
        timeout: float | None = DEFAULT_GH_TIMEOUT,
    ):
        self.gh_path = gh_path
        self.runner = runner
        self.host = host
        self.timeout = timeout

    def list_commit_pulls(self, slug: str, sha: str) -> PrLookup:
        endpoint = f"repos/{slug}/commits/{sha}/pulls"
        base_args = [self.gh_path, "api", "--hostname", self.host, "--method", "GET"]
        result = self.runner.run([*base_args, "--paginate", "--slurp", endpoint], timeout=self.timeout)
        if result.returncode != 0 and not result.timed_out and b"slurp" in result.stderr.lower():
            result = self.runner.run([*base_args, "--paginate", endpoint], timeout=self.timeout)
        if result.timed_out:
            return PrLookup(None, "gh PR lookup timed out")
        if result.returncode != 0:
            stderr = decode(result.stderr)
            lowered = stderr.lower()
            if result.returncode == 4 or "authentication" in lowered or "http 401" in lowered or "gh auth login" in lowered:
                return PrLookup(None, f"PR lookup could not authenticate with GitHub: {bounded(stderr)}")
            return PrLookup(None, f"PR lookup failed (exit {result.returncode}): {bounded(stderr)}")
        try:
            documents = parse_json_documents(result.stdout)
        except HelperError as exc:
            return PrLookup(None, str(exc))
        return PrLookup(documents, None)


def pr_preservation(gh: GhClient | None, repo: Repository, tip: str) -> Preservation:
    evidence = {"tip": tip, "slug": repo.origin.slug if repo.origin else None}
    if repo.origin is None:
        return Preservation(
            False,
            "pr",
            f"origin cannot be resolved to a GitHub repository: {repo.origin_reason or 'unknown reason'}",
            evidence,
        )
    if gh is None:
        return Preservation(False, "pr", "no GitHub client is configured", evidence)
    lookup = gh.list_commit_pulls(repo.origin.slug, tip)
    if lookup.error is not None:
        return Preservation(False, "pr", lookup.error, evidence)
    return evaluate_pr_evidence(lookup.documents or [], repo.origin.slug, tip)


def preservation_for_tip(
    runner: Any,
    repo: Repository,
    tip: str,
    default_tip: str,
    gh: GhClient | None,
    *,
    timeout: float | None = None,
    cache: dict[str, Preservation] | None = None,
) -> Preservation:
    key = tip.lower()
    if cache is not None and key in cache:
        return cache[key]
    result = ancestry_preservation(runner, repo, tip, default_tip, timeout=timeout)
    if not result.proven:
        pr_result = pr_preservation(gh, repo, tip)
        if pr_result.proven:
            result = pr_result
        elif repo.origin is None:
            result = pr_result
        else:
            result = Preservation(
                False,
                "none",
                f"{result.reason}; {pr_result.reason}",
                {"ancestry": result.evidence, "pr": pr_result.evidence},
            )
    if cache is not None:
        cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def branch_usage_by_ref(records: Sequence[WorktreeRecord]) -> dict[str, list[str]]:
    usage: dict[str, list[str]] = {}
    for record in records:
        if record.branch_ref:
            usage.setdefault(record.branch_ref, []).append(record.path)
    return usage


def _overlaps_other(worktree_path: str, others: Sequence[str]) -> str | None:
    for other in others:
        if canonical(other) == canonical(worktree_path):
            continue
        if is_within(other, worktree_path):
            return other
    return None


def build_worktree_entry(
    runner: Any,
    repo: Repository,
    record: WorktreeRecord,
    remote: RemoteDefault,
    aliases: Mapping[str, list[str]],
    other_paths: Sequence[str],
    gh: GhClient | None,
    *,
    timeout: float | None,
    preserve_cache: dict[str, Preservation],
) -> dict[str, Any]:
    path = record.path
    is_main = canonical(path) == canonical(repo.root)
    entry: dict[str, Any] = {
        "kind": "worktree",
        "id": path,
        "canonical": canonical(path),
        "head": record.head,
        "branch_ref": record.branch_ref,
        "branch_name": (
            record.branch_ref[len("refs/heads/") :]
            if record.branch_ref and record.branch_ref.startswith("refs/heads/")
            else None
        ),
        "detached": record.detached,
        "locked": record.locked,
        "prunable": record.prunable,
        "is_main": is_main,
        "git_dir": None,
        "file_state": {"inspected": False},
        "preservation": None,
        "requires_ignored_override": False,
        "eligibility": {"eligible": False, "blocking": [], "overridable": []},
    }
    blocking: list[str] = entry["eligibility"]["blocking"]
    overridable: list[str] = entry["eligibility"]["overridable"]

    if is_main:
        blocking.append("main worktree is always retained")
    if record.prunable is not None:
        blocking.append(f"worktree registration is prunable: {bounded(record.prunable)}")
    if record.locked is not None:
        blocking.append(f"worktree is locked: {bounded(record.locked or 'no reason given')}")
    if record.detached:
        blocking.append("detached HEAD worktrees are always retained")
    if not record.branch_ref:
        blocking.append("worktree has no attached branch")
    if len(aliases.get(canonical(path), [])) > 1:
        blocking.append("worktree path aliases another registration")
    overlap = _overlaps_other(path, other_paths)
    if overlap is not None:
        blocking.append(f"worktree path is nested with another registration: {display(overlap)}")
    if not is_main and is_within(repo.root, path):
        blocking.append("worktree path is inside the main working tree")
    if not is_main and is_within(path, repo.root):
        blocking.append("worktree path contains the main working tree")
    if os.path.islink(path):
        blocking.append("worktree path is a symlink")
    if not os.path.exists(path):
        blocking.append("worktree path is missing")
    if os.path.exists(path) and not os.path.isdir(path):
        blocking.append("worktree path is not a directory")

    tip: str | None = None
    if not blocking:
        git_dir = absolute_git_dir(runner, path, timeout=timeout)
        if git_dir is None:
            blocking.append("worktree administrative directory cannot be verified")
        else:
            entry["git_dir"] = git_dir
            if canonical(git_dir) == canonical(repo.common_dir):
                blocking.append("worktree administrative directory is the common directory")
        head_now = rev_parse_commit(runner, path, "HEAD", timeout=timeout)
        if head_now is None:
            blocking.append("worktree HEAD cannot be resolved to a commit")
        elif record.head and head_now.lower() != record.head.lower():
            blocking.append("worktree HEAD does not match the registered HEAD")
        tip = rev_parse_commit(runner, repo.root, record.branch_ref, timeout=timeout) if record.branch_ref else None
        if record.branch_ref and tip is None:
            blocking.append(f"branch {display(record.branch_ref)} does not resolve to a commit")

    if not blocking:
        state = inspect_file_state(runner, path, entry["git_dir"], enumerate_ignored=True, timeout=timeout)
        entry["file_state"] = state
        for error in state["errors"]:
            blocking.append(f"file inspection error: {error}")
        for change in state["tracked_changes"]:
            blocking.append(f"staged or unstaged change: {change}")
        for conflict in state["conflicts"]:
            blocking.append(f"unresolved conflict: {conflict}")
        for untracked_path in state["untracked"]:
            blocking.append(f"untracked file: {untracked_path}")
        for marker in state["in_progress"]:
            blocking.append(f"in-progress Git operation: {display(marker)}")
        for flag in state["index_flags"]:
            blocking.append(f"index flag can hide tracked changes: {flag}")
        for submodule in state["submodules"]:
            blocking.append(f"populated submodule: {submodule}")
        if state["sparse_checkout"]:
            blocking.append("sparse-checkout state can hide tracked changes")
        if state["ignored_count"] and not state["errors"]:
            overridable.append("ignored-files")
            entry["requires_ignored_override"] = True

    if not blocking and tip is not None:
        preservation = preservation_for_tip(
            runner, repo, tip, remote.tip, gh, timeout=timeout, cache=preserve_cache
        )
        entry["preservation"] = preservation.to_json()
        if not preservation.proven:
            blocking.append(f"not preserved: {preservation.reason}")
        else:
            entry["preservation"]["tip"] = tip

    entry["eligibility"]["eligible"] = not blocking and not overridable
    return entry


def build_branch_entry(
    runner: Any,
    repo: Repository,
    record: BranchRecord,
    remote: RemoteDefault,
    usage: Mapping[str, list[str]],
    gh: GhClient | None,
    *,
    timeout: float | None,
    preserve_cache: dict[str, Preservation],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": "branch",
        "id": record.name,
        "ref": record.ref,
        "tip": record.tip,
        "object_type": record.object_type,
        "symbolic": record.symbolic,
        "upstream": record.upstream,
        "used_by_worktrees": sorted(usage.get(record.ref, [])),
        "preservation": None,
        "eligibility": {"eligible": False, "blocking": []},
    }
    blocking: list[str] = entry["eligibility"]["blocking"]
    if record.symbolic is not None:
        blocking.append(f"symbolic branch ref resolves to {display(record.symbolic)}")
    if record.object_type != "commit":
        blocking.append(f"ref points to {display(record.object_type or 'an unknown object type')}, not a commit")
    if record.name == repo.main_branch:
        blocking.append("the default branch is always retained")
    if record.ref in usage and any(canonical(path) == canonical(repo.root) for path in usage[record.ref]):
        blocking.append("checked out in the main worktree")

    if not blocking:
        preservation = preservation_for_tip(
            runner, repo, record.tip, remote.tip, gh, timeout=timeout, cache=preserve_cache
        )
        entry["preservation"] = preservation.to_json()
        if not preservation.proven:
            blocking.append(f"not preserved: {preservation.reason}")

    entry["eligibility"]["eligible"] = not blocking
    return entry


def inspect_repository(
    runner: Any,
    repository_path: str | None = None,
    *,
    gh: GhClient | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    fetch_timeout: float | None = DEFAULT_FETCH_TIMEOUT,
    gh_timeout: float | None = DEFAULT_GH_TIMEOUT,
) -> dict[str, Any]:
    """Build the snapshot for one main-worktree repository. Removes nothing."""
    repo = resolve_repository(runner, repository_path, timeout=timeout)
    remote = refresh_remote_default(runner, repo, fetch_timeout=fetch_timeout)
    if gh is None:
        gh = GhClient("gh", runner, timeout=gh_timeout)

    records = list_worktrees(runner, repo, timeout=timeout)
    branches = list_local_branches(runner, repo, timeout=timeout)

    aliases: dict[str, list[str]] = {}
    for record in records:
        aliases.setdefault(canonical(record.path), []).append(record.path)
    other_paths = [record.path for record in records]
    usage = branch_usage_by_ref(records)
    preserve_cache: dict[str, Preservation] = {}

    worktree_entries = [
        build_worktree_entry(
            runner, repo, record, remote, aliases, other_paths, gh,
            timeout=timeout, preserve_cache=preserve_cache,
        )
        for record in sorted(records, key=lambda item: item.path)
    ]
    branch_entries = [
        build_branch_entry(
            runner, repo, record, remote, usage, gh,
            timeout=timeout, preserve_cache=preserve_cache,
        )
        for record in sorted(branches, key=lambda item: item.ref)
    ]

    origin_json = None
    if repo.origin is not None:
        origin_json = {
            "host": repo.origin.host,
            "owner": repo.origin.owner,
            "repo": repo.origin.repo,
            "slug": repo.origin.slug,
        }
    return {
        "schema": SCHEMA_SNAPSHOT,
        "tool": {"name": "git_clean_worktrees.py", "version": TOOL_VERSION},
        "repository": {
            "input": repo.input_path,
            "root": repo.root,
            "common_dir": repo.common_dir,
            "git_dir": repo.git_dir,
            "identity_key": canonical(repo.common_dir),
            "main_branch": repo.main_branch,
            "main_head": repo.main_head,
            "origin_url": repo.origin_url,
            "origin": origin_json,
            "origin_reason": repo.origin_reason,
        },
        "remote_default": {"name": remote.name, "tip": remote.tip, "fetched_ref": remote.fetched_ref},
        "worktrees": worktree_entries,
        "branches": branch_entries,
    }


# ---------------------------------------------------------------------------
# Plan construction and validation
# ---------------------------------------------------------------------------

PLAN_TOP_KEYS = frozenset({"schema", "snapshot_schema", "repository_identity", "expected", "selections", "actions", "retained"})
PLAN_IDENTITY_KEYS = frozenset({"root", "common_dir", "main_branch", "origin_url", "origin_slug", "origin_host"})
PLAN_EXPECTED_KEYS = frozenset({"main_head", "default_branch", "worktrees", "branches"})
PLAN_WORKTREE_ACTION_KEYS = frozenset(
    {"kind", "id", "path", "git_dir", "head", "branch", "allow_ignored", "ignored_fingerprint", "reason"}
)
PLAN_BRANCH_ACTION_KEYS = frozenset({"kind", "id", "ref", "tip", "depends_on", "reason"})
PLAN_RETAINED_KEYS = frozenset({"kind", "id", "reason"})
PLAN_SELECTION_KEYS = frozenset({"remove_worktrees", "delete_branches", "allow_ignored"})


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HelperError(f"{where} must be an object")
    return value


def _require_keys(value: Any, allowed: frozenset[str], where: str, required: Iterable[str] = ()) -> dict[str, Any]:
    obj = _require_mapping(value, where)
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        raise HelperError(f"{where} has unknown field(s): {', '.join(display(item) for item in unknown)}")
    missing = [key for key in required if key not in obj]
    if missing:
        raise HelperError(f"{where} is missing field(s): {', '.join(display(item) for item in missing)}")
    return obj


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise HelperError(f"{where} must be a non-empty string")
    return value


def index_worktrees(snapshot: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_canonical: dict[str, list[str]] = {}
    for entry in snapshot.get("worktrees", []):
        by_id[entry["id"]] = entry
        by_canonical.setdefault(entry["canonical"], []).append(entry["id"])
    return by_id, by_canonical


def find_worktree(snapshot: Mapping[str, Any], value: str) -> dict[str, Any] | None:
    by_id, by_canonical = index_worktrees(snapshot)
    if value in by_id:
        return by_id[value]
    matches = by_canonical.get(canonical(value), [])
    if len(matches) == 1:
        return by_id[matches[0]]
    return None


def find_branch(snapshot: Mapping[str, Any], value: str) -> dict[str, Any] | None:
    for entry in snapshot.get("branches", []):
        if entry["id"] == value or entry["ref"] == value:
            return entry
    return None


def ensure_artifact_outside(output_path: str | None, worktree_paths: Sequence[str]) -> None:
    if not output_path:
        return
    for path in worktree_paths:
        if is_within(path, output_path):
            raise HelperError(
                f"output artifact {display(output_path)} is inside a worktree selected for removal: {display(path)}"
            )


def build_plan(
    snapshot: Mapping[str, Any],
    *,
    remove_worktrees: Sequence[str] = (),
    delete_branches: Sequence[str] = (),
    allow_ignored: Sequence[str] = (),
    output_path: str | None = None,
) -> dict[str, Any]:
    """Construct one exact plan from explicit snapshot identifiers."""
    if snapshot.get("schema") != SCHEMA_SNAPSHOT:
        raise HelperError(f"unsupported snapshot schema: {display(snapshot.get('schema'))}")

    def dedupe(values: Sequence[str], label: str) -> list[str]:
        seen: list[str] = []
        for value in values:
            if value in seen:
                raise HelperError(f"duplicate {label}: {display(value)}")
            seen.append(value)
        return seen

    removals = dedupe(list(remove_worktrees), "--remove-worktree")
    deletions = dedupe(list(delete_branches), "--delete-branch")
    overrides = dedupe(list(allow_ignored), "--allow-ignored")

    selected_worktrees: list[dict[str, Any]] = []
    for value in removals:
        entry = find_worktree(snapshot, value)
        if entry is None:
            available = ", ".join(display(item["id"]) for item in snapshot.get("worktrees", []))
            raise HelperError(f"unknown worktree {display(value)}; snapshot ids: {available}")
        blocking = entry["eligibility"]["blocking"]
        override = value in overrides or entry["id"] in overrides
        if blocking:
            raise HelperError(
                f"worktree {display(entry['id'])} cannot be removed: " + "; ".join(bounded(item) for item in blocking)
            )
        if entry["requires_ignored_override"] and not override:
            raise HelperError(
                f"worktree {display(entry['id'])} has ignored files; select it with --allow-ignored {display(entry['id'])}"
            )
        if override and not entry["requires_ignored_override"]:
            raise HelperError(f"worktree {display(entry['id'])} has no ignored files, so --allow-ignored is not valid")
        selected_worktrees.append(entry)

    for value in overrides:
        if find_worktree(snapshot, value) is None:
            raise HelperError(f"unknown --allow-ignored worktree {display(value)}")
        if find_worktree(snapshot, value)["id"] not in [entry["id"] for entry in selected_worktrees]:
            raise HelperError(f"--allow-ignored {display(value)} requires the same worktree in --remove-worktree")

    selected_ids = {entry["id"] for entry in selected_worktrees}
    selected_branches: list[dict[str, Any]] = []
    for value in deletions:
        entry = find_branch(snapshot, value)
        if entry is None:
            available = ", ".join(display(item["id"]) for item in snapshot.get("branches", []))
            raise HelperError(f"unknown branch {display(value)}; snapshot ids: {available}")
        blocking = entry["eligibility"]["blocking"]
        if blocking:
            raise HelperError(
                f"branch {display(entry['id'])} cannot be deleted: " + "; ".join(bounded(item) for item in blocking)
            )
        unselected = [item for item in entry.get("used_by_worktrees", []) if item not in selected_ids]
        if unselected:
            raise HelperError(
                f"branch {display(entry['id'])} is still used by retained worktree(s): "
                + ", ".join(display(item) for item in unselected)
            )
        selected_branches.append(entry)

    actions: list[dict[str, Any]] = []
    for entry in sorted(selected_worktrees, key=lambda item: item["id"]):
        file_state = entry.get("file_state") or {}
        actions.append(
            {
                "kind": "remove_worktree",
                "id": entry["id"],
                "path": entry["id"],
                "git_dir": entry["git_dir"],
                "head": entry["head"],
                "branch": entry["branch_ref"],
                "allow_ignored": bool(entry["requires_ignored_override"]),
                "ignored_fingerprint": file_state.get("ignored_fingerprint"),
                "reason": "approved by plan",
            }
        )
    for entry in sorted(selected_branches, key=lambda item: item["ref"]):
        depends = [f"remove_worktree:{item}" for item in sorted(entry.get("used_by_worktrees", [])) if item in selected_ids]
        actions.append(
            {
                "kind": "delete_branch",
                "id": entry["id"],
                "ref": entry["ref"],
                "tip": entry["tip"],
                "depends_on": depends,
                "reason": "approved by plan",
            }
        )

    retained: list[dict[str, str]] = []
    for entry in sorted(snapshot.get("worktrees", []), key=lambda item: item["id"]):
        if entry["id"] in selected_ids:
            continue
        blocking = entry["eligibility"]["blocking"]
        reason = "; ".join(bounded(item) for item in blocking) if blocking else "not selected"
        if entry["requires_ignored_override"] and not blocking:
            reason = "has ignored files and was not selected with an override"
        retained.append({"kind": "worktree", "id": entry["id"], "reason": reason})
    for entry in sorted(snapshot.get("branches", []), key=lambda item: item["id"]):
        if entry["id"] in {item["id"] for item in selected_branches}:
            continue
        blocking = entry["eligibility"]["blocking"]
        retained.append(
            {
                "kind": "branch",
                "id": entry["id"],
                "reason": "; ".join(bounded(item) for item in blocking) if blocking else "not selected",
            }
        )

    identity = snapshot["repository"]
    plan: dict[str, Any] = {
        "schema": SCHEMA_PLAN,
        "snapshot_schema": SCHEMA_SNAPSHOT,
        "repository_identity": {
            "root": identity["root"],
            "common_dir": identity["common_dir"],
            "main_branch": identity["main_branch"],
            "origin_url": identity["origin_url"],
            "origin_slug": (identity.get("origin") or {}).get("slug"),
            "origin_host": (identity.get("origin") or {}).get("host"),
        },
        "expected": {
            "main_head": identity["main_head"],
            "default_branch": snapshot["remote_default"]["name"],
            "worktrees": {
                entry["id"]: {
                    "path": entry["id"],
                    "git_dir": entry["git_dir"],
                    "head": entry["head"],
                    "branch": entry["branch_ref"],
                }
                for entry in selected_worktrees
            },
            "branches": {entry["ref"]: entry["tip"] for entry in selected_branches},
        },
        "selections": {
            "remove_worktrees": [entry["id"] for entry in selected_worktrees],
            "delete_branches": [entry["id"] for entry in selected_branches],
            "allow_ignored": [entry["id"] for entry in selected_worktrees if entry["requires_ignored_override"]],
        },
        "actions": actions,
        "retained": retained,
    }
    ensure_artifact_outside(output_path, [entry["id"] for entry in selected_worktrees])
    validate_plan(plan)
    return plan


def validate_plan(plan: Any) -> dict[str, Any]:
    """Strictly validate plan structure and internal consistency."""
    obj = _require_keys(
        plan,
        PLAN_TOP_KEYS,
        "plan",
        required=("schema", "repository_identity", "expected", "actions", "retained"),
    )
    if obj["schema"] != SCHEMA_PLAN:
        raise HelperError(f"unsupported plan schema: {display(obj['schema'])}")
    if obj.get("snapshot_schema") not in (None, SCHEMA_SNAPSHOT):
        raise HelperError(f"unsupported snapshot schema: {display(obj['snapshot_schema'])}")
    identity = _require_keys(obj["repository_identity"], PLAN_IDENTITY_KEYS, "plan.repository_identity")
    _require_str(identity.get("root"), "plan.repository_identity.root")
    _require_str(identity.get("common_dir"), "plan.repository_identity.common_dir")
    _require_str(identity.get("main_branch"), "plan.repository_identity.main_branch")
    expected = _require_keys(obj["expected"], PLAN_EXPECTED_KEYS, "plan.expected", required=("main_head", "default_branch"))
    _require_str(expected["main_head"], "plan.expected.main_head")
    _require_str(expected["default_branch"], "plan.expected.default_branch")
    _require_mapping(expected.get("worktrees", {}), "plan.expected.worktrees")
    _require_mapping(expected.get("branches", {}), "plan.expected.branches")
    if obj.get("selections") is not None:
        selections = _require_keys(obj["selections"], PLAN_SELECTION_KEYS, "plan.selections")
        for key in PLAN_SELECTION_KEYS:
            if key in selections and not isinstance(selections[key], list):
                raise HelperError(f"plan.selections.{key} must be a list")
    actions = obj["actions"]
    if not isinstance(actions, list):
        raise HelperError("plan.actions must be a list")

    seen_worktrees: set[str] = set()
    seen_branches: set[str] = set()
    for index, action in enumerate(actions):
        where = f"plan.actions[{index}]"
        if not isinstance(action, dict):
            raise HelperError(f"{where} must be an object")
        kind = action.get("kind")
        if kind == "remove_worktree":
            item = _require_keys(
                action,
                PLAN_WORKTREE_ACTION_KEYS,
                where,
                required=("kind", "id", "path", "git_dir", "head", "branch", "allow_ignored"),
            )
            _require_str(item["id"], f"{where}.id")
            _require_str(item["path"], f"{where}.path")
            _require_str(item["git_dir"], f"{where}.git_dir")
            if not isinstance(item["allow_ignored"], bool):
                raise HelperError(f"{where}.allow_ignored must be a boolean")
            if item["allow_ignored"] and not item.get("ignored_fingerprint"):
                raise HelperError(f"{where}.allow_ignored requires an ignored_fingerprint")
            if item["id"] in seen_worktrees:
                raise HelperError(f"duplicate worktree action: {display(item['id'])}")
            seen_worktrees.add(item["id"])
            if item["id"] not in expected.get("worktrees", {}):
                raise HelperError(f"{where} is not recorded in plan.expected.worktrees")
        elif kind == "delete_branch":
            item = _require_keys(action, PLAN_BRANCH_ACTION_KEYS, where, required=("kind", "id", "ref", "tip"))
            _require_str(item["ref"], f"{where}.ref")
            _require_str(item["tip"], f"{where}.tip")
            if not item["ref"].startswith("refs/heads/"):
                raise HelperError(f"{where}.ref must be a full refs/heads/ ref")
            depends = item.get("depends_on", [])
            if not isinstance(depends, list) or not all(isinstance(entry, str) for entry in depends):
                raise HelperError(f"{where}.depends_on must be a list of strings")
            if item["ref"] in seen_branches:
                raise HelperError(f"duplicate branch action: {display(item['ref'])}")
            seen_branches.add(item["ref"])
            if item["ref"] not in expected.get("branches", {}):
                raise HelperError(f"{where} is not recorded in plan.expected.branches")
            if expected["branches"][item["ref"]].lower() != item["tip"].lower():
                raise HelperError(f"{where}.tip does not match plan.expected.branches")
        else:
            raise HelperError(f"{where}.kind must be remove_worktree or delete_branch")

    for index, action in enumerate(actions):
        if action.get("kind") != "delete_branch":
            continue
        for dependency in action.get("depends_on", []):
            kind, _, target = dependency.partition(":")
            if kind != "remove_worktree" or target not in seen_worktrees:
                raise HelperError(f"plan.actions[{index}] depends on an action that is not in the plan: {display(dependency)}")

    retained = obj["retained"]
    if not isinstance(retained, list):
        raise HelperError("plan.retained must be a list")
    for index, entry in enumerate(retained):
        item = _require_keys(entry, PLAN_RETAINED_KEYS, f"plan.retained[{index}]", required=("kind", "id", "reason"))
        if item["kind"] not in ("worktree", "branch"):
            raise HelperError(f"plan.retained[{index}].kind must be worktree or branch")
        if item["id"] in seen_worktrees or (
            item["kind"] == "branch" and any(action["id"] == item["id"] for action in actions if action["kind"] == "delete_branch")
        ):
            raise HelperError(f"plan.retained[{index}] is also an approved action")
    return obj


def plan_file(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema": SCHEMA_PLAN_FILE, "digest": plan_digest(plan), "plan": plan}


def load_plan_file(path: str) -> tuple[dict[str, Any], str]:
    document = _require_keys(read_json_file(path), frozenset({"schema", "digest", "plan"}), "plan file", required=("schema", "plan"))
    if document["schema"] != SCHEMA_PLAN_FILE:
        raise HelperError(f"unsupported plan file schema: {display(document['schema'])}")
    plan = validate_plan(document["plan"])
    recorded = normalize_digest(_require_str(document.get("digest"), "plan file digest"))
    recomputed = plan_digest(plan)
    if recorded != recomputed:
        raise HelperError(
            f"plan digest mismatch: file records {recorded} but the content hashes to {recomputed}; the plan was modified"
        )
    return plan, recomputed


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

REFL0G_NOTE = (
    "Deleting a ref removes its reflog, so these SHAs are recoverable only while the objects remain "
    "available; no reflog-based restore is promised."
)
CONCURRENCY_NOTE = (
    "Worktree deletion cannot be atomic with concurrent file writers, and ref deletion cannot atomically "
    "exclude another process checking the branch out. Stop writers and concurrent Git operations while applying."
)


def _worktree_observed(runner: Any, repo: Repository, path: str, *, timeout: float | None) -> dict[str, Any]:
    records = list_worktrees(runner, repo, timeout=timeout)
    present = any(canonical(record.path) == canonical(path) for record in records)
    return {"registration_present": present, "path_exists": os.path.exists(path)}


def _apply_remove_worktree(
    runner: Any,
    repo: Repository,
    remote: RemoteDefault,
    action: Mapping[str, Any],
    gh: GhClient | None,
    expected: Mapping[str, Any],
    *,
    timeout: float | None,
) -> dict[str, Any]:
    path = action["path"]
    outcome: dict[str, Any] = {"kind": "remove_worktree", "id": action["id"], "path": path, "status": "failed", "reason": ""}

    def skip(reason: str) -> dict[str, Any]:
        outcome["status"] = "skipped"
        outcome["reason"] = reason
        outcome["observed"] = _worktree_observed(runner, repo, path, timeout=timeout)
        return outcome

    records = list_worktrees(runner, repo, timeout=timeout)
    matches = [record for record in records if canonical(record.path) == canonical(path)]
    if not matches:
        return skip("the worktree registration disappeared after the plan was approved")
    if len(matches) > 1:
        return skip("the worktree path aliases another registration")
    record = matches[0]
    if canonical(path) == canonical(repo.root):
        return skip("the target is now the main worktree")
    if record.locked is not None:
        return skip(f"the worktree is now locked: {bounded(record.locked or 'no reason given')}")
    if record.prunable is not None:
        return skip(f"the worktree registration is now prunable: {bounded(record.prunable)}")
    if record.detached:
        return skip("the worktree now has a detached HEAD")
    if record.branch_ref != expected.get("branch"):
        return skip("the worktree branch changed after the plan was approved")
    if os.path.islink(path) or not os.path.isdir(path):
        return skip("the worktree path is missing or is a symlink")
    if is_within(repo.root, path):
        return skip("the worktree path is now inside the main working tree")
    for other in records:
        if canonical(other.path) != canonical(path) and is_within(other.path, path):
            return skip(f"the worktree path is nested with another registration: {display(other.path)}")

    git_dir = absolute_git_dir(runner, path, timeout=timeout)
    if git_dir is None or canonical(git_dir) != canonical(action["git_dir"]):
        return skip("the worktree administrative identity changed after the plan was approved")
    head_now = rev_parse_commit(runner, path, "HEAD", timeout=timeout)
    if head_now is None or head_now.lower() != str(action["head"]).lower():
        return skip("the worktree HEAD changed after the plan was approved")

    state = inspect_file_state(runner, path, git_dir, enumerate_ignored=True, timeout=timeout)
    if state["errors"]:
        return skip("file inspection failed: " + "; ".join(bounded(item) for item in state["errors"]))
    for key, label in (
        ("tracked_changes", "staged or unstaged change"),
        ("conflicts", "unresolved conflict"),
        ("untracked", "untracked file"),
        ("in_progress", "in-progress Git operation"),
        ("index_flags", "index flag that can hide tracked changes"),
        ("submodules", "populated submodule"),
    ):
        if state[key]:
            return skip(f"{label}: {state[key][0]}")
    if state["sparse_checkout"]:
        return skip("sparse-checkout state can hide tracked changes")
    if state["ignored_count"]:
        if not action["allow_ignored"]:
            return skip(f"the worktree now has {state['ignored_count']} ignored file(s) that were not approved")
        if state["ignored_fingerprint"] != action["ignored_fingerprint"]:
            return skip("ignored files changed after the plan was approved")

    tip = rev_parse_commit(runner, repo.root, expected["branch"], timeout=timeout) if expected.get("branch") else None
    if expected.get("branch") and tip is None:
        return skip("the worktree branch no longer resolves to a commit")
    if tip is not None and tip.lower() != str(action["head"]).lower():
        return skip("the worktree branch tip changed after the plan was approved")
    if tip is not None:
        preservation = preservation_for_tip(runner, repo, tip, remote.tip, gh, timeout=timeout)
        if not preservation.proven:
            return skip(f"preservation is no longer proven: {preservation.reason}")

    result = run_git(runner, ["-C", repo.root, "worktree", "remove", "--", path], timeout=timeout)
    observed = _worktree_observed(runner, repo, path, timeout=timeout)
    outcome["observed"] = observed
    if result.timed_out:
        outcome["reason"] = "git worktree remove timed out"
    elif result.returncode != 0:
        outcome["reason"] = f"git worktree remove failed (exit {result.returncode}): {bounded(decode(result.stderr))}"
    elif observed["registration_present"] or observed["path_exists"]:
        outcome["reason"] = "git worktree remove reported success but the worktree is still present"
    else:
        outcome["status"] = "removed"
        outcome["reason"] = "removed with git worktree remove"
    return outcome


def _apply_delete_branch(
    runner: Any,
    repo: Repository,
    remote: RemoteDefault,
    action: Mapping[str, Any],
    gh: GhClient | None,
    *,
    timeout: float | None,
) -> dict[str, Any]:
    ref = action["ref"]
    name = ref[len("refs/heads/") :]
    outcome: dict[str, Any] = {"kind": "delete_branch", "id": action["id"], "ref": ref, "status": "failed", "reason": ""}

    symbolic = run_git(runner, ["-C", repo.root, "symbolic-ref", "--quiet", ref], timeout=timeout)
    if symbolic.returncode == 0:
        outcome["status"] = "skipped"
        outcome["reason"] = f"the ref is now symbolic and points at {display(decode(symbolic.stdout).strip())}"
        return outcome

    tip = rev_parse_commit(runner, repo.root, ref, timeout=timeout)
    if tip is None:
        outcome["status"] = "skipped"
        outcome["reason"] = "the branch no longer exists"
        outcome["observed"] = {"ref_exists": False}
        return outcome
    if tip.lower() != str(action["tip"]).lower():
        outcome["status"] = "skipped"
        outcome["reason"] = f"the branch advanced to {display(tip)} after the plan was approved"
        outcome["observed"] = {"ref_exists": True, "tip": tip}
        return outcome

    records = list_worktrees(runner, repo, timeout=timeout)
    for record in records:
        if record.branch_ref == ref:
            outcome["status"] = "skipped"
            outcome["reason"] = f"the branch is still used by worktree {display(record.path)}"
            return outcome

    preservation = preservation_for_tip(runner, repo, tip, remote.tip, gh, timeout=timeout)
    if not preservation.proven:
        outcome["status"] = "skipped"
        outcome["reason"] = f"preservation is no longer proven: {preservation.reason}"
        return outcome

    result = run_git(
        runner,
        ["-C", repo.root, "update-ref", "--no-deref", "-d", ref, str(action["tip"])],
        timeout=timeout,
    )
    still_there = rev_parse_commit(runner, repo.root, ref, timeout=timeout)
    outcome["observed"] = {"ref_exists": still_there is not None}
    if result.timed_out:
        outcome["reason"] = "git update-ref timed out"
    elif result.returncode != 0:
        outcome["reason"] = (
            f"compare-and-delete failed (exit {result.returncode}): {bounded(decode(result.stderr))}"
        )
    elif still_there is not None:
        outcome["reason"] = "git update-ref reported success but the ref still exists"
    else:
        outcome["status"] = "deleted"
        outcome["reason"] = "deleted with git update-ref --no-deref -d and the expected tip"
        outcome["deleted_sha"] = tip
    return outcome


def branch_config_leftovers(runner: Any, repo: Repository, names: Sequence[str], *, timeout: float | None) -> dict[str, list[str]]:
    if not names:
        return {}
    result = run_git(runner, ["-C", repo.root, "config", "--local", "--name-only", "--get-regexp", "^branch\\."], timeout=timeout)
    if result.returncode not in (0, 1):
        return {}
    keys = [line for line in decode(result.stdout).splitlines() if line]
    leftovers: dict[str, list[str]] = {}
    for name in names:
        prefix = f"branch.{name}."
        matched = sorted(key for key in keys if key.startswith(prefix))
        if matched:
            leftovers[name] = matched
    return leftovers


def apply_plan(
    runner: Any,
    plan: Mapping[str, Any],
    approved_digest: str,
    *,
    gh: GhClient | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    fetch_timeout: float | None = DEFAULT_FETCH_TIMEOUT,
    gh_timeout: float | None = DEFAULT_GH_TIMEOUT,
    output_path: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Apply exactly the approved plan. Raises HelperError for global aborts."""
    approved = normalize_digest(approved_digest)
    validate_plan(plan)
    recomputed = plan_digest(plan)
    if approved != recomputed:
        raise HelperError(
            f"approved digest {approved} does not match the plan content digest {recomputed}; "
            "re-display the plan and approve the current digest"
        )
    removals = [action for action in plan["actions"] if action["kind"] == "remove_worktree"]
    ensure_artifact_outside(output_path, [action["path"] for action in removals])

    identity = plan["repository_identity"]
    repo = resolve_repository(runner, identity["root"], timeout=timeout)
    if canonical(repo.common_dir) != canonical(identity["common_dir"]):
        raise HelperError("the repository identity changed after the plan was approved; refusing to apply")
    if repo.main_branch != identity["main_branch"]:
        raise HelperError("the checked out default branch changed after the plan was approved; refusing to apply")
    if repo.origin_url != identity.get("origin_url"):
        raise HelperError("the origin remote changed after the plan was approved; refusing to apply")
    if repo.main_head != plan["expected"]["main_head"]:
        raise HelperError("the main worktree HEAD changed after the plan was approved; refusing to apply")

    remote = refresh_remote_default(runner, repo, fetch_timeout=fetch_timeout)
    if remote.name != plan["expected"]["default_branch"]:
        raise HelperError(
            f"the remote default branch is now {display(remote.name)}, not "
            f"{display(plan['expected']['default_branch'])}; refusing to apply"
        )
    if gh is None:
        gh = GhClient("gh", runner, timeout=gh_timeout)

    outcomes: list[dict[str, Any]] = []
    for action in plan["actions"]:
        if action["kind"] == "remove_worktree":
            outcomes.append(
                _apply_remove_worktree(runner, repo, remote, action, gh, plan["expected"]["worktrees"][action["id"]], timeout=timeout)
            )
        else:
            outcomes.append(_apply_delete_branch(runner, repo, remote, action, gh, timeout=timeout))

    deleted_names = [action["id"] for action, outcome in zip(plan["actions"], outcomes) if outcome["status"] == "deleted"]
    result: dict[str, Any] = {
        "schema": SCHEMA_RESULT,
        "digest": approved,
        "repository": {
            "root": repo.root,
            "common_dir": repo.common_dir,
            "origin_url": repo.origin_url,
            "main_branch": repo.main_branch,
        },
        "default_branch": remote.name,
        "fetched_default_tip": remote.tip,
        "actions": outcomes,
        "removed_worktrees": [item["id"] for item in outcomes if item["status"] == "removed"],
        "deleted_branches": [item["id"] for item in outcomes if item["status"] == "deleted"],
        "skipped": [{"id": item["id"], "reason": item["reason"]} for item in outcomes if item["status"] == "skipped"],
        "failed": [{"id": item["id"], "reason": item["reason"]} for item in outcomes if item["status"] == "failed"],
        "deleted_shas": {item["ref"]: item["deleted_sha"] for item in outcomes if item.get("deleted_sha")},
        "branch_config_leftovers": branch_config_leftovers(runner, repo, deleted_names, timeout=timeout),
        "retained": plan.get("retained", []),
        "notes": [REFL0G_NOTE, CONCURRENCY_NOTE],
    }
    exit_code = 0 if not result["skipped"] and not result["failed"] else 2
    result["exit_code"] = exit_code
    return result, exit_code


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git_clean_worktrees.py",
        description=(
            "Inspect one repository, build an approval-bound plan, and remove only the approved linked "
            "worktrees and local branches. No Bash, PowerShell, jq, or third-party Python package is required."
        ),
    )
    subparsers = parser.add_subparsers(dest="phase", required=True)

    inspect = subparsers.add_parser(
        "inspect",
        help="discover the repository and write a snapshot; removes nothing",
        description=(
            "Validate the main worktree and default branch, fetch the remote default branch into its "
            "origin remote-tracking ref, and record every worktree and local branch with eligibility and evidence."
        ),
    )
    inspect.add_argument("repository", nargs="?", default=None, help="repository path inside the main worktree (default: the invocation directory)")
    inspect.add_argument("--output", required=True, metavar="FILE", help="snapshot JSON path; keep it outside any worktree you may remove")
    inspect.add_argument("--gh-path", default="gh", metavar="PATH", help="gh executable used for merged PR lookups (default: gh)")
    inspect.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS", help="per-command Git timeout (default: 60)")
    inspect.add_argument("--fetch-timeout", type=float, default=DEFAULT_FETCH_TIMEOUT, metavar="SECONDS", help="timeout for ls-remote and fetch (default: 300)")
    inspect.add_argument("--gh-timeout", type=float, default=DEFAULT_GH_TIMEOUT, metavar="SECONDS", help="per-command gh timeout (default: 90)")

    plan = subparsers.add_parser(
        "plan",
        help="build one exact plan from explicit snapshot identifiers",
        description=(
            "Select targets by their snapshot ids: a worktree id is the registered absolute path shown in "
            "the snapshot, and a branch id is the short branch name. Nothing is matched by substring."
        ),
    )
    plan.add_argument("--snapshot", required=True, metavar="FILE", help="snapshot produced by the inspect phase")
    plan.add_argument("--remove-worktree", action="append", default=[], metavar="ID", help="snapshot worktree id to remove (repeatable)")
    plan.add_argument("--delete-branch", action="append", default=[], metavar="ID", help="snapshot branch id to delete (repeatable)")
    plan.add_argument("--allow-ignored", action="append", default=[], metavar="ID", help="approve deleting the ignored files of one selected worktree (repeatable)")
    plan.add_argument("--output", required=True, metavar="FILE", help="plan JSON path; keep it outside every selected worktree")

    apply = subparsers.add_parser(
        "apply",
        help="revalidate and apply exactly the approved plan",
        description=(
            "Revalidate the repository, the freshly fetched default branch, and every target, then run only "
            "the approved actions. Exit codes: 0 every action succeeded, 2 some target was skipped or failed, "
            "1 the run aborted before any action."
        ),
    )
    apply.add_argument("--plan", required=True, metavar="FILE", help="plan JSON produced by the plan phase")
    apply.add_argument("--approved-digest", required=True, metavar="SHA256", help="exact plan digest shown by the plan phase")
    apply.add_argument("--gh-path", default="gh", metavar="PATH", help="gh executable used for merged PR lookups (default: gh)")
    apply.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS", help="per-command Git timeout (default: 60)")
    apply.add_argument("--fetch-timeout", type=float, default=DEFAULT_FETCH_TIMEOUT, metavar="SECONDS", help="timeout for ls-remote and fetch (default: 300)")
    apply.add_argument("--gh-timeout", type=float, default=DEFAULT_GH_TIMEOUT, metavar="SECONDS", help="per-command gh timeout (default: 90)")
    apply.add_argument("--output", default=None, metavar="FILE", help="optional path for the result JSON")
    return parser


def summarize_snapshot(snapshot: Mapping[str, Any]) -> list[str]:
    repo = snapshot["repository"]
    remote = snapshot["remote_default"]
    lines = [
        f"repository: {display(repo['root'])}",
        f"origin: {display(repo['origin_url'])}"
        + (f" ({display(repo['origin']['slug'])})" if repo.get("origin") else " (PR proof unavailable)"),
        f"default branch: {display(remote['name'])} at {display(remote['tip'])} (fetched into {display(remote['fetched_ref'])})",
        "worktrees:",
    ]
    for entry in snapshot["worktrees"]:
        status = "eligible" if entry["eligibility"]["eligible"] else "retained"
        if entry["requires_ignored_override"] and not entry["eligibility"]["blocking"]:
            status = "eligible with --allow-ignored"
        detail = "" if status == "eligible" else "  (" + "; ".join(entry["eligibility"]["blocking"] or entry["eligibility"]["overridable"]) + ")"
        lines.append(f"  [{status}] {display(entry['id'])}{detail}")
    lines.append("branches:")
    for entry in snapshot["branches"]:
        status = "eligible" if entry["eligibility"]["eligible"] else "retained"
        detail = "" if status == "eligible" else "  (" + "; ".join(entry["eligibility"]["blocking"]) + ")"
        lines.append(f"  [{status}] {display(entry['id'])}{detail}")
    return lines


def summarize_plan(document: Mapping[str, Any]) -> list[str]:
    plan = document["plan"]
    lines = [f"plan digest: {document['digest']}"]
    if not plan["actions"]:
        lines.append("no actions selected; nothing would change")
    for action in plan["actions"]:
        if action["kind"] == "remove_worktree":
            override = " (deletes approved ignored files)" if action["allow_ignored"] else ""
            lines.append(f"remove worktree: {display(action['id'])}{override}")
        else:
            lines.append(f"delete branch: {display(action['ref'])} at {display(action['tip'])}")
    lines.append(f"retained targets: {len(plan['retained'])}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="backslashreplace")
            sys.stderr.reconfigure(errors="backslashreplace")
        except (ValueError, AttributeError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    command_timeout = getattr(args, "timeout", DEFAULT_TIMEOUT)
    runner = SubprocessRunner(default_timeout=command_timeout)
    try:
        if args.phase == "inspect":
            gh = GhClient(args.gh_path, runner, timeout=args.gh_timeout)
            snapshot = inspect_repository(
                runner,
                args.repository,
                gh=gh,
                timeout=args.timeout,
                fetch_timeout=args.fetch_timeout,
                gh_timeout=args.gh_timeout,
            )
            write_json_file(args.output, snapshot)
            for line in summarize_snapshot(snapshot):
                print(line)
            print(f"snapshot: {display(args.output)}")
            return 0

        if args.phase == "plan":
            snapshot = read_json_file(args.snapshot)
            plan = build_plan(
                snapshot,
                remove_worktrees=args.remove_worktree,
                delete_branches=args.delete_branch,
                allow_ignored=args.allow_ignored,
                output_path=args.output,
            )
            document = plan_file(plan)
            write_json_file(args.output, document)
            for line in summarize_plan(document):
                print(line)
            print(f"plan file: {display(args.output)}")
            print(f"Approve this digest before applying: {document['digest']}")
            return 0

        document = read_json_file(args.plan)
        plan = validate_plan(document.get("plan")) if isinstance(document, dict) else None
        if plan is None:
            raise HelperError("plan file must be a JSON object")
        recorded = document.get("digest")
        if isinstance(recorded, str) and recorded.lower() != plan_digest(plan):
            raise HelperError("plan file digest does not match its content; the plan was modified since it was written")
        gh = GhClient(args.gh_path, runner, timeout=args.gh_timeout)
        result, exit_code = apply_plan(
            runner,
            plan,
            args.approved_digest,
            gh=gh,
            timeout=args.timeout,
            fetch_timeout=args.fetch_timeout,
            gh_timeout=args.gh_timeout,
            output_path=args.output,
        )
        if args.output:
            write_json_file(args.output, result)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return exit_code
    except HelperError as error:
        print(f"error: {error}", file=sys.stderr)
        if error.details:
            print(f"details: {bounded(error.details)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
