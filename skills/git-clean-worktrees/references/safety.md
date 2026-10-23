# git-clean-worktrees safety contract

This reference records what the helper guarantees, what it deliberately refuses to do, and where its limits are. Read the skill first.

## Scope and discovery

- One repository per run. The input defaults to the invocation directory and must resolve to the main, non-bare working tree.
- Linked-worktree, bare, missing, non-repository, detached-HEAD, and unborn-HEAD inputs are rejected before anything is refreshed.
- The helper discovers worktrees through `git worktree list --porcelain -z` and branches through `git for-each-ref refs/heads/`, so naming conventions, directory layout, and missing upstreams never decide eligibility.
- The actual remote default branch is read from the remote's symbolic HEAD with `ls-remote --symref` and then fetched into its explicit origin remote-tracking ref. Cached `origin/HEAD` and configured fetch refspecs are not treated as proof of freshness.
- The remote default branch must be the branch checked out in the main worktree. The local tip does not have to equal the remote tip, and the helper never switches, pulls, merges, or resets anything.
- Shallow, grafted, and replaced histories are rejected so ancestry proof is never weakened.

## Preservation proof

A branch tip is preserved when either:

1. It is an ancestor of the freshly fetched remote default tip (`git merge-base --is-ancestor`), or
2. A merged pull request in the actual origin repository records exactly that head SHA, with non-null merge metadata and a matching base repository.

Squash and rebase merges qualify through the exact recorded head SHA even when Git ancestry fails. Preservation is never inferred from branch names, remote deletion, a missing upstream, or an older merged PR for a reused branch. Lookup omissions, malformed responses, pagination failures, authentication failures, and timeouts are treated conservatively and keep the target retained.

If the origin URL cannot be resolved safely to a `github.com` repository, the run continues in ancestry-only mode. The helper never switches GitHub accounts.

## File protections

Removal is blocked by any of:

- staged or unstaged tracked changes
- non-ignored untracked files
- unresolved conflicts or in-progress Git operations (merge, rebase, cherry-pick, revert, bisect, am, sequencer)
- index flags that can hide tracked changes (assume-unchanged, skip-worktree)
- populated submodules, sparse-checkout state, or file-inspection errors

These checks pass explicit per-invocation overrides, `-c core.fsmonitor=false -c core.untrackedCache=false -c core.ignoreStat=false`, so a repository-local filesystem monitor, untracked cache, or `ignoreStat` setting cannot hide changes. These are command overrides, not changes to global Git configuration.

Ignored files are enumerated separately and block removal by default. They can be released only through `--allow-ignored` for that one worktree, bound into the approved digest. An ignored override never bypasses tracked changes, untracked files, preservation, locks, worktree identity, or fingerprint checks.

Ignored entries are fingerprinted with a SHA-256 over each path plus file content (or symlink target), without following symlinks. A changed ignored path, a changed file, or an added file changes the fingerprint, so apply skips the target. Entries are refused rather than skipped when a parent component is a symlink, junction, or non-directory, when the entry is unreadable, or when the entry is not a regular file or symlink (a FIFO, device, socket, or directory, including a nested repository Git reports as a directory). Such refusals retain the target even with an ignored override. Enumerating and hashing ignored trees can be expensive for large build or dependency directories such as `node_modules`; that cost is intentional because it is the change detector.

## Approval boundary

The plan is canonical JSON and carries a SHA-256 digest. Apply requires that exact digest, rejects malformed schemas, unknown fields, duplicate or conflicting targets, unsafe overrides, and actions that are not recorded in the plan's expected state. The digest is an accidental-edit and approval-boundary check, not authentication against a malicious local actor: anyone who can rewrite the plan file can also recompute the digest.

## Destructive revalidation

Before any action, apply revalidates the repository identity, the origin URL, the main HEAD, and the refreshed remote default branch, and aborts the whole run if any of those changed or if refresh fails. Immediately before each action it rediscovers registrations, rechecks locks, existence, path identity, file protections, ignored fingerprints, and preservation. A target that changed is skipped; other independent approved actions continue.

## Removal primitives

- Worktrees are removed with `git worktree remove -- <absolute path>` and never with `--force`. If normal removal refuses, the action fails and is reported; the helper does not escalate. Ignored-file approval is a helper policy decision, not permission to force through Git's other protections.
- Branches are deleted with `git update-ref --no-deref -d refs/heads/<name> <expected-tip>`. This compare-and-delete refuses if the branch advanced. Symbolic refs and refs still used by any registration are rejected first; plumbing deletion does not provide porcelain branch-in-use safeguards, so the helper performs those checks explicitly.
- `branch.<name>` configuration is left in place rather than mutated in a second non-atomic step. Apply reports any leftover `branch.<name>.*` keys.
- Deleting a ref removes its reflog. The result records the deleted SHAs, but recovery is possible only while the objects remain reachable; no reflog-based restore is promised.

## Concurrency

Worktree deletion cannot be atomic with concurrent file writers, and ref deletion cannot atomically exclude another process checking the branch out. Revalidation and compare-and-delete reduce the window but do not eliminate it. Stop writers and concurrent Git operations for the cleanup window.

## What the helper never does

- It never recursively deletes directories with Python.
- It never unlocks, prunes, or repairs worktrees.
- It never mutates a remote branch.
- It never switches GitHub accounts.
- It never changes global Git configuration or bypasses Git's safe-directory checks.

## Testing

`tests/test_git_clean_worktrees.py` runs offline with a standard-library-only test suite. Network behavior is injected; Git behavior runs against disposable repositories created under a temporary directory with an empty global and system Git configuration. Run it from the repository root:

```console
python3 -m unittest discover -s tests -v
```