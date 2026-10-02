#!/usr/bin/env sh
# Build the widget and prepare both demo pages. Run after `docker compose up -d`.
set -eu
cd "$(dirname "$0")/.."

echo "1/2 Building the widget (web/widget)..."
(cd web/widget && npm ci --no-audit --no-fund --loglevel=error && npm run build)

echo "2/2 Seeding demo tenants and writing web/demo/config.local.js (git-ignored)..."
docker compose run --rm migrate python -m app.cli seed-demo \
  --demo-config /site/config.local.js --demo-api http://localhost:8000

echo
echo "Done. Open:"
echo "  http://localhost:8080/nodi-kitchen/"
echo "  http://localhost:8080/jamdani-lane/"
