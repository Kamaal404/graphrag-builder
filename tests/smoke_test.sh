#!/usr/bin/env bash
# Offline smoke test.
#
# Covers every stage that needs neither an LLM key nor a running database:
# PDF diagnosis, PDF extraction, page mapping, segmentation, cross-reference
# resolution, and profile validity. If this passes, the deterministic half of
# the pipeline is healthy and any later failure is an LLM, schema, or Neo4j
# problem rather than a parsing one.
#
#   ./tests/smoke_test.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PY=.venv/bin/python

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  \033[32mPASS\033[0m %s\n' "$name"; pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m %s\n' "$name"; fail=$((fail+1))
  fi
}

expect() {   # expect <name> <expected> <actual>
  if [ "$2" = "$3" ]; then
    printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m %s (expected %s, got %s)\n' "$1" "$2" "$3"; fail=$((fail+1))
  fi
}

echo "profiles parse and declare a closed schema"
for p in profiles/*.yaml; do
  check "$(basename "$p")" "$PY" - "$p" <<'EOF'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
s = d["schema"]
assert d["segmentation"]["chunk_at"], "chunk_at missing"
assert s["node_types"] and s["relationship_types"] and s["patterns"]
# An open schema is the single most common cause of a hairball graph.
for k in ("additional_node_types", "additional_relationship_types", "additional_patterns"):
    assert s.get(k) is False, f"{k} must be false"
labels = {n if isinstance(n, str) else n["label"] for n in s["node_types"]}
rels = {r if isinstance(r, str) else r["label"] for r in s["relationship_types"]}
for src, rel, dst in s["patterns"]:
    assert src in labels and dst in labels, f"pattern endpoint not declared: {src}/{dst}"
    assert rel in rels, f"pattern relation not declared: {rel}"
assert d["retrieval"]["expansion_query"].strip()
EOF
done

echo
echo "PDF pipeline"
check "diagnose detects a text layer" \
  "$PY" scripts/extract_pdf.py --input samples/legal/code_travail_sample.pdf --out "$WORK/d" --diagnose-only

"$PY" scripts/extract_pdf.py --input samples/legal/code_travail_sample.pdf --out "$WORK/x" >/dev/null 2>&1
check "extraction writes text" test -s "$WORK/x/code_travail_sample.txt"
check "extraction writes a page map" test -s "$WORK/x/code_travail_sample.pages.json"

check "running header removed" bash -c \
  "! grep -q 'BULLETIN OFFICIEL' '$WORK/x/code_travail_sample.txt'"
check "running footer removed" bash -c \
  "! grep -q 'Page 1 sur 3' '$WORK/x/code_travail_sample.txt'"
check "hyphenation rejoined" grep -q 'relations de travail' "$WORK/x/code_travail_sample.txt"
check "soft wrap rejoined" grep -q "prevue a l'article 12 est puni" "$WORK/x/code_travail_sample.txt"
check "headings survived" grep -q '^Article 12' "$WORK/x/code_travail_sample.txt"

echo
echo "segmentation, all three PDF engines agree"
for engine in poppler pdfplumber pypdf; do
  rm -rf "$WORK/cache"
  if ! "$PY" scripts/segment.py --profile legal \
        --input samples/legal/code_travail_sample.pdf \
        --out "$WORK/c-$engine.jsonl" --pdf-cache "$WORK/cache" \
        --pdf-engine "$engine" >/dev/null 2>&1; then
    printf '  \033[31mFAIL\033[0m %s segmentation errored\n' "$engine"; fail=$((fail+1)); continue
  fi
  sig=$("$PY" - "$WORK/c-$engine.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
print(" ".join(f"a{r['number']}p{r.get('page')}" for r in rows),
      sum(len(r["xrefs"]) for r in rows))
EOF
)
  expect "$engine" "a1p1 a2p1 a12p2 a13p2 a14p3 3" "$sig"
done

echo
echo "cross-reference typing"
sig=$("$PY" - "$WORK/c-poppler.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
print(",".join(f"{r['number']}:{x['relation']}:{x['raw']}" for r in rows for x in r["xrefs"]))
EOF
)
# The specific relation must win over the generic one on the same target, and a
# chunk must never reference itself.
expect "typed and deduped" "12:EXCEPTION_TO:2,13:REFERS_TO:12,14:AMENDS:12" "$sig"

echo
echo "plain text and markdown still work"
"$PY" scripts/segment.py --profile legal --input samples/legal/code_travail_sample.txt \
  --out "$WORK/t.jsonl" --pdf-cache "$WORK/cache2" >/dev/null 2>&1
expect "text corpus chunks" "5" "$(wc -l < "$WORK/t.jsonl" | tr -d ' ')"

"$PY" scripts/segment.py --profile recipes --input samples/recipes \
  --out "$WORK/r.jsonl" --pdf-cache "$WORK/cache3" >/dev/null 2>&1
expect "recipe corpus chunks" "2" "$(wc -l < "$WORK/r.jsonl" | tr -d ' ')"
check "recipe title captured" grep -q '"title": "Lemon Garlic Pasta"' "$WORK/r.jsonl"
check "recipe cross-ref resolved" grep -q 'PAIRS_WITH' "$WORK/r.jsonl"

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
