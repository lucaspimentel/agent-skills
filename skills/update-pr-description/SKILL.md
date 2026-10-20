---
name: update-pr-description
description: "Update the PR's title and description to accurately reflect the changes."
disable-model-invocation: true
---

Update the PR's title and description to accurately reflect the changes.

## Analysis Steps
1. Fetch PR details: `gh pr view --json title,body,commits,files`
2. Review all commits and changes: `gh pr diff`
3. Understand the full scope of changes (not just latest commit)
4. If the PR title and description were not empty, show me a summary of what will change (added, removed, etc) before updating the PR

## Title Requirements
- Keep concise (≤ 72 characters recommended)
- Use imperative mood (e.g., "Add feature" not "Adds feature")
- Accurately summarize the overall change
- If the title already starts with a tag in `[]` (e.g., `[Build]`, `[Azure Function]`), preserve it exactly and only update the rest of the title.
- If there is no existing tag, add an appropriate topic tag in `[]`:
  e.g., "[Azure Function] Add feature"

## Description Requirements
- Follow repository template if available (search case-insensitive):
  - `pull_request_template.md`
  - `.github/pull_request_template.md`
  - `docs/pull_request_template.md`
- If no template, include:
  - Summary of changes (what and why)
  - Testing performed (do not include test counts such as "23 tests passed"; state that the suite passes instead)
  - Breaking changes or migration notes (if applicable)
- Keep concise but complete
- Use bullet points for clarity
- The joke footer is opt-in, never automatic:
  1. Generate three short, concrete joke candidates appropriate to the changes; include the existing joke as a candidate if the description already has one.
  2. Ask the user which to use via the host's structured question tool (e.g., AskUserQuestion in Claude Code, ask_user_question in pi, or the equivalent); present the actual joke text in each option plus an explicit "No joke" choice.
  3. Wait for the answer. If the tool is unavailable or the question goes unanswered, stop before applying the PR update; do not infer consent.
- Only the selected candidate becomes the footer, formatted as an italicized quote with no model, agent, or harness name:
  > *"[selected joke]"* 🤖
- If the user selects "No joke", omit the joke footer and remove any existing joke footer and attribution. Otherwise, replace an existing joke with the selected candidate. Never keep or add a joke the user did not select.

## Updating PR

### When editing an existing PR description
1. Save the current PR description to a temp file and print its path: `tmp=$(mktemp) && gh pr view --json body --jq .body > "$tmp" && echo "$tmp"`
2. Use the Edit tool to modify the temp file at the echoed path with the new description (this lets the user see the diff and approve before changes are applied)
3. Once approved, apply: `gh pr edit PR_NUMBER --title "..." --body-file <echoed-path>`
4. Clean up the temp file at the echoed path

### When creating a new PR or the existing description is empty
1. Write the description directly using `gh pr edit PR_NUMBER --title "..." --body "..."`
   - No temp file needed since there's nothing to diff against
