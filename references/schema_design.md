# Schema design

Read this before writing a new corpus profile.

## Contents
- [What becomes a node](#what-becomes-a-node)
- [Domain and range discipline](#domain-and-range-discipline)
- [Provenance](#provenance)
- [Temporal validity](#temporal-validity)
- [Worked ontology: legal corpus](#worked-ontology-legal-corpus)
- [Worked ontology: recipe corpus](#worked-ontology-recipe-corpus)
- [Canonical vocabularies](#canonical-vocabularies)
- [Profile file format](#profile-file-format)

## What becomes a node

A thing earns a node if both are true:
1. It could be the answer unit to a real user query.
2. It recurs across documents.

Everything else is a property on a node, or an attribute on an edge. This single rule prevents most schema bloat. Quantity does not become a `Quantity` node, it becomes `amount` and `unit` on the `USES_INGREDIENT` edge. A date does not become a `Date` node unless users ask questions like "what changed in 2019".

Aim for 6 to 12 node types. Beyond about 15 the extraction quality drops noticeably, because the LLM is choosing among too many near-synonymous labels and the choice becomes arbitrary. If a domain seems to need 25 types, most of them are properties.

Same for edges: 8 to 15 relationship types. Prefer a smaller set with typed properties over a large set of near-duplicates. One `APPLIES_TO` with a `scope` property beats `APPLIES_TO_PERSON`, `APPLIES_TO_ORG`, `APPLIES_TO_SECTOR`.

## Domain and range discipline

Every relationship type declares which source and target node types are legal. In `neo4j-graphrag` this is the `patterns` list of `(source_label, REL_TYPE, target_label)` triples.

This is the highest-leverage validation available. Any extracted edge that violates its type signature can be dropped without a human reading it, and that mechanically removes a large share of LLM extraction noise at zero cost. Set `additional_patterns: false` alongside `additional_relationship_types: false` to make the library enforce it during pruning.

Write the patterns list exhaustively. A relationship type with no declared pattern is effectively unconstrained.

## Provenance

Every extracted edge and every entity carries:
- `chunk_id`: which chunk it came from
- `char_start`, `char_end`: the span within that chunk
- `quote`: the verbatim supporting text, truncated to a reasonable length

For legal, regulatory, medical, and financial corpora this is the difference between a system someone can rely on and a system nobody is allowed to deploy. A user asking "where does it say that" must get a citation, not a paraphrase.

The lexical graph (`Chunk` nodes, `PART_OF_CHUNK` edges) gives you node-level provenance for free. Edge-level provenance needs the extraction prompt to ask for it explicitly.

## Temporal validity

Any corpus where content is amended over time (law, policy, standards, terms of service) needs validity intervals on nodes and edges:
- `in_force_from`, `in_force_to` (null means currently in force)
- `superseded_by` pointing at the replacing node

Without this, repealed rules answer queries and nobody notices until it matters. Retrieval must then filter on the as-of date by default, and only include historical versions when the user explicitly asks what the rule used to be.

Model amendments as edges (`AMENDS`, `REPEALS`) between clause nodes rather than by mutating the amended node. The history is the valuable part.

## Worked ontology: legal corpus

The atomic node is the **clause or alinéa**, not the document and not the article. Articles are frequently multi-part with independent obligations, and retrieving a whole article to answer a question about one of its paragraphs dilutes the context.

Node types:
`Clause`, `Definition`, `Actor`, `Obligation`, `Condition`, `Sanction`, `Authority`, `Act`, `Jurisdiction`

Relationship types with patterns:
```
(Act,        CONTAINS,        Clause)
(Clause,     PARENT_OF,       Clause)
(Clause,     DEFINES,         Definition)
(Clause,     REFERS_TO,       Clause)
(Clause,     AMENDS,          Clause)
(Clause,     REPEALS,         Clause)
(Clause,     EXCEPTION_TO,    Clause)
(Clause,     DEROGATES_FROM,  Clause)
(Clause,     IMPOSES,         Obligation)
(Obligation, APPLIES_TO,      Actor)
(Obligation, CONDITIONED_ON,  Condition)
(Obligation, SANCTIONED_BY,   Sanction)
(Authority,  ENFORCES,        Obligation)
(Act,        IN_FORCE_IN,     Jurisdiction)
```

`REFERS_TO`, `AMENDS`, `REPEALS`, `PARENT_OF` and `CONTAINS` all come from the deterministic layer. Only the obligation and condition structure needs the LLM.

Typical queries this shape serves: "what must an employer do when X", "which clauses were changed by the 2023 amendment", "what are the exceptions to article 12", "what penalty applies if Y".

## Worked ontology: recipe corpus

Node types:
`Recipe`, `Ingredient`, `Technique`, `Equipment`, `Cuisine`, `DietTag`, `Allergen`, `Course`

Relationship types with patterns:
```
(Recipe,     USES_INGREDIENT,    Ingredient)   # amount, unit, prep, optional
(Recipe,     REQUIRES_TECHNIQUE, Technique)
(Recipe,     REQUIRES_EQUIPMENT, Equipment)
(Recipe,     BELONGS_TO,         Cuisine)
(Recipe,     IS_COURSE,          Course)
(Recipe,     VARIANT_OF,         Recipe)
(Recipe,     PAIRS_WITH,         Recipe)
(Ingredient, SUBSTITUTES_FOR,    Ingredient)   # ratio, context, quality_loss
(Ingredient, CONTAINS_ALLERGEN,  Allergen)
(Ingredient, EXCLUDED_BY,        DietTag)
(DietTag,    EXCLUDES,           Allergen)
```

Quantity, unit and preparation are edge properties on `USES_INGREDIENT`, never nodes.

Allergen and diet exclusions must be reachable in one hop from `Ingredient` so a hard Cypher filter is cheap. Something like `WHERE NOT EXISTS { (r)-[:USES_INGREDIENT]->(:Ingredient)-[:CONTAINS_ALLERGEN]->(:Allergen {name: $a}) }` has to be a fast, obviously correct query, and that constrains the schema.

## Canonical vocabularies

For any domain with a recurring entity type that appears in thousands of surface forms, build the canonical vocabulary **before** ingesting anything. Ingredients are the clearest case, but the same applies to legal actor types, drug names, part numbers, and job titles.

Four-tier normalization, in order, first hit wins:
1. exact match against the canonical list
2. fuzzy match above a threshold (RapidFuzz)
3. embedding nearest neighbour above a threshold
4. LLM fallback constrained to choose from a shortlist, never free text

Tier 4 must be closed-list. An open-ended "what is this ingredient" call reintroduces exactly the variance the vocabulary exists to remove.

Seed the vocabulary from an authoritative external source where one exists (USDA FoodData Central for ingredients, official nomenclatures for legal and medical domains). Hand-curating from scratch is slow and produces gaps you find in production.

## Profile file format

```yaml
name: legal
description: Statutes and regulations with clause-level granularity

segmentation:
  mode: structural            # structural | fixed | hybrid
  hierarchy:                  # ordered, outermost first
    - level: title
      pattern: '^TITRE\s+([IVXLC]+)\s*[-–:]?\s*(.*)$'
    - level: article
      pattern: '^Article\s+(\d+(?:\s*bis|\s*ter)?)\s*[.:]?\s*(.*)$'
  chunk_at: article           # which level becomes a Chunk
  max_chunk_chars: 4000       # split further only above this
  xref_patterns:
    - name: article_ref
      pattern: '(?:art\.|article)\s*(\d+)'
      target_level: article

schema:
  additional_node_types: false
  additional_relationship_types: false
  additional_patterns: false
  node_types: [...]           # neo4j-graphrag NodeType dicts
  relationship_types: [...]
  patterns: [...]

resolution:
  - labels: [Actor, Authority]
    strategy: fuzzy           # exact | fuzzy | semantic
    threshold: 0.88
  - labels: [Definition]
    strategy: exact

indexes:
  vector:
    name: chunk_embedding
    label: Chunk
    property: embedding
    dimensions: 1536
    similarity: cosine
  fulltext:
    name: chunk_fulltext
    labels: [Chunk]
    properties: [text]

retrieval:
  hops: 3
  seed_labels: [Chunk]
  expansion_query: |
    MATCH (node)<-[:PART_OF_CHUNK]-(e)
    OPTIONAL MATCH (e)-[r:REFERS_TO|EXCEPTION_TO*1..2]-(rel)
    RETURN node.text AS text, node.path AS path,
           collect(DISTINCT rel.name) AS related, score
```

Keep the profile the only place domain knowledge lives. If a script needs a domain-specific branch, that is a signal the profile format needs a new field, not that the script needs an `if`.
