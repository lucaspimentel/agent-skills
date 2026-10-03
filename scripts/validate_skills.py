"""Validate every skill in skills/ against the Agent Skills spec via skills-ref.

Runs skills-ref's own checks, except that it tolerates frontmatter fields the
agents this repo targets rely on but the spec does not list:

- argument-hint, context, agent: Claude Code only; pi ignores them.
- disable-model-invocation: honored by both Claude Code and pi.

Run with: uvx --from skills-ref==0.1.1 python scripts/validate_skills.py
"""

import sys
from pathlib import Path

from skills_ref.errors import ParseError
from skills_ref.parser import find_skill_md, parse_frontmatter
from skills_ref.validator import validate_metadata

EXTRA_FIELDS = {"argument-hint", "context", "agent", "disable-model-invocation"}

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def validate(skill_dir: Path) -> list[str]:
    skill_md = find_skill_md(skill_dir)
    if skill_md is None:
        return ["Missing required file: SKILL.md"]
    try:
        metadata, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    except ParseError as e:
        return [str(e)]
    metadata = {k: v for k, v in metadata.items() if k not in EXTRA_FIELDS}
    return validate_metadata(metadata, skill_dir)


def main() -> int:
    failed = 0
    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        errors = validate(skill_dir)
        if errors:
            failed += 1
            print(f"FAIL {skill_dir.name}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"ok   {skill_dir.name}")
    if failed:
        print(f"{failed} skill(s) failed validation")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
