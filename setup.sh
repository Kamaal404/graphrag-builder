#!/usr/bin/env bash
# VPS bootstrap. Installs system deps, Python deps, and starts Neo4j.
#
#   chmod +x setup.sh && ./setup.sh
#
# Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- system deps
info "System dependencies"
if command -v apt-get >/dev/null 2>&1; then
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -qq
  # poppler-utils gives pdftotext/pdffonts/pdfinfo, used by the default PDF engine.
  # ocrmypdf is optional and only needed for scanned PDFs.
  $SUDO apt-get install -y -qq python3-venv python3-pip poppler-utils
  echo "installed: python3-venv python3-pip poppler-utils"
else
  warn "Not a Debian/Ubuntu host. Install manually: python3-venv, poppler-utils"
fi

if ! command -v docker >/dev/null 2>&1; then
  warn "Docker not found. Install it, then re-run:"
  echo "    curl -fsSL https://get.docker.com | sh"
  echo "    sudo usermod -aG docker \$USER   # then log out and back in"
fi

# -------------------------------------------------------------------- python
info "Python environment"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "venv ready: source .venv/bin/activate"

# ----------------------------------------------------------------------- env
info "Configuration"
if [ ! -f .env ]; then
  cp .env.example .env
  # Neo4j refuses to start with the default password, so generate a real one.
  PW="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
  sed -i "s|^NEO4J_PASSWORD=.*|NEO4J_PASSWORD=${PW}|" .env
  echo "wrote .env with a generated NEO4J_PASSWORD"
  warn "Add your OPENAI_API_KEY (or ANTHROPIC_API_KEY) to .env before extraction."
else
  echo ".env already exists, leaving it alone"
fi

# --------------------------------------------------------------------- neo4j
if command -v docker >/dev/null 2>&1; then
  info "Neo4j"
  docker compose up -d
  echo -n "waiting for Neo4j to accept connections"
  for _ in $(seq 1 60); do
    if docker compose exec -T neo4j cypher-shell -u neo4j \
        -p "$(grep '^NEO4J_PASSWORD=' .env | cut -d= -f2-)" \
        "RETURN 1" >/dev/null 2>&1; then
      echo " ok"
      break
    fi
    echo -n "."
    sleep 3
  done
  docker compose exec -T neo4j cypher-shell -u neo4j \
    -p "$(grep '^NEO4J_PASSWORD=' .env | cut -d= -f2-)" \
    "RETURN apoc.version() AS apoc" 2>/dev/null \
    || warn "APOC not responding yet. Check: docker compose logs neo4j"
fi

# --------------------------------------------------------------------- smoke
info "Offline smoke test"
./tests/smoke_test.sh

cat <<'EOF'

Next steps:

  source .venv/bin/activate
  make init                                  # constraints + indexes
  make segment PROFILE=legal INPUT=./samples/legal
  make dry     PROFILE=legal                 # needs an LLM key
  make load    PROFILE=legal
  make validate PROFILE=legal
  make query   PROFILE=legal Q="obligations of the employer"

Neo4j browser: http://<your-vps-ip>:7474
Do not expose 7474/7687 to the public internet without a firewall rule.
EOF
