#!/bin/bash
# Run from anywhere:  bash "/Users/nikhil/enam assg/run_all.sh"
# Everything is created inside this folder (venv, downloaded PDFs, results).
# Installs into .venv only: requests, pymupdf, psutil. Downloads two PDFs from BSE
# (about 11 MB total). Does NOT download any model.

cd "$(dirname "$0")" || exit 1
mkdir -p results data/pdfs data/text

echo "== run started $(date)" | tee results/run_log.txt
python3 --version 2>&1 | tee -a results/run_log.txt
sw_vers 2>&1 | tee -a results/run_log.txt
uname -m 2>&1 | tee -a results/run_log.txt

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet requests pymupdf psutil
.venv/bin/python -m pip freeze > results/pip_freeze.txt

echo "== 1. access probe (existing probe_access.py, unchanged)"
.venv/bin/python probe_access.py > results/01_probe_mac.txt 2>&1
cat results/01_probe_mac.txt

echo "== 2. PDF text extraction"
.venv/bin/python extract_test.py 2>&1 | tee results/02_extraction_log.txt

echo "== 3 and 4. local model check and evaluation"
.venv/bin/python model_eval.py 2>&1 | tee results/03_eval_log.txt

echo "== done $(date). Results are in the results folder."
