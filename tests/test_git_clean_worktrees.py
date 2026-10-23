"""Offline unit and disposable-repository integration tests for git-clean-worktrees.

Network behavior (gh) is injected. Git behavior runs against real, throwaway
repositories created under the test's own temporary directory, with an empty
global and system Git configuration so developer settings cannot leak in.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "git-clean-worktrees" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import git_clean_worktrees as gcw  # noqa: E402

FAKE_GH = "fake-gh"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_env(base: Path) -> dict[str, str]:
    config = base / "empty.gitconfig"
    config.write_text("", encoding="utf-8")
    home = base / "home"
    home.mkdir(exist_ok=True)
    return {
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_SYSTEM": str(config),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }


class GitFixture:
    def __init__(self, base: Path):
        self.base = base
        self.env = make_env(base)
        self.runner = gcw.SubprocessRunner(env_overrides=self.env)

    def git(self, *args: str, cwd: str | None = None, check: bool = True) -> gcw.Result:
        result = self.runner.run(["git", *args], cwd=cwd)
        if check and result.returncode != 0:
            raise AssertionError(f"git {args} failed ({result.returncode}): {gcw.decode(result.stderr)}")
        return result

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def repo(self, name: str = "clone", default_branch: str = "main", push: bool = True) -> "RepoFixture":
        return RepoFixture(self, name=name, default_branch=default_branch, push=push)

    def clone_from(self, source: str, name: str) -> str:
        target = self.base / name
        self.git("clone", "-q", source, str(target))
        return str(target)


class RepoFixture:
    def __init__(self, fixture: GitFixture, *, name: str, default_branch: str, push: bool):
        self.fixture = fixture
        self.default_branch = default_branch
        self.origin = fixture.base / f"{name}-origin.git"
        self.root = fixture.base / name
        fixture.git("init", "-q", "--bare", "-b", default_branch, str(self.origin))
        fixture.git("clone", "-q", str(self.origin), str(self.root))
        fixture.write(self.root / "a.txt", "one\n")
        fixture.git("add", "a.txt", cwd=str(self.root))
        fixture.git("commit", "-qm", "one", cwd=str(self.root))
        if push:
            fixture.git("push", "-q", "origin", default_branch, cwd=str(self.root))

    @property
    def path(self) -> str:
        return str(self.root)

    def git(self, *args: str, cwd: str | None = None, check: bool = True) -> gcw.Result:
        return self.fixture.git(*args, cwd=cwd or self.path, check=check)

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        self.fixture.write(path, text)
        return path

    def commit(self, message: str, relative: str | None = None, text: str | None = None) -> str:
        if relative is not None:
            self.write(relative, text if text is not None else message + "\n")
            self.git("add", "--", relative)
        self.git("commit", "-qm", message)
        return gcw.decode(self.git("rev-parse", "HEAD").stdout).strip()

    def commit_in(self, cwd: str, message: str, relative: str, text: str) -> str:
        self.fixture.write(Path(cwd) / relative, text)
        self.fixture.git("add", "--", relative, cwd=cwd)
        self.fixture.git("commit", "-qm", message, cwd=cwd)
        return gcw.decode(self.fixture.git("rev-parse", "HEAD", cwd=cwd).stdout).strip()

    def sha(self, ref: str = "HEAD", cwd: str | None = None) -> str:
        return gcw.decode(self.git("rev-parse", ref, cwd=cwd).stdout).strip()

    def branch(self, name: str, ref: str = "HEAD") -> str:
        self.git("branch", name, ref)
        return self.sha(f"refs/heads/{name}")

    def worktree(self, location: str, *, branch: str | None = None, detach: bool = False) -> str:
        args = ["worktree", "add", "-q"]
        if detach:
            args.append("--detach")
        args.append(location)
        if branch:
            args.append(branch)
        self.git(*args)
        return location

    def push(self, branch: str | None = None) -> None:
        self.git("push", "-q", "origin", branch or self.default_branch)

    def use_github_origin(self, slug: str = "owner/repo") -> None:
        """Point origin at a github URL that rewrites to the local bare repo."""
        local = "file://" + str(self.origin)
        self.git("config", f"url.{local}.insteadOf", f"https://github.com/{slug}.git")
        self.git("remote", "set-url", "origin", f"https://github.com/{slug}.git")


class FakeGhRunner:
    """Delegates everything to a real runner except the fake gh executable."""

    def __init__(self, real: gcw.SubprocessRunner, handler=None):
        self.real = real
        self.handler = handler or (lambda args: gcw.Result(0, b"[]", b""))
        self.calls: list[list[str]] = []

    def run(self, args, cwd=None, timeout=None, input_bytes=None):
        if args and str(args[0]) == FAKE_GH:
            self.calls.append([str(a) for a in args])
            return self.handler([str(a) for a in args])
        return self.real.run(args, cwd=cwd, timeout=timeout, input_bytes=input_bytes)


def gh_client(real_runner: gcw.SubprocessRunner, handler=None) -> tuple[gcw.GhClient, FakeGhRunner]:
    fake = FakeGhRunner(real_runner, handler)
    return gcw.GhClient(FAKE_GH, fake), fake


def pr_payload(number: int, head_sha: str, *, merged: bool = True, base: str = "owner/repo", state: str = "closed") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": state if merged else "open",
        "merged_at": "2026-01-01T00:00:00Z" if merged else None,
        "merge_commit_sha": "f" * 40 if merged else None,
        "head": {"sha": head_sha},
        "base": {"repo": {"full_name": base, "id": 1}},
    }


def gh_pulls_handler(prs_by_sha: dict[str, list[dict]], *, returncode: int = 0, stderr: bytes = b""):
    def handler(args: list[str]) -> gcw.Result:
        if returncode != 0:
            return gcw.Result(returncode, b"", stderr)
        endpoint = args[-1]
        sha = endpoint.rsplit("/", 2)[-2]
        payload = prs_by_sha.get(sha, [])
        return gcw.Result(0, json.dumps(payload).encode(), b"")

    return handler


# ---------------------------------------------------------------------------
# Synthetic snapshot helpers for pure plan tests
# ---------------------------------------------------------------------------

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def worktree_entry(
    path: str,
    *,
    canonical_path: str | None = None,
    eligible: bool = False,
    blocking=(),
    overridable=(),
    requires_ignored_override: bool = False,
    git_dir: str | None = None,
    head: str = SHA_C,
    branch_ref: str | None = "refs/heads/topic",
    is_main: bool = False,
    fingerprint: str | None = None,
) -> dict:
    return {
        "kind": "worktree",
        "id": path,
        "canonical": canonical_path or gcw.canonical(path),
        "head": head,
        "branch_ref": branch_ref,
        "branch_name": branch_ref[len("refs/heads/") :] if branch_ref else None,
        "detached": False,
        "locked": None,
        "prunable": None,
        "is_main": is_main,
        "git_dir": git_dir or ("/repo/.git/worktrees/wt-" + path.strip("/").replace("/", "_")),
        "file_state": {"inspected": True, "ignored_fingerprint": fingerprint},
        "preservation": {"proven": True, "method": "ancestry", "reason": "ok", "evidence": {}},
        "requires_ignored_override": requires_ignored_override,
        "eligibility": {"eligible": eligible, "blocking": list(blocking), "overridable": list(overridable)},
    }


def branch_entry(
    name: str,
    *,
    eligible: bool = False,
    blocking=(),
    tip: str = SHA_C,
    used_by_worktrees=(),
) -> dict:
    return {
        "kind": "branch",
        "id": name,
        "ref": f"refs/heads/{name}",
        "tip": tip,
        "object_type": "commit",
        "symbolic": None,
        "upstream": None,
        "used_by_worktrees": list(used_by_worktrees),
        "preservation": {"proven": True, "method": "ancestry", "reason": "ok", "evidence": {}},
        "eligibility": {"eligible": eligible, "blocking": list(blocking)},
    }


def make_snapshot(
    *,
    worktrees=(),
    branches=(),
    origin: bool = True,
    slug: str = "owner/repo",
    default_branch: str = "main",
    root: str = "/repo",
    common_dir: str = "/repo/.git",
    origin_url: str = "https://github.com/owner/repo.git",
    main_head: str = SHA_A,
    remote_tip: str = SHA_B,
) -> dict:
    return {
        "schema": gcw.SCHEMA_SNAPSHOT,
        "tool": {"name": "git_clean_worktrees.py", "version": "1"},
        "repository": {
            "input": root,
            "root": root,
            "common_dir": common_dir,
            "git_dir": common_dir,
            "identity_key": gcw.canonical(common_dir),
            "main_branch": default_branch,
            "main_head": main_head,
            "origin_url": origin_url,
            "origin": {"host": "github.com", "owner": slug.split("/")[0], "repo": slug.split("/")[1], "slug": slug}
            if origin
            else None,
            "origin_reason": None if origin else "not a GitHub URL",
        },
        "remote_default": {"name": default_branch, "tip": remote_tip, "fetched_ref": f"refs/remotes/origin/{default_branch}"},
        "worktrees": list(worktrees),
        "branches": list(branches),
    }


# ---------------------------------------------------------------------------
# Parsing and pure decisions
# ---------------------------------------------------------------------------


class WorktreeParserTests(unittest.TestCase):
    def test_parses_main_linked_detached_locked_and_prunable(self):
        data = (
            b"worktree /repo\0HEAD " + SHA_A.encode() + b"\0branch refs/heads/main\0\0"
            b"worktree /repo--topic\0HEAD " + SHA_B.encode() + b"\0branch refs/heads/topic\0\0"
            b"worktree /repo--detached\0HEAD " + SHA_C.encode() + b"\0detached\0\0"
            b"worktree /repo--locked\0HEAD " + SHA_C.encode() + b"\0branch refs/heads/locked\0locked busy\0\0"
            b"worktree /repo--missing\0HEAD " + SHA_C.encode() + b"\0branch refs/heads/gone\0prunable gitdir file points to non-existent location\0\0"
        )
        records = gcw.parse_worktree_porcelain(data)
        self.assertEqual([r.path for r in records], ["/repo", "/repo--topic", "/repo--detached", "/repo--locked", "/repo--missing"])
        self.assertEqual(records[0].branch_ref, "refs/heads/main")
        self.assertTrue(records[2].detached)
        self.assertEqual(records[3].locked, "busy")
        self.assertEqual(records[4].prunable, "gitdir file points to non-existent location")

    def test_handles_missing_trailing_separator(self):
        data = b"worktree /repo\0HEAD " + SHA_A.encode() + b"\0branch refs/heads/main"
        records = gcw.parse_worktree_porcelain(data)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].path, "/repo")

    def test_locked_without_reason(self):
        data = b"worktree /repo\0HEAD " + SHA_A.encode() + b"\0branch refs/heads/main\0locked\0\0"
        records = gcw.parse_worktree_porcelain(data)
        self.assertEqual(records[0].locked, "")


class RefParserTests(unittest.TestCase):
    def test_splits_nul_fields_and_newline_records(self):
        data = (
            b"refs/heads/alpha\0" + SHA_A.encode() + b"\0commit\0\0refs/remotes/origin/alpha\n"
            b"refs/heads/beta\0" + SHA_B.encode() + b"\0commit\0refs/heads/alpha\0refs/heads/alpha\n"
        )
        records = gcw.parse_ref_records(data)
        self.assertEqual([r.name for r in records], ["alpha", "beta"])
        self.assertEqual(records[1].symbolic, "refs/heads/alpha")
        self.assertEqual(records[1].upstream, "refs/heads/alpha")
        self.assertEqual(records[0].tip, SHA_A)

    def test_ignores_non_head_refs(self):
        data = b"refs/tags/v1\0" + SHA_A.encode() + b"\0commit\0\0\n"
        self.assertEqual(gcw.parse_ref_records(data), [])


class StatusParserTests(unittest.TestCase):
    def test_buckets_changes_conflicts_and_unknown(self):
        data = (
            b"1 MM N... 100644 100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" a.txt\0"
            b"u UU N... 100644 100644 100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" " + b"3" * 40 + b" b.txt\0"
            b"z unexpected\0"
        )
        parsed = gcw.parse_status_v2(data)
        self.assertEqual(parsed["changes"], [{"xy": "MM", "path": "a.txt"}])
        self.assertEqual(parsed["conflicts"], [{"xy": "UU", "path": "b.txt"}])
        self.assertEqual(len(parsed["unknown"]), 1)

    def test_keeps_paths_with_spaces_intact(self):
        data = b"1 M. N... 100644 100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" a file with spaces.txt\0"
        parsed = gcw.parse_status_v2(data)
        self.assertEqual(parsed["changes"][0]["path"], "a file with spaces.txt")


class LsFilesParserTests(unittest.TestCase):
    def test_flags_assume_unchanged_and_skip_worktree(self):
        data = b"H .gitignore\0h a.txt\0S b.txt\0s c.txt\0? d.txt\0"
        safe, flagged = gcw.parse_ls_files_verbose(data)
        self.assertEqual(safe, [".gitignore"])
        self.assertEqual(len(flagged), 4)

    def test_reports_unrecognized_records(self):
        _, flagged = gcw.parse_ls_files_verbose(b"notag\n\0")
        self.assertEqual(len(flagged), 1)


class JsonDocumentTests(unittest.TestCase):
    def test_parses_concatenated_arrays(self):
        documents = gcw.parse_json_documents(b'[{"a":1}]\n[{"b":2}]')
        self.assertEqual(len(documents), 2)

    def test_rejects_malformed_json(self):
        with self.assertRaises(gcw.HelperError):
            gcw.parse_json_documents(b"{not json")

    def test_flattens_slurped_pages(self):
        documents = gcw.parse_json_documents(b'[[{"a":1}],[{"b":2}]]')
        self.assertEqual(gcw.flatten_documents(documents), [{"a": 1}, {"b": 2}])

    def test_flattens_flat_array(self):
        self.assertEqual(gcw.flatten_documents([[{"a": 1}]]), [{"a": 1}])


class OriginUrlTests(unittest.TestCase):
    def test_accepts_common_github_forms(self):
        for url in (
            "https://github.com/owner/repo.git",
            "https://github.com/owner/repo",
            "http://github.com/owner/repo.git",
            "ssh://git@github.com/owner/repo.git",
            "git@github.com:owner/repo.git",
            "git@GitHub.com:owner/repo.git",
            "github.com:owner/repo.git",
        ):
            with self.subTest(url=url):
                identity, reason = gcw.parse_origin_url(url)
                self.assertIsNone(reason)
                self.assertEqual(identity.slug, "owner/repo")
                self.assertEqual(identity.host, "github.com")

    def test_rejects_unsupported_locations(self):
        for url in (
            "",
            "/tmp/local/repo.git",
            "file:///tmp/local/repo.git",
            "https://gitlab.com/owner/repo.git",
            "https://github.com/owner/repo/extra.git",
            "https://github.com/owner.git",
            "git@github.com:owner/repo/extra.git",
        ):
            with self.subTest(url=url):
                identity, reason = gcw.parse_origin_url(url)
                self.assertIsNone(identity)
                self.assertIsNotNone(reason)


class PreservationDecisionTests(unittest.TestCase):
    def test_merged_pr_with_exact_head_proves_tip(self):
        docs = [[pr_payload(7, SHA_A)], [pr_payload(9, SHA_B, base="someone/fork")]]
        result = gcw.evaluate_pr_evidence(docs, "owner/repo", SHA_A)
        self.assertTrue(result.proven)
        self.assertEqual(result.method, "pr")
        self.assertEqual(result.evidence["number"], 7)

    def test_open_pr_does_not_prove(self):
        result = gcw.evaluate_pr_evidence([[pr_payload(1, SHA_A, merged=False)]], "owner/repo", SHA_A)
        self.assertFalse(result.proven)

    def test_reused_branch_name_with_older_merged_pr_does_not_prove(self):
        result = gcw.evaluate_pr_evidence([[pr_payload(1, SHA_B)]], "owner/repo", SHA_A)
        self.assertFalse(result.proven)
        self.assertIn("no merged PR", result.reason)

    def test_mismatched_base_repository_does_not_prove(self):
        result = gcw.evaluate_pr_evidence([[pr_payload(1, SHA_A, base="other/repo")]], "owner/repo", SHA_A)
        self.assertFalse(result.proven)

    def test_skips_malformed_entries_but_still_matches(self):
        docs = [[{"number": "x"}, pr_payload(4, SHA_A)]]
        result = gcw.evaluate_pr_evidence(docs, "owner/repo", SHA_A)
        self.assertTrue(result.proven)
        self.assertEqual(result.evidence["number"], 4)

    def test_all_malformed_is_unproven(self):
        result = gcw.evaluate_pr_evidence([[{"nope": True}]], "owner/repo", SHA_A)
        self.assertFalse(result.proven)
        self.assertIn("unproven", result.reason)

    def test_prefers_lowest_matching_pr_number(self):
        docs = [[pr_payload(30, SHA_A), pr_payload(12, SHA_A)]]
        result = gcw.evaluate_pr_evidence(docs, "owner/repo", SHA_A)
        self.assertEqual(result.evidence["number"], 12)


class PreservationFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = GitFixture(Path(self.temp.name))
        self.repo_fixture = self.fixture.repo()
        self.repo_fixture.use_github_origin()
        self.runner = self.fixture.runner
        self.repo = gcw.resolve_repository(self.runner, self.repo_fixture.path)

    def test_ancestry_success_short_circuits_pr_lookup(self):
        gh, fake = gh_client(self.runner)
        result = gcw.preservation_for_tip(self.runner, self.repo, self.repo.main_head, self.repo.main_head, gh)
        self.assertTrue(result.proven)
        self.assertEqual(result.method, "ancestry")
        self.assertEqual(fake.calls, [])

    def test_ancestry_failure_falls_back_to_merged_pr(self):
        extra = self.repo_fixture.commit("extra", "b.txt", "two\n")
        gh, fake = gh_client(self.runner, gh_pulls_handler({extra: [pr_payload(5, extra)]}))
        result = gcw.preservation_for_tip(self.runner, self.repo, extra, self.repo.main_head, gh)
        self.assertTrue(result.proven)
        self.assertEqual(result.method, "pr")
        self.assertTrue(fake.calls)

    def test_ancestry_failure_without_usable_gh_stays_unproven(self):
        extra = self.repo_fixture.commit("extra", "b.txt", "two\n")
        gh, _ = gh_client(self.runner, gh_pulls_handler({extra: [pr_payload(5, extra, merged=False)]}))
        result = gcw.preservation_for_tip(self.runner, self.repo, extra, self.repo.main_head, gh)
        self.assertFalse(result.proven)
        self.assertIn("no merged PR", result.reason)

    def test_missing_upstream_neither_blocks_nor_proves(self):
        self.repo_fixture.git("branch", "--unset-upstream")
        branch = self.repo_fixture.branch("no-upstream")
        gh, _ = gh_client(self.runner)
        result = gcw.preservation_for_tip(self.runner, self.repo, branch, self.repo.main_head, gh)
        self.assertTrue(result.proven)
        self.assertEqual(result.method, "ancestry")

    def test_ancestry_error_is_distinct_from_not_ancestor(self):
        def handler(args):
            return gcw.Result(128, b"", b"fatal: bad object")

        failing = FakeGhRunner(self.runner, handler)
        broken = gcw.SubprocessRunner(env_overrides=self.fixture.env)
        broken.run = lambda args, cwd=None, timeout=None, input_bytes=None: (  # type: ignore[assignment]
            gcw.Result(128, b"", b"fatal: bad object")
        )
        result = gcw.ancestry_preservation(broken, self.repo, SHA_A, SHA_B)
        self.assertFalse(result.proven)
        self.assertIn("errored", result.reason)
        self.assertIsNotNone(failing)

    def test_origin_without_github_identity_uses_ancestry_only(self):
        plain = self.fixture.repo(name="plain")
        repo = gcw.resolve_repository(self.runner, plain.path)
        self.assertIsNone(repo.origin)
        gh, fake = gh_client(self.runner)
        result = gcw.preservation_for_tip(self.runner, repo, repo.main_head, repo.main_head, gh)
        self.assertTrue(result.proven)
        self.assertEqual(result.method, "ancestry")
        self.assertEqual(fake.calls, [])


class GhClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = GitFixture(Path(self.temp.name))

    def client_with(self, handler) -> gcw.GhClient:
        return gh_client(self.fixture.runner, handler)[0]

    def test_targets_explicit_host_and_repository(self):
        client = self.client_with(gh_pulls_handler({SHA_A: [pr_payload(1, SHA_A)]}))
        lookup = client.list_commit_pulls("owner/repo", SHA_A)
        self.assertIsNone(lookup.error)
        args = client.runner.calls[-1]
        self.assertIn("--hostname", args)
        self.assertIn("github.com", args)
        self.assertIn("repos/owner/repo/commits/" + SHA_A + "/pulls", args)

    def test_authentication_failure_is_reported_conservatively(self):
        client = self.client_with(lambda args: gcw.Result(4, b"", b"HTTP 401: Bad credentials"))
        lookup = client.list_commit_pulls("owner/repo", SHA_A)
        self.assertIsNone(lookup.documents)
        self.assertIn("authenticate", lookup.error)

    def test_api_failure_is_reported_conservatively(self):
        client = self.client_with(lambda args: gcw.Result(1, b"", b"HTTP 500: server error"))
        lookup = client.list_commit_pulls("owner/repo", SHA_A)
        self.assertIn("failed", lookup.error)

    def test_timeout_is_reported_conservatively(self):
        client = self.client_with(lambda args: gcw.Result(-1, b"", b"", timed_out=True, error="timeout"))
        lookup = client.list_commit_pulls("owner/repo", SHA_A)
        self.assertIn("timed out", lookup.error)

    def test_malformed_response_is_reported_conservatively(self):
        client = self.client_with(lambda args: gcw.Result(0, b"{oops", b""))
        lookup = client.list_commit_pulls("owner/repo", SHA_A)
        self.assertIsNone(lookup.documents)
        self.assertIn("malformed", lookup.error)

    def test_missing_gh_binary_is_reported_conservatively(self):
        client = self.client_with(lambda args: gcw.Result(127, b"", b"No such file or directory"))
        lookup = client.list_commit_pulls("owner/repo", SHA_A)
        self.assertIsNotNone(lookup.error)

    def test_retries_without_slurp_when_unsupported(self):
        attempts: list[list[str]] = []

        def handler(args: list[str]) -> gcw.Result:
            attempts.append(args)
            if "--slurp" in args:
                return gcw.Result(1, b"", b"unknown flag: --slurp")
            return gcw.Result(0, json.dumps([pr_payload(3, SHA_A)]).encode(), b"")

        client = self.client_with(handler)
        lookup = client.list_commit_pulls("owner/repo", SHA_A)
        self.assertIsNone(lookup.error)
        self.assertEqual(len(lookup.documents), 1)
        self.assertNotIn("--slurp", attempts[-1])


class RunnerTests(unittest.TestCase):
    def test_clears_repository_redirecting_environment(self):
        redirects = {
            "GIT_DIR": "/elsewhere",
            "GIT_WORK_TREE": "/elsewhere",
            "GIT_INDEX_FILE": "/elsewhere/index",
            "GIT_OBJECT_DIRECTORY": "/elsewhere/objects",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "true",
        }
        with mock.patch.dict(os.environ, redirects):
            runner = gcw.SubprocessRunner()
        for key in redirects:
            self.assertNotIn(key, runner.env)
        self.assertEqual(runner.env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(runner.env["LC_ALL"], "C")

    def test_explicit_overrides_win_over_clearing(self):
        runner = gcw.SubprocessRunner(env_overrides={"GIT_CONFIG_GLOBAL": "/tmp/empty"})
        self.assertEqual(runner.env["GIT_CONFIG_GLOBAL"], "/tmp/empty")

    def test_missing_executable_becomes_a_result(self):
        runner = gcw.SubprocessRunner()
        result = runner.run(["definitely-not-a-real-binary-xyz"])
        self.assertEqual(result.returncode, 127)
        self.assertIsNotNone(result.error)

    def test_timeout_is_reported(self):
        runner = gcw.SubprocessRunner()
        result = runner.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)


class PathTests(unittest.TestCase):
    def test_is_within_matches_children_only(self):
        parent = os.path.join(os.sep, "tmp", "parent")
        self.assertTrue(gcw.is_within(parent, os.path.join(parent, "child")))
        self.assertTrue(gcw.is_within(parent, parent))
        self.assertFalse(gcw.is_within(parent, os.path.join(os.sep, "tmp", "parent-sibling")))

    def test_display_escapes_control_characters(self):
        self.assertEqual(gcw.display("a\tb"), "a\\tb")
        self.assertEqual(gcw.display("a\nb"), "a\\nb")
        self.assertNotIn("\t", gcw.bounded("a\tb"))

    def test_canonical_normalizes_equivalent_forms(self):
        with tempfile.TemporaryDirectory() as temp:
            nested = os.path.join(temp, "sub")
            os.makedirs(nested)
            self.assertEqual(gcw.canonical(nested), gcw.canonical(os.path.join(temp, ".", "sub")))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_canonical_resolves_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            real = os.path.join(temp, "real")
            os.makedirs(real)
            link = os.path.join(temp, "link")
            try:
                os.symlink(real, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not permitted")
            self.assertEqual(gcw.canonical(link), gcw.canonical(real))


# ---------------------------------------------------------------------------
# Plan construction and validation (pure)
# ---------------------------------------------------------------------------


class PlanTests(unittest.TestCase):
    def snapshot_with_overridable(self) -> dict:
        wt = worktree_entry(
            "/repo--topic",
            eligible=False,
            overridable=["ignored-files"],
            requires_ignored_override=True,
            fingerprint="sha256:" + "1" * 64,
        )
        main = worktree_entry("/repo", eligible=False, blocking=["main worktree is always retained"], is_main=True, branch_ref="refs/heads/main")
        return make_snapshot(
            worktrees=[wt, main],
            branches=[branch_entry("topic", eligible=True, used_by_worktrees=["/repo--topic"])],
        )

    def test_builds_removal_and_branch_deletion_with_dependency(self):
        snapshot = self.snapshot_with_overridable()
        plan = gcw.build_plan(
            snapshot,
            remove_worktrees=["/repo--topic"],
            delete_branches=["topic"],
            allow_ignored=["/repo--topic"],
        )
        kinds = [action["kind"] for action in plan["actions"]]
        self.assertEqual(kinds, ["remove_worktree", "delete_branch"])
        removal = plan["actions"][0]
        self.assertTrue(removal["allow_ignored"])
        self.assertEqual(removal["ignored_fingerprint"], "sha256:" + "1" * 64)
        self.assertEqual(plan["actions"][1]["depends_on"], ["remove_worktree:/repo--topic"])
        self.assertTrue(plan["retained"])

    def test_requires_override_for_ignored_files(self):
        snapshot = self.snapshot_with_overridable()
        with self.assertRaises(gcw.HelperError) as caught:
            gcw.build_plan(snapshot, remove_worktrees=["/repo--topic"])
        self.assertIn("--allow-ignored", str(caught.exception))

    def test_rejects_override_that_cannot_bypass_other_protections(self):
        blocked = worktree_entry(
            "/repo--dirty",
            eligible=False,
            blocking=["staged or unstaged change: MM a.txt"],
            overridable=["ignored-files"],
            requires_ignored_override=True,
            fingerprint="sha256:" + "2" * 64,
        )
        snapshot = make_snapshot(worktrees=[blocked])
        with self.assertRaises(gcw.HelperError) as caught:
            gcw.build_plan(snapshot, remove_worktrees=["/repo--dirty"], allow_ignored=["/repo--dirty"])
        self.assertIn("staged or unstaged change", str(caught.exception))

    def test_rejects_override_without_selection(self):
        snapshot = self.snapshot_with_overridable()
        with self.assertRaises(gcw.HelperError):
            gcw.build_plan(snapshot, allow_ignored=["/repo--topic"])

    def test_rejects_override_for_clean_worktree(self):
        clean = worktree_entry("/repo--clean", eligible=True)
        snapshot = make_snapshot(worktrees=[clean])
        with self.assertRaises(gcw.HelperError):
            gcw.build_plan(snapshot, remove_worktrees=["/repo--clean"], allow_ignored=["/repo--clean"])

    def test_rejects_unknown_and_duplicate_identifiers(self):
        snapshot = self.snapshot_with_overridable()
        with self.assertRaises(gcw.HelperError):
            gcw.build_plan(snapshot, remove_worktrees=["/nope"])
        with self.assertRaises(gcw.HelperError):
            gcw.build_plan(snapshot, remove_worktrees=["/repo--topic", "/repo--topic"])

    def test_rejects_branch_still_used_by_retained_worktree(self):
        snapshot = make_snapshot(
            worktrees=[worktree_entry("/repo--topic", blocking=["staged or unstaged change: MM a.txt"])],
            branches=[branch_entry("topic", eligible=True, used_by_worktrees=["/repo--topic"])],
        )
        with self.assertRaises(gcw.HelperError) as caught:
            gcw.build_plan(snapshot, delete_branches=["topic"])
        self.assertIn("still used", str(caught.exception))

    def test_rejects_blocked_branch(self):
        snapshot = make_snapshot(branches=[branch_entry("topic", blocking=["the default branch is always retained"])])
        with self.assertRaises(gcw.HelperError):
            gcw.build_plan(snapshot, delete_branches=["topic"])

    def test_digest_is_deterministic_across_key_order(self):
        snapshot = self.snapshot_with_overridable()
        first = gcw.build_plan(snapshot, remove_worktrees=["/repo--topic"], allow_ignored=["/repo--topic"])
        reordered = dict(reversed(list(first.items())))
        self.assertEqual(gcw.plan_digest(first), gcw.plan_digest(reordered))

    def test_artifact_inside_selected_worktree_is_rejected(self):
        snapshot = self.snapshot_with_overridable()
        with self.assertRaises(gcw.HelperError):
            gcw.build_plan(
                snapshot,
                remove_worktrees=["/repo--topic"],
                allow_ignored=["/repo--topic"],
                output_path="/repo--topic/plan.json",
            )


class PlanValidationTests(unittest.TestCase):
    def valid_document(self) -> dict:
        snapshot = make_snapshot(
            worktrees=[worktree_entry("/repo--topic", eligible=True)],
            branches=[branch_entry("topic", eligible=True, used_by_worktrees=["/repo--topic"])],
        )
        plan = gcw.build_plan(snapshot, remove_worktrees=["/repo--topic"], delete_branches=["topic"])
        return gcw.plan_file(plan)

    def test_round_trips_through_a_file(self):
        document = self.valid_document()
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "plan.json")
            gcw.write_json_file(path, document)
            plan, digest = gcw.load_plan_file(path)
            self.assertEqual(digest, document["digest"])
            self.assertEqual(plan["actions"][0]["id"], "/repo--topic")

    def test_rejects_tampered_content(self):
        document = self.valid_document()
        document["plan"]["actions"][0]["head"] = "9" * 40
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "plan.json")
            gcw.write_json_file(path, document)
            with self.assertRaises(gcw.HelperError) as caught:
                gcw.load_plan_file(path)
            self.assertIn("digest", str(caught.exception))

    def test_rejects_unknown_fields(self):
        document = self.valid_document()
        document["plan"]["extra"] = True
        with self.assertRaises(gcw.HelperError):
            gcw.validate_plan(document["plan"])

    def test_rejects_allow_ignored_without_fingerprint(self):
        document = self.valid_document()
        document["plan"]["actions"][0]["allow_ignored"] = True
        with self.assertRaises(gcw.HelperError):
            gcw.validate_plan(document["plan"])

    def test_rejects_bad_dependency(self):
        document = self.valid_document()
        document["plan"]["actions"][1]["depends_on"] = ["remove_worktree:/somewhere-else"]
        with self.assertRaises(gcw.HelperError):
            gcw.validate_plan(document["plan"])

    def test_rejects_non_head_branch_ref(self):
        document = self.valid_document()
        document["plan"]["actions"][1]["ref"] = "refs/tags/topic"
        with self.assertRaises(gcw.HelperError):
            gcw.validate_plan(document["plan"])

    def test_rejects_tip_mismatch_with_expected(self):
        document = self.valid_document()
        ref = document["plan"]["actions"][1]["ref"]
        document["plan"]["expected"]["branches"][ref] = "7" * 40
        with self.assertRaises(gcw.HelperError):
            gcw.validate_plan(document["plan"])

    def test_rejects_retained_target_that_is_also_an_action(self):
        document = self.valid_document()
        document["plan"]["retained"].append({"kind": "worktree", "id": "/repo--topic", "reason": "no"})
        with self.assertRaises(gcw.HelperError):
            gcw.validate_plan(document["plan"])

    def test_rejects_missing_repository_identity(self):
        document = self.valid_document()
        del document["plan"]["repository_identity"]
        with self.assertRaises(gcw.HelperError):
            gcw.validate_plan(document["plan"])


# ---------------------------------------------------------------------------
# Integration base
# ---------------------------------------------------------------------------


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = GitFixture(self.base)
        self.repo = self.fixture.repo()
        self.runner = self.fixture.runner

    def inspect(self, path: str | None = None, gh: gcw.GhClient | None = None) -> dict:
        return gcw.inspect_repository(self.runner, path or self.repo.path, gh=gh)

    def entry_for(self, snapshot: dict, path: str) -> dict:
        for entry in snapshot["worktrees"]:
            if gcw.canonical(entry["id"]) == gcw.canonical(path):
                return entry
        raise AssertionError(f"no worktree entry for {path}")

    def branch_for(self, snapshot: dict, name: str) -> dict:
        for entry in snapshot["branches"]:
            if entry["id"] == name:
                return entry
        raise AssertionError(f"no branch entry for {name}")

    def new_topic_worktree(self, branch: str = "topic", location: str = "wt-topic") -> str:
        path = str(self.base / location)
        self.repo.git("worktree", "add", "-q", "-b", branch, path)
        return path

    def blocking_text(self, entry: dict) -> str:
        return " | ".join(entry["eligibility"]["blocking"])

    def exclude(self, *patterns: str) -> None:
        path = self.repo.root / ".git" / "info" / "exclude"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(patterns) + "\n", encoding="utf-8")


class DiscoveryTests(IntegrationBase):
    def test_main_worktree_input_is_accepted(self):
        snapshot = self.inspect()
        self.assertEqual(snapshot["repository"]["main_branch"], "main")
        self.assertEqual(snapshot["remote_default"]["name"], "main")
        self.assertTrue(any(entry["is_main"] for entry in snapshot["worktrees"]))

    def test_linked_worktree_input_is_rejected(self):
        path = self.new_topic_worktree()
        with self.assertRaises(gcw.HelperError) as caught:
            self.inspect(path)
        self.assertIn("linked worktree", str(caught.exception))

    def test_missing_and_non_repository_inputs_are_rejected(self):
        with self.assertRaises(gcw.HelperError):
            self.inspect(str(self.base / "does-not-exist"))
        empty = self.base / "empty"
        empty.mkdir()
        with self.assertRaises(gcw.HelperError):
            self.inspect(str(empty))

    def test_bare_repository_is_rejected(self):
        bare = self.base / "bare.git"
        self.fixture.git("init", "-q", "--bare", str(bare))
        with self.assertRaises(gcw.HelperError):
            self.inspect(str(bare))

    def test_unborn_head_is_rejected(self):
        fresh = self.base / "fresh"
        self.fixture.git("init", "-q", "-b", "main", str(fresh))
        self.fixture.git("remote", "add", "origin", str(self.repo.origin), cwd=str(fresh))
        with self.assertRaises(gcw.HelperError) as caught:
            self.inspect(str(fresh))
        self.assertIn("unborn", str(caught.exception))

    def test_detached_main_head_is_rejected(self):
        self.repo.git("checkout", "-q", "--detach")
        with self.assertRaises(gcw.HelperError) as caught:
            self.inspect()
        self.assertIn("detached", str(caught.exception))

    def test_nonstandard_default_branch_name(self):
        repo = self.fixture.repo(name="trunk-repo", default_branch="trunk")
        snapshot = gcw.inspect_repository(self.runner, repo.path)
        self.assertEqual(snapshot["repository"]["main_branch"], "trunk")
        self.assertEqual(snapshot["remote_default"]["name"], "trunk")

    def test_stale_origin_head_is_ignored(self):
        self.repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/does-not-exist")
        snapshot = self.inspect()
        self.assertEqual(snapshot["remote_default"]["name"], "main")

    def test_main_branch_mismatch_with_remote_default_is_rejected(self):
        self.repo.git("branch", "other")
        self.repo.git("checkout", "-q", "other")
        with self.assertRaises(gcw.HelperError) as caught:
            self.inspect()
        self.assertIn("remote default branch", str(caught.exception))

    def test_refresh_failure_fails_closed(self):
        self.repo.git("remote", "set-url", "origin", str(self.base / "missing-origin.git"))
        with self.assertRaises(gcw.HelperError) as caught:
            self.inspect()
        self.assertIn("remote default branch", str(caught.exception))

    def test_default_branch_change_during_refresh_is_rejected(self):
        calls = {"count": 0}
        real = self.runner

        class ChangingRunner:
            def run(self_inner, args, cwd=None, timeout=None, input_bytes=None):
                result = real.run(args, cwd=cwd, timeout=timeout, input_bytes=input_bytes)
                if "ls-remote" in args:
                    calls["count"] += 1
                    if calls["count"] >= 2:
                        return gcw.Result(
                            0,
                            b"ref: refs/heads/renamed\tHEAD\n" + self.repo.sha().encode() + b"\tHEAD\n",
                            b"",
                        )
                return result

        with self.assertRaises(gcw.HelperError) as caught:
            gcw.inspect_repository(ChangingRunner(), self.repo.path)
        self.assertIn("changed during refresh", str(caught.exception))

    def test_shallow_history_is_rejected(self):
        self.fixture.write(self.repo.root / ".git" / "shallow", self.repo.sha() + "\n")
        with self.assertRaises(gcw.HelperError) as caught:
            self.inspect()
        self.assertIn("shallow", str(caught.exception))

    def test_grafted_history_is_rejected(self):
        self.fixture.write(self.repo.root / ".git" / "info" / "grafts", self.repo.sha() + "\n")
        with self.assertRaises(gcw.HelperError) as caught:
            self.inspect()
        self.assertIn("grafted", str(caught.exception))

    def test_replaced_history_is_rejected(self):
        old = self.repo.sha()
        self.repo.commit("two", "b.txt", "two\n")
        self.repo.git("replace", old, self.repo.sha())
        with self.assertRaises(gcw.HelperError) as caught:
            self.inspect()
        self.assertIn("replaced", str(caught.exception))

    def test_arbitrarily_located_worktrees_are_discovered(self):
        nested = self.base / "far" / "away" / "wt"
        nested.parent.mkdir(parents=True)
        self.repo.git("worktree", "add", "-q", "-b", "far", str(nested))
        snapshot = self.inspect()
        entry = self.entry_for(snapshot, str(nested))
        self.assertTrue(entry["eligibility"]["eligible"], self.blocking_text(entry))

    def test_detached_worktree_is_retained(self):
        detached = str(self.base / "wt-detached")
        self.repo.git("worktree", "add", "-q", "--detach", detached)
        entry = self.entry_for(self.inspect(), detached)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("detached", self.blocking_text(entry))

    def test_locked_worktree_is_retained(self):
        locked = self.new_topic_worktree(branch="locked-topic", location="wt-locked")
        self.repo.git("worktree", "lock", locked, "--reason", "busy")
        entry = self.entry_for(self.inspect(), locked)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("locked", self.blocking_text(entry))

    def test_prunable_registration_is_retained_and_keeps_branch(self):
        path = self.new_topic_worktree(branch="prunable-topic", location="wt-prunable")
        shutil.rmtree(path)
        snapshot = self.inspect()
        entry = self.entry_for(snapshot, path)
        self.assertIn("prunable", self.blocking_text(entry))
        branch = self.branch_for(snapshot, "prunable-topic")
        self.assertIn(path, branch["used_by_worktrees"])
        with self.assertRaises(gcw.HelperError) as caught:
            gcw.build_plan(snapshot, delete_branches=["prunable-topic"])
        self.assertIn("still used", str(caught.exception))

    def test_live_worktree_branch_records_its_registration(self):
        path = self.new_topic_worktree(branch="live-topic", location="wt-live")
        branch = self.branch_for(self.inspect(), "live-topic")
        self.assertEqual(branch["used_by_worktrees"], [path])

    def test_branch_usage_records_every_registration(self):
        records = [
            gcw.WorktreeRecord(path="/a", head=SHA_A, branch_ref="refs/heads/shared", detached=False, locked=None, prunable=None),
            gcw.WorktreeRecord(path="/b", head=SHA_A, branch_ref="refs/heads/shared", detached=False, locked=None, prunable="stale"),
        ]
        usage = gcw.branch_usage_by_ref(records)
        self.assertEqual(usage["refs/heads/shared"], ["/a", "/b"])

    def test_standalone_branch_without_worktree_is_discovered(self):
        tip = self.repo.branch("standalone")
        branch = self.branch_for(self.inspect(), "standalone")
        self.assertEqual(branch["tip"], tip)
        self.assertEqual(branch["used_by_worktrees"], [])
        self.assertTrue(branch["eligibility"]["eligible"])


# ---------------------------------------------------------------------------
# File protections
# ---------------------------------------------------------------------------


class FileProtectionTests(IntegrationBase):
    def test_staged_change_blocks_removal(self):
        path = self.new_topic_worktree()
        self.fixture.write(Path(path) / "staged.txt", "staged\n")
        self.repo.git("-C", path, "add", "staged.txt")
        entry = self.entry_for(self.inspect(), path)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("staged or unstaged change", self.blocking_text(entry))

    def test_unstaged_change_blocks_removal(self):
        path = self.new_topic_worktree()
        self.fixture.write(Path(path) / "a.txt", "changed\n")
        entry = self.entry_for(self.inspect(), path)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("staged or unstaged change", self.blocking_text(entry))

    def test_nonignored_untracked_file_blocks_removal(self):
        path = self.new_topic_worktree()
        self.fixture.write(Path(path) / "untracked.txt", "new\n")
        entry = self.entry_for(self.inspect(), path)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("untracked file", self.blocking_text(entry))

    def test_ignored_files_require_an_override(self):
        path = self.new_topic_worktree()
        self.exclude("build/")
        self.fixture.write(Path(path) / "build" / "out.bin", "payload\n")
        entry = self.entry_for(self.inspect(), path)
        self.assertTrue(entry["requires_ignored_override"])
        self.assertIn("ignored-files", entry["eligibility"]["overridable"])
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertFalse(entry["eligibility"]["blocking"])
        self.assertEqual(entry["file_state"]["ignored"], ["build/out.bin"])
        self.assertTrue(entry["file_state"]["ignored_fingerprint"].startswith("sha256:"))

    def test_ignored_fingerprint_changes_with_content(self):
        path = self.new_topic_worktree()
        self.exclude("cache/")
        target = Path(path) / "cache" / "state.bin"
        self.fixture.write(target, "one\n")
        first = self.entry_for(self.inspect(), path)["file_state"]["ignored_fingerprint"]
        self.fixture.write(target, "two\n")
        second = self.entry_for(self.inspect(), path)["file_state"]["ignored_fingerprint"]
        self.assertNotEqual(first, second)

    def test_ignored_symlink_records_its_target(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        path = self.new_topic_worktree()
        self.exclude("link")
        try:
            os.symlink("/etc/hostname", os.path.join(path, "link"))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not permitted")
        first = self.entry_for(self.inspect(), path)["file_state"]["ignored_fingerprint"]
        os.remove(os.path.join(path, "link"))
        os.symlink("/etc/hosts", os.path.join(path, "link"))
        second = self.entry_for(self.inspect(), path)["file_state"]["ignored_fingerprint"]
        self.assertNotEqual(first, second)

    @unittest.skipIf(os.name == "nt", "permission bits are not enforced on Windows")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_ignored_file_is_conservative(self):
        path = self.new_topic_worktree()
        self.exclude("secret.bin")
        secret = Path(path) / "secret.bin"
        self.fixture.write(secret, "payload\n")
        os.chmod(secret, 0)
        try:
            entry = self.entry_for(self.inspect(), path)
        finally:
            os.chmod(secret, stat.S_IRUSR | stat.S_IWUSR)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("file inspection error", self.blocking_text(entry))
        self.assertTrue(any("secret.bin" in item for item in entry["eligibility"]["blocking"]))

    def test_assume_unchanged_index_flag_blocks_removal(self):
        path = self.new_topic_worktree()
        self.repo.git("-C", path, "update-index", "--assume-unchanged", "a.txt")
        entry = self.entry_for(self.inspect(), path)
        self.assertIn("index flag", self.blocking_text(entry))

    def test_skip_worktree_index_flag_blocks_removal(self):
        path = self.new_topic_worktree()
        self.repo.git("-C", path, "update-index", "--skip-worktree", "a.txt")
        entry = self.entry_for(self.inspect(), path)
        self.assertIn("index flag", self.blocking_text(entry))

    def test_sparse_checkout_state_blocks_removal(self):
        path = self.new_topic_worktree()
        git_dir = gcw.decode(self.repo.git("-C", path, "rev-parse", "--absolute-git-dir").stdout).strip()
        self.fixture.write(Path(git_dir) / "info" / "sparse-checkout", "/*\n!/a.txt\n")
        self.repo.git("-C", path, "config", "core.sparseCheckout", "true")
        entry = self.entry_for(self.inspect(), path)
        self.assertIn("sparse-checkout", self.blocking_text(entry))

    def test_populated_submodule_blocks_removal(self):
        dependency = self.fixture.repo(name="dependency")
        path = self.new_topic_worktree()
        self.repo.git(
            "-C",
            path,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            dependency.path,
            "sub",
        )
        self.repo.git("-C", path, "commit", "-qm", "add submodule")
        entry = self.entry_for(self.inspect(), path)
        self.assertIn("populated submodule", self.blocking_text(entry))

    def test_unresolved_conflict_blocks_removal(self):
        path = self.new_topic_worktree()
        self.repo.commit_in(path, "topic change", "a.txt", "topic\n")
        self.repo.commit("main change", "a.txt", "main\n")
        self.repo.git("-C", path, "merge", "main", check=False)
        entry = self.entry_for(self.inspect(), path)
        self.assertTrue(
            "unresolved conflict" in self.blocking_text(entry) or "in-progress Git operation" in self.blocking_text(entry),
            self.blocking_text(entry),
        )

    def test_in_progress_operation_marker_blocks_removal(self):
        path = self.new_topic_worktree()
        git_dir = gcw.decode(self.repo.git("-C", path, "rev-parse", "--absolute-git-dir").stdout).strip()
        self.fixture.write(Path(git_dir) / "MERGE_HEAD", self.repo.sha() + "\n")
        entry = self.entry_for(self.inspect(), path)
        self.assertIn("in-progress Git operation", self.blocking_text(entry))

    def test_main_worktree_is_always_retained(self):
        snapshot = self.inspect()
        main = self.entry_for(snapshot, self.repo.path)
        self.assertTrue(main["is_main"])
        self.assertIn("main worktree is always retained", self.blocking_text(main))
        with self.assertRaises(gcw.HelperError):
            gcw.build_plan(snapshot, remove_worktrees=[self.repo.path])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinked_worktree_path_is_retained(self):
        path = self.new_topic_worktree()
        target = self.base / "plain-target"
        target.mkdir()
        shutil.rmtree(path)
        try:
            os.symlink(str(target), path)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not permitted")
        entry = self.entry_for(self.inspect(), path)
        self.assertIn("symlink", self.blocking_text(entry))

    def test_paths_with_spaces_unicode_and_leading_dash(self):
        names = ["wt with spaces", "wt-ünïcode-dir", "-wt-leading-dash"]
        for index, name in enumerate(names):
            path = str(self.base / name)
            self.repo.git("worktree", "add", "-q", "-b", f"path-topic-{index}", path)
            entry = self.entry_for(self.inspect(), path)
            self.assertTrue(entry["eligibility"]["eligible"], f"{name}: {self.blocking_text(entry)}")

    @unittest.skipIf(os.name == "nt", "Windows forbids control characters in file names")
    def test_paths_with_tab_and_newline(self):
        for index, name in enumerate(["wt-tab\tname", "wt-line\nbreak"]):
            path = str(self.base / name)
            self.repo.git("worktree", "add", "-q", "-b", f"control-topic-{index}", path)
            entry = self.entry_for(self.inspect(), path)
            self.assertTrue(entry["eligibility"]["eligible"], f"{name!r}: {self.blocking_text(entry)}")


# ---------------------------------------------------------------------------
# Apply integration
# ---------------------------------------------------------------------------


class ApplyTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.gh, self.fake_gh = gh_client(self.runner)

    def plan_for(self, *, remove=(), delete=(), allow=()) -> dict:
        snapshot = gcw.inspect_repository(self.runner, self.repo.path, gh=self.gh)
        plan = gcw.build_plan(
            snapshot,
            remove_worktrees=list(remove),
            delete_branches=list(delete),
            allow_ignored=list(allow),
        )
        return gcw.plan_file(plan)

    def apply(self, document: dict, *, digest: str | None = None, runner=None, output_path: str | None = None):
        return gcw.apply_plan(
            runner or self.runner,
            document["plan"],
            digest if digest is not None else document["digest"],
            gh=self.gh,
            output_path=output_path,
        )

    def ref_exists(self, ref: str) -> bool:
        return self.repo.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).returncode == 0

    def test_inspect_removes_nothing(self):
        path = self.new_topic_worktree()
        self.inspect(gh=self.gh)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(self.ref_exists("refs/heads/topic"))

    def test_removes_worktree_and_deletes_branch(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path], delete=["topic"])
        result, code = self.apply(document)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(self.ref_exists("refs/heads/topic"))
        self.assertEqual(result["removed_worktrees"], [path])
        self.assertIn("refs/heads/topic", result["deleted_shas"])

    def test_worktree_removal_does_not_implicitly_delete_the_branch(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])
        result, code = self.apply(document)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(self.ref_exists("refs/heads/topic"))
        self.assertEqual(result["deleted_shas"], {})

    def test_ignored_override_deletes_ignored_files_without_force(self):
        path = self.new_topic_worktree()
        self.exclude("build/")
        self.fixture.write(Path(path) / "build" / "out.bin", "payload\n")
        document = self.plan_for(remove=[path], delete=["topic"], allow=[path])
        self.assertTrue(document["plan"]["actions"][0]["allow_ignored"])
        result, code = self.apply(document)
        self.assertEqual(code, 0, result)
        self.assertFalse(os.path.exists(path))

    def test_changed_ignored_content_is_skipped(self):
        path = self.new_topic_worktree()
        self.exclude("build/")
        target = Path(path) / "build" / "out.bin"
        self.fixture.write(target, "one\n")
        document = self.plan_for(remove=[path], delete=["topic"], allow=[path])
        self.fixture.write(target, "two\n")
        result, code = self.apply(document)
        self.assertEqual(code, 2)
        self.assertEqual(result["actions"][0]["status"], "skipped")
        self.assertIn("ignored files changed", result["actions"][0]["reason"])
        self.assertTrue(os.path.exists(path))
        self.assertTrue(self.ref_exists("refs/heads/topic"))

    def test_partial_outcome_continues_independent_targets(self):
        first = self.new_topic_worktree(branch="topic-a", location="wt-a")
        second = self.new_topic_worktree(branch="topic-b", location="wt-b")
        document = self.plan_for(remove=[first, second], delete=["topic-a", "topic-b"])
        self.fixture.write(Path(first) / "a.txt", "changed\n")
        result, code = self.apply(document)
        self.assertEqual(code, 2)
        statuses = {item["id"]: item["status"] for item in result["actions"]}
        self.assertEqual(statuses[first], "skipped")
        self.assertEqual(statuses[second], "removed")
        self.assertFalse(os.path.exists(second))
        self.assertFalse(self.ref_exists("refs/heads/topic-b"))
        # The skipped worktree keeps its branch, exactly as the safety rule requires.
        self.assertTrue(self.ref_exists("refs/heads/topic-a"))

    def test_advanced_branch_is_skipped(self):
        self.repo.branch("topic")
        document = self.plan_for(delete=["topic"])
        self.repo.git("checkout", "-q", "-b", "advance")
        self.repo.commit("advance", "b.txt", "two\n")
        advanced = self.repo.sha()
        self.repo.git("checkout", "-q", "main")
        self.repo.git("update-ref", "refs/heads/topic", advanced)
        result, code = self.apply(document)
        self.assertEqual(code, 2)
        self.assertIn("advanced", result["actions"][0]["reason"])
        self.assertEqual(self.repo.sha("refs/heads/topic"), advanced)

    def test_newly_attached_branch_is_skipped(self):
        self.repo.branch("topic")
        document = self.plan_for(delete=["topic"])
        attached = str(self.base / "wt-attached")
        self.repo.git("worktree", "add", "-q", attached, "topic")
        result, code = self.apply(document)
        self.assertEqual(code, 2)
        self.assertIn("still used by worktree", result["actions"][0]["reason"])
        self.assertTrue(self.ref_exists("refs/heads/topic"))

    def test_disappearing_worktree_skips_removal_but_allows_branch_deletion(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path], delete=["topic"])
        self.repo.git("worktree", "remove", "--force", path)
        result, code = self.apply(document)
        self.assertEqual(code, 2)
        self.assertEqual(result["actions"][0]["status"], "skipped")
        self.assertEqual(result["actions"][1]["status"], "deleted")
        self.assertFalse(self.ref_exists("refs/heads/topic"))

    def test_locked_after_plan_is_skipped(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path], delete=["topic"])
        self.repo.git("worktree", "lock", path, "--reason", "busy")
        result, code = self.apply(document)
        self.assertEqual(code, 2)
        self.assertIn("locked", result["actions"][0]["reason"])
        self.assertTrue(os.path.exists(path))
        self.assertTrue(self.ref_exists("refs/heads/topic"))

    def test_digest_mismatch_is_rejected(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])
        with self.assertRaises(gcw.HelperError) as caught:
            self.apply(document, digest="0" * 64)
        self.assertIn("does not match", str(caught.exception))

    def test_tampered_action_is_rejected(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])
        document["plan"]["actions"][0]["head"] = "9" * 40
        with self.assertRaises(gcw.HelperError) as caught:
            self.apply(document)
        self.assertIn("does not match", str(caught.exception))

    def test_unauthorized_addition_is_rejected(self):
        first = self.new_topic_worktree(branch="topic-a", location="wt-a")
        second = self.new_topic_worktree(branch="topic-b", location="wt-b")
        document = self.plan_for(remove=[first])
        extra = {
            "kind": "remove_worktree",
            "id": second,
            "path": second,
            "git_dir": "/repo/.git/worktrees/wt-b",
            "head": self.repo.sha(),
            "branch": "refs/heads/topic-b",
            "allow_ignored": False,
            "ignored_fingerprint": None,
            "reason": "added later",
        }
        document["plan"]["actions"].append(extra)
        document["plan"]["expected"]["worktrees"][second] = {
            "path": second,
            "git_dir": extra["git_dir"],
            "head": extra["head"],
            "branch": extra["branch"],
        }
        document["plan"]["retained"] = [
            item for item in document["plan"]["retained"] if not (item["kind"] == "worktree" and item["id"] == second)
        ]
        with self.assertRaises(gcw.HelperError) as caught:
            self.apply(document)
        self.assertIn("does not match", str(caught.exception))
        self.assertTrue(os.path.exists(second))

    def test_action_not_recorded_in_expected_is_rejected(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])
        del document["plan"]["expected"]["worktrees"][path]
        with self.assertRaises(gcw.HelperError):
            gcw.validate_plan(document["plan"])

    def test_changed_repository_identity_is_rejected(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])
        document["plan"]["repository_identity"]["common_dir"] = "/somewhere/else/.git"
        document["digest"] = gcw.plan_digest(document["plan"])
        with self.assertRaises(gcw.HelperError) as caught:
            self.apply(document)
        self.assertIn("identity changed", str(caught.exception))

    def test_changed_main_head_is_rejected(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])
        self.repo.commit("moved main", "b.txt", "two\n")
        with self.assertRaises(gcw.HelperError) as caught:
            self.apply(document)
        self.assertIn("main worktree HEAD changed", str(caught.exception))
        self.assertTrue(os.path.exists(path))

    def test_refresh_failure_at_apply_aborts_everything(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])

        class FailingFetch:
            def __init__(self, real):
                self.real = real

            def run(self, args, cwd=None, timeout=None, input_bytes=None):
                if "fetch" in args:
                    return gcw.Result(1, b"", b"could not read from remote repository")
                return self.real.run(args, cwd=cwd, timeout=timeout, input_bytes=input_bytes)

        with self.assertRaises(gcw.HelperError) as caught:
            self.apply(document, runner=FailingFetch(self.runner))
        self.assertIn("refresh", str(caught.exception))
        self.assertTrue(os.path.exists(path))

    def test_update_ref_failure_is_reported_and_ref_retained(self):
        self.repo.branch("topic")
        document = self.plan_for(delete=["topic"])

        class FailingUpdateRef:
            def __init__(self, real):
                self.real = real

            def run(self, args, cwd=None, timeout=None, input_bytes=None):
                if "update-ref" in args:
                    return gcw.Result(1, b"", b"cannot lock ref: is at a different value")
                return self.real.run(args, cwd=cwd, timeout=timeout, input_bytes=input_bytes)

        result, code = self.apply(document, runner=FailingUpdateRef(self.runner))
        self.assertEqual(code, 2)
        self.assertEqual(result["actions"][0]["status"], "failed")
        self.assertIn("compare-and-delete failed", result["actions"][0]["reason"])
        self.assertTrue(self.ref_exists("refs/heads/topic"))

    def test_worktree_remove_timeout_reports_observed_state(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])

        class TimingOutRemove:
            def __init__(self, real):
                self.real = real

            def run(self, args, cwd=None, timeout=None, input_bytes=None):
                if "worktree" in args and "remove" in args:
                    return gcw.Result(-1, b"", b"", timed_out=True, error="timeout")
                return self.real.run(args, cwd=cwd, timeout=timeout, input_bytes=input_bytes)

        result, code = self.apply(document, runner=TimingOutRemove(self.runner))
        self.assertEqual(code, 2)
        self.assertEqual(result["actions"][0]["status"], "failed")
        self.assertIn("timed out", result["actions"][0]["reason"])
        self.assertTrue(result["actions"][0]["observed"]["registration_present"])
        self.assertTrue(os.path.exists(path))

    def test_reflog_removed_and_branch_config_reported(self):
        self.repo.branch("topic")
        self.repo.git("config", "branch.topic.remote", "origin")
        document = self.plan_for(delete=["topic"])
        result, code = self.apply(document)
        self.assertEqual(code, 0)
        self.assertFalse((self.repo.root / ".git" / "logs" / "refs" / "heads" / "topic").exists())
        self.assertFalse(self.ref_exists("refs/heads/topic"))
        self.assertIn("topic", result["branch_config_leftovers"])
        self.assertIn("branch.topic.remote", result["branch_config_leftovers"]["topic"])
        self.assertEqual(gcw.decode(self.repo.git("config", "--get", "branch.topic.remote").stdout).strip(), "origin")
        self.assertTrue(any("reflog" in note for note in result["notes"]))

    def test_output_artifact_inside_selected_worktree_is_rejected(self):
        path = self.new_topic_worktree()
        document = self.plan_for(remove=[path])
        with self.assertRaises(gcw.HelperError):
            self.apply(document, output_path=os.path.join(path, "result.json"))

    def test_empty_plan_applies_nothing(self):
        document = self.plan_for()
        self.assertEqual(document["plan"]["actions"], [])
        result, code = self.apply(document)
        self.assertEqual(code, 0)
        self.assertEqual(result["actions"], [])
        self.assertTrue(self.ref_exists("refs/heads/main"))

    def test_removal_of_worktree_with_spaces_in_its_path(self):
        path = str(self.base / "wt with spaces")
        self.repo.git("worktree", "add", "-q", "-b", "spaced", path)
        document = self.plan_for(remove=[path], delete=["spaced"])
        result, code = self.apply(document)
        self.assertEqual(code, 0, result)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(self.ref_exists("refs/heads/spaced"))

    @unittest.skipUnless(os.name == "nt", "Windows case aliases only")
    def test_windows_case_alias_selection(self):
        path = self.new_topic_worktree()
        variant = path.upper()
        document = self.plan_for(remove=[variant], delete=[f"refs/heads/topic"])
        result, code = self.apply(document)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))


# ---------------------------------------------------------------------------
# PR preservation and end-to-end command line
# ---------------------------------------------------------------------------


class PrPreservationIntegrationTests(IntegrationBase):
    def setUp(self):
        super().setUp()
        self.repo.use_github_origin()

    def test_squash_merged_head_is_removable_end_to_end(self):
        path = self.new_topic_worktree()
        tip = self.repo.commit_in(path, "topic work", "b.txt", "two\n")
        gh, _ = gh_client(self.runner, gh_pulls_handler({tip: [pr_payload(11, tip)]}))
        snapshot = gcw.inspect_repository(self.runner, self.repo.path, gh=gh)
        entry = self.entry_for(snapshot, path)
        self.assertTrue(entry["eligibility"]["eligible"], self.blocking_text(entry))
        self.assertEqual(entry["preservation"]["method"], "pr")
        self.assertEqual(entry["preservation"]["evidence"]["number"], 11)
        plan = gcw.build_plan(snapshot, remove_worktrees=[path], delete_branches=["topic"])
        result, code = gcw.apply_plan(self.runner, plan, gcw.plan_digest(plan), gh=gh)
        self.assertEqual(code, 0, result)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.repo.git("rev-parse", "--verify", "--quiet", "refs/heads/topic", check=False).returncode, 1)

    def test_additional_commit_after_merged_pr_head_is_not_preserved(self):
        path = self.new_topic_worktree()
        merged_head = self.repo.commit_in(path, "topic work", "b.txt", "two\n")
        self.repo.commit_in(path, "more work", "b.txt", "three\n")
        gh, _ = gh_client(self.runner, gh_pulls_handler({merged_head: [pr_payload(11, merged_head)]}))
        entry = self.entry_for(gcw.inspect_repository(self.runner, self.repo.path, gh=gh), path)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("not preserved", self.blocking_text(entry))

    def test_open_pr_is_not_preserved(self):
        path = self.new_topic_worktree()
        tip = self.repo.commit_in(path, "topic work", "b.txt", "two\n")
        gh, _ = gh_client(self.runner, gh_pulls_handler({tip: [pr_payload(12, tip, merged=False)]}))
        entry = self.entry_for(gcw.inspect_repository(self.runner, self.repo.path, gh=gh), path)
        self.assertFalse(entry["eligibility"]["eligible"])

    def test_mismatched_pr_repository_is_not_preserved(self):
        path = self.new_topic_worktree()
        tip = self.repo.commit_in(path, "topic work", "b.txt", "two\n")
        gh, _ = gh_client(self.runner, gh_pulls_handler({tip: [pr_payload(13, tip, base="someone/else")]}))
        entry = self.entry_for(gcw.inspect_repository(self.runner, self.repo.path, gh=gh), path)
        self.assertFalse(entry["eligibility"]["eligible"])

    def test_auth_failure_keeps_target_but_does_not_abort_inspection(self):
        path = self.new_topic_worktree()
        self.repo.commit_in(path, "topic work", "b.txt", "two\n")
        gh, _ = gh_client(self.runner, lambda args: gcw.Result(4, b"", b"HTTP 401: Bad credentials"))
        snapshot = gcw.inspect_repository(self.runner, self.repo.path, gh=gh)
        entry = self.entry_for(snapshot, path)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("authenticate", self.blocking_text(entry))

    def test_gh_timeout_keeps_target_but_does_not_abort_inspection(self):
        path = self.new_topic_worktree()
        self.repo.commit_in(path, "topic work", "b.txt", "two\n")
        gh, _ = gh_client(self.runner, lambda args: gcw.Result(-1, b"", b"", timed_out=True, error="timeout"))
        snapshot = gcw.inspect_repository(self.runner, self.repo.path, gh=gh)
        entry = self.entry_for(snapshot, path)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("timed out", self.blocking_text(entry))


class TimeoutAndReregistrationTests(IntegrationBase):
    def test_fetch_timeout_at_inspect_fails_closed(self):
        class SlowFetch:
            def __init__(self, real):
                self.real = real

            def run(self, args, cwd=None, timeout=None, input_bytes=None):
                if "fetch" in args:
                    return gcw.Result(-1, b"", b"", timed_out=True, error="timeout")
                return self.real.run(args, cwd=cwd, timeout=timeout, input_bytes=input_bytes)

        with self.assertRaises(gcw.HelperError) as caught:
            gcw.inspect_repository(SlowFetch(self.runner), self.repo.path)
        self.assertIn("timed out", str(caught.exception))

    def test_reregistered_worktree_path_is_skipped(self):
        path = self.new_topic_worktree()
        gh, _ = gh_client(self.runner)
        snapshot = gcw.inspect_repository(self.runner, self.repo.path, gh=gh)
        plan = gcw.build_plan(snapshot, remove_worktrees=[path], delete_branches=["topic"])
        moved = str(self.base / "wt-moved")
        self.repo.git("worktree", "move", path, moved)
        result, code = gcw.apply_plan(self.runner, plan, gcw.plan_digest(plan), gh=gh)
        self.assertEqual(code, 2)
        self.assertEqual(result["actions"][0]["status"], "skipped")
        self.assertIn("disappeared", result["actions"][0]["reason"])
        # The branch is still checked out at the moved registration, so it survives.
        self.assertEqual(result["actions"][1]["status"], "skipped")
        self.assertIn("still used", result["actions"][1]["reason"])
        self.assertTrue(os.path.isdir(moved))
        self.assertTrue(self.repo.git("rev-parse", "--verify", "--quiet", "refs/heads/topic", check=False).returncode == 0)


class CommandLineTests(IntegrationBase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gcw.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_three_phase_run_through_the_cli(self):
        path = self.new_topic_worktree()
        snapshot_path = str(self.base / "snapshot.json")
        plan_path = str(self.base / "plan.json")
        result_path = str(self.base / "result.json")

        code, out, _ = self.run_cli(["inspect", self.repo.path, "--output", snapshot_path, "--gh-path", FAKE_GH])
        self.assertEqual(code, 0)
        self.assertIn("default branch", out)
        self.assertTrue(os.path.isfile(snapshot_path))

        code, out, _ = self.run_cli(
            [
                "plan",
                "--snapshot",
                snapshot_path,
                "--remove-worktree",
                path,
                "--delete-branch",
                "topic",
                "--output",
                plan_path,
            ]
        )
        self.assertEqual(code, 0)
        document = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], gcw.SCHEMA_PLAN_FILE)
        self.assertIn(document["digest"], out)

        code, out, _ = self.run_cli(
            [
                "apply",
                "--plan",
                plan_path,
                "--approved-digest",
                document["digest"],
                "--gh-path",
                FAKE_GH,
                "--output",
                result_path,
            ]
        )
        self.assertEqual(code, 0)
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
        self.assertEqual(result["removed_worktrees"], [path])
        self.assertFalse(os.path.exists(path))
        self.assertIn("removed_worktrees", out)

    def test_cli_reports_errors_with_exit_code_one(self):
        code, _, err = self.run_cli(["inspect", str(self.base / "missing"), "--output", str(self.base / "s.json")])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)

    def test_cli_apply_returns_two_for_partial_outcomes(self):
        path = self.new_topic_worktree()
        snapshot_path = str(self.base / "snapshot.json")
        plan_path = str(self.base / "plan.json")
        self.run_cli(["inspect", self.repo.path, "--output", snapshot_path, "--gh-path", FAKE_GH])
        self.run_cli(["plan", "--snapshot", snapshot_path, "--remove-worktree", path, "--output", plan_path])
        document = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        self.fixture.write(Path(path) / "a.txt", "changed\n")
        code, out, _ = self.run_cli(
            ["apply", "--plan", plan_path, "--approved-digest", document["digest"], "--gh-path", FAKE_GH]
        )
        self.assertEqual(code, 2)
        self.assertIn("\"skipped\"", out)


class SummaryTests(unittest.TestCase):
    def test_snapshot_and_plan_summaries_mention_targets_and_digest(self):
        snapshot = make_snapshot(
            worktrees=[worktree_entry("/repo--topic", eligible=True)],
            branches=[branch_entry("topic", eligible=True, used_by_worktrees=["/repo--topic"])],
        )
        snapshot_lines = gcw.summarize_snapshot(snapshot)
        self.assertTrue(any("/repo--topic" in line for line in snapshot_lines))
        plan = gcw.build_plan(snapshot, remove_worktrees=["/repo--topic"], delete_branches=["topic"])
        document = gcw.plan_file(plan)
        plan_lines = gcw.summarize_plan(document)
        self.assertTrue(any(document["digest"] in line for line in plan_lines))
        self.assertTrue(any("/repo--topic" in line for line in plan_lines))


# ---------------------------------------------------------------------------
# Hardening: configuration independence and ignored-entry framing
# ---------------------------------------------------------------------------


class FileStateConfigurationTests(IntegrationBase):
    CACHE_FLAGS = ("core.fsmonitor=false", "core.untrackedCache=false", "core.ignoreStat=false")

    def test_file_protection_commands_disable_caching_configuration(self):
        class Recorder:
            def __init__(self, real):
                self.real = real
                self.calls: list[list[str]] = []

            def run(self, args, cwd=None, timeout=None, input_bytes=None):
                self.calls.append([str(a) for a in args])
                return self.real.run(args, cwd=cwd, timeout=timeout, input_bytes=input_bytes)

        self.new_topic_worktree()
        recorder = Recorder(self.runner)
        gcw.inspect_repository(recorder, self.repo.path, gh=gh_client(self.runner)[0])
        status_calls = [call for call in recorder.calls if "status" in call]
        listing_calls = [call for call in recorder.calls if "ls-files" in call]
        self.assertTrue(status_calls)
        self.assertTrue(listing_calls)
        for call in [*status_calls, *listing_calls]:
            for flag in self.CACHE_FLAGS:
                self.assertIn(flag, call, call)

    def test_bogus_fsmonitor_does_not_hide_a_tracked_change(self):
        path = self.new_topic_worktree()
        self.repo.git("-C", path, "config", "core.fsmonitor", str(self.base / "bogus-monitor"))
        self.repo.git("-C", path, "config", "core.untrackedCache", "true")
        self.repo.git("-C", path, "config", "core.ignoreStat", "true")
        self.fixture.write(Path(path) / "a.txt", "changed after caching was configured\n")
        entry = self.entry_for(self.inspect(), path)
        self.assertFalse(entry["eligibility"]["eligible"])
        self.assertIn("staged or unstaged change", self.blocking_text(entry))

    def test_untracked_cache_does_not_hide_a_new_file(self):
        path = self.new_topic_worktree()
        self.repo.git("-C", path, "config", "core.untrackedCache", "true")
        self.repo.git("-C", path, "status", "--porcelain")
        self.fixture.write(Path(path) / "brand-new.txt", "new\n")
        entry = self.entry_for(self.inspect(), path)
        self.assertIn("untracked file", self.blocking_text(entry))


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.worktree = Path(self.temp.name) / "wt"
        self.worktree.mkdir()

    def fingerprint(self, paths):
        return gcw.fingerprint_ignored(str(self.worktree), paths)

    def test_fingerprint_is_stable_and_order_independent(self):
        (self.worktree / "one.bin").write_bytes(b"one")
        (self.worktree / "two.bin").write_bytes(b"two")
        first, errors = self.fingerprint(["one.bin", "two.bin"])
        second, _ = self.fingerprint(["two.bin", "one.bin"])
        self.assertEqual(errors, [])
        self.assertEqual(first, second)

    def test_content_and_target_changes_change_the_fingerprint(self):
        target = self.worktree / "state.bin"
        target.write_bytes(b"one")
        first, _ = self.fingerprint(["state.bin"])
        target.write_bytes(b"one!")
        second, _ = self.fingerprint(["state.bin"])
        self.assertNotEqual(first, second)
        if hasattr(os, "symlink"):
            try:
                os.symlink("/etc/hostname", self.worktree / "link")
                os.symlink("/etc/hosts", self.worktree / "link2")
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not permitted")
            first_link, _ = self.fingerprint(["link"])
            second_link, _ = self.fingerprint(["link2"])
            self.assertNotEqual(first_link, second_link)

    def test_escaping_and_absolute_paths_are_rejected(self):
        for path in ("../outside.bin", "/etc/hosts", "sub/../../outside.bin"):
            with self.subTest(path=path):
                fingerprint, errors = self.fingerprint([path])
                self.assertIsNone(fingerprint)
                self.assertTrue(errors)

    def test_missing_entry_is_rejected(self):
        fingerprint, errors = self.fingerprint(["gone.bin"])
        self.assertIsNone(fingerprint)
        self.assertIn("cannot inspect", errors[0])

    def test_directory_entry_is_unsupported(self):
        (self.worktree / "subdir").mkdir()
        fingerprint, errors = self.fingerprint(["subdir"])
        self.assertIsNone(fingerprint)
        self.assertIn("unsupported file type", errors[0])

    @unittest.skipIf(os.name == "nt", "FIFOs are not available on Windows")
    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are not available")
    def test_fifo_entry_is_unsupported(self):
        os.mkfifo(self.worktree / "pipe")
        fingerprint, errors = self.fingerprint(["pipe"])
        self.assertIsNone(fingerprint)
        self.assertIn("unsupported file type", errors[0])

    @unittest.skipIf(os.name == "nt", "permission bits are not enforced on Windows")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_entry_is_rejected(self):
        secret = self.worktree / "secret.bin"
        secret.write_bytes(b"payload")
        os.chmod(secret, 0)
        try:
            fingerprint, errors = self.fingerprint(["secret.bin"])
        finally:
            os.chmod(secret, stat.S_IRUSR | stat.S_IWUSR)
        self.assertIsNone(fingerprint)
        self.assertTrue(any("cannot read" in error for error in errors))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_aliased_parent_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "file.bin").write_bytes(b"payload")
        try:
            os.symlink(str(outside), str(self.worktree / "cache"))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not permitted")
        fingerprint, errors = self.fingerprint(["cache/file.bin"])
        self.assertIsNone(fingerprint)
        self.assertTrue(any("aliased parent" in error for error in errors))

    def test_real_parent_directory_is_accepted(self):
        (self.worktree / "build").mkdir()
        (self.worktree / "build" / "out.bin").write_bytes(b"payload")
        fingerprint, errors = self.fingerprint(["build/out.bin"])
        self.assertEqual(errors, [])
        self.assertTrue(fingerprint.startswith("sha256:"))
