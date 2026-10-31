#!/bin/bash
# Run ON the deployment host, inside this folder:  bash run_host_check.sh
# Put FINN_LLM_* in .env (copy .env.example) or export them. Keys are never printed.
cd "$(dirname "$0")" || exit 1
mkdir -p results data/pdfs data/text
if [ ! -d .venv-host ]; then
  python3 -m venv .venv-host
fi
.venv-host/bin/python -m pip install --quiet -r requirements.txt
.venv-host/bin/python tests/test_adapter.py 2>&1 | tail -4
.venv-host/bin/python host_check.py
