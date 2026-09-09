# neo4j-graphrag pipeline cookbook

API reference for the extraction and loading stages. Current as of `neo4j-graphrag` 1.18.x. Several parameter names changed recently, so prefer this file over older blog posts and tutorials.

## Contents
- [Install](#install)
- [Pipeline components](#pipeline-components)
- [SimpleKGPipeline](#simplekgpipeline)
- [Schema definition](#schema-definition)
- [Pruning, the part that enforces the schema](#pruning-the-part-that-enforces-the-schema)
- [Structured output](#structured-output)
- [Custom text splitter](#custom-text-splitter)
- [Dry run: writing to JSON instead of Neo4j](#dry-run-writing-to-json-instead-of-neo4j)
- [Entity resolution](#entity-resolution)
- [The lexical graph](#the-lexical-graph)
- [Config-file driven pipelines](#config-file-driven-pipelines)
- [Common failures](#common-failures)

## Install

```bash
pip install "neo4j-graphrag[openai]"        # or [anthropic], [google], [mistralai], [cohere], [ollama]
pip install "neo4j-graphrag[fuzzy-matching]"  # RapidFuzz, for FuzzyMatchResolver
pip install "neo4j-graphrag[nlp]"             # spaCy, for SpaCySemanticMatchResolver
```

The `nlp` extra does not currently install on Python 3.14 because of an upstream spaCy issue. Use 3.11 or 3.12 for the smoothest ride.

APOC core must be installed in the Neo4j instance or the KG builder will fail.

## Pipeline components

The full pipeline, in order. `SimpleKGPipeline` wires these together; the lower-level `Pipeline` class lets you swap any of them.

1. **Data loader** — file to text (`PdfLoader`, `MarkdownLoader`, or a custom `DataLoader`)
2. **Text splitter** — text to chunks (`FixedSizeSplitter` or a custom `TextSplitter`)
3. **Chunk embedder** — `TextChunkEmbedder`, optional but needed for vector retrieval
4. **Schema builder** — `SchemaBuilder` (manual) or `SchemaFromTextExtractor` (LLM-derived)
5. **Lexical graph builder** — `Document` and `Chunk` nodes plus `NEXT_CHUNK`, `FROM_DOCUMENT`
6. **Entity and relation extractor** — `LLMEntityRelationExtractor`
7. **Graph pruner** — enforces the schema on extracted output
8. **KG writer** — `Neo4jWriter`, or a custom `KGWriter`
9. **Entity resolver** — merges duplicate entity nodes

## SimpleKGPipeline

```python
import asyncio
import neo4j
from neo4j_graphrag.embeddings import OpenAIEmbeddings
from neo4j_graphrag.llm import OpenAILLM
from neo4j_graphrag.experimental.pipeline.kg_builder import SimpleKGPipeline

driver = neo4j.GraphDatabase.driver(URI, auth=(USER, PASSWORD))

kg_builder = SimpleKGPipeline(
    llm=OpenAILLM(model_name="gpt-4.1-mini", model_params={"temperature": 0}),
    driver=driver,
    embedder=OpenAIEmbeddings(model="text-embedding-3-small"),
    schema=SCHEMA,                    # see below
    from_file=False,                  # True to read a PDF/markdown path
    perform_entity_resolution=True,
    on_error="IGNORE",                # "RAISE" to fail the whole run on any bad chunk
    neo4j_database="neo4j",
)

asyncio.run(kg_builder.run_async(text=chunk_text))
# or: run_async(file_path="doc.pdf") when from_file=True
```

`from_pdf` and `pdf_loader` are deprecated aliases for `from_file` and `file_loader`. They still work with a warning.

`run_async` also accepts `document_metadata` (a dict), saved as properties on the `Document` node. Use it for `jurisdiction`, `source_url`, `version`, `visibility`, and anything else you will later filter on.

## Schema definition

Node and relationship types are either bare label strings or dicts:

```python
NODE_TYPES = [
    "Actor",
    {"label": "Clause", "description": "A single numbered clause or alinéa"},
    {"label": "Sanction", "properties": [
        {"name": "name", "type": "STRING"},
        {"name": "amount", "type": "FLOAT"},
        {"name": "currency", "type": "STRING"},
    ]},
]

RELATIONSHIP_TYPES = [
    "REFERS_TO",
    {"label": "EXCEPTION_TO", "description": "This clause carves out an exception to the target"},
    {"label": "USES_INGREDIENT", "properties": [
        {"name": "amount", "type": "FLOAT"},
        {"name": "unit", "type": "STRING"},
    ]},
]

PATTERNS = [
    ("Clause", "REFERS_TO", "Clause"),
    ("Clause", "EXCEPTION_TO", "Clause"),
]

SCHEMA = {
    "node_types": NODE_TYPES,
    "relationship_types": RELATIONSHIP_TYPES,
    "patterns": PATTERNS,
    "additional_node_types": False,
    "additional_relationship_types": False,
    "additional_patterns": False,
}
```

Property types: `STRING`, `INTEGER`, `FLOAT`, `BOOLEAN`, `DATE`, `LOCAL_DATETIME`, `LIST`.

Schema modes on the `schema` parameter:
- a dict, as above, is the grounded case and what this skill uses
- `"EXTRACTED"` or `None` derives a schema from the text once with an LLM, then applies it to all chunks
- `"FREE"` or `{"node_types": ()}` disables schema guidance entirely, which is the hairball case

A node type with no `properties` key automatically gets `name: STRING` and `additional_properties=True`. Passing `"properties": []` explicitly raises a `ValidationError`.

## Pruning, the part that enforces the schema

The schema guides the LLM but does not constrain it. Enforcement happens afterwards in the graph pruner, driven by these flags:

- `additional_node_types: false` — drop nodes with labels not in the schema
- `additional_relationship_types: false` — drop edges with types not in the schema
- `additional_patterns: false` — drop edges whose endpoint types violate the declared patterns. Requires `additional_relationship_types: false` as well.
- `additional_properties: false` on a node or relationship type — drop properties not declared

Defaults: `additional_properties` is `False` when at least one property is declared and `True` otherwise. `additional_patterns` defaults to `True`, so **set it to false explicitly** or your type signatures are not enforced.

The pruner also unconditionally removes nodes with empty label or ID, nodes left with no properties after property pruning, edges with empty type, edges whose endpoints no longer exist, and it corrects reversed edge directions.

Mandatory properties go through `GraphSchema.constraints` with type `EXISTENCE`; nodes and edges missing them are pruned. The old per-property `required` flag is deprecated and migrated automatically.

## Structured output

For `OpenAILLM`, `VertexAILLM` and `AnthropicLLM`, enable structured output. It enforces the response shape at the API level rather than parsing JSON out of prose, which noticeably reduces dropped chunks.

```python
from neo4j_graphrag.components.entity_relation_extractor import LLMEntityRelationExtractor

llm = OpenAILLM(model_name="gpt-4.1-mini", model_params={"temperature": 0})
extractor = LLMEntityRelationExtractor(llm=llm, use_structured_output=True)
```

`SimpleKGPipeline` enables this automatically when the LLM declares `supports_structured_output = True`. Do not also pass `response_format` in `model_params`, it will be ignored, and passing it to `SchemaFromTextExtractor` with `use_structured_output=True` raises.

Using `use_structured_output=True` with an unsupported provider raises `ValueError`.

## Custom text splitter

The built-in splitter is fixed-size. For structured corpora, pre-segment with `scripts/segment.py` and feed chunks in one at a time with `from_file=False`, or wrap your own splitter:

```python
from neo4j_graphrag.components.text_splitters.base import TextSplitter
from neo4j_graphrag.components.types import TextChunks, TextChunk

class ProfileSplitter(TextSplitter):
    def __init__(self, boundaries):
        self.boundaries = boundaries

    async def run(self, text: str) -> TextChunks:
        return TextChunks(chunks=[
            TextChunk(text=t, index=i, metadata={"path": p})
            for i, (t, p) in enumerate(self.boundaries(text))
        ])
```

The built-in one, if you do need it:
```python
from neo4j_graphrag.components.text_splitters.fixed_size_splitter import FixedSizeSplitter
FixedSizeSplitter(chunk_size=4000, chunk_overlap=200, approximate=True)
```
`approximate=True` avoids cutting mid-word.

LangChain and LlamaIndex splitters work through `LangChainTextSplitterAdapter` and `LlamaIndexTextSplitterAdapter`.

## Dry run: writing to JSON instead of Neo4j

This is stage 3 of the pipeline and the reason extraction problems are cheap to fix here.

```python
import json
from pydantic import validate_call
from neo4j_graphrag.components.kg_writer import KGWriter
from neo4j_graphrag.components.types import KGWriterModel, Neo4jGraph

class JsonWriter(KGWriter):
    def __init__(self, path: str):
        self.path = path

    @validate_call
    async def run(self, graph: Neo4jGraph, **kwargs) -> KGWriterModel:
        with open(self.path, "a") as f:
            f.write(json.dumps(graph.model_dump(), ensure_ascii=False) + "\n")
        return KGWriterModel(status="SUCCESS")

kg_builder = SimpleKGPipeline(..., kg_writer=JsonWriter("graph.jsonl"))
```

The `@validate_call` decorator is required whenever a parameter is a Pydantic model.

Also set `perform_entity_resolution=False` for dry runs, since the resolvers operate on the database and there is nothing there yet.

## Entity resolution

Three resolvers ship with the package:

| Resolver | Basis | Use when |
|---|---|---|
| `SinglePropertyExactMatchResolver` | identical label + `name` | Names are already normalized upstream |
| `FuzzyMatchResolver` | RapidFuzz Levenshtein | Fast, tolerant of typos and spacing, default choice |
| `SpaCySemanticMatchResolver` | spaCy static embeddings, cosine | Synonyms and translations, highest quality, slowest |

```python
from neo4j_graphrag.components.resolver import FuzzyMatchResolver

resolver = FuzzyMatchResolver(driver, filter_query="WHERE NOT entity:Resolved")
await resolver.run()
```

All three **replace** the nodes the writer created, and by default they touch every node carrying the `__Entity__` label. On a growing database that means re-resolving everything on each run, which is slow and can merge things a human already separated. Always scope with `filter_query`:

```python
# skip anything already resolved
filter_query = "WHERE NOT entity:Resolved"

# skip entities from a previous run of the same document
filter_query = "WHERE NOT EXISTS((entity)-[:FROM_DOCUMENT]->(:OldDocument))"
```

For a canonical-vocabulary domain, resolve against the vocabulary rather than pairwise between extracted nodes. Pairwise resolution drifts; anchoring to a fixed list does not.

## The lexical graph

Created by default. Node labels and edge types are configurable:

```python
from neo4j_graphrag.components.types import LexicalGraphConfig

config = LexicalGraphConfig(
    chunk_node_label="Chunk",
    document_node_label="Document",
    chunk_to_document_relationship_type="PART_OF_DOCUMENT",
    next_chunk_relationship_type="NEXT_CHUNK",
    node_to_chunk_relationship_type="PART_OF_CHUNK",
    chunk_embedding_property="embeddings",
)
```

Keep it. Entity-only graphs answer worse than graphs that carry chunk text, because the entity gives the traversal path and the chunk gives the grounding.

Setting `create_lexical_graph=False` while still passing a `lexical_graph_config` skips the `Document` and `Chunk` nodes but keeps entity-to-chunk edges, which is what you want when chunks were written by a separate earlier process. If the chunks do not exist, no relationships get created at all and the failure is silent.

## Config-file driven pipelines

Whole pipelines can be defined in YAML or JSON, with env-var resolution for secrets:

```yaml
version_: 1
template_: SimpleKGPipeline
neo4j_config:
  params_:
    uri: bolt://localhost:7687
    user: neo4j
    password:
      resolver_: ENV
      var_: NEO4J_PASSWORD
llm_config:
  class_: OpenAILLM
  params_:
    model_name: gpt-4.1-mini
    api_key:
      resolver_: ENV
      var_: OPENAI_API_KEY
embedder_config:
  class_: OpenAIEmbeddings
from_file: false
perform_entity_resolution: true
schema:
  node_types: [...]
  relationship_types: [...]
  patterns: [...]
```

```python
from neo4j_graphrag.experimental.pipeline.config.runner import PipelineRunner
pipeline = PipelineRunner.from_config_file("my_config.yaml")
await pipeline.run({"text": "..."})
```

Useful when the same pipeline runs in CI or on a schedule. For interactive development the Python API is easier to debug.

## Common failures

**Everything extracts but the graph is a hairball.** `additional_patterns` left at its `True` default. Set all three `additional_*` flags to false.

**Extraction silently drops chunks.** `on_error="IGNORE"` is the default. Set `"RAISE"` during development to see what is actually failing, then switch back for production runs.

**Vector retriever returns nothing.** The vector index does not exist, or its dimensions do not match the embedder. Run `init_db.py` first and check `SHOW INDEXES`.

**Duplicate entities everywhere.** Resolution not run, or run without the property the entities actually differ on. Check that `name` is populated, since all three resolvers key on it by default.

**Pipeline fails on startup with a procedure error.** APOC missing, or an APOC version that does not match the Neo4j year.month.

**Relations exist but have no properties.** The relationship type declared no properties, so `additional_properties` defaulted to `True` and the LLM produced arbitrary keys, or it declared properties and the extras got pruned. Declare what you want explicitly.
