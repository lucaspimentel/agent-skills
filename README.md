# agent-skills

Coding-agent skills shared between [Claude Code](https://code.claude.com/docs/en/skills) and [pi](https://github.com/earendil-works/pi). Every skill lives once, in `skills/<name>/SKILL.md`, in the [Agent Skills](https://agentskills.io) format that both agents consume. The repo doubles as a Claude Code plugin marketplace and a pi package, so each agent installs the same files natively.

## Skills

| Skill | Purpose | Invocation |
|---|---|---|
| `git-commit` | Commit pending changes, optionally pushing | Model-invoked |
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
| `atlassian-cli` | Atlassian CLI (acli) usage for Jira and Confluence | Model-invoked |
| `zsa-layouts` | Fetch, render, diff, and evaluate ZSA keyboard layouts from Oryx | Model-invoked |
| `grill-me` | Relentless design interview until no ambiguities remain | `/grill-me` |

Most skills set `disable-model-invocation: true`, so they run only when invoked explicitly: `/name` in Claude Code, `/skill:name` in pi. `git-commit` and `atlassian-cli` are model-invoked from their trigger descriptions.

Claude-specific frontmatter (`argument-hint`, `context`, `agent`) is carried in the shared files; pi ignores unknown fields.

## Install

### Claude Code

Register the repo as a plugin marketplace, then install the plugin:

```
/plugin marketplace add lucaspimentel/agent-skills
/plugin install agent-skills@lucaspimentel-agent-skills
```

### pi

```
pi install https://github.com/lucaspimentel/agent-skills
```

## Layout

```
agent-skills/
├── .claude-plugin/
│   ├── marketplace.json     # Claude Code marketplace manifest
│   └── plugin.json          # plugin: skills/ at repo root
├── package.json             # pi package manifest: pi.skills = ["./skills"]
├── LICENSE
├── README.md
└── skills/
    └── <name>/
        └── SKILL.md
```

## Editing a skill

Edit `skills/<name>/SKILL.md` here; both agents pick up the change on their next session (pi: `/reload`). Keep descriptions under 1024 characters and rich in trigger phrases even for slash-only skills: they drive command-menu discoverability and any future switch to model invocation.
