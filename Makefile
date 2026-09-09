.DEFAULT_GOAL := help
SHELL := /bin/bash

PROFILE ?= legal
INPUT   ?= ./samples/legal
CHUNKS  ?= chunks.jsonl
GRAPH   ?= graph.jsonl
LIMIT   ?= 10
Q       ?= obligations of the employer
TOPK    ?= 5

PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

# Every target that touches the database or an LLM needs .env loaded.
define load_env
set -a; [ -f .env ] && source .env; set +a;
endef

.PHONY: help setup up down logs shell reset test segment extract dry init load validate query all

help:
	@echo "graphrag-builder"
	@echo ""
	@echo "  make setup                       one-time VPS bootstrap"
	@echo "  make test                        offline smoke test (no LLM, no DB)"
	@echo ""
	@echo "  make up / down / logs / shell    Neo4j container"
	@echo "  make reset                       wipe the database volume"
	@echo ""
	@echo "  make extract  INPUT=./pdfs       PDFs -> clean text (optional, segment does it)"
	@echo "  make segment  PROFILE=legal INPUT=./samples/legal"
	@echo "  make dry      PROFILE=legal LIMIT=10      extraction preview, writes no DB"
	@echo "  make init     PROFILE=legal               constraints + indexes"
	@echo "  make load     PROFILE=legal               write to Neo4j + resolve"
	@echo "  make validate PROFILE=legal"
	@echo "  make query    PROFILE=legal Q='...'"
	@echo ""
	@echo "  make all      PROFILE=legal INPUT=./samples/legal"
	@echo ""
	@echo "current: PROFILE=$(PROFILE) INPUT=$(INPUT) PY=$(PY)"

setup:
	./setup.sh

test:
	./tests/smoke_test.sh

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f neo4j

shell:
	@$(load_env) docker compose exec neo4j cypher-shell -u $$NEO4J_USER -p $$NEO4J_PASSWORD

# Destroys the volume. The dry-run JSONL reloads without any LLM spend, which is
# why it is worth keeping around.
reset:
	docker compose down -v && docker compose up -d

extract:
	$(PY) scripts/extract_pdf.py --input $(INPUT) --out ./corpus

segment:
	$(PY) scripts/segment.py --profile $(PROFILE) --input $(INPUT) --out $(CHUNKS)

dry:
	@$(load_env) $(PY) scripts/build_graph.py --profile $(PROFILE) \
		--chunks $(CHUNKS) --dry-run --out $(GRAPH) --limit $(LIMIT)

init:
	@$(load_env) $(PY) scripts/init_db.py --profile $(PROFILE)

load:
	@$(load_env) $(PY) scripts/build_graph.py --profile $(PROFILE) \
		--chunks $(CHUNKS) --write

validate:
	@$(load_env) $(PY) scripts/validate.py --profile $(PROFILE)

query:
	@$(load_env) $(PY) scripts/query.py --profile $(PROFILE) --q "$(Q)" --top-k $(TOPK)

# Stops after the dry run on purpose. Inspect the extraction before paying to
# load it; that review is the whole point of the two-phase design.
all: segment dry
	@echo ""
	@echo "Dry run complete. Inspect $(GRAPH), then:  make init load validate"
