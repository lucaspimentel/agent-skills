---
name: chezmoi-diff
allowed-tools: Bash(chezmoi status *) Bash(chezmoi diff *) Bash(chezmoi cat *) Bash(chezmoi source-path *) Bash(chezmoi target-path *) Bash(git status *) Bash(git add *) Bash(git commit *) Bash(git diff *) Bash(git log *)
description: >
  Help resolve differences between chezmoi-managed dotfiles and local files.
  Use this skill whenever the user mentions chezmoi, dotfiles sync, chezmoi update,
  chezmoi apply, chezmoi diff, or wants to compare/resolve their chezmoi source
  files with local destination files. Also trigger when the user wants to understand
  what changed in their dotfiles, which direction changes should flow (source→local
  or local→source), or needs help interpreting chezmoi file naming conventions
  (dot_, executable_, .tmpl, symlink_, etc.).
---

# Chezmoi Diff Resolution

Help the user understand and resolve differences between their chezmoi source directory
and local destination files. The goal is to make `chezmoi diff` output actionable by
walking through changes and deciding what to do with each one.

## Context: How Chezmoi Works

Chezmoi manages dotfiles by keeping a "source of truth" copy in a source directory
(typically `~/.local/share/chezmoi/`) and applying those files to the home directory.
When the source and destination diverge, the user needs to decide which version to keep.

**Two directions of sync:**
- **Source → Destination** (`chezmoi apply`): Overwrite local files with chezmoi source versions
- **Destination → Source** (`chezmoi add`): Copy local file changes back into chezmoi source

The source directory uses special filename prefixes (`dot_`, `executable_`, `symlink_`, etc.)
and suffixes (`.tmpl` for templates). For the full prefix/suffix table, ordering rules and
source-to-destination examples, see [reference.md](reference.md).

## Workflow

### Step 1: Get the Diff

Run `chezmoi status` first for a quick overview of which files changed and the type of change
(M=modified, A=added, D=deleted). Then run `chezmoi diff` for the full unified diff output.

If both produce no output, tell the user everything is in sync and stop.

### Step 2: Present a Summary

Show a table of all changed files with:
- The **destination path** (the human-readable path like `~/.gitconfig`)
- Whether the file is a **template** (`.tmpl` suffix in source) — templates are important
  because the source will contain Go template syntax while the destination has rendered values
- A brief characterization of the change: lines added/removed, or "new file", "deleted", etc.

Example format:

```
| # | File                         | Template | Changes        |
|---|------------------------------|----------|----------------|
| 1 | ~/.config/powershell/profile | no       | +3 -1 lines    |
| 2 | ~/.gitconfig                 | yes      | +5 -2 lines    |
| 3 | ~/.claude/settings.json      | yes      | +1 -2 lines    |
```

### Step 3: Walk Through Each File

For each changed file, show the diff and explain what's different in plain language. If the user chooses **Edit / merge**, handle it inline right then — read both versions, suggest a merged result, write it to the right location — before moving on to the next file.

**For template files (`.tmpl`):**
- The raw source will contain template directives like `{{ .chezmoi.hostname }}`.
  These are not real differences — they render to concrete values on apply.
- Run `chezmoi cat <destination-path>` to get the rendered version of the source template.
- Compare the **rendered** source against the **actual** destination file to show the real diff.
- Clearly note which parts of the source are template expressions that render to values.
- Pay special attention to **conditional blocks** like `{{ if eq .chezmoi.hostname "myhost" }}`.
  These mean the rendered output differs per machine. Explain which branch applies to the
  current machine and what values other machines would get — this helps the user understand
  whether a diff is machine-specific or a real change they made locally.

**Reading the diff correctly:**
`chezmoi diff` outputs a unified diff where `---` is the local **destination** (current file on disk) and `+++` is what chezmoi **would write** (from the source).
- Lines starting with `-` exist locally but **not** in the chezmoi source
- Lines starting with `+` exist in the chezmoi source but **not** locally

So if X appears on a `-` line, your local file has X and the chezmoi source doesn't.
Always phrase the explanation from the local file's perspective: "your local file has X / is missing Y".

**For regular files:**
- Show the diff as-is. Read both the source and destination files if needed for context.

**For each file, use the `AskUserQuestion` tool to present the decision as a single-select question:**

- Header: the filename (short form, e.g. `settings.json`)
- Question: `What do you want to do with ~/.claude/settings.json?`
- Options (always these 4, in this order):
  1. **Apply from chezmoi** — overwrite local with the chezmoi source version
  2. **Copy local to chezmoi** — update the chezmoi source with your local changes
     (for template files, this requires manual editing to preserve template directives)
  3. **Skip** — leave this file alone for now
  4. **Edit / merge** — help me manually merge the two versions

Process files one at a time — show the diff explanation, then immediately present the
`AskUserQuestion` for that file before moving on to the next.

### Step 4: Execute Decisions

After all files have been decided, execute the Apply and Copy actions in batch:
- **Apply**: run `chezmoi apply --force <destination-path>`
- **Copy local to chezmoi**:
  - **Regular files**: run `chezmoi add --force <destination-path>` — for executable files (`.sh`, `.ps1`, etc.)
    that need the `executable_` prefix in the source, run `chezmoi chattr +x <destination-path>` after adding
  - **Template files**: DO NOT use `chezmoi add` — it would overwrite the source `.tmpl` file with
    a plain copy of the rendered destination, stripping all template directives and breaking the template.
    Instead, use `chezmoi source-path <destination-path>` to find the source file, then manually
    edit it to incorporate the local changes while preserving all `{{ }}` template expressions.
    Show the user a diff of what you plan to write, and ask for confirmation before writing.

Confirm the full batch with the user before executing any writes.

### Step 5: Offer to Commit and Push

After all apply/copy actions are executed, check the chezmoi source directory for uncommitted git changes:

1. Get the chezmoi source directory path with `chezmoi source-path` (no arguments).
2. Run `git status` in that directory to check for uncommitted changes.
3. If there are no changes, skip this step silently.
4. If there are changes, show a summary of what changed (the files modified in the source directory)
   and use `AskUserQuestion` to offer:
   - **Commit and push** — stage all changes, commit with a descriptive message summarizing what was synced, and push to remote
   - **Commit only** — stage and commit, but don't push
   - **Skip** — leave changes uncommitted
5. If committing, stage the changed files with `git add`, create a commit with a message like
   `chezmoi: sync <list of dotfiles changed>`, and push if requested.

Note: `git push` is intentionally excluded from `allowed-tools` so the user always gets a manual
confirmation prompt before pushing to remote.

## Important Details

- Always use `chezmoi source-path` to find the source directory — don't hardcode it.
- Use `chezmoi cat <path>` to render templates for comparison, not raw file reads of `.tmpl` files.
- Use `chezmoi target-path <source-path>` or `chezmoi source-path <target-path>` to map between paths.
- The source directory may have a subdirectory structure (e.g. a `windows/` subfolder as the root)
  based on the `sourceDir` config. Respect whatever `chezmoi source-path` returns.
- On Windows, chezmoi may report paths with forward slashes. Normalize as needed.
- When showing diffs, use the destination path (the human-friendly one) as the primary identifier.
- For complex merges, `chezmoi merge <path>` opens the user's configured merge tool — mention
  this as an option if the user prefers a visual merge tool over inline editing.

## Reference Documentation

- [reference.md](reference.md): chezmoi source file naming conventions (prefixes, suffixes, ordering, examples)
- [Command Overview](https://www.chezmoi.io/user-guide/command-overview/)
- [Reference](https://www.chezmoi.io/reference/)
