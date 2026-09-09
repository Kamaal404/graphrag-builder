#!/usr/bin/env python3
"""Stage 5b: retrieval smoke test.

    python scripts/query.py --profile legal --q "obligations of the employer"
    python scripts/query.py --profile recipes --q "quick weeknight pasta" \
        --param exclude_allergens='["peanut","milk"]'
    python scripts/query.py --profile legal --q "..." --answer   # full RAG

Hard filters (allergens, diet, jurisdiction, visibility) are enforced by the
profile's expansion_query in Cypher, never by prompting. Pass their values with
--param so they bind as query parameters.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import build_embedder, build_llm, database, get_driver, load_profile  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--q", required=True)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--answer", action="store_true", help="run generation, not just retrieval")
    ap.add_argument(
        "--param",
        action="append",
        default=[],
        help="key=json_value, bound into the expansion query (repeatable)",
    )
    ap.add_argument("--no-hybrid", action="store_true", help="vector only, skip fulltext")
    args = ap.parse_args()

    profile = load_profile(args.profile)
    idx = profile["indexes"]
    ret = profile.get("retrieval", {})
    expansion = ret.get("expansion_query")
    if not expansion:
        sys.exit("profile has no retrieval.expansion_query")

    params = {}
    for kv in args.param:
        k, _, v = kv.partition("=")
        try:
            params[k] = json.loads(v)
        except json.JSONDecodeError:
            params[k] = v

    driver = get_driver()
    embedder = build_embedder()
    db = database(profile)

    from neo4j_graphrag.retrievers import HybridCypherRetriever, VectorCypherRetriever

    if args.no_hybrid or "fulltext" not in idx:
        retriever = VectorCypherRetriever(
            driver=driver,
            index_name=idx["vector"]["name"],
            retrieval_query=expansion,
            embedder=embedder,
            neo4j_database=db,
        )
    else:
        # Hybrid is the default: fulltext matters more than people expect on
        # structured corpora, where users type "article 42" and short rare terms.
        retriever = HybridCypherRetriever(
            driver=driver,
            vector_index_name=idx["vector"]["name"],
            fulltext_index_name=idx["fulltext"]["name"],
            retrieval_query=expansion,
            embedder=embedder,
            neo4j_database=db,
        )

    if args.answer:
        from neo4j_graphrag.generation import GraphRAG

        rag = GraphRAG(retriever=retriever, llm=build_llm())
        res = rag.search(
            query_text=args.q,
            retriever_config={"top_k": args.top_k, "query_params": params} if params
            else {"top_k": args.top_k},
            return_context=True,
        )
        print(res.answer)
        print("\n--- context ---")
        for item in res.retriever_result.items[: args.top_k]:
            print(item.content[:400])
            print()
        return

    kwargs = {"query_text": args.q, "top_k": args.top_k}
    if params:
        kwargs["query_params"] = params
    result = retriever.search(**kwargs)

    if not result.items:
        print("no results.")
        print("Check: does the vector index exist (scripts/init_db.py --show)?")
        print("Do index dimensions match the embedder? Did anything get written?")
        return

    for i, item in enumerate(result.items, 1):
        print(f"--- {i} ---")
        print(item.content[:800])
        print()


if __name__ == "__main__":
    main()
