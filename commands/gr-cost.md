---
description: Pick an extraction model and estimate what a run will cost
argument-hint: [profile] [chunks.jsonl]
allowed-tools: Read, Bash
---

Help choose a model and estimate cost for: **$ARGUMENTS**

1. `scripts/list_models.py --cheapest --min-context 32000`

2. Weigh structured-output support above price. Without it the extractor parses
   JSON out of prose and silently drops malformed chunks, so a cheaper model
   without it costs more per usable edge.

3. Avoid reasoning models. Extraction is instruction-following, not reasoning;
   thinking tokens are pure waste here.

4. `scripts/list_models.py --estimate --profile <p> --chunks chunks.jsonl --model <slug>`

5. Lead with the schema overhead figure. The schema is re-sent on every chunk, so
   when overhead is a large share of input tokens, trimming type descriptions or
   raising `max_chunk_chars` saves more than switching models. Say that before
   recommending a cheaper model.

Final advice is always to decide on the dry run's provenance coverage, not the
price list. A cheap model that hallucinates edges costs more to clean up than it
saved.
