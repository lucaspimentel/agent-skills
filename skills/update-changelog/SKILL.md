---
name: update-changelog
description: "Manage CHANGELOG.md files and GitHub releases: add new entries, backfill missing versions, sync release notes."
argument-hint: "[version] [description]"
allowed-tools: Bash(git log *) Bash(git tag *) Bash(git diff *) Bash(gh release *) Bash(gh auth status *)
disable-model-invocation: true
---

Manage CHANGELOG.md files and GitHub releases — add new entries, backfill missing versions, and sync release notes.

## Formatting

### CHANGELOG.md format

Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with these specific conventions:

```markdown
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Add git commit via action palette

## [1.2.0] - 2026-03-15

### Added
- Add git stage/unstage actions to action palette

### Fixed
- Fix Ctrl+R refresh not reloading git status

## [1.1.0] - 2026-03-14

### Added
- Introduce the action palette

[Unreleased]: https://github.com/OWNER/REPO/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/OWNER/REPO/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/OWNER/REPO/releases/tag/v1.1.0
```

Rules:
- **File header**: on a new file use the example's header, followed by `## [Unreleased]`. Keep the SemVer claim and link only when the project documents that policy; otherwise state its documented versioning policy, or omit the claim if unknown. Preserve an existing header as-is.
- **Version heading**: `## [x.y.z] - YYYY-MM-DD` (square brackets around the version, space-dash-space before the date). Append ` [YANKED]` for a release that was pulled; retain its entry.
- **Unreleased heading**: `## [Unreleased]` (no date, always the first version section). Keep this section even when empty so it is ready for upcoming changes.
- **Category headings**: `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security` (only include categories that have entries)
- **Category order**: Added → Changed → Deprecated → Removed → Fixed → Security
- **Entry format**: `- Verb-led phrase` (present-tense verb, no trailing period, no sub-bullets)
- **Verb matches category**:
  - Added → "Add ...", "Enable ...", "Introduce ..."
  - Changed → "Update ...", "Move ...", "Use ...", "Rename ...", "Convert ..."
  - Deprecated → "Deprecate ...", "Mark ... as deprecated"
  - Removed → "Remove ...", "Delete ...", "Drop ..."
  - Fixed → "Fix ...", "Correct ...", "Prevent ..."
  - Security → "Patch ...", "Address ...", "Fix ..." (when it closes a vulnerability)
- **Reference links**: preserve an existing file's link style. For new files, use reference-link definitions as in the example. Keep definitions in sync when adding or promoting versions, including the Unreleased comparison base. Derive the HTTPS base URL from `git remote get-url origin`, converting SSH syntax and removing a trailing `.git` (for example `git@github.com:OWNER/REPO.git` becomes `https://github.com/OWNER/REPO`). If no usable remote exists, ask for the repository URL. Reuse the repo's tag convention:
  - `[Unreleased]: {base}/compare/{latest-tag}...HEAD`; before the first release, use `{base}/commits/HEAD`
  - `[x.y.z]: {base}/compare/{previous-tag}...{tag}`
  - The oldest version, which has no predecessor, uses `{base}/releases/tag/{tag}`
  - Put one blank line between the last entry and the reference-link block
- **Whitespace**: one blank line between version sections, one blank line between category sections, no blank lines between entries within a category, no trailing blank line at end of file

### GitHub release format

Release notes use the same content as the CHANGELOG.md entry but **without** the version heading (`## [x.y.z] - ...`). Include all categories present in the changelog entry, using the entries promoted out of `## [Unreleased]` if the release came from there. Keep the category headings and entries as-is. For `[1.2.0]` above:

```markdown
### Added
- Add git stage/unstage actions to action palette

### Fixed
- Fix Ctrl+R refresh not reloading git status
```

## Modes of operation

This skill operates in two modes based on user intent:

1. **Add/update** (default) — add a changelog entry, either under `## [Unreleased]` or as a dated version section, optionally synced to a GitHub release
2. **Backfill** — populate missing versions in CHANGELOG.md and/or GitHub releases from git history

## Add/update mode

### Tag naming

Versions are always bare (e.g. `1.2.0`), including versions passed in by callers such as the `ship` skill; never add a second `v` to an input that already has one. Build tag names from the repo's existing convention:

```bash
git tag --list 'v*'
git tag --list '[0-9]*'
```

- If version tags use a `v` prefix, the tag for a version is `v{version}`; if they are bare, it is `{version}`.
- If no version tags exist, default to `v{version}` and tell the user.

Below, `{tag}` means the tag built this way for the version in question.

### Step 1 — Find CHANGELOG.md

Glob for `**/CHANGELOG.md`. If multiple are found, ask the user which to update. If none found, offer to create one with the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) file header from the Formatting section.

### Step 2 — Determine target and changes

Establish whether the user wants upcoming changes or a dated version; ask if unclear.

