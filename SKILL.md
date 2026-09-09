---
name: graphrag-builder
description: Turn a raw text corpus (laws and regulations, recipe collections, policies, manuals, standards, medical or technical documentation) into a schema-grounded Neo4j knowledge graph and a working GraphRAG retrieval layer. Use this skill whenever the user wants to build a knowledge graph from documents, mentions GraphRAG, graph RAG, Neo4j ingestion, ontology or schema design for a corpus, entity and relation extraction, entity resolution, Cypher retrievers, or says something like "turn these documents into a graph", "I have raw data and want nodes and edges", "make this searchable with relationships", or "load this corpus into Neo4j". Also use it when the user is debugging a knowledge graph that came out as a hairball, has duplicate entities, or gets bad answers from graph retrieval.
---

# GraphRAG Builder

Build a knowledge graph plus retrieval layer from an unstructured or semi-structured corpus, using Neo4j and the `neo4j-graphrag` Python package.

The core claim this skill is built around: **open-ended "read text, make a graph" extraction produces a hairball that retrieves worse than plain vector RAG.** What makes GraphRAG work is a closed, domain-specific schema plus a deterministic structural layer. So this skill is one pipeline with swappable **corpus profiles**, not a general-purpose triple extractor.

## The five-stage pipeline

Run these in order. Each stage writes an inspectable artifact, so failures are localized instead of showing up as bad answers three weeks later.

```
raw docs (pdf / txt / md)
  └─0─> extract_pdf.py → clean .txt + page map   (only for PDFs; segment.py calls it for you)
  └─1─> segment.py    → chunks.jsonl        (deterministic: hierarchy, IDs, cross-refs)
  └─2─> profile.yaml  → schema              (ontology: node types, edge types, patterns)
  └─3─> build_graph.py --dry-run → graph.json  (LLM extraction, nothing written yet)
  └─4─> build_graph.py --write   → Neo4j    (load + resolve entities)
  └─5─> validate.py / query.py   → report   (structural checks, then retrieval)
```

Stage 3 before stage 4 matters. Extraction is where the quality is won or lost, and a JSON dump is far cheaper to inspect and iterate on than a database you have to keep wiping. Only write to Neo4j once the dry run looks sane on a 5 to 10 document sample.

## Stage 0a: PDFs

`scripts/segment.py` accepts `.pdf` directly and extracts as needed, caching to
`.text-cache/`. Run the extractor standalone when you want to inspect or tune it:

```bash
python scripts/extract_pdf.py --input ./pdfs --out ./corpus --diagnose-only
python scripts/extract_pdf.py --input ./pdfs --out ./corpus
```

Naive PDF extraction quietly destroys the structure everything downstream
depends on, and the damage surfaces much later as "the graph came out empty".
The extractor handles the four failure modes that matter:

- **Running headers and footers** land mid-article and get swallowed into clause
  text. Detected by fingerprint frequency across pages (digits normalised, so
  "Page 12 of 340" and "Page 13 of 340" collapse together) and stripped.
- **Hyphenated line breaks** split words, so `obliga-\ntion` never matches a
  search for "obligation". Rejoined when the continuation is lowercase.
- **Soft wraps** put "Article" and "12" on separate lines and the profile's
  hierarchy regexes stop matching. Rejoined by line width rather than
  punctuation: a wrapped line runs to the right margin, while headings and
  paragraph-final lines are short. Width is measured before any joining, since
  measuring after lets joined lines inflate the statistic and block the rest.
- **Scanned PDFs** have no text layer at all. Detected via `pdffonts` and
  reported with the `ocrmypdf` command to run, rather than silently producing
  an empty corpus.

Engines: `poppler` (default, `pdftotext -layout`, best on multi-column),
`pdfplumber` (better on irregular word spacing), `pypdf` (fallback, no system
deps). All three converge on the same chunks after cleanup. If output looks
wrong, try another engine before touching the profile.

