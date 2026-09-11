---
description: Query the knowledge graph and explain what the traversal returned
argument-hint: <profile> <question>
allowed-tools: Read, Bash, Grep
---

Query the graph: **$ARGUMENTS**

Use `scripts/query.py --profile <p> --q "<question>"`. Read
`references/retrieval.md` before changing any retrieval behaviour.

Pass hard filters as `--param key='<json>'`, never as prompt text. Allergens,
dietary flags, jurisdiction, in-force dates and visibility are enforced in Cypher
by the profile's `expansion_query`. A prompt-level constraint can be talked
around; a WHERE clause cannot.

If nothing comes back, check in this order: does the vector index exist
(`init_db.py --show`), do its dimensions match the embedder, and did anything
actually get written.

Answer from the returned chunk text and always give the citation path. If the
retrieved context does not support an answer, say so rather than filling the gap
from general knowledge. For a corpus the user chose to build a graph from, an
uncited answer defeats the point of building it.
