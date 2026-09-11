---
description: Design a new corpus profile (ontology) and get sign-off before extracting
argument-hint: <path to corpus dir or PDF> [domain description]
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Design a new profile in `profiles/` for this corpus: **$ARGUMENTS**

Read `references/schema_design.md` first. Do not skip it; it holds the node
granularity rules, the domain/range discipline, and the provenance requirements
that decide whether this graph is usable.

Work in this order:

1. **Look at the actual text before proposing anything.** If the input is a PDF,
   run `scripts/extract_pdf.py --diagnose-only` then extract it, and read a real
   sample. Never design an ontology from the filename or the domain alone.

2. **Find the structure.** Show the user the headings you found and the regexes
   you propose for `segmentation.hierarchy`. Confirm what becomes a chunk. Flag
   front matter, tables of contents and indexes, which match heading patterns and
   generate junk chunks that cost money to extract nothing from.

3. **Propose the ontology.** 6 to 12 node types, 8 to 15 relationship types, and
   an exhaustive `patterns` list. For each type say what user question it answers.
   Anything that is not an answer unit and does not recur across documents is a
   property or an edge attribute, not a node.

4. **Stop and get explicit sign-off on the ontology.** This is a hard gate. A
   wrong schema wastes real money and produces a graph the user has to argue with
   rather than correct.

5. Write the profile, set all three `additional_*` flags to false, then verify
   with `./tests/smoke_test.sh` which validates every profile in `profiles/`.

Do not start extraction from this command. End by telling the user the exact
`segment.py` and `build_graph.py --dry-run --limit 10` commands to run next.
