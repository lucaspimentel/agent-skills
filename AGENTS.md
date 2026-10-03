# agent-skills

Dual-manifest repo: every skill in `skills/<name>/` serves both Claude Code (plugin marketplace) and pi (package). One copy per skill; never fork per-agent. Both manifests auto-discover `skills/`, so adding a skill needs no manifest edits.

## Editing a skill

Edit `skills/<name>/SKILL.md` directly; see README.md ("Editing a skill") for description guidelines. Keep Claude-specific frontmatter (`argument-hint`, `context`, `agent`): pi ignores unknown fields.

Run `scripts/validate.sh` before committing. A new frontmatter field outside the Agent Skills spec must be added to `EXTRA_FIELDS` in `scripts/validate_skills.py`.

## Forking a third-party skill

Follow the convention in `NOTICE`: upstream LICENSE reproduced verbatim as the skill dir's `LICENSE`, an entry in `NOTICE`, a README table row, and a provenance HTML comment in SKILL.md.

Keep upstream prose verbatim, em dashes included: adaptations are only for what breaks on pi.

- pi has no Skill tool: rewrite "call the Skill tool with X" to name the skill or file to consult.
- Do not carry upstream `agents/` dirs (`openai.yaml` is OpenAI-only metadata).

## Fresh content

Text you author here (NOTICE entries, README rows, new files): no em dashes.
