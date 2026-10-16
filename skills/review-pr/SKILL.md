---
name: review-pr
description: "Review a pull request for issues and feedback."
argument-hint: "[findings|fix|post]"
disable-model-invocation: true
---

Review a pull request and provide detailed feedback on the changes.

## Modes

The skill has three modes, selected by an optional argument:

- `findings` — display review findings only; never modify code or post to GitHub
- `fix` — display findings, then walk through each issue and apply local fixes as commits
- `post` — display findings, then post review comments to GitHub via `gh api`

If no argument is provided, run the findings phase first, then use `AskUserQuestion` to ask whether to `fix`, `post`, or `stop`.

## Review Steps (all modes)

1. Fetch PR details and full diff using `gh pr view` and `gh pr diff`. If the diff is empty, stop and tell the user.
2. Skip generated/vendored files: lock files (`*.lock`, `package-lock.json`, `yarn.lock`), `*.designer.cs`, auto-generated code, vendored dependencies
3. For large PRs, prioritize the most-changed files first
4. Focus on actionable issues, not positive feedback

## Delegation

When the host provides a subagent delegation tool, delegate the findings phase to a subagent instead of reviewing inline. Resolve the repository and PR number first (Review Steps, step 1) so the dispatch can name its target. Try dispatch targets in this order, skipping any that a prior dispatch error already ruled out; fall back to an inline review under Review Steps only when no rung applies.

1. A reviewer agent: one whose role is code review (in pi's subagent extension: `reviewer`).
2. A read-only agent: an unknown-agent error lists the available agents; pick one whose toolset is restricted to read-style tools and includes read-only GitHub access (in pi's subagent extension: `scout`).
3. Any remaining listed agent, even one with write tools: the dispatch task's read-only rule is then the only guard, so state it prominently.
4. No delegation tool: run the Findings Phase inline yourself.

### Dispatch task

Subagents run in isolated context, so the dispatch must be self-contained. Include:

- The repository (owner/name) and PR number, and an instruction to fetch the PR details and diff itself with its read-only gh tool.
- The read-only rule: review only, no file modifications, no git mutations, and no writes to GitHub (never `gh api` writes or review posting).
- The review rules from Review Steps: the generated/vendored skip list and the large-PR prioritization.
- The full findings contract from the Findings Phase section, restated: the severity emoji labels with their definitions, the sort order, one line per issue with `file_path:line_number` and a brief description, and the no-issues outcome.

### Presenting the result

Relay the subagent's findings verbatim: they are the review output, not raw material to rewrite. Check them against the findings contract before presenting, and repair missing required elements inline if needed (for example, missing severity labels). Everything after the findings phase is always yours: mode selection, `AskUserQuestion`, the Fix Flow, and the Post Flow are never delegated.

## Findings Phase (always run first)

- Display all review findings in the CLI output only
- Label each comment with a severity emoji: 🔴 CRITICAL, 🟠 HIGH, 🟡 MEDIUM, 🟢 LOW
- Sort comments by severity (most critical first), then by file path and line number:
  1. 🔴 **CRITICAL**: Security vulnerabilities, data loss, crashes
  2. 🟠 **HIGH**: Logic errors, bugs, incorrect behavior
  3. 🟡 **MEDIUM**: Performance issues, missing error handling
  4. 🟢 **LOW**: Code style, minor improvements, suggestions
- Keep the summary concise: one line per issue with severity, `file_path:line_number`, and a brief description

If no issues were found, say so and stop regardless of mode.

## After Findings

Behavior after the findings phase depends on the mode:

- **`findings` mode**: stop here.
- **`fix` mode**: continue to "Fix Flow" below.
- **`post` mode**: continue to "Post Flow" below.
- **No mode argument**: use `AskUserQuestion` with options `fix`, `post`, `stop`. Then run the corresponding flow (or stop).

## Fix Flow

DO NOT post any comments to GitHub in this flow.

### Step 1 — Ask About Each Issue

For each issue in order, use `AskUserQuestion` to ask what to do. Offer options:
- **"Fix it"** — investigate and implement a fix
- **"Skip"** — do nothing for this issue

Collect answers for **all** issues before applying any fixes. Track each answer as `{issue index, severity, file:line, action}`.

### Step 2 — Apply Fixes

After all answers are collected, work through each "Fix it" issue in order (most critical first):

1. Make the code change
2. Stage and commit the change following the `git-commit` skill conventions (imperative mood, concise subject ≤ 50 chars)
3. Move to the next "Fix it" issue

Skipped issues are not touched.

### Step 3 — Final Summary

If any changes were committed, show a summary listing:
- Issues fixed, with the commit subject for each
- Issues skipped

If no changes were made, no summary is needed.

## Post Flow

- Use `gh api` to create a review with specific line comments
- Endpoint: `repos/OWNER/REPO/pulls/PR_NUMBER/reviews`
- Each comment must specify: `path`, `line` (or `start_line`/`end_line`), `body`
- Include footer in review body: `"\n\n---\n*Review by an AI agent*"`
- ALWAYS use event type: `COMMENT`
- NEVER use `REQUEST_CHANGES` or `APPROVE` — human review required
- Group related comments under a single review

### Comment body guidelines

- Reference code directly with `file_path:line_number`
- Explain why something is an issue, not just what
- Suggest a concrete fix when possible
