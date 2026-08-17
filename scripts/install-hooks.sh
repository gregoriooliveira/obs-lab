#!/usr/bin/env bash
# Instala os git hooks do repo (anti-vazamento de segredos).
set -euo pipefail
cd "$(dirname "$0")/.."
git config core.hooksPath scripts
chmod +x scripts/pre-commit 2>/dev/null || true
echo "  ok  core.hooksPath = scripts (pre-commit ativo)"
