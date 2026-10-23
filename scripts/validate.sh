#!/usr/bin/env bash
# Full validation: static plugin/skill checks (need claude and uv) plus the
# portable behavioral suite (needs Python 3.11+ and Git, no credentials).
set -euo pipefail
cd "$(dirname "$0")/.."

claude plugin validate --strict .claude-plugin/marketplace.json
claude plugin validate --strict .claude-plugin/plugin.json
claude plugin validate --strict skills
uvx --from skills-ref==0.1.1 python scripts/validate_skills.py

PYTHON=python3
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi
"$PYTHON" -m unittest discover -s tests -v
