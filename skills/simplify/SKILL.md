---
name: simplify
description: "Quality-only pass over recently changed code: simplify, reuse, and tidy within diff scope, then apply the improvements. Not a bug hunt; use the review-pr skill for correctness. With no argument, reviews the current branch's open PR (or the branch vs the default branch). Accepts 'pending changes', 'last commit', or a free-form description of what to review (paths, commits, ranges)."
argument-hint: "[pending changes | last commit | <anything>]"
disable-model-invocation: true
---

Review recently changed code and apply quality improvements: simplification, reuse, efficiency, and altitude. This is a quality pass, not a bug hunt. For a correctness review, use the `review-pr` skill or another dedicated review; note defects and leave them alone.

In Claude Code the built-in `/simplify` owns the bare name, so invoke this skill as `/agent-skills:simplify`. In pi, invoke it as `/skill:simplify`.

When this skill says "ask", use the `ask_user_question` / `AskUserQuestion` tool if available. If no question tool is available or the session is non-interactive, follow the **Non-interactive** rules at each point where a question would be asked.

## Step 1: Parse the argument

Normalize the argument: trim, lowercase, and ignore fillers such as "the" and "my". Then match the **whole** argument:

- empty: **default** target (step 2)
- `pending changes`: **pending** target
- `last commit`: **last-commit** target
- anything else: **free-form** (step 2)

Partial matches are free-form: `last commit in src/auth` is not the `last commit` phrase.

## Step 2: Resolve the scope

The scope script resolves the target, lists the changed files, and computes their changed line ranges. Resolve its path as `scripts/scope.sh` relative to this skill's own directory, and invoke it with `bash` (do not rely on the executable bit):

```console
bash <skill-dir>/scripts/scope.sh <mode> [--path P]...
```

Pick the mode from step 1; "Free-form arguments" below covers the rest:

- default target: `default`
- pending target: `pending`
- last-commit target: `last-commit`
- free-form diff target: `diff BASE [END]`, where omitting `END` means the working tree
- free-form whole-file target: `files PATH...`

Add `--path <p>` (repeatable) to filter a diff target. The script expands whole-file paths itself, resolving them from the current directory (so a relative path still works from a subdirectory); the paths it prints are repository-root relative.

The script prints the scope list: one `Target:` line and one bullet per file. Substitute the whole list for `<scope list>` in step 3, and show the `Target:` line to the user as the resolved target. When PR detection fails, that line says the default branch was used; pass that on.

Exit codes to handle:

- `0`: the scope was printed.
- `2`: usage error. Re-check the invocation against the modes above.
- `3`: not a git repository, or a detached HEAD for the default target. Stop and tell the user.
- `4`: the base or default branch could not be resolved. Stop and tell the user what failed.
- `5`: a named path does not exist or matches nothing. Stop and tell the user.
- `6`: a whole-file target matches more than 10 files. The script lists the matches on stderr. Ask whether to include them all, then rerun with `--allow-many`. **Non-interactive:** stop and list the matches.
- `7`: the scope is empty. Tell the user there are no changes to simplify for the resolved target and stop.

### Free-form arguments

For anything that is neither empty, `pending changes`, nor `last commit`, interpret the argument and call the script in one of two modes:

- **Diff mode** whenever the argument has diff context, such as a commit, range, branch, or time ("last commit in src/auth", "commits since yesterday", "abc123..HEAD"): `diff BASE [END]`, plus `--path` filters. The scope is the target's changed lines, filtered to the paths.
- **Files mode** when the argument names only paths, directories, or globs with no diff context ("src/foo.ts", "src/auth", "**/*.cs"): `files PATH...`. Every line of every matched file is in scope.

Rules:

- If the reading is clear, proceed without asking.
- If the argument plausibly maps to more than one target, ask which one before continuing. **Non-interactive:** stop and list the possible readings.
- If the argument can't be turned into a git diff or file set, stop and list the valid arguments: none, `pending changes`, `last commit`, or a description of paths or commits.

## Step 3: Run the review

With the scope list substituted below, follow this prompt exactly:

Review the following recently changed files and apply simplification improvements.

## Principles

