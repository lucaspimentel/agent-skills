---
name: simplify
description: "Review recently changed code for simplification and apply clarity improvements. Use when the user says 'simplify', 'simplify my changes', 'clean up my diff', 'review what I just changed', 'simplify the last commit', 'tidy up the changed lines', or any variation of wanting the current diff simplified for clarity, consistency, and maintainability. Supports '--staged' to review the index, '--ref=<ref>' to diff against a specific ref, and an optional file list to narrow scope."
argument-hint: "[--staged] [--ref=<ref>] [file...]"
disable-model-invocation: true
---

Review recently changed files and apply simplification improvements.

## Step 1 — Determine the diff target

Parse the user's arguments:

- `--staged` present → diff the index (add `--cached` to git commands)
- `--ref=<ref>` present → diff against that ref
- otherwise → diff against `HEAD`

## Step 2 — List changed files

Run:

```
git diff --name-status [--cached | <target-ref>]
```

Parse each line: the first character is the status (`M` modified, `A` added, `R` renamed, `C` copied). For `R` and `C` entries (`R100\told\tnew`), use the second path (the new one).

- If the user passed an explicit file list, use only those files, treated as modified against the target ref.
- If the file list is empty and the target is `HEAD`, fall back to `git diff --name-status HEAD~1` and use `HEAD~1` as the target ref for step 3.
- If still empty, tell the user there are no changes to simplify and stop.

## Step 3 — Compute changed line ranges

For each non-added file, run:

```
git diff -U0 --no-ext-diff [--cached | <target-ref>] -- <path>
```

Parse each hunk header `@@ -a,b +c,d @@`: the changed range in the current file starts at `c` and spans `d` lines (a bare `+c @@` means `d` is 1). Skip hunks where `d` is 0 (pure deletions). Merge ranges that touch or overlap (a range starting at or before the previous range's end + 1 becomes one range spanning both).

- For added files (`A`), the entire file is in scope.
- If the per-file diff fails, mark the file "changed lines unavailable — inspect git diff before editing".
- If a file has no remaining ranges, mark it "deletions only — no current lines to simplify".

Format each file for the scope list as one of:

- `- <path> (added; entire file is in scope)`
- `- <path> (<status>; changed lines unavailable — inspect git diff before editing)`
- `- <path> (<status>; deletions only — no current lines to simplify)`
- `- <path> (<status>; changed lines: 12, 30-45)` — single lines bare, ranges as `start-end`, comma-separated

## Step 4 — Run the review

With the scope list from step 3 substituted below, follow this prompt exactly:

Review the following recently changed files and apply simplification improvements.

## Principles

- **Preserve functionality**: Never change what the code does. All existing tests must continue to pass.
- **Apply project standards**: Follow any conventions from CLAUDE.md or AGENTS.md in this project.
- **Enhance clarity**: Reduce unnecessary complexity and nesting, eliminate redundant code and abstractions, improve variable and function names, and consolidate related logic. Keep valuable comments that explain design rationale, business rules, non-obvious behaviour, or intent. Remove only truly redundant noise, such as `// increment i` above `i++`. Avoid nested ternary operators: prefer switch statements or if/else chains for multiple conditions.
- **Maintain balance**: Do not over-simplify. Avoid overly clever solutions that are hard to understand. Do not combine too many concerns into single functions. Do not remove helpful abstractions. Prioritize readability over fewer lines.

## Scope

Only review and modify the changed lines listed below. Changed line numbers refer to
the current file contents. You may read surrounding code for context, but must not
edit it. For added files, the entire file is considered changed.

<scope list from step 3>

## Process

1. Read each file listed above and inspect its changed lines
2. Identify concrete improvements within those lines (dead code, unclear names, redundant logic, inconsistent patterns)
3. Apply changes one file at a time, keeping every edit within the listed line ranges
4. After all changes, run existing tests to verify nothing is broken
5. Summarize what you changed and why

Do NOT add new features, change public APIs, or refactor code outside the listed
line ranges. If a worthwhile simplification would require editing unchanged code,
leave it alone and mention it in the summary instead.
