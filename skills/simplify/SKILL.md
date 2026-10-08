---
name: simplify
description: "Review recently changed code for simplification and apply clarity improvements. With no argument, reviews the current branch's open PR (or the branch vs the default branch). Accepts 'pending changes', 'last commit', or a free-form description of what to review (paths, commits, ranges)."
argument-hint: "[pending changes | last commit | <anything>]"
disable-model-invocation: true
---

Review recently changed code and apply simplification improvements.

When this skill says "ask", use the `ask_user_question` / `AskUserQuestion` tool if available. If no question tool is available or the session is non-interactive, follow the **Non-interactive** rules at each point where a question would be asked.

## Step 1 — Parse the argument

Normalize the argument: trim, lowercase, and ignore fillers such as "the" and "my". Then match the **whole** argument:

- empty → **default** target (step 2)
- `pending changes` → **pending** target
- `last commit` → **last-commit** target
- anything else → **free-form** (step 3)

Partial matches are free-form: `last commit in src/auth` is not the `last commit` phrase.

## Step 2 — Resolve the target

Every target resolves to a **base** and an **end**:

| Target | Base | End | Untracked files |
|---|---|---|---|
| default | merge-base (below) | working tree | included as added |
| pending | `HEAD` | working tree | included as added |
| last-commit | `HEAD~1` (first parent for merges; the empty tree `4b825dc642cb6eb9a060e54bf8d69288fbee4904` if `HEAD` is a root commit) | `HEAD` | excluded |

### Default target: find the merge-base

1. Confirm a git repo (`git rev-parse --show-toplevel`) and a checked-out branch (`git symbolic-ref -q HEAD`). If either fails, stop and tell the user. Never fall back to `HEAD`.
2. **PR:** run `gh pr view --json state,baseRefName,baseRefOid`.
   - If `state` is `OPEN` (drafts included): make sure `baseRefOid` exists locally (`git cat-file -e <baseRefOid>^{commit}`). If missing, `git fetch` the base branch from the remote for the PR's base repository (`upstream` in a fork checkout, otherwise `origin`). Base = `git merge-base HEAD <baseRefOid>`.
   - If the branch has no PR, or the PR is `MERGED` or `CLOSED`: continue to the next step.
   - If `gh` itself fails (not installed, not authenticated, network error), or the base commit still can't be found after fetching: continue to the next step and tell the user that PR detection failed and the default branch is being used instead.
3. **Default branch:** resolve with `git symbolic-ref --short refs/remotes/origin/HEAD`; if unset, `gh repo view --json defaultBranchRef --jq .defaultBranchRef.name` (as `origin/<name>`). Base = `git merge-base HEAD <default-branch-ref>`.
4. If no base can be resolved, stop and tell the user what failed.

On the default branch with no PR, the merge-base is the remote default branch, so the target is unpushed commits plus working-tree changes.

## Step 3 — Free-form arguments

Interpret the argument and turn it into one of:

- **Diff target:** a base and end (as in step 2), optionally filtered to paths. Use this whenever the argument has diff context, such as a commit, range, branch, or time ("last commit in src/auth", "commits since yesterday", "abc123..HEAD"). Scope is the target's changed lines, filtered to the paths.
- **Whole-file target:** the argument names only paths, directories, or globs with no diff context ("src/foo.ts", "src/auth", "**/*.cs"). Expand directories and globs to tracked and untracked (non-ignored) files. Every line of every matched file is in scope.

Rules:

- If the reading is clear, proceed without asking. Mention the resolved target in the scope list.
- If the argument plausibly maps to more than one target, ask which one before continuing. **Non-interactive:** stop and list the possible readings.
- If a whole-file target matches more than 10 files, ask before continuing. **Non-interactive:** stop and list the matches.
- If a named path does not exist, stop and tell the user.
- If the argument can't be turned into a git diff or file set, stop and list the valid arguments: none, `pending changes`, `last commit`, or a description of paths or commits.

## Step 4 — List changed files

Skip this step and step 5 for whole-file targets: list each file as `- <path> (whole file in scope)` and go to step 6.

Run, adding `-- <paths>` when the target is path-filtered:

- end is the working tree: `git diff --name-status <base>`
- end is a commit: `git diff --name-status <base> <end>`

