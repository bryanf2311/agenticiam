#!/usr/bin/env bash
# Builds a single-file AgenticIAM executable for the current OS/arch into dist/.
# On Windows this produces dist/agenticiam.exe; on Linux/macOS, dist/agenticiam.
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m pip install -e ".[dev]"
rm -rf build dist
pyinstaller agenticiam.spec

echo
echo "Built: $(ls dist/)"
echo "Try it: dist/agenticiam init"
