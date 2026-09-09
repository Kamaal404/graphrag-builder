"""Shared helpers: profile loading, Neo4j driver, schema translation."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

PROFILE_DIRS = [
    Path(__file__).resolve().parent.parent / "profiles",
    Path.cwd() / "profiles",
]


def load_profile(name_or_path: str) -> dict[str, Any]:
    """Accept a profile name ('legal') or an explicit path."""
    p = Path(name_or_path)
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    for d in PROFILE_DIRS:
        for cand in (d / f"{name_or_path}.yaml", d / f"{name_or_path}.yml"):
            if cand.exists():
                return yaml.safe_load(cand.read_text(encoding="utf-8"))
    sys.exit(
        f"profile '{name_or_path}' not found. Looked in: "
        + ", ".join(str(d) for d in PROFILE_DIRS)
    )


def graph_schema(profile: dict) -> dict:
    """Profile schema block -> the dict shape SimpleKGPipeline expects.

    Patterns are lists in YAML but the library wants tuples.
    """
    s = dict(profile["schema"])
    s["patterns"] = [tuple(p) for p in s.get("patterns", [])]
    return s


def extraction_hint(profile: dict) -> str:
    """Tell the extractor which relations the parser already handled.

    Asking the LLM for relations you derive deterministically wastes tokens and
    produces lower-precision duplicates of edges you already trust.
    """
    det = profile.get("deterministic_relations") or []
    if not det:
        return ""
    return (
        "\nThe following relationship types are extracted by a separate "
        "deterministic parser. Do NOT extract them: " + ", ".join(det) + ".\n"
    )


def get_driver():
    import neo4j

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD")
    if not pwd:
        sys.exit("NEO4J_PASSWORD is not set. Copy assets/.env.example to .env and fill it in.")
    driver = neo4j.GraphDatabase.driver(uri, auth=(user, pwd))
    driver.verify_connectivity()
    return driver


def database(profile: dict) -> str:
    return os.environ.get("NEO4J_DATABASE", "neo4j")


def build_llm():
    """Extraction LLM. Temperature 0: extraction is not a creative task."""
    model = os.environ.get("GRAPHRAG_LLM", "gpt-4.1-mini")
    if model.startswith("claude"):
        from neo4j_graphrag.llm import AnthropicLLM

        return AnthropicLLM(model_name=model, model_params={"temperature": 0, "max_tokens": 8000})
    from neo4j_graphrag.llm import OpenAILLM

    return OpenAILLM(model_name=model, model_params={"temperature": 0})


def build_embedder():
    from neo4j_graphrag.embeddings import OpenAIEmbeddings

    return OpenAIEmbeddings(model=os.environ.get("GRAPHRAG_EMBEDDER", "text-embedding-3-small"))
