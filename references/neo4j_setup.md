# Neo4j in Docker

Docker is the right default for GraphRAG development and for small to mid production. Compose file is at the repo root.

## Contents
- [Quick start](#quick-start)
- [Versioning and APOC compatibility](#versioning-and-apoc-compatibility)
- [Memory](#memory)
- [Plugins](#plugins)
- [Connecting](#connecting)
- [Backup and reset](#backup-and-reset)
- [When not to use Docker](#when-not-to-use-docker)

## Quick start

```bash
cp .env.example .env      # set NEO4J_PASSWORD
docker compose up -d
docker compose logs -f neo4j     # wait for "Started."
```

Browser UI at http://localhost:7474, Bolt at `bolt://localhost:7687`.

Verify APOC loaded, which is the most common setup failure:
```bash
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "RETURN apoc.version();"
```

## Versioning and APOC compatibility

Neo4j moved to calendar versioning. Tags look like `2026.07.1`, not `5.x`. Two rules:

1. **Pin an exact tag.** `neo4j:latest` will roll forward under you and break a working setup at the worst time.
2. **APOC year and month must match the Neo4j year and month.** Patch numbers need not match, so APOC `2026.07.0` works with Neo4j `2026.07.2`. A mismatch produces a warning in `neo4j.log` and procedures that fail at call time rather than at startup.

Using `NEO4J_PLUGINS='["apoc"]'` handles this automatically by installing the bundled matching version, which is why the compose file uses it. That mechanism is intended for development. For production, download the matching APOC jar and mount it at `/plugins`:

```bash
mkdir -p plugins
wget -P plugins https://github.com/neo4j/apoc/releases/download/2026.07.1/apoc-2026.07.1-core.jar
# then mount ./plugins:/plugins and drop NEO4J_PLUGINS
```

Vector indexes need a reasonably recent Neo4j. Any 2025.x or 2026.x calendar release is fine.

## Memory

The default heap is small and will not survive a real ingest. Set both explicitly:

```yaml
NEO4J_server_memory_heap_initial__size: 2G
NEO4J_server_memory_heap_max__size: 4G
NEO4J_server_memory_pagecache__size: 2G
```

Rules of thumb: page cache should be roughly the size of the graph on disk so the working set stays resident. Heap 2 to 4G handles most corpora up to a few million nodes. Give the Docker VM at least 2G more than heap plus page cache combined.

Note the double underscore in the env var names. It encodes a literal underscore in the Neo4j config key, and getting it wrong means the setting is silently ignored rather than rejected.

## Plugins

Set via `NEO4J_PLUGINS` as a JSON list. Supported keys: `apoc`, `apoc-extended`, `bloom`, `genai`, `graph-data-science`, `n10s`.

- `apoc` — required by the KG builder pipeline
- `graph-data-science` — only needed for community detection (Leiden) if you build global search. Heavy, skip it until you need it.
- `genai` — in-database embedding generation. Useful if you would rather not embed client-side.
- `n10s` (neosemantics) — RDF and OWL import. Relevant if the domain already has a published ontology worth importing rather than hand-writing.

If a plugin needs a licence, mount the licence directory at `/licenses`.

## Connecting

```python
import neo4j
driver = neo4j.GraphDatabase.driver(
    "bolt://localhost:7687",
    auth=("neo4j", os.environ["NEO4J_PASSWORD"]),
)
driver.verify_connectivity()
```

From another container on the same compose network, the host is the service name (`bolt://neo4j:7687`), not `localhost`.

Neo4j refuses to start with the default password `neo4j`; set a real one before first boot.

## Backup and reset

Dump, with the database stopped:
```bash
docker compose stop neo4j
docker run --rm \
  --volumes-from $(docker compose ps -q neo4j) \
  -v $PWD/backups:/backups \
  neo4j:2026.07.1 \
  neo4j-admin database dump neo4j --to-path=/backups
docker compose start neo4j
```

Wipe and start over, which you will do often while iterating on a schema:
```bash
docker compose down -v && docker compose up -d
```

Cheaper partial reset that keeps the container running:
```cypher
MATCH (n) DETACH DELETE n;
```
Slow above a few hundred thousand nodes. Past that, `down -v` is faster.

Snapshot a known-good graph before a risky re-ingest. Rebuilding from raw documents costs real LLM spend, and the dry-run JSONL from stage 3 is worth keeping for exactly this reason: reloading from it needs no LLM calls at all.

## When not to use Docker

- **Neo4j Aura** if nobody wants to operate a database. Managed, has vector indexes, APOC core is preinstalled. Reasonable for production when the graph is not huge.
- **Bare metal or a VM** for very large graphs where page cache should be tens of gigabytes and container overhead plus volume indirection start to matter.
- **In-memory NetworkX** for a prototype under roughly 50k nodes where the goal is validating the ontology rather than building a system. Faster iteration, no infrastructure, and the profile and extraction stages carry over unchanged when you move to Neo4j.