- **Upcoming changes**: use the user-provided description, or git history below if none is supplied. Preserve existing Unreleased entries.
- **Release**: select the Unreleased entries being released now, excluding anything explicitly reserved for later. Merge user-provided additions for this release without duplicates. Use git history below if neither source supplies entries for this release.
- **Update an existing version**: preserve its entries and merge the user's corrections; use history for that version's tag range if needed.

When using **git history** for upcoming changes or a new release:

- Read the latest dated version section in CHANGELOG.md, skipping `## [Unreleased]`, to get the latest version number. If there is no dated version, treat it as a fresh changelog with no prior version.
- Run `git log {tag}..HEAD --oneline` (where `{tag}` is the tag for the latest version) to list commits since that version (if no matching tag exists, use the entry's date with `--since=YYYY-MM-DD`; if no prior version at all, use the full log)
- Summarize notable user-facing changes into concise bullet points, grouping related commits and omitting development noise. Exclude changes already recorded in Unreleased.

### Step 3 — Determine version and date

For upcoming changes, target `## [Unreleased]` without a version or date, and skip Step 6.

For an existing version, use its version number and preserve its release date unless the user explicitly asks to correct it.

For a new dated version:

- If the user provided a version, use it
- If a version was recently bumped in the working tree (check `git diff` and `git diff --staged` for version file changes), detect and use the new version
- Otherwise, ask the user for the new version
- Use the user-provided release date, or default to today

### Step 4 — Categorize entries

Classify each change into [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories:

| Category | When to use | Git message keywords |
|---|---|---|
| **Added** | New features, files, capabilities | "add", "introduce", "new" |
| **Changed** | Modifications to existing functionality | "update", "improve", "refactor" |
| **Deprecated** | Features slated for removal | "deprecate", "obsolete" |
| **Removed** | Removed features or files | "remove", "delete", "drop" |
| **Fixed** | Bug fixes | "fix", "repair", "resolve" |
| **Security** | Vulnerability fixes | "security", "vulnerability", "CVE", "sanitize" |

Respect user-provided categorization.

### Step 5 — Insert the entry

Create `## [Unreleased]` after the introductory paragraphs if it is missing, then apply the selected target:

- **Upcoming changes**: merge new entries into Unreleased under the matching categories without duplicates
- **Release**: write only the entries selected in Step 2 into a new dated section immediately below Unreleased. Remove only those promoted entries from Unreleased, leaving all other entries there
- **Update an existing version**: merge corrections into its dated section without changing Unreleased or creating a duplicate version

Apply the Formatting rules, including Reference links. Use the Edit tool rather than a full file rewrite. If the file was just created (empty or header-only), writing the full file is acceptable.

### Step 6 — GitHub release

Check if the repo uses GitHub releases:

```bash
gh release list --limit 1
```

If `gh` is not available or not authenticated, skip this step and inform the user.

If releases exist or the user explicitly asked to create/update a release:

- **Existing release**: `gh release edit {tag} --notes "..."`
- **New release**: `gh release create {tag} --title "{tag}" --notes "..."`
- Use the GitHub release format described in the Formatting section above
- If the version's tag doesn't exist yet, inform the user that the tag must be created first (tags should be created from a specific commit, not implicitly by `gh release create`)

## Backfill mode

Activate when the user asks to backfill, populate missing entries, or sync historical versions.

### Tag discovery

Discover version tags by trying both prefixed and bare formats:

```bash
git tag --list 'v*' --sort=creatordate
git tag --list '[0-9]*' --sort=creatordate
```

Use whichever pattern matches the repo's convention. If no version tags exist at all, inform the user that backfilling requires version tags and stop.

### Backfill CHANGELOG.md

1. List all version tags using the discovery step above
2. Read existing CHANGELOG.md to find which versions already have entries
3. For each missing version, generate an entry from git log between that tag and its predecessor: `git log {prev}..{version} --oneline`
4. If the git log between two adjacent tags is empty (e.g., fast-forward merge), create a minimal entry noting "No significant changes"
5. Categorize commits using the keyword table above
6. Present the proposed entries as a numbered list showing version, date, and bullet points — ask the user to confirm before writing
7. Insert confirmed entries in the correct chronological position within the file, applying the Formatting rules

### Backfill GitHub releases

If `gh` is not available or not authenticated, skip this section and inform the user.

1. List existing releases: `gh release list --limit 100`
2. List all version tags using the discovery step above
3. For each tag without a matching release:
   - Use the CHANGELOG.md entry as release notes if available
   - Otherwise generate notes from git log between adjacent tags
4. Present a summary table of releases to create (columns: tag, date, entry count) and ask for confirmation
5. Create confirmed releases: `gh release create {tag} --title "{tag}" --notes "..."`

## Final step

Show the user what was added or created. Do not commit or push unless also asked.
