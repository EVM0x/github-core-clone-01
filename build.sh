#!/usr/bin/env bash
# Concatenate src/*.py into the single-file entrypoint main.py.
set -euo pipefail
cd "$(dirname "$0")"
cat src/*.py > main.py
python3 -c "import ast,sys; ast.parse(open('main.py').read())" && echo "main.py OK $(wc -c < main.py) bytes"