Extraction also writes `<name>.pages.json`, a char-offset to page-number map.
`segment.py` uses it to stamp every chunk with its page, so citations read
`Article 12 (p. 34)`. For legal and regulatory corpora that page number is the
difference between an answer someone can check and one they cannot.

Always open one extracted `.txt` before segmenting. If headings did not survive,
that is a cleanup problem here, not a profile problem, and no amount of regex
tuning in the profile will fix it.

## Stage 0b: pick or write a profile

Everything domain-specific lives in `profiles/<domain>.yaml`. Never edit the scripts for a new corpus, write a profile.

Shipped profiles: `profiles/legal.yaml`, `profiles/recipes.yaml`. Read one before writing a new one, they are the format spec.

A profile declares:
- `segmentation`: regex patterns for the document hierarchy, plus cross-reference patterns
- `node_types` / `relationship_types` / `patterns`: the closed ontology, in `neo4j-graphrag` GraphSchema form
- `resolution`: which node labels get merged and by what strategy
- `indexes`: vector and fulltext index targets
- `retrieval`: default traversal depth and the Cypher used to expand a seed hit

When the user hands over a new corpus, read a sample of it first, then propose the ontology and get sign-off before extracting anything. Guessing at a schema and running a full extraction pass wastes real money and produces something the user has to argue with rather than correct.

Design rules for the ontology are in `references/schema_design.md`. Read it before writing a new profile, it covers node granularity, the domain/range discipline that kills most bad edges, and provenance requirements.

## Stage 1: deterministic segmentation

`scripts/segment.py --profile <p> --input <dir> --out chunks.jsonl`

Accepts `.txt`, `.md` and `.pdf`. PDFs route through stage 0a automatically.

Never let the LLM do what a parser can do. Structure, numbering, and cross-references are parseable at near-100% precision, and for legal corpora this layer alone is roughly 70% of the value.

| Parse deterministically | Send to the LLM |
|---|---|
| Document hierarchy (Book, Title, Chapter, Article) | Semantic type assignment |
| Article and clause numbering | Relation classification from a closed list |
| Cross-references ("cf. art. 42", "voir alinéa 3") | Entity linking to a canonical name |
| Dates, quantities, units, currency | Conditions and exceptions |
| Amendment and repeal markers | Substitution and equivalence |

Chunk on structural boundaries, not fixed character counts, whenever the corpus has structure. A recipe is a chunk. A legal article is a chunk. Fixed-size splitting cuts relations in half and those relations are the ones you then fail to extract.

Every chunk carries: stable `id`, `doc_id`, `path` (the hierarchy breadcrumb), `text`, `char_start`, `char_end`, and any `xrefs` found.

## Stage 2 and 3: schema-grounded extraction

`scripts/build_graph.py --profile <p> --chunks chunks.jsonl --dry-run --out graph.json`

Uses `neo4j-graphrag`'s `SimpleKGPipeline` with an explicit schema and pruning turned on. The API details, including the exact schema dict shape and the pruning flags that actually enforce it, are in `references/pipeline.md`. Read that file before writing pipeline code, the library moved recently and several parameter names in older blog posts are deprecated.

Two settings carry most of the quality:

- `additional_node_types: false` and `additional_relationship_types: false` in the schema. Without these the schema is a suggestion and the LLM invents labels freely.
- `use_structured_output=True` on the extractor when the LLM supports it (OpenAI, VertexAI, Anthropic). This enforces conformance at the API level instead of hoping JSON parses.

### How good can extraction get

This is the question users actually want answered, so give them the honest tier list rather than a vague "it depends":

| Tier | What it extracts | Realistic precision |
|---|---|---|
| L0 | Hierarchy and cross-references, pure parsing | ~99% |
| L1 | Typed entity mentions against a closed vocabulary | 90-95% |
| L2 | Relations where both endpoints sit in the same chunk | 80-90% |
| L3 | Cross-chunk or implicit relations | 50-70% |
| L4 | Open extraction, no schema | 30-50% |

