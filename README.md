# agent-skills

Coding-agent skills shared between [Claude Code](https://code.claude.com/docs/en/skills) and [pi](https://github.com/earendil-works/pi). Every skill lives once, in `skills/<name>/SKILL.md`, in the [Agent Skills](https://agentskills.io) format that both agents consume. The repo doubles as a Claude Code plugin marketplace and a pi package, so each agent installs the same files natively.

## Skills

| Skill | Purpose | Invocation |
|---|---|---|
| `git-commit` | Commit pending changes, optionally pushing | Model-invoked |
| `chezmoi-diff` | Resolve differences between chezmoi source and local dotfiles | `/chezmoi-diff` |
| `ship` | Release automation: version, changelog, docs, commit, tag, push, watch, release | `/ship` |
| `add-todo` | Append tasks to TODO.md | `/add-todo` |
| `whats-next` | Prioritized list of incomplete TODO.md tasks | `/whats-next` |
| `review-pr` | Review a pull request and provide feedback | `/review-pr` |
| `simplify` | Simplify recently changed code within diff scope | `/simplify` |
| `address-pr-comments` | Work through PR review comments one at a time | `/address-pr-comments` |
| `update-pr-description` | Refresh a PR's title and description | `/update-pr-description` |
| `update-docs` | Sync project docs with the current codebase | `/update-docs` |
| `update-changelog` | Maintain CHANGELOG.md and GitHub releases | `/update-changelog` |
| `update-github-actions` | Update and pin GitHub Actions to commit SHAs | `/update-github-actions` |
| `atlassian` | Atlassian work via the acli CLI or the Atlassian MCP server: Jira, Confluence, bulk ops, sprint reports | Model-invoked |
| `zsa-layouts` | Fetch, render, diff, and evaluate ZSA keyboard layouts from Oryx | `/zsa-layouts` |
| `grill-me` | Relentless design interview until no ambiguities remain | `/grill-me` |
| `tdd` | Test-driven red-green-refactor loop with test-quality rules | Model-invoked |
| `diagnosing-bugs` | Diagnosis loop for hard bugs: build and tighten a feedback loop | Model-invoked |
| `session-handoff` | Compact the current conversation into a handoff document | `/session-handoff` |
| `writing-for-agents` | Reference for writing skills and AGENTS.md-style docs agents consume | Model-invoked |

`grill-me`, `tdd`, `diagnosing-bugs`, `session-handoff`, and `writing-for-agents` are derived from [mattpocock/skills](https://github.com/mattpocock/skills), MIT licensed. `atlassian` is derived from [leweii/atlassian-cli](https://github.com/leweii/atlassian-cli), MIT licensed. See `NOTICE` and the `LICENSE` file in each skill directory.

Most skills set `disable-model-invocation: true`, so they run only when invoked explicitly: `/name` in Claude Code, `/skill:name` in pi. `git-commit`, `atlassian`, `tdd`, `diagnosing-bugs`, and `writing-for-agents` are model-invoked from their trigger descriptions.

Claude-specific frontmatter (`argument-hint`, `context`, `agent`) is carried in the shared files; pi ignores unknown fields.

## Install

### Claude Code

Register the repo as a plugin marketplace, then install the plugin:

```
/plugin marketplace add lucaspimentel/agent-skills
/plugin install agent-skills@lucasp-agent-skills
```

### pi

```
pi install https://github.com/lucaspimentel/agent-skills
```

## Layout

```
agent-skills/
├── .github/workflows/
│   └── validate.yml         # CI: runs scripts/validate.sh
├── .claude-plugin/
│   ├── marketplace.json     # Claude Code marketplace manifest
│   └── plugin.json          # plugin: skills/ at repo root
├── package.json             # pi package manifest: pi.skills = ["./skills"]
├── LICENSE
├── NOTICE                   # attribution for third-party-derived skills
├── README.md
├── scripts/
│   ├── validate.sh          # static checks for manifests and skills
│   └── validate_skills.py   # skills-ref spec check with an allowlist
└── skills/
    └── <name>/
        ├── SKILL.md         # + optional supporting files (reference docs, scripts)
        └── LICENSE          # only for third-party-derived skills
```

## Editing a skill

Edit `skills/<name>/SKILL.md` here; both agents pick up the change on their next session (pi: `/reload`). Keep descriptions under 1024 characters and rich in trigger phrases even for slash-only skills: they drive command-menu discoverability and any future switch to model invocation. Conventions for forking third-party skills and attribution live in [AGENTS.md](AGENTS.md).

## Validation

`scripts/validate.sh` runs the static checks that CI runs on every push to `main` and every pull request. It needs `claude` and `uv`, and no credentials.

- `claude plugin validate --strict` on the marketplace manifest, the plugin manifest, and `skills/`.
- `scripts/validate_skills.py`, which applies the [Agent Skills spec](https://agentskills.io/specification) checks from [skills-ref](https://pypi.org/project/skills-ref/): frontmatter format, `name` rules (including matching the directory), `description` length, and unknown fields.

The spec does not list `argument-hint`, `context`, `agent`, or `disable-model-invocation`, so `validate_skills.py` allows those four by name. The first three are Claude Code only; `disable-model-invocation` is honored by both Claude Code and pi. Any other field, including a misspelling of these, fails.
