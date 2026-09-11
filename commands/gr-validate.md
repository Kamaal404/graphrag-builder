---
description: Validate a loaded graph and diagnose quality problems
argument-hint: <profile>
allowed-tools: Read, Edit, Bash, Grep
---

Run `scripts/validate.py --profile $ARGUMENTS` and act on the result.

Diagnose failures in this order, because it is almost never the model being bad
at extraction and almost always one of these:

1. **PDF extraction mangled the structure.** For PDF corpora this is the most
   common cause by a wide margin, and the fastest to check: open the extracted
   `.txt` in `.text-cache/` and confirm headings survived.
2. **Schema too open.** Undeclared labels or pattern violations mean one of the
   `additional_*` flags is not false. `additional_patterns` defaults to true, so
   it is usually that one.
3. **Chunking cut relations in half.** Endpoints landing in different chunks
   cannot be extracted as an edge.
4. **Resolution did not run**, or ran on the wrong property. Duplicate entity
   names is the tell.
5. **Retrieval query wrong.** Only reachable after the four above are clean.

For hub nodes, check whether the degree is real domain structure or a resolution
failure before touching anything. Some hubs are legitimate and belong in the
profile's `max_degree_exempt`.

Report what failed, what you changed, and re-run validation to confirm.