**Target L2 automatic, L3 with a verification pass.** The verifier re-feeds each candidate edge together with both source spans and asks accept or reject. It recovers most of the L3 gap and roughly doubles extraction cost. L4 is not worth shipping: it produces hub nodes with thousands of edges that poison every traversal.

Tell users this tier list when they ask "will this work on my data". It reframes the question from yes/no into which tier their queries need.

## Stage 4: load and resolve

`scripts/init_db.py --profile <p>` then `scripts/build_graph.py --profile <p> --chunks chunks.jsonl --write`

`init_db.py` creates uniqueness constraints and the vector plus fulltext indexes the retrievers need. Run it before the first write, and note that indexes must exist before the vector retriever will return anything at all, which is a common silent failure.

**Keep the lexical graph.** The `Document` and `Chunk` nodes with their `NEXT_CHUNK` and `FROM_DOCUMENT` edges are not overhead. Graphs with chunk text attached to entities measurably outperform entity-only graphs on answer accuracy and completeness, because the entity gives you the path and the chunk gives you the grounding the model actually quotes from.

**Entity resolution is not optional.** "farine de blé", "wheat flour", and "flour, all-purpose" must collapse to one node. Unresolved entities are the single biggest cause of GraphRAG that sounds confident and is wrong. The library ships three resolvers (exact, fuzzy via RapidFuzz, semantic via spaCy) and `references/pipeline.md` covers when each one is appropriate and how to scope them with `filter_query` so re-runs do not re-resolve the whole database.

Writes are idempotent via `MERGE` on deterministic IDs, so re-ingesting a document updates rather than duplicates.

## Stage 5: validate, then retrieve

`scripts/validate.py --profile <p>` runs structural checks that catch the failure modes before users do:
- pattern violations (edges whose endpoint types the profile forbids)
- hub detection (nodes above a degree threshold, almost always a resolution failure or a junk entity)
- orphans (entities with no chunk provenance)
- provenance coverage (share of edges that can name a source span)
- duplicate candidates (same label, high name similarity, not merged)

`scripts/query.py --profile <p> --q "..."` runs the retrieval smoke test.

Retrieval guidance including which retriever to use for which query shape, the `retrieval_query` traversal patterns, and how to enforce hard filters is in `references/retrieval.md`.

The short version: **keep vector search, do not replace it.** GraphRAG beats plain vector RAG on multi-hop and aggregation questions and loses on simple lookup. The working pattern is hybrid search to pick seed nodes, then k-hop expansion (k=2 usually, k=3 for legal corpora with dense cross-references), then rerank the assembled subgraph.

Anything used as a hard filter (allergens, dietary flags, jurisdiction, in-force dates, access permissions) belongs as a graph property enforced in Cypher, never as an instruction in the system prompt. Prompt-level filtering can be talked around; a `WHERE` clause cannot.

## Neo4j in Docker

Docker is the right call for development and small production. `docker-compose.yml` at the repo root is ready to run. Setup details, version and APOC compatibility rules, memory tuning, and the backup command are in `references/neo4j_setup.md`.

Three things worth knowing up front, because each one costs an hour when discovered late:
- APOC is required by the KG builder pipeline, and the APOC year.month must match the Neo4j year.month.
- Neo4j now uses calendar versioning (`2026.07.x`), not semver. Pin an exact tag; `:latest` will change under you.
- The default 512MB heap will not survive a real ingest. Set heap and page cache explicitly in the compose file.

## Working style

Match the user's technical level, but for anyone building this, assume they can read Cypher and Python.

Prefer showing the artifact over describing it. Run the dry run on a sample and show the actual extracted nodes and edges rather than explaining what extraction would produce.

When a graph comes out badly, diagnose in this order: schema too open, then chunking cutting relations, then resolution not run, then retrieval query wrong. It is almost never the LLM being bad at extraction, and almost always one of those four.
