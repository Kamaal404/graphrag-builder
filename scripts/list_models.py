#!/usr/bin/env python3
"""Pick an extraction model by price, and estimate what a run will cost.

The catalog is live and changes, so this queries it rather than hardcoding a
recommendation that goes stale.

    python scripts/list_models.py --cheapest
    python scripts/list_models.py --cheapest --min-context 32000
    python scripts/list_models.py --estimate --profile firecraft --chunks chunks.jsonl
    python scripts/list_models.py --estimate --profile firecraft --chunks chunks.jsonl \
        --model qwen3.8-27b

Extraction is instruction-following, not reasoning, so the cheapest model that
holds the schema usually wins. Decide with the dry run's provenance coverage,
not with the price list alone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import graph_schema, load_profile  # noqa: E402

DEFAULT_API = "https://api.experientiallabs.ai"


def api_get(path: str, params: dict | None = None) -> dict:
    base = os.environ.get("GRAPHRAG_CATALOG_URL", DEFAULT_API).rstrip("/")
    key = os.environ.get("GRAPHRAG_API_KEY") or os.environ.get("EXPLABS_API_KEY")
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"catalog request failed ({e.code}): {e.read()[:300].decode(errors='replace')}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"catalog request failed: {e}")


def micro_to_usd_per_m(v) -> float | None:
    """Catalog prices are micro-USD per million tokens."""
    try:
        return float(v) / 1_000_000
    except (TypeError, ValueError):
        return None


def cmd_cheapest(args) -> None:
    data = api_get(
        "/api/models",
        {
            "sort": "price",
            "limit": args.limit,
            "min_context": args.min_context or None,
            "modality": "text",
        },
    )
    rows = data.get("data") or data.get("models") or (data if isinstance(data, list) else [])
    if not rows:
        print("No models returned. Check GRAPHRAG_API_KEY and the catalog URL.")
        print(json.dumps(data)[:500])
        return

    print(f"{'slug':<34} {'in $/M':>9} {'out $/M':>9} {'context':>9}  structured")
    print("-" * 78)
    for m in rows[: args.limit]:
        slug = m.get("slug") or m.get("id") or "?"
        pin = micro_to_usd_per_m(m.get("input_micro_usd_per_million"))
        pout = micro_to_usd_per_m(m.get("output_micro_usd_per_million"))
        ctx = m.get("context_length") or m.get("max_context") or ""
        supports = m.get("supports") or []
        if isinstance(supports, dict):
            supports = [k for k, v in supports.items() if v]
        struct = "yes" if any(
            s in str(supports).lower() for s in ("structured", "json_schema", "response_format")
        ) else "?"
        print(
            f"{slug:<34} {('%.3f' % pin) if pin is not None else '?':>9} "
            f"{('%.3f' % pout) if pout is not None else '?':>9} {str(ctx):>9}  {struct}"
        )

    print(
        "\nStructured output matters more than price here. Without it the extractor"
        "\nparses JSON out of prose and silently drops malformed chunks."
    )


def cmd_estimate(args) -> None:
    """Token cost is dominated by the schema, which is re-sent on every chunk."""
    profile = load_profile(args.profile)
    schema = graph_schema(profile)
    schema_chars = len(json.dumps(schema, default=str))

    chunks = [json.loads(l) for l in open(args.chunks, encoding="utf-8") if l.strip()]
    if not chunks:
        sys.exit("no chunks")

    body_chars = sum(len(c["text"]) for c in chunks)
    prompt_overhead_chars = 700  # the instruction block around schema and text

    # ~4 chars per token is close enough for a planning estimate.
    per_chunk_fixed = (schema_chars + prompt_overhead_chars) / 4
    input_tokens = per_chunk_fixed * len(chunks) + body_chars / 4
    # Extracted graphs run roughly a third of the input for dense instructional prose.
    output_tokens = body_chars / 4 * 0.35

    print(f"chunks              : {len(chunks)}")
    print(f"corpus text         : {body_chars:,} chars")
    print(f"schema per call     : {schema_chars:,} chars (~{schema_chars//4:,} tokens)")
    print(f"  resent {len(chunks)}x   : ~{int(per_chunk_fixed * len(chunks)):,} input tokens")
    print(f"estimated input     : ~{int(input_tokens):,} tokens")
    print(f"estimated output    : ~{int(output_tokens):,} tokens")

    fixed_share = per_chunk_fixed * len(chunks) / max(input_tokens, 1)
    print(f"\nschema overhead is {fixed_share:.0%} of input tokens.")
    if fixed_share > 0.4:
        print(
            "That is high. Trimming node/relationship descriptions once the profile\n"
            "is proven, or raising segmentation.max_chunk_chars to make fewer and\n"
            "larger calls, cuts more than switching to a cheaper model does."
        )

    if args.model:
        data = api_get(f"/api/models/{args.model}")
        m = data.get("data") or data
        pin = micro_to_usd_per_m(m.get("input_micro_usd_per_million"))
        pout = micro_to_usd_per_m(m.get("output_micro_usd_per_million"))
        if pin is not None and pout is not None:
            cost = input_tokens / 1e6 * pin + output_tokens / 1e6 * pout
            print(f"\n{args.model}: ~${cost:.2f} for a full pass")
            print(f"  dry run on --limit 10: ~${cost * 10 / len(chunks):.3f}")
        else:
            print(f"\n{args.model}: catalog returned no usable price")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cheapest", action="store_true")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--profile")
    ap.add_argument("--chunks")
    ap.add_argument("--model")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-context", type=int, default=0)
    args = ap.parse_args()

    if args.cheapest:
        import urllib.parse  # noqa: F401

        cmd_cheapest(args)
    elif args.estimate:
        if not (args.profile and args.chunks):
            sys.exit("--estimate needs --profile and --chunks")
        cmd_estimate(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    import urllib.parse  # noqa: F401

    main()
