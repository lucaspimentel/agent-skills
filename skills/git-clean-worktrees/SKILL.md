---
name: git-clean-worktrees
disable-model-invocation: true
description: "Remove already-preserved linked worktrees and local branches from one repository, only through an explicit plan the user approves by digest."
argument-hint: "[repository-path]"
---

# git-clean-worktrees

Inspect one repository, propose an exact cleanup plan, and remove only the worktrees and local branches the user approves. The helper proves preservation for every exact branch tip before anything is deleted, and it never forces, prunes, unlocks, or touches a remote.

## Before starting

1. **Pick a Python 3.11+ interpreter.** Try `python3`, then `python`, then `py -3` on Windows. Check `--version` and stop if it is older than 3.11.
2. **Resolve the helper path** as `scripts/git_clean_worktrees.py` relative to this skill's own directory. Use that absolute path for every phase; do not resolve it against the current working directory.
3. **Confirm access.** The helper needs filesystem access to the repository and its worktrees, plus network access for `git fetch` and, when needed, the `gh api` merged-PR lookup. In pi, if the sandbox cannot reach the repository or the network, run the commands with the `host_bash` tool instead of the sandboxed `bash` tool. Keep Git, `gh`, and Python in the same environment; the helper does not translate paths between WSL and native Windows.
4. **Keep artifacts outside removal targets.** Write the snapshot, plan, and result JSON somewhere outside every worktree that may be removed, for example the main checkout's `.git` directory or a temporary directory.
5. Run the helper with `--help` if you need the exact flags for the current version.

## Phase 1: inspect

```console
python3 <skill-dir>/scripts/git_clean_worktrees.py inspect [<repository-path>] --output <snapshot.json>
```

The optional repository path defaults to the current directory. It must be inside the main (non-bare) working tree; a linked worktree is rejected.

Inspection removes nothing, but it does fetch the remote's actual default branch into its origin remote-tracking ref, so it writes Git objects, as documented in `references/safety.md`.

The snapshot lists every registered worktree and every local branch with an eligibility decision and an explicit reason. Read it, then decide which targets to propose.

- Worktree ids are the registered absolute paths.
- Branch ids are the short branch names (`topic/x`, not `refs/heads/topic/x`).
- `--allow-ignored` is valid only together with removing that same worktree, and only when the snapshot shows the worktree needs the ignored-file override.

## Phase 2: plan

```console
python3 <skill-dir>/scripts/git_clean_worktrees.py plan --snapshot <snapshot.json> \
  [--remove-worktree <id> ...] [--delete-branch <id> ...] [--allow-ignored <id> ...] \
  --output <plan.json>
```

Worktree removal and branch deletion are separate selections. Removing a worktree never authorizes deleting its branch; select `--delete-branch` explicitly.

Show the user the complete plan and its digest, then wait for explicit approval before continuing. If the user changes any target or ignored-file override, rebuild the plan: the digest changes and the old approval no longer applies.

## Phase 3: apply

```console
python3 <skill-dir>/scripts/git_clean_worktrees.py apply --plan <plan.json> --approved-digest <sha256>
```

Apply revalidates the repository, refreshes the default branch again, rechecks every lock, file protection, fingerprint, and preservation proof, and skips any target that changed or became unsafe. It continues with the other independent approved actions.

Exit codes: `0` every approved action succeeded, `2` some target was skipped or failed, `1` the run aborted before any action.

## Report

Summarize the result for the user: removed worktrees, deleted branches with their recorded SHAs, and every skipped or failed target with the helper's reason. Note that deleting a ref removes its reflog, so a recorded SHA is only recoverable while its objects remain.

## Non-negotiables

- Never select or apply a target that is not in the approved plan, and never widen the approved digest.
- Never use `--force`, `git worktree unlock`, `git worktree prune`, or hand-rolled deletion.
- Never switch GitHub accounts. If `gh` authentication cannot see the origin repository, leave those targets retained.
- Never delete a remote branch.

Detailed safety and concurrency contracts, limitations, and recovery notes live in [references/safety.md](references/safety.md).