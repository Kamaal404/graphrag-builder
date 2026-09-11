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
    """Extraction LLM. Temperature 0: extraction is not a creative task.

    A base URL wins over slug sniffing. Gateways serve Anthropic-named slugs
    over the OpenAI wire protocol, so 'claude-*' through a gateway must still go
    to OpenAILLM; routing it to AnthropicLLM would hit api.anthropic.com with a
    gateway key and fail.
    """
    model = os.environ.get("GRAPHRAG_LLM", "claude-sonnet-5")
    base_url = os.environ.get("GRAPHRAG_LLM_BASE_URL", "").strip()

    if base_url:
        from neo4j_graphrag.llm import OpenAILLM

        key = (
            os.environ.get("GRAPHRAG_API_KEY")
            or os.environ.get("EXPLABS_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        if not key:
            sys.exit("GRAPHRAG_LLM_BASE_URL is set but no GRAPHRAG_API_KEY found.")
        return OpenAILLM(
            model_name=model,
            model_params={"temperature": 0},
            base_url=base_url,
            api_key=key,
        )

    if model.startswith("claude"):
        from neo4j_graphrag.llm import AnthropicLLM

        return AnthropicLLM(model_name=model, model_params={"temperature": 0, "max_tokens": 8000})
    if model.startswith("ollama/"):
        from neo4j_graphrag.llm import OllamaLLM

        return OllamaLLM(model_name=model.split("/", 1)[1], model_params={"temperature": 0})
    from neo4j_graphrag.llm import OpenAILLM

    return OpenAILLM(model_name=model, model_params={"temperature": 0})


# Embedding dimensions must match the vector index in the profile exactly, or
# retrieval silently returns nothing.
EMBEDDER_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "all-MiniLM-L6-v2": 384,
    "all-mpnet-base-v2": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
}


def build_embedder():
    """Embedder, chosen by GRAPHRAG_EMBEDDER_PROVIDER.

    Anthropic does not offer an embeddings API, so a Claude-only setup still
    needs embeddings from elsewhere. 'local' is the answer for that: it runs
    sentence-transformers on your machine, costs nothing, needs no second API
    key, and works offline.
    """
    provider = os.environ.get("GRAPHRAG_EMBEDDER_PROVIDER", "local").lower()
    model = os.environ.get("GRAPHRAG_EMBEDDER", "")

    if provider in ("local", "sentence-transformers", "st"):
        from neo4j_graphrag.embeddings import SentenceTransformerEmbeddings

        return SentenceTransformerEmbeddings(model=model or "all-MiniLM-L6-v2")
    if provider == "ollama":
        from neo4j_graphrag.embeddings import OllamaEmbeddings

        return OllamaEmbeddings(model=model or "nomic-embed-text")
    if provider == "openai":
        from neo4j_graphrag.embeddings import OpenAIEmbeddings

        return OpenAIEmbeddings(model=model or "text-embedding-3-small")
    if provider == "cohere":
        from neo4j_graphrag.embeddings import CohereEmbeddings

        return CohereEmbeddings(model=model or "embed-english-v3.0")
    if provider == "mistral":
        from neo4j_graphrag.embeddings import MistralAIEmbeddings

        return MistralAIEmbeddings(model=model or "mistral-embed")
    sys.exit(
        f"unknown GRAPHRAG_EMBEDDER_PROVIDER '{provider}'. "
        "Use: local, ollama, openai, cohere, mistral."
    )


def check_embedder_dims(profile: dict) -> None:
    """Warn when the profile's vector index does not match the embedder.

    A mismatch does not raise, it just makes every search return nothing, which
    is a miserable thing to debug from the query side.
    """
    model = os.environ.get("GRAPHRAG_EMBEDDER", "")
    provider = os.environ.get("GRAPHRAG_EMBEDDER_PROVIDER", "local").lower()
    if not model:
        model = {"local": "all-MiniLM-L6-v2", "ollama": "nomic-embed-text"}.get(
            provider, "text-embedding-3-small"
        )
    expected = EMBEDDER_DIMS.get(model)
    declared = (profile.get("indexes", {}).get("vector") or {}).get("dimensions")
    if expected and declared and expected != declared:
        print(
            f"WARNING: {model} produces {expected}-d vectors but the profile "
            f"declares {declared}. Fix indexes.vector.dimensions, then drop and "
            f"recreate the index, or retrieval will return nothing.",
            file=sys.stderr,
        )
