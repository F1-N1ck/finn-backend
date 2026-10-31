#!/bin/bash
# Run on your Mac:  bash "/Users/nikhil/enam assg/run_render_host_check.sh"
# Asks for the admin token with NO echo. The token is never printed or saved to a file.
# Output: results/render_host_check.json  (contains no secrets; results/ is git-ignored)

URL="${1:-https://finn-backend-b4j5.onrender.com}"
cd "$(dirname "$0")" || exit 1
mkdir -p results

echo "Open Render > finn-backend > Environment > FINN_ADMIN_TOKEN, click reveal, copy it."
echo "Paste it below and press Enter. Nothing will be shown on screen."
read -rs FINN_TOKEN
echo

if [ -z "$FINN_TOKEN" ]; then
  echo "No token entered. Stopping."
  exit 1
fi

# The header goes in through stdin, so the token never appears in the process list.
call_admin() {
  curl -s -m 60 -H @- "$@" <<EOT
Authorization: Bearer $FINN_TOKEN
EOT
}

echo "1. Health (public):"
curl -s -m 120 "$URL/healthz"
echo
echo

echo "2. Starting host check (Yahoo, NSE, Screener, BSE PDF, BSE API, 2 Groq requests)..."
START=$(call_admin -X POST "$URL/admin/host-check?model=1")
echo "start response: $START"

JOB=$(echo "$START" | python3 -c "import sys, json; print(json.load(sys.stdin)['job_id'])" 2>/dev/null)
if [ -z "$JOB" ]; then
  echo "Could not start the job. If you saw 401 the token was wrong; 404 means the check is disabled."
  unset FINN_TOKEN
  exit 1
fi

echo "3. Waiting for job $JOB (up to about 5 minutes)..."
COUNT=0
while [ $COUNT -lt 32 ]; do
  sleep 10
  call_admin "$URL/admin/host-check/$JOB" -o results/render_host_check.json
  STATUS=$(python3 -c "import json; print(json.load(open('results/render_host_check.json')).get('status'))" 2>/dev/null)
  echo "   status: $STATUS"
  if [ "$STATUS" != "running" ]; then
    break
  fi
  COUNT=$((COUNT + 1))
done

unset FINN_TOKEN
echo
echo "Saved: results/render_host_check.json  (final status: $STATUS)"
echo "Tell me it is saved. Do NOT paste the file or the token into chat."
