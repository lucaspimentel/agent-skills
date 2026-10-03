#!/usr/bin/env bash
# Static validation for the plugin manifests and skills. Requires claude and uv.
set -euo pipefail
cd "$(dirname "$0")/.."

claude plugin validate --strict .claude-plugin/marketplace.json
claude plugin validate --strict .claude-plugin/plugin.json
claude plugin validate --strict skills
uvx --from skills-ref==0.1.1 python scripts/validate_skills.py
