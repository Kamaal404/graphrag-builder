#!/usr/bin/env python3
"""Create constraints and indexes declared in the profile.

Run before the first --write. A missing vector index does not error, it just
returns nothing forever, which is a miserable thing to debug at query time.

    python scripts/init_db.py --profile legal
    python scripts/init_db.py --profile legal --show
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import check_embedder_dims, database, get_driver, load_profile  # noqa: E402


def ident(s: str) -> str:
    if not s.replace("_", "").isalnum():
        sys.exit(f"unsafe identifier in profile: {s!r}")
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--show", action="store_true", help="just list what exists")
    args = ap.parse_args()

    profile = load_profile(args.profile)
    check_embedder_dims(profile)
    idx = profile.get("indexes", {})
    driver = get_driver()
    db = database(profile)

    with driver.session(database=db) as s:
        if args.show:
            for rec in s.run("SHOW INDEXES"):
                print(f"{rec['name']:<28} {rec['type']:<10} {rec.get('labelsOrTypes')} {rec.get('properties')}")
            for rec in s.run("SHOW CONSTRAINTS"):
                print(f"{rec['name']:<28} CONSTRAINT {rec.get('labelsOrTypes')} {rec.get('properties')}")
            return

        for c in idx.get("constraints", []):
            label, prop = ident(c["label"]), ident(c["property"])
            name = f"{label.lower()}_{prop}_unique"
            s.run(
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )
            print(f"constraint  {name}")

        v = idx.get("vector")
        if v:
            name, label, prop = ident(v["name"]), ident(v["label"]), ident(v["property"])
            s.run(
                f"""
                CREATE VECTOR INDEX {name} IF NOT EXISTS
                FOR (n:{label}) ON (n.{prop})
                OPTIONS {{indexConfig: {{
                  `vector.dimensions`: $dims,
                  `vector.similarity_function`: $sim
                }}}}
                """,
                dims=int(v.get("dimensions", 1536)),
                sim=v.get("similarity", "cosine"),
            )
            print(f"vector      {name}  ({v.get('dimensions')}d, {v.get('similarity')})")

        ft = idx.get("fulltext")
        if ft:
            name = ident(ft["name"])
            labels = "|".join(ident(l) for l in ft["labels"])
            props = ", ".join(f"n.{ident(p)}" for p in ft["properties"])
            s.run(
                f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS "
                f"FOR (n:{labels}) ON EACH [{props}]"
            )
            print(f"fulltext    {name}")

    print("\nVector index dimensions must match your embedder exactly.")
    print("text-embedding-3-small = 1536, text-embedding-3-large = 3072.")
    print("Changing embedder means dropping and rebuilding the index.")


if __name__ == "__main__":
    main()