- **Preserve functionality**: Never change what the code does. All existing tests must continue to pass.
- **Apply project standards**: Follow any conventions from CLAUDE.md or AGENTS.md in this project.
- **Quality only**: Do not hunt for bugs, race conditions, or correctness defects. Note anything you notice and leave it to a dedicated correctness review (`review-pr` covers pull requests).
- **Enhance clarity**: Reduce unnecessary complexity and nesting, eliminate redundant code and abstractions, improve variable and function names, and consolidate related logic. Keep valuable comments that explain design rationale, business rules, non-obvious behaviour, or intent. Remove only truly redundant noise, such as `// increment i` above `i++`. Avoid nested ternary operators: prefer switch statements or if/else chains for multiple conditions.
- **Maintain balance**: Do not over-simplify. Avoid overly clever solutions that are hard to understand. Do not combine too many concerns into single functions. Do not remove helpful abstractions. Prioritize readability over fewer lines.

## Checklist

For every in-scope change, check four angles:

- **Simplification**: redundant or derivable state, copy-paste with small variations, deep nesting, dead code, unclear names, inconsistent patterns.
- **Reuse**: search shared and nearby modules for an existing helper the change re-implements, and name the helper to call instead.
- **Efficiency**: redundant computation or I/O, independent operations run one after another, blocking work on startup or a hot path, and long-lived closures that keep their whole enclosing scope alive (prefer a type that copies only the fields it needs).
- **Altitude**: does the fix sit at the right depth? A special case bolted onto shared infrastructure usually belongs in the underlying mechanism. An altitude fix usually reaches outside the diff, so route it to the confirmed batch in step 5.

## Scope

<scope list from step 2>

Changed line numbers refer to the current file contents. Lines listed above are **in scope**; everything else is **out of scope**. You may read out-of-scope code for context. For added files and whole-file targets, every line is in scope.

An out-of-scope finding is **related** only if it is tied to the in-scope changes: code the changes made dead or redundant, comments the changes made stale, or code whose simplification would directly simplify the changed lines. Never act on or suggest unrelated out-of-scope improvements, however worthwhile.

## Finding format

Report each finding as `path:line`, a one-line summary, and the concrete cost (tokens, allocations, I/O, indirection, or lines). Deduplicate findings that share a mechanism: keep one entry and note the other sites. Record every finding you skip, with the reason.

## Process

1. **Baseline tests.** Find the project's test command before editing anything: check AGENTS.md or CLAUDE.md, then `package.json` scripts, a `*.sln`, a `Makefile`, or the equivalent build file. Run it and record any failures that already exist. If no test command is found, skip the tests steps and say so; do not guess.
2. **Scoped pass.** Read each file and apply concrete improvements within the in-scope lines (dead code, unclear names, redundant logic, inconsistent patterns), one file at a time.
3. **Auto tier.** Apply related out-of-scope edits that only remove dead code or redundant/stale comments. Do not apply any other out-of-scope edit in this step.
4. **Tests.** Run the tests again. If a new failure appears, revert the edits in the implicated files, re-run, and list them in the summary as skipped with the reason. Run the configured formatter or linter over the changed files only; keep its edits inside the in-scope ranges and route any formatting churn outside the scope to the confirmed batch (step 5).
5. **Confirmed batch.** Collect the remaining related out-of-scope findings (renames, restructuring, consolidation, altitude fixes, and anything beyond dead code and comments). List them with `path:line` references and ask once which to apply. Apply only the approved ones, then run the tests again. Skip this step if there are no findings or every line is in scope. **Non-interactive:** do not apply them; list them in the summary as suggestions.
6. **Summarize** what changed and why, grouped by stage: baseline test results, scoped pass, auto tier (list every auto-applied out-of-scope edit explicitly), tests, and confirmed batch. List every skipped finding with its reason.

### Large diffs

When a subagent or Task tool is available (built into Claude Code; in pi only when an extension provides one) and the scope list covers more than 10 files, dispatch one subagent per checklist angle (simplification, reuse, efficiency, altitude). Each subagent reads the in-scope files and returns findings in the format above; it edits nothing. Merge the results, deduplicate by mechanism, then apply as in the process above. With no subagent tool, or a smaller scope, run the single pass.

Do NOT add new features or change public APIs.
