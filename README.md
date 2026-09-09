# graphrag-builder

Turn a raw corpus (laws, regulations, recipes, policies, manuals) into a
schema-grounded Neo4j knowledge graph plus a working GraphRAG retrieval layer.

Also packaged as a Claude skill: `SKILL.md` at the repo root makes the whole
thing usable from Claude directly.

## Why this is not just "extract triples with an LLM"

Open-ended extraction produces a hairball that retrieves worse than plain vector
RAG. Two things fix that, and both are structural rather than prompt tricks:

1. **A closed, domain-specific schema** with declared type signatures on every
   relationship. Any edge violating its signature is dropped mechanically, with
   no human reading it.
2. **A deterministic layer** that parses hierarchy, numbering and
   cross-references instead of asking a model to. On legal corpora that layer
   alone carries most of the value, at near-100% precision.

Everything domain-specific lives in `profiles/*.yaml`. A new corpus means a new
profile, never a change to `scripts/`.

## Pipeline

```
raw docs (pdf / txt / md)
  |-0-> extract_pdf.py  -> clean .txt + page map     (segment.py calls this for you)
  |-1-> segment.py      -> chunks.jsonl              (hierarchy, IDs, cross-refs)
  |-2-> profiles/*.yaml -> schema
  |-3-> build_graph.py --dry-run -> graph.jsonl      (LLM extraction, nothing written)
  |-4-> build_graph.py --write   -> Neo4j            (load + entity resolution)
  |-5-> validate.py / query.py                       (structural checks, retrieval)
```

Stage 3 before stage 4 is the point. Extraction is where quality is won or lost,
and a JSONL file is much cheaper to iterate on than a database you keep wiping.

## Quick start on a VPS

```bash
git clone <your-repo-url> graphrag-builder && cd graphrag-builder
chmod +x setup.sh && ./setup.sh
```

`setup.sh` installs system packages, creates a venv, generates a `.env` with a
real Neo4j password, starts the container and runs the offline test suite. It is
safe to re-run.

Then add an LLM key to `.env` and run the pipeline:

```bash
source .venv/bin/activate

make segment PROFILE=legal INPUT=./samples/legal
make dry     PROFILE=legal LIMIT=10     # inspect graph.jsonl before paying to load
make init    PROFILE=legal
make load    PROFILE=legal
make validate PROFILE=legal
make query   PROFILE=legal Q="obligations of the employer"
```

`make help` lists every target.

## Requirements

- Docker and Docker Compose
- Python 3.11 or 3.12 (3.14 breaks the optional spaCy resolver)
- `poppler-utils` for the default PDF engine
- An OpenAI or Anthropic API key, for extraction and embeddings only. Stages 0
  and 1 are fully offline.

## Testing without a key or a database

```bash
make test
```

Runs 18 checks over profile validity, PDF diagnosis, header and footer removal,
hyphenation, soft-wrap rejoining, page mapping, cross-reference typing, and
three-engine agreement. If this passes, the deterministic half is healthy and
any later failure is an LLM, schema or Neo4j problem rather than a parsing one.

## Layout

```
SKILL.md              skill entry point: pipeline, extraction quality tiers, working style
README.md             this file
Makefile              wraps every stage
setup.sh              VPS bootstrap
docker-compose.yml    Neo4j 2026.07.1 + APOC, memory tuned
.env.example          copy to .env

profiles/
  legal.yaml          clause-level statutes, temporal validity, French xref patterns
  recipes.yaml        recipes, allergens enforced in Cypher, canonical ingredient vocab

scripts/
  extract_pdf.py      PDF to clean text + page map
  segment.py          structural chunking + cross-reference resolution
  build_graph.py      LLM extraction, dry-run or write, entity resolution
  init_db.py          constraints, vector and fulltext indexes
  validate.py         pattern violations, hubs, orphans, provenance, duplicates
  query.py            retrieval smoke test
  common.py           profile loading, driver, schema translation

references/
  schema_design.md    ontology design rules, worked legal and recipe ontologies
  pipeline.md         neo4j-graphrag 1.18 API cookbook and failure modes
  retrieval.md        retriever selection, traversal queries, evaluation
  neo4j_setup.md      Docker, versioning, APOC compatibility, memory, backup

samples/              a 3-page PDF with real-world defects, plus text and markdown
tests/smoke_test.sh   offline test suite
```

## PDF handling

PDFs go straight into `segment.py`; extraction is automatic and cached in
`.text-cache/`. Naive extraction silently destroys the structure everything
downstream depends on, so the extractor handles four failure modes:

- **Running headers and footers** get swallowed into clause text. Detected by
  fingerprint frequency across pages, with digits normalised so "Page 12 of 340"
  and "Page 13 of 340" collapse to one fingerprint.
- **Hyphenated line breaks** split words, so a word broken as `obliga-` plus
  `tion` never matches a search for "obligation".
- **Soft wraps** put "Article" and "12" on separate lines and the hierarchy
  regexes stop matching. Rejoined by line width rather than punctuation, since a
  wrapped line runs to the right margin while a heading is short.
- **Scanned PDFs** have no text layer. Detected and reported with the
  `ocrmypdf` command to run, rather than producing an empty corpus.

Extraction also emits a char-offset to page-number map, so citations read
`Article 12 (p. 34)`.

Engines: `poppler` (default), `pdfplumber`, `pypdf`. All three converge on the
same chunks. If output looks wrong, try another engine before editing a profile.

Known limitation: dehyphenation does not cross page boundaries, because pages
are cleaned independently to keep offsets exact. A word hyphenated across a page
break stays split.

## Security note for VPS deployments

`docker-compose.yml` publishes 7474 and 7687. Do not leave those open to the
internet. Bind them to localhost and use an SSH tunnel:

```yaml
ports:
  - "127.0.0.1:7474:7474"
  - "127.0.0.1:7687:7687"
```

```bash
ssh -L 7474:localhost:7474 -L 7687:localhost:7687 user@your-vps
```

## License

MIT. See `LICENSE`.
