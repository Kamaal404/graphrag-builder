---
description: Run the full ingest pipeline for a corpus, stopping at the dry run
argument-hint: <profile> <path to corpus>
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Ingest **$ARGUMENTS** through the pipeline.

Read `SKILL.md` for the stage definitions and `references/pipeline.md` for the
current `neo4j-graphrag` API before writing any pipeline code.

1. **Segment.** `scripts/segment.py --profile <p> --input <path> --out chunks.jsonl`
   PDFs are handled automatically. Check the reported chunk count, median size,
   cross-references resolved, and page coverage. If it reports one chunk per
   document the hierarchy regexes did not match; fix that before going further,
   and for PDFs check the extracted `.txt` first since that is the more common
   cause than the profile.

2. **Estimate.** `scripts/list_models.py --estimate --profile <p> --chunks chunks.jsonl`
   Report the schema overhead share. If it is above 40%, say so and offer the
   cheaper fixes (trim descriptions, raise `max_chunk_chars`) before suggesting a
   cheaper model.

3. **Dry run on 10 chunks.** `--dry-run --limit 10`. Never go straight to `--write`.

4. **Read the dry run output properly.** Report node and edge counts by type, and
   provenance coverage. Below 80% coverage means the model is inferring rather
   than quoting; say that plainly rather than proceeding.

5. **Stop here.** Show the user what was extracted and ask whether to load. Loading
   is `init_db.py` then `build_graph.py --write` then `validate.py`, and the user
   decides when to spend that.
