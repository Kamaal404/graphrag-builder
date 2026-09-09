#!/usr/bin/env python3
"""Stages 3 and 4: schema-grounded extraction, then load.

    # dry run first, always
    python scripts/build_graph.py --profile legal --chunks chunks.jsonl \
        --dry-run --out graph.jsonl --limit 10

    # inspect graph.jsonl, fix the profile, repeat, then:
    python scripts/init_db.py --profile legal
    python scripts/build_graph.py --profile legal --chunks chunks.jsonl --write

The dry run exists because extraction is where quality is won or lost, and a
JSONL file is far cheaper to iterate on than a database you keep wiping.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    build_embedder,
    build_llm,
    database,
    extraction_hint,
    get_driver,
    graph_schema,
    load_profile,
)


class JsonlWriter:
    """KGWriter that appends each chunk's extracted graph to a JSONL file.

    Subclassed lazily inside build() so importing this module does not require
    neo4j-graphrag to be installed.
    """


def make_jsonl_writer(path: str):
    from pydantic import validate_call
    from neo4j_graphrag.components.kg_writer import KGWriter
    from neo4j_graphrag.components.types import KGWriterModel, Neo4jGraph

    class _Writer(KGWriter):
        def __init__(self, out: str):
            self.out = Path(out)
            self.out.write_text("", encoding="utf-8")

        # validate_call is required whenever an argument is a Pydantic model.
        @validate_call
        async def run(self, graph: Neo4jGraph, **kwargs) -> KGWriterModel:
            with self.out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(graph.model_dump(), ensure_ascii=False, default=str) + "\n")
            return KGWriterModel(status="SUCCESS")

    return _Writer(path)


def load_chunks(path: str, limit: int = 0) -> list[dict]:
    chunks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks[:limit] if limit else chunks


def build_prompt_template(profile: dict) -> str:
    """Extend the default extraction prompt with provenance and skip instructions.

    {text}, {schema} and {examples} are the variables the library substitutes.
    """
    return (
        "You extract a knowledge graph from a document chunk.\n"
        "Use ONLY the node and relationship types in this schema:\n{schema}\n"
        + extraction_hint(profile)
        + "For every entity and relationship, include a 'quote' property holding the\n"
        "verbatim span of the input that supports it. If a fact is not literally\n"
        "supported by the text, do not extract it. Inferring plausible-sounding\n"
        "relationships is the main failure mode here.\n"
        "{examples}\n"
        "Chunk:\n{text}\n"
    )


async def build(args) -> None:
    from neo4j_graphrag.experimental.pipeline.kg_builder import SimpleKGPipeline

    profile = load_profile(args.profile)
    chunks = load_chunks(args.chunks, args.limit)
    if not chunks:
        sys.exit("no chunks loaded")

    schema = graph_schema(profile)
    llm = build_llm()
    embedder = build_embedder()

    driver = None
    kwargs = {}
    if args.dry_run:
        # Resolution runs against the database; there is nothing there in a dry run.
        kwargs["kg_writer"] = make_jsonl_writer(args.out)
        kwargs["perform_entity_resolution"] = False
        driver = get_driver()  # still required by the constructor
    else:
        driver = get_driver()
        kwargs["perform_entity_resolution"] = False  # run explicitly at the end, scoped

    pipeline = SimpleKGPipeline(
        llm=llm,
        driver=driver,
        embedder=embedder,
        schema=schema,
        from_file=False,
        prompt_template=build_prompt_template(profile),
        on_error="RAISE" if args.strict else "IGNORE",
        neo4j_database=database(profile),
        **kwargs,
    )

    ok, failed = 0, 0
    for i, c in enumerate(chunks, 1):
        try:
            await pipeline.run_async(
                text=c["text"],
                document_metadata={
                    "chunk_id": c["id"],
                    "doc_id": c["doc_id"],
                    "path": c["path"],
                    "char_start": c["char_start"],
                    "char_end": c["char_end"],
                },
            )
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  chunk {c['id']} failed: {type(e).__name__}: {e}", file=sys.stderr)
            if args.strict:
                raise
        if i % 10 == 0 or i == len(chunks):
            print(f"  {i}/{len(chunks)} chunks  ({ok} ok, {failed} failed)")

    if args.dry_run:
        summarize_dry_run(args.out)
        print("\nInspect the output above. When the node and edge mix looks right,")
        print("run scripts/init_db.py then re-run with --write.")
        return

    write_deterministic_edges(driver, chunks, profile, database(profile))
    await resolve_entities(driver, profile)
    print("\nLoaded. Next: scripts/validate.py --profile", args.profile)


def summarize_dry_run(path: str) -> None:
    from collections import Counter

    nodes, rels, no_quote = Counter(), Counter(), 0
    total_edges = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            g = json.loads(line)
            for n in g.get("nodes", []):
                nodes[n.get("label", "?")] += 1
            for r in g.get("relationships", []):
                rels[r.get("type", "?")] += 1
                total_edges += 1
                if not (r.get("properties") or {}).get("quote"):
                    no_quote += 1

    print(f"\nnodes by label ({sum(nodes.values())} total):")
    for k, v in nodes.most_common():
        print(f"  {v:6d}  {k}")
    print(f"\nedges by type ({total_edges} total):")
    for k, v in rels.most_common():
        print(f"  {v:6d}  {k}")
    if total_edges:
        cov = 1 - no_quote / total_edges
        print(f"\nprovenance coverage: {cov:.1%} of edges carry a supporting quote")
        if cov < 0.8:
            print("  low. The extraction prompt is being ignored, or the model is inferring.")


def write_deterministic_edges(driver, chunks, profile, db) -> None:
    """Cross-references from the parser. These are high-precision and free."""
    rows = [
        {"src": c["id"], "dst": x["target_chunk_id"], "rel": x["relation"], "raw": x["raw"]}
        for c in chunks
        for x in c.get("xrefs", [])
        if x.get("target_chunk_id")
    ]
    if not rows:
        return
    by_rel: dict[str, list] = {}
    for r in rows:
        by_rel.setdefault(r["rel"], []).append(r)

    with driver.session(database=db) as s:
        for rel, batch in by_rel.items():
            # Relationship type cannot be parameterised, so it is interpolated.
            # It comes from the profile, not user input.
            if not rel.replace("_", "").isalnum():
                continue
            s.run(
                f"""
                UNWIND $rows AS row
                MATCH (a:Chunk {{id: row.src}}), (b:Chunk {{id: row.dst}})
                MERGE (a)-[r:{rel}]->(b)
                SET r.source = 'parser', r.raw = row.raw
                """,
                rows=batch,
            )
            print(f"  wrote {len(batch)} deterministic {rel} edges")


async def resolve_entities(driver, profile) -> None:
    """Scoped resolution. Unscoped runs re-resolve the whole database each time,
    which is slow and can re-merge things someone deliberately separated."""
    rules = profile.get("resolution") or []
    if not rules:
        return
    from neo4j_graphrag.components.resolver import (
        FuzzyMatchResolver,
        SinglePropertyExactMatchResolver,
    )

    for rule in rules:
        labels = rule["labels"]
        strategy = rule.get("strategy", "exact")
        if strategy == "vocabulary":
            print(f"  {labels}: vocabulary resolution, handle upstream in normalization")
            continue
        label_filter = " OR ".join(f"entity:{l}" for l in labels)
        fq = f"WHERE ({label_filter}) AND NOT entity:Resolved"
        if strategy == "fuzzy":
            resolver = FuzzyMatchResolver(driver, filter_query=fq)
        elif strategy == "semantic":
            from neo4j_graphrag.components.resolver import SpaCySemanticMatchResolver

            resolver = SpaCySemanticMatchResolver(driver, filter_query=fq)
        else:
            resolver = SinglePropertyExactMatchResolver(driver, filter_query=fq)
        res = await resolver.run()
        print(f"  resolved {labels} ({strategy}): {res}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", default="graph.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--strict", action="store_true", help="fail on first bad chunk")
    args = ap.parse_args()

    if args.dry_run == args.write:
        sys.exit("pick exactly one of --dry-run or --write")

    asyncio.run(build(args))


if __name__ == "__main__":
    main()
