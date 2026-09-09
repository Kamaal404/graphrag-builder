# Retrieval

How to get answers out of the graph once it is built.

## Contents
- [Choosing a retriever](#choosing-a-retriever)
- [The retrieval_query](#the-retrieval_query)
- [Hard filters belong in Cypher](#hard-filters-belong-in-cypher)
- [Hop depth](#hop-depth)
- [Indexes](#indexes)
- [Global questions and community summaries](#global-questions-and-community-summaries)
- [Evaluating retrieval](#evaluating-retrieval)

## Choosing a retriever

`neo4j-graphrag` ships these, all with a `.search()` method:

| Retriever | What it does | Best for |
|---|---|---|
| `VectorRetriever` | Vector index similarity, returns node + score | Simple lookup |
| `VectorCypherRetriever` | Vector search, then a Cypher traversal | Multi-hop from a semantic seed |
| `HybridRetriever` | Vector + fulltext, fused | Corpora with proper nouns, codes, article numbers |
| `HybridCypherRetriever` | Hybrid, then a Cypher traversal | **The default choice for this skill** |
| `Text2CypherRetriever` | LLM writes Cypher, executes it | Aggregation and counting questions |
| `ToolsRetriever` | LLM picks among retrievers exposed as tools | Mixed query workloads, agentic setups |
| `Weaviate/Pinecone/QdrantNeo4jRetriever` | External vector DB + Neo4j | Vectors already live elsewhere |

Start with `HybridCypherRetriever`. Fulltext matters more than people expect on structured corpora, because users type "article 42" and "sourdough" and vector similarity is mediocre at both exact identifiers and short rare terms.

Add `Text2CypherRetriever` as a second tool when users ask counting or aggregation questions ("how many recipes use saffron", "which articles were amended in 2023"). It has no embedder requirement. The generated Cypher is not guaranteed to parse and raises `Text2CypherRetrievalError` when it does not, so wrap it and fall back.

`ToolsRetriever` on top of both is the clean way to route without writing a classifier:

```python
vector_tool = hybrid_retriever.convert_to_tool(
    name="semantic_search",
    description="Find clauses by topic or meaning",
)
cypher_tool = text2cypher.convert_to_tool(
    name="structured_query",
    description="Count, aggregate, or filter on exact properties",
)
tools_retriever = ToolsRetriever(driver=driver, llm=llm, tools=[vector_tool, cypher_tool])
```

## The retrieval_query

The `*CypherRetriever` variants take a `retrieval_query` that runs after the index hit. The matched node is bound to the variable `node`, and the similarity score to `score`.

```python
RETRIEVAL_QUERY = """
MATCH (node)<-[:PART_OF_CHUNK]-(e)
OPTIONAL MATCH path = (e)-[:REFERS_TO|EXCEPTION_TO*1..2]-(related)
RETURN node.text                        AS text,
       node.path                        AS citation,
       collect(DISTINCT related.name)   AS related_entities,
       [r IN relationships(path) | type(r)] AS relation_types,
       score                            AS similarity
"""

retriever = HybridCypherRetriever(
    driver=driver,
    vector_index_name="chunk_embedding",
    fulltext_index_name="chunk_fulltext",
    retrieval_query=RETRIEVAL_QUERY,
    embedder=embedder,
)
result = retriever.search(query_text="...", top_k=5)
```

Patterns worth knowing:

**`collect{}` subqueries** keep one row per seed instead of fanning out:
```cypher
RETURN node.title AS title,
       collect { MATCH (i:Ingredient)<-[:USES_INGREDIENT]-(node) RETURN i.name } AS ingredients,
       score
```

**Always return the chunk text**, not just entity names. The entity is the path; the text is what the model grounds its answer in. Returning only names produces fluent answers with nothing behind them.

**Always return the citation path.** For any corpus where "where does it say that" is a legitimate question, the answer is only usable with a source.

## Hard filters belong in Cypher

Anything that must never leak (allergens, dietary restrictions, jurisdiction, in-force dates, tenant or user visibility, security classification) goes into the `retrieval_query` as a `WHERE` clause on graph properties.

```cypher
MATCH (node)
WHERE node.visibility = 'public' OR node.owner_id = $user_id
  AND (node.in_force_to IS NULL OR node.in_force_to > date())
```

Never enforce these in the system prompt. Prompt-level constraints are advisory and can be talked around; a `WHERE` clause cannot. For safety-relevant filters, write an automated leak test suite and treat it as a release blocker rather than a nice-to-have.

Structured filter extraction ("vegan", "under 30 minutes", "in force in 2022") should run as a separate small LLM call that emits parameters, which then bind into the Cypher. Extract to parameters, filter in Cypher.

## Hop depth

- k=1: the seed and its immediate neighbours. Fast, often enough for lookup.
- k=2: the practical default. Catches "which obligations attach to this actor via this clause".
- k=3: legal corpora with dense cross-references, and substitution chains in recipes.
- k>3: almost never useful. The subgraph gets large, latency climbs, and precision collapses because everything connects to everything within four hops.

Cap the assembled subgraph by node count as well as hop count, since one high-degree node blows the budget on its own.

## Indexes

Both indexes must exist before retrieval returns anything, and a missing index fails quietly rather than loudly.

```cypher
CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS
FOR (c:Chunk) ON (c.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 1536,
  `vector.similarity_function`: 'cosine'
}};

CREATE FULLTEXT INDEX chunk_fulltext IF NOT EXISTS
FOR (c:Chunk) ON EACH [c.text];

CREATE CONSTRAINT chunk_id IF NOT EXISTS
FOR (c:Chunk) REQUIRE c.id IS UNIQUE;
```

Dimensions must match the embedder exactly: 1536 for `text-embedding-3-small` and `ada-002`, 3072 for `text-embedding-3-large`. Changing embedder means dropping and rebuilding the index.

Check with `SHOW INDEXES` when a retriever returns an empty list.

## Global questions and community summaries

Local traversal answers "what does the graph say about X". It does not answer "what are the overall themes" or "summarise every obligation this act imposes", because the answer spans the whole graph rather than a neighbourhood.

For those, run community detection (Leiden, via Graph Data Science) over the resolved graph and summarise each community with an LLM, then retrieve over the summaries. This is the local-search vs global-search split from Microsoft's GraphRAG.

Be honest with users about the cost. Community summarisation is a multi-pass LLM job over the entire corpus, the index grows super-linearly with corpus size, incremental updates are awkward, and query latency goes up meaningfully versus local traversal. Only build it when global questions are a real requirement rather than a hypothetical one. Most production systems need only local search.

## Evaluating retrieval

Build a small labelled set early, 30 to 50 questions is enough to steer decisions. Cover the query shapes deliberately:

- **lookup** ("what does article 12 say") — plain vector RAG should match or beat the graph here. If the graph is worse, the retrieval query is over-expanding.
- **multi-hop** ("what penalty applies if an employer fails the obligation in article 12") — this is where the graph should clearly win.
- **aggregation** ("how many clauses reference article 5") — Text2Cypher territory.
- **negative and filter** ("nut-free desserts", "rules not in force after 2020") — tests the hard filters. Any failure here is a bug, not a tuning issue.

Measure retrieval separately from generation. Recall@k on the gold chunk tells you whether the problem is retrieval or the answer model, and conflating them wastes days.

Baseline against plain vector RAG on the same corpus, always. GraphRAG costs meaningfully more to build and run. If it does not beat the baseline on the query shapes that matter, the schema or the chunking is wrong and more graph will not fix it.
