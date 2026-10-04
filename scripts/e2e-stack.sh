#!/usr/bin/env sh
# Start (or stop) the end-to-end stack: a separate Compose project with offline providers and
# its own ports, seeded with the demo tenants. Usage: scripts/e2e-stack.sh up|down
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=bap-e2e
export API_PORT="${E2E_API_PORT:-18000}" DASHBOARD_PORT="${E2E_DASHBOARD_PORT:-13001}"
export POSTGRES_PORT="${E2E_POSTGRES_PORT:-15432}" REDIS_PORT="${E2E_REDIS_PORT:-16379}"
export DEMO_PORT="${E2E_DEMO_PORT:-18080}"
# Never use real providers here, whatever .env says.
export GEMINI_API_KEY="" OPENAI_COMPAT_BASE_URL="" OPENAI_COMPAT_API_KEY="" OPENAI_COMPAT_MODEL=""
compose="docker compose -f docker-compose.yml -f docker-compose.e2e.yml"

if [ "${1:-up}" = "down" ]; then
  $compose down -v
  exit 0
fi

$compose up -d postgres redis api worker dashboard
for i in $(seq 1 60); do
  curl -sf "http://localhost:$API_PORT/health" >/dev/null && break
  sleep 2
done
# Demo tenants and documents (hashing embeddings). New tenants' keys are printed by seed-demo:
# keep them out of the log.
$compose run --rm -T migrate python -m app.cli seed-demo | grep -v 'bap_' || true
for i in $(seq 1 30); do
  curl -sf "http://localhost:$DASHBOARD_PORT/login" >/dev/null && break
  sleep 2
done
echo "e2e stack ready: API http://localhost:$API_PORT, dashboard http://localhost:$DASHBOARD_PORT"
