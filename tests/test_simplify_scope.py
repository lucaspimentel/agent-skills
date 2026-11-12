"""Offline tests for skills/simplify/scripts/scope.sh.

Each test runs the script against real repositories created under its own
temporary directory, with an empty global and system Git configuration so
developer settings cannot leak in. The GitHub CLI is replaced by a fake `gh`
placed first on PATH, so the suite needs no network and no credentials.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "simplify" / "scripts"
SCOPE = SCRIPTS / "scope.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")

GH_DENY = "#!/usr/bin/env bash\nexit 1\n"

TEN_LINES = "".join(f"{n}\n" for n in range(1, 11))


@unittest.skipUnless(BASH is not None and GIT is not None, "bash and git are required")
class ScopeScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

        config = self.base / "empty.gitconfig"
        config.write_text("", encoding="utf-8", newline="\n")
        home = self.base / "home"
        home.mkdir()

        self.env = os.environ.copy()
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": str(config),
                "GIT_CONFIG_SYSTEM": str(config),
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "GIT_TERMINAL_PROMPT": "0",
            }
        )

        self.bindir = self.base / "bin"
        self.bindir.mkdir()
        self.set_gh(None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def set_gh(self, body: str | None) -> None:
        script = self.bindir / "gh"
        script.write_text(body or GH_DENY, encoding="utf-8", newline="\n")
        script.chmod(0o755)

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [GIT, *args], cwd=str(cwd or self.base), env=self.env, capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"git {args} failed ({proc.returncode}): {proc.stderr}")
        return proc

    def scope(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env["PATH"] = str(self.bindir) + os.pathsep + self.env.get("PATH", "")
        return subprocess.run(
            [BASH, SCOPE.as_posix(), *args], cwd=str(cwd or self.base), env=env, capture_output=True, text=True
        )

    def write(self, work: Path, path: str, text: str) -> Path:
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        return target

    def make_repo(self) -> tuple[Path, Path]:
        origin = self.base / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin))
        work = self.base / "work"
        self.git("clone", "-q", str(origin), str(work))
        return work, origin

    def commit_all(self, work: Path, message: str) -> None:
        self.git("add", "-A", cwd=work)
        self.git("commit", "-qm", message, cwd=work)

    def push(self, work: Path) -> None:
        self.git("push", "-q", "-u", "origin", "main", cwd=work)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=work)

    def standard_repo(self) -> Path:
        work, _ = self.make_repo()
        self.write(work, "a.txt", TEN_LINES)
        self.write(work, "src/b.txt", "x\ny\nz\n")
        self.write(work, "old.txt", "rename me\n")
        self.commit_all(work, "one")
        self.push(work)
        return work

    # -- pending ---------------------------------------------------------

    def test_pending_lists_modified_and_untracked(self) -> None:
        work = self.standard_repo()
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.write(work, "u.txt", "new\n")

        result = self.scope("pending", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Target: pending changes (working tree vs HEAD", result.stdout)
        self.assertIn("- a.txt (M; changed lines: 2)", result.stdout)
        self.assertIn("- u.txt (added; entire file is in scope)", result.stdout)

    def test_pending_omits_deleted_file(self) -> None:
        work = self.standard_repo()
        (work / "src" / "b.txt").unlink()
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))

        result = self.scope("pending", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("src/b.txt", result.stdout)
        self.assertIn("- a.txt (M; changed lines: 2)", result.stdout)

    def test_pending_modified_file_with_only_deletions(self) -> None:
        work = self.standard_repo()
        lines = TEN_LINES.splitlines(keepends=True)
        self.write(work, "a.txt", "".join(lines[:5] + lines[6:]))

        result = self.scope("pending", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- a.txt (M; deletions only, no current lines to simplify)", result.stdout)

    def test_deletion_only_scope_exits_7(self) -> None:
        work = self.standard_repo()
        (work / "src" / "b.txt").unlink()

        self.assertEqual(self.scope("pending", cwd=work).returncode, 7)

        self.commit_all(work, "two")

        self.assertEqual(self.scope("last-commit", cwd=work).returncode, 7)

    def test_pending_merges_adjacent_hunks(self) -> None:
        work = self.standard_repo()
        changed = TEN_LINES.replace("2\n", "TWO\n").replace("3\n", "THREE\n").replace("10\n", "TEN\n")
        self.write(work, "a.txt", changed)

        result = self.scope("pending", cwd=work)

        self.assertIn("- a.txt (M; changed lines: 2-3, 10)", result.stdout)

    def test_pending_with_unborn_head_lists_everything(self) -> None:
        work, _ = self.make_repo()
        self.write(work, "a.txt", "one\n")

        result = self.scope("pending", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unborn HEAD", result.stdout)
        self.assertIn("- a.txt (added; entire file is in scope)", result.stdout)

    def test_pending_marks_binary_file_unavailable(self) -> None:
        work = self.standard_repo()
        (work / "bin.dat").write_bytes(b"\x00\x01\x02binary\n")
        self.commit_all(work, "bin")
        (work / "bin.dat").write_bytes(b"\x00\x01\x02binary2\n")

        result = self.scope("pending", cwd=work)

        self.assertIn("- bin.dat (M; changed lines unavailable, inspect git diff before editing)", result.stdout)

    # -- last-commit -----------------------------------------------------

    def test_last_commit_adds_whole_file(self) -> None:
        work = self.standard_repo()
        self.write(work, "new.txt", "brand new\n")
        self.commit_all(work, "two")

        result = self.scope("last-commit", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Target: last commit", result.stdout)
        self.assertIn("- new.txt (added; entire file is in scope)", result.stdout)

    def test_last_commit_detects_rename(self) -> None:
        work = self.standard_repo()
        self.git("mv", "old.txt", "renamed.txt", cwd=work)
        self.commit_all(work, "two")

        result = self.scope("last-commit", cwd=work)

        self.assertIn("- renamed.txt (R; changed lines: 1)", result.stdout)

    def test_last_commit_marks_file_with_uncommitted_changes_unavailable(self) -> None:
        work = self.standard_repo()
        changed = TEN_LINES.replace("2\n", "TWO\n")
        self.write(work, "a.txt", changed)
        self.commit_all(work, "two")
        self.write(work, "a.txt", changed + "MORE\n")

        result = self.scope("last-commit", cwd=work)

        self.assertIn("- a.txt (M; changed lines unavailable, inspect git diff before editing)", result.stdout)

    def test_last_commit_on_root_commit_lists_whole_tree(self) -> None:
        work, _ = self.make_repo()
        self.write(work, "a.txt", "one\ntwo\n")
        self.commit_all(work, "root")

        result = self.scope("last-commit", cwd=work)

        self.assertIn("Target: last commit (root commit; entire tree is new)", result.stdout)
        self.assertIn("- a.txt (added; entire file is in scope)", result.stdout)

    # -- default ---------------------------------------------------------

    def test_default_uses_merge_base_with_default_branch(self) -> None:
        work = self.standard_repo()
        self.git("checkout", "-q", "-b", "feature", cwd=work)
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.commit_all(work, "two")

        result = self.scope("default", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Target: default branch (working tree vs merge-base", result.stdout)
        self.assertIn("- a.txt (M; changed lines: 2)", result.stdout)

    def test_default_uses_open_pr_base(self) -> None:
        work = self.standard_repo()
        main_oid = self.git("rev-parse", "main", cwd=work).stdout.strip()
        self.git("checkout", "-q", "-b", "feature", cwd=work)
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.commit_all(work, "two")
        self.set_gh(f"#!/usr/bin/env bash\nprintf 'OPEN\\nmain\\n{main_oid}\\n'\n")

        result = self.scope("default", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Target: open PR on feature (working tree vs merge-base", result.stdout)

    def test_default_without_pr_has_no_detection_note(self) -> None:
        work = self.standard_repo()
        self.git("checkout", "-q", "-b", "feature", cwd=work)
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.commit_all(work, "two")
        self.set_gh("#!/usr/bin/env bash\nprintf 'no pull requests found for branch \"feature\"\\n' >&2\nexit 1\n")

        result = self.scope("default", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Target: default branch (working tree vs merge-base", result.stdout)
        self.assertNotIn("PR detection unavailable", result.stdout)

    def test_default_gh_failure_keeps_detection_note(self) -> None:
        work = self.standard_repo()
        self.git("checkout", "-q", "-b", "feature", cwd=work)
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.commit_all(work, "two")
        self.set_gh("#!/usr/bin/env bash\nprintf 'HTTP 401: Bad credentials\\n' >&2\nexit 1\n")

        result = self.scope("default", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(PR detection unavailable; using the default branch)", result.stdout)

    def test_default_detached_head_exits_3(self) -> None:
        work = self.standard_repo()
        self.git("checkout", "-q", "--detach", "HEAD", cwd=work)

        result = self.scope("default", cwd=work)

        self.assertEqual(result.returncode, 3)

    def test_non_default_modes_work_with_detached_head(self) -> None:
        work = self.standard_repo()
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.git("checkout", "-q", "--detach", "HEAD", cwd=work)

        for args in (("pending",), ("last-commit",), ("diff", "HEAD"), ("files", "a.txt")):
            with self.subTest(mode=args[0]):
                result = self.scope(*args, cwd=work)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_without_a_base_exits_4(self) -> None:
        work = self.base / "solo"
        self.git("init", "-q", "-b", "main", str(work))
        self.write(work, "a.txt", "one\n")
        self.commit_all(work, "one")

        result = self.scope("default", cwd=work)

        self.assertEqual(result.returncode, 4, result.stderr)

    # -- diff ------------------------------------------------------------

    def test_diff_between_commits_uses_changed_lines(self) -> None:
        work = self.standard_repo()
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.commit_all(work, "two")

        result = self.scope("diff", "HEAD~1", "HEAD", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- a.txt (M; changed lines: 2)", result.stdout)

    def test_path_filter_limits_the_scope(self) -> None:
        work = self.standard_repo()
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.write(work, "src/b.txt", "x\nY\nz\n")

        result = self.scope("pending", "--path", "a.txt", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[paths: a.txt]", result.stdout)
        self.assertIn("- a.txt (M; changed lines: 2)", result.stdout)
        self.assertNotIn("src/b.txt", result.stdout)

    def test_diff_ending_at_older_commit_marks_later_changed_file_unavailable(self) -> None:
        work = self.standard_repo()
        self.write(work, "f.txt", "a\nb\nc\n")
        self.commit_all(work, "two")
        self.write(work, "f.txt", "a\nB\nc\n")
        self.commit_all(work, "three")
        self.write(work, "f.txt", "x\ny\nz\na\nB\nc\n")
        self.commit_all(work, "four")

        result = self.scope("diff", "HEAD~2", "HEAD~1", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- f.txt (M; changed lines unavailable, inspect git diff before editing)", result.stdout)
        self.assertNotIn("changed lines: 2", result.stdout)

    def test_diff_ending_at_older_commit_keeps_lines_for_unchanged_file(self) -> None:
        work = self.standard_repo()
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.commit_all(work, "two")
        self.write(work, "src/b.txt", "x\nY\nz\n")
        self.commit_all(work, "three")

        result = self.scope("diff", "HEAD~2", "HEAD~1", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- a.txt (M; changed lines: 2)", result.stdout)

    def test_diff_ending_at_older_commit_marks_file_renamed_later_unavailable(self) -> None:
        work = self.standard_repo()
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.commit_all(work, "two")
        self.git("mv", "a.txt", "renamed.txt", cwd=work)
        self.commit_all(work, "rename")

        result = self.scope("diff", "HEAD~2", "HEAD~1", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- a.txt (M; changed lines unavailable, inspect git diff before editing)", result.stdout)
        self.assertNotIn("changed lines: 2", result.stdout)

    def test_diff_ending_at_older_commit_marks_file_deleted_later_unavailable(self) -> None:
        work = self.standard_repo()
        self.write(work, "a.txt", TEN_LINES.replace("2\n", "TWO\n"))
        self.commit_all(work, "two")
        (work / "a.txt").unlink()

        result = self.scope("diff", "HEAD~1", "HEAD", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- a.txt (M; changed lines unavailable, inspect git diff before editing)", result.stdout)

    # -- files -----------------------------------------------------------

    def test_files_expands_directory_and_keeps_spaces(self) -> None:
        work = self.standard_repo()
        self.write(work, "src/file with space.txt", "hello\n")
        self.commit_all(work, "two")

        result = self.scope("files", "src", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- src/b.txt (whole file in scope)", result.stdout)
        self.assertIn("- src/file with space.txt (whole file in scope)", result.stdout)

    def test_files_more_than_ten_requires_allow_many(self) -> None:
        work = self.standard_repo()
        for n in range(12):
            self.write(work, f"f{n}.txt", "x\n")

        blocked = self.scope("files", ".", cwd=work)
        allowed = self.scope("files", ".", "--allow-many", cwd=work)

        self.assertEqual(blocked.returncode, 6)
        self.assertIn("--allow-many", blocked.stderr)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    # MSYS expands wildcard arguments when bash.exe is spawned from a
    # non-Cygwin process, so on Windows the pattern never reaches the script.
    @unittest.skipIf(sys.platform == "win32", "MSYS globs wildcard argv before bash sees it")
    def test_files_expands_a_glob(self) -> None:
        work = self.standard_repo()

        result = self.scope("files", "**/*.txt", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- a.txt (whole file in scope)", result.stdout)
        self.assertIn("- src/b.txt (whole file in scope)", result.stdout)

    def test_files_missing_path_exits_5(self) -> None:
        work = self.standard_repo()

        result = self.scope("files", "missing.ts", cwd=work)

        self.assertEqual(result.returncode, 5)

    def test_files_resolves_paths_from_a_subdirectory(self) -> None:
        work = self.standard_repo()

        result = self.scope("files", "b.txt", cwd=work / "src")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- src/b.txt (whole file in scope)", result.stdout)

    def test_files_absolute_path_prints_repo_relative(self) -> None:
        work = self.standard_repo()

        result = self.scope("files", str(work / "src" / "b.txt"), cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- src/b.txt (whole file in scope)", result.stdout)
        self.assertNotIn(str(work), result.stdout)

        directory = self.scope("files", str(work / "src"), cwd=work)

        self.assertEqual(directory.returncode, 0, directory.stderr)
        self.assertIn("- src/b.txt (whole file in scope)", directory.stdout)

    def test_files_absolute_path_from_a_subdirectory(self) -> None:
        work = self.standard_repo()

        result = self.scope("files", str(work / "src" / "b.txt"), cwd=work / "src")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- src/b.txt (whole file in scope)", result.stdout)

    def test_files_normalizes_dot_dot_segments(self) -> None:
        work = self.standard_repo()

        result = self.scope("files", "src/../a.txt", cwd=work)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- a.txt (whole file in scope)", result.stdout)

    def test_files_outside_repository_exits_5(self) -> None:
        work = self.standard_repo()
        outside = self.write(self.base, "outside.txt", "outside\n")

        for args in ((str(outside),), ("../outside.txt",), (str(self.base / "home"),)):
            with self.subTest(path=args[0]):
                result = self.scope("files", *args, cwd=work)
                self.assertEqual(result.returncode, 5, result.stdout)
                self.assertNotIn("outside.txt", result.stdout)

    def test_files_inside_git_directory_exits_5(self) -> None:
        work = self.standard_repo()

        for path in (".git/config", ".git"):
            with self.subTest(path=path):
                result = self.scope("files", path, cwd=work)
                self.assertEqual(result.returncode, 5, result.stdout)
                self.assertIn("inside a .git directory", result.stderr)

    def test_files_skips_deleted_tracked_file(self) -> None:
        work = self.standard_repo()
        (work / "src" / "b.txt").unlink()

        result = self.scope("files", "src", cwd=work)

        self.assertEqual(result.returncode, 7)
        self.assertNotIn("src/b.txt", result.stdout)

    # -- errors ----------------------------------------------------------

    def test_empty_scope_exits_7(self) -> None:
        work = self.standard_repo()

        result = self.scope("pending", cwd=work)

        self.assertEqual(result.returncode, 7)

    def test_unknown_mode_exits_2(self) -> None:
        work = self.standard_repo()

        result = self.scope("bogus", cwd=work)

        self.assertEqual(result.returncode, 2)

    def test_no_arguments_exits_2(self) -> None:
        result = self.scope()

        self.assertEqual(result.returncode, 2)

    def test_outside_a_repository_exits_3(self) -> None:
        result = self.scope("pending")

        self.assertEqual(result.returncode, 3)


if __name__ == "__main__":
    unittest.main()