Parse each line: the first character is the status (`M` modified, `A` added, `R` renamed, `C` copied). For `R` and `C` entries (`R100\told\tnew`), use the second path (the new one).

When untracked files are included (see step 2), also run `git ls-files --others --exclude-standard [-- <paths>]` and add each file with status `A` (added), so the entire file is in scope.

If the list is empty, tell the user there are no changes to simplify for the resolved target and stop.

## Step 5 — Compute changed line ranges

For each non-added file, run:

- end is the working tree: `git diff -U0 --no-ext-diff <base> -- <path>`
- end is a commit: `git diff -U0 --no-ext-diff <base> <end> -- <path>`

Parse each hunk header `@@ -a,b +c,d @@`: the changed range in the end version starts at `c` and spans `d` lines (a bare `+c @@` means `d` is 1). Skip hunks where `d` is 0 (pure deletions). Merge ranges that touch or overlap (a range starting at or before the previous range's end + 1 becomes one range spanning both).

- For added files (`A`), the entire file is in scope.
- If the per-file diff fails, mark the file "changed lines unavailable — inspect git diff before editing".
- When the end is a commit and the file also has uncommitted changes (`git diff --quiet HEAD -- <path>` fails), the line numbers no longer match the current file: mark it "changed lines unavailable — inspect git diff before editing".
- If a file has no remaining ranges, mark it "deletions only — no current lines to simplify".

Format each file for the scope list as one of:

- `- <path> (added; entire file is in scope)`
- `- <path> (<status>; changed lines unavailable — inspect git diff before editing)`
- `- <path> (<status>; deletions only — no current lines to simplify)`
- `- <path> (<status>; changed lines: 12, 30-45)` — single lines bare, ranges as `start-end`, comma-separated

Start the scope list with one line naming the resolved target, e.g. `Target: PR #123 (working tree vs merge-base abc1234 with main)`.

## Step 6 — Run the review

With the scope list substituted below, follow this prompt exactly:

Review the following recently changed files and apply simplification improvements.

## Principles

- **Preserve functionality**: Never change what the code does. All existing tests must continue to pass.
- **Apply project standards**: Follow any conventions from CLAUDE.md or AGENTS.md in this project.
- **Enhance clarity**: Reduce unnecessary complexity and nesting, eliminate redundant code and abstractions, improve variable and function names, and consolidate related logic. Keep valuable comments that explain design rationale, business rules, non-obvious behaviour, or intent. Remove only truly redundant noise, such as `// increment i` above `i++`. Avoid nested ternary operators: prefer switch statements or if/else chains for multiple conditions.
- **Maintain balance**: Do not over-simplify. Avoid overly clever solutions that are hard to understand. Do not combine too many concerns into single functions. Do not remove helpful abstractions. Prioritize readability over fewer lines.

## Scope

<scope list from step 4 or 5>

Changed line numbers refer to the current file contents. Lines listed above are **in scope**; everything else is **out of scope**. You may read out-of-scope code for context. For added files and whole-file targets, every line is in scope.

An out-of-scope finding is **related** only if it is tied to the in-scope changes: code the changes made dead or redundant, comments the changes made stale, or code whose simplification would directly simplify the changed lines. Never act on or suggest unrelated out-of-scope improvements, however worthwhile.

## Process

1. **Scoped pass.** Read each file and apply concrete improvements within the in-scope lines (dead code, unclear names, redundant logic, inconsistent patterns), one file at a time.
2. **Auto tier.** Apply related out-of-scope edits that only remove dead code or redundant/stale comments. Do not apply any other out-of-scope edit in this step.
3. Run the existing tests.
4. **Confirmed batch.** Collect the remaining related out-of-scope findings (renames, restructuring, consolidation, anything beyond dead code and comments). If there are any, list them with file and line references and ask once which to apply. Apply only the approved ones, then run the tests again. Skip this step if there are no findings or every line is in scope. **Non-interactive:** do not apply them; list them in the summary as suggestions.
5. **Summarize** what changed and why, grouped by stage: scoped pass, auto tier (list every auto-applied out-of-scope edit explicitly), and confirmed batch. Report test results for each run.

Do NOT add new features or change public APIs.
