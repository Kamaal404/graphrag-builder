#!/usr/bin/env python3
"""Stage 5a: structural validation.

Catches the failure modes that otherwise show up as confidently wrong answers
weeks later. Run after every load.

    python scripts/validate.py --profile legal
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import database, get_driver, load_profile  # noqa: E402

FAIL = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    mark = "PASS" if ok else "FAIL"
    if not ok:
        FAIL += 1
    print(f"[{mark}] {name}")
    if detail:
        for line in detail.rstrip().splitlines():
            print(f"       {line}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    profile = load_profile(args.profile)
    v = profile.get("validation", {})
    driver = get_driver()
    db = database(profile)

    with driver.session(database=db) as s:
        # --- inventory -------------------------------------------------------
        labels = s.run(
            "MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) AS c ORDER BY c DESC"
        ).data()
        rels = s.run(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC"
        ).data()
        print("nodes by label:")
        for row in labels:
            print(f"  {row['c']:8d}  {row['l']}")
        print("edges by type:")
        for row in rels:
            print(f"  {row['c']:8d}  {row['t']}")
        print()

        declared_nodes = {
            (n if isinstance(n, str) else n["label"])
            for n in profile["schema"]["node_types"]
        }
        declared_rels = {
            (r if isinstance(r, str) else r["label"])
            for r in profile["schema"]["relationship_types"]
        }
        declared_rels |= set(profile.get("deterministic_relations") or [])

        # --- 1. undeclared labels and types ---------------------------------
        infra = {"Chunk", "Document", "__Entity__", "__KGBuilder__", "Resolved"}
        stray_labels = [r["l"] for r in labels if r["l"] not in declared_nodes | infra]
        report(
            "no undeclared node labels",
            not stray_labels,
            f"undeclared: {stray_labels}\nSet schema.additional_node_types: false" if stray_labels else "",
        )

        infra_rels = {"PART_OF_CHUNK", "PART_OF_DOCUMENT", "NEXT_CHUNK", "FROM_DOCUMENT"}
        stray_rels = [r["t"] for r in rels if r["t"] not in declared_rels | infra_rels]
        report(
            "no undeclared relationship types",
            not stray_rels,
            f"undeclared: {stray_rels}\nSet schema.additional_relationship_types: false" if stray_rels else "",
        )

        # --- 2. pattern violations ------------------------------------------
        patterns = {(tuple(p)) for p in profile["schema"].get("patterns", [])}
        violations = []
        for (src, rel, dst) in sorted({(p[0], p[1], p[2]) for p in patterns}):
            pass
        found = s.run(
            """
            MATCH (a)-[r]->(b)
            WHERE NOT type(r) IN $infra
            WITH labels(a) AS la, type(r) AS t, labels(b) AS lb, count(*) AS c
            RETURN la, t, lb, c ORDER BY c DESC
            """,
            infra=list(infra_rels),
        ).data()
        for row in found:
            src_labels = [l for l in row["la"] if l in declared_nodes]
            dst_labels = [l for l in row["lb"] if l in declared_nodes]
            if not src_labels or not dst_labels:
                continue
            if row["t"] not in declared_rels:
                continue
            legal = any(
                (s_, row["t"], d_) in patterns for s_ in src_labels for d_ in dst_labels
            )
            if not legal:
                violations.append(f"({src_labels[0]})-[:{row['t']}]->({dst_labels[0]})  x{row['c']}")
        report(
            "all edges satisfy declared patterns",
            not violations,
            "\n".join(violations[: args.top]) + "\nSet schema.additional_patterns: false" if violations else "",
        )

        # --- 3. hub detection ------------------------------------------------
        max_degree = v.get("max_degree", 500)
        exempt = [e.lower() for e in v.get("max_degree_exempt", [])]
        hubs = s.run(
            """
            MATCH (n:__Entity__)
            WITH n, count { (n)--() } AS deg
            WHERE deg > $max
            RETURN labels(n) AS labels, n.name AS name, deg
            ORDER BY deg DESC LIMIT $top
            """,
            max=max_degree,
            top=args.top,
        ).data()
        hubs = [h for h in hubs if (h["name"] or "").lower() not in exempt]
        report(
            f"no hub nodes above degree {max_degree}",
            not hubs,
            "\n".join(f"{h['deg']:6d}  {h['labels']} {h['name']}" for h in hubs)
            + "\nA hub is usually a resolution failure or a junk entity, not real structure."
            if hubs
            else "",
        )

        # --- 4. orphans ------------------------------------------------------
        for label in v.get("forbid_orphan_labels", []):
            n = s.run(
                f"MATCH (n:`{label}`) WHERE NOT (n)-[:PART_OF_CHUNK]->() RETURN count(n) AS c"
            ).single()["c"]
            report(f"{label} nodes all have chunk provenance", n == 0, f"{n} orphans" if n else "")

        # --- 5. provenance coverage -----------------------------------------
        min_cov = v.get("min_provenance_coverage", 0.9)
        row = s.run(
            """
            MATCH ()-[r]->()
            WHERE NOT type(r) IN $infra
            RETURN count(r) AS total,
                   count(CASE WHEN r.quote IS NOT NULL OR r.source = 'parser' THEN 1 END) AS with_prov
            """,
            infra=list(infra_rels),
        ).single()
        cov = (row["with_prov"] / row["total"]) if row["total"] else 1.0
        report(
            f"provenance coverage >= {min_cov:.0%}",
            cov >= min_cov,
            f"{cov:.1%} of {row['total']} edges carry a quote or parser source",
        )

        # --- 6. duplicate candidates ----------------------------------------
        dupes = s.run(
            """
            MATCH (n:__Entity__)
            WHERE n.name IS NOT NULL
            WITH labels(n) AS l, toLower(trim(n.name)) AS norm, collect(n.name) AS names, count(*) AS c
            WHERE c > 1
            RETURN l, norm, names, c ORDER BY c DESC LIMIT $top
            """,
            top=args.top,
        ).data()
        report(
            "no exact-duplicate entity names",
            not dupes,
            "\n".join(f"{d['c']}x {d['l']} {d['names'][:4]}" for d in dupes)
            + "\nEntity resolution did not run, or ran before these were written."
            if dupes
            else "",
        )

        # --- 7. index sanity -------------------------------------------------
        idx_names = {r["name"] for r in s.run("SHOW INDEXES")}
        want = set()
        if profile.get("indexes", {}).get("vector"):
            want.add(profile["indexes"]["vector"]["name"])
        if profile.get("indexes", {}).get("fulltext"):
            want.add(profile["indexes"]["fulltext"]["name"])
        missing = want - idx_names
        report(
            "retrieval indexes exist",
            not missing,
            f"missing: {sorted(missing)}\nRun scripts/init_db.py" if missing else "",
        )

    print(f"\n{FAIL} check(s) failed.")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
