#!/usr/bin/env python3
"""Stage 1: deterministic segmentation.

Splits a corpus on structural boundaries declared in the profile, assigns stable
IDs, records the hierarchy breadcrumb, and extracts cross-references by regex.

No LLM involved. This layer runs at near-100% precision and for structured
corpora it carries most of the graph's value on its own.

    python scripts/segment.py --profile legal --input ./corpus --out chunks.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_profile  # noqa: E402

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst"}


def stable_id(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:16]


def ensure_text(input_path: Path, cache: Path, engine: str) -> Path:
    """PDFs go through extract_pdf.py first.

    Doing it here rather than making the user run two commands means the page
    map is always produced, so chunks keep their page numbers. A citation
    without a page number is not much of a citation.
    """
    pdfs = (
        [input_path]
        if input_path.is_file() and input_path.suffix.lower() == ".pdf"
        else (sorted(input_path.rglob("*.pdf")) if input_path.is_dir() else [])
    )
    if not pdfs:
        return input_path

    import argparse as _ap

    import extract_pdf

    cache.mkdir(parents=True, exist_ok=True)
    args = _ap.Namespace(
        engine=engine, diagnose_only=False, keep_headers=False, no_reflow=False
    )
    print(f"extracting {len(pdfs)} PDF(s) -> {cache}")
    for pdf in pdfs:
        out_txt = cache / f"{pdf.stem}.txt"
        if out_txt.exists() and out_txt.stat().st_mtime > pdf.stat().st_mtime:
            print(f"  {pdf.name}: cached")
            continue
        extract_pdf.process(pdf, cache, args)

    if input_path.is_dir():
        # Keep any hand-written .txt/.md sitting alongside the PDFs.
        for f in input_path.rglob("*"):
            if f.suffix.lower() in TEXT_SUFFIXES:
                target = cache / f.name
                if not target.exists():
                    target.write_text(f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    return cache


def load_page_map(doc_path: Path) -> list[dict]:
    """Page offsets written by extract_pdf.py, if this document came from a PDF."""
    pm = doc_path.with_suffix("").with_suffix(".pages.json")
    if not pm.exists():
        pm = doc_path.parent / f"{doc_path.stem}.pages.json"
    if pm.exists():
        try:
            return json.loads(pm.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def page_for_offset(page_map: list[dict], offset: int) -> int | None:
    if not page_map:
        return None
    lo, hi = 0, len(page_map) - 1
    best = page_map[0]["page"]
    while lo <= hi:
        mid = (lo + hi) // 2
        entry = page_map[mid]
        if offset < entry["char_start"]:
            hi = mid - 1
        else:
            best = entry["page"]
            lo = mid + 1
    return best


def read_documents(input_path: Path) -> list[tuple[str, str]]:
    """Return (doc_id, text) pairs. PDFs are converted upstream by ensure_text()."""
    if input_path.is_file():
        files = [input_path]
    else:
        files = sorted(p for p in input_path.rglob("*") if p.suffix.lower() in TEXT_SUFFIXES)
    if not files:
        sys.exit(
            f"no .txt/.md/.pdf files under {input_path}. "
            "If these are scanned PDFs, OCR them first (see scripts/extract_pdf.py)."
        )
    out = []
    for f in files:
        out.append((str(f), f.read_text(encoding="utf-8", errors="replace")))
    return out


def segment_document(doc_id: str, text: str, profile: dict) -> list[dict]:
    seg = profile["segmentation"]
    hierarchy = seg["hierarchy"]
    chunk_at = seg["chunk_at"]
    max_chars = seg.get("max_chunk_chars", 4000)
    subsplit = seg.get("subsplit_pattern")

    levels = [h["level"] for h in hierarchy]
    compiled = [(h["level"], re.compile(h["pattern"], re.MULTILINE)) for h in hierarchy]
    if chunk_at not in levels:
        sys.exit(f"chunk_at '{chunk_at}' is not one of the declared hierarchy levels {levels}")

    # Find every heading of every level, in document order.
    marks: list[tuple[int, str, str, str]] = []  # (pos, level, number, title)
    for level, rx in compiled:
        for m in rx.finditer(text):
            groups = m.groups()
            if len(groups) > 1:
                number = (groups[0] or "").strip()
                title = (groups[1] or "").strip()
            else:
                # Single capture group means the heading has no separate number
                # (markdown recipe titles, named sections). The text is the title.
                number = ""
                title = (groups[0] or "").strip() if groups else ""
            marks.append((m.start(), level, number, title))
    marks.sort(key=lambda x: x[0])

    if not marks:
        # No structure detected. Fall back to whole-document chunking rather than
        # silently producing nothing; the caller will notice a single fat chunk.
        return [
            {
                "id": stable_id(doc_id, "0"),
                "doc_id": doc_id,
                "path": Path(doc_id).stem,
                "level": "document",
                "number": "",
                "title": Path(doc_id).stem,
                "text": text.strip(),
                "char_start": 0,
                "char_end": len(text),
                "xrefs": [],
            }
        ]

    rank = {lvl: i for i, lvl in enumerate(levels)}
    chunk_rank = rank[chunk_at]

    chunks: list[dict] = []
    breadcrumb: dict[str, str] = {}

    for i, (pos, level, number, title) in enumerate(marks):
        label = f"{level} {number}".strip() + (f" {title}" if title else "")
        # Reset deeper levels when a shallower heading appears.
        for lvl in levels[rank[level] :]:
            breadcrumb.pop(lvl, None)
        breadcrumb[level] = label

        if rank[level] != chunk_rank:
            continue

        # Chunk body runs to the next heading at this level or shallower.
        end = len(text)
        for pos2, level2, _, _ in marks[i + 1 :]:
            if rank[level2] <= chunk_rank:
                end = pos2
                break

        raw_body = text[pos:end]
        # The hierarchy patterns start with \s*, so under re.MULTILINE the match
        # begins on the newline before the heading. Left uncorrected, every
        # chunk that opens a page lands one char short of the page boundary and
        # gets cited on the previous page.
        lead = len(raw_body) - len(raw_body.lstrip())
        body = raw_body.strip()
        path = " > ".join(breadcrumb[l] for l in levels if l in breadcrumb)

        for part_text, off in split_oversized(body, max_chars, subsplit):
            start = pos + lead + off
            chunks.append(
                {
                    "id": stable_id(doc_id, path, str(start)),
                    "doc_id": doc_id,
                    "path": path,
                    "level": level,
                    "number": number,
                    "title": title,
                    "text": part_text,
                    "char_start": start,
                    "char_end": start + len(part_text),
                    "xrefs": [],
                }
            )
    return chunks


def split_oversized(body: str, max_chars: int, subsplit: str | None):
    """Only split when actually over the limit. Splitting a short article at every
    numbered item cuts relations in half and those are the ones extraction misses."""
    if len(body) <= max_chars:
        return [(body, 0)]
    if not subsplit:
        return [
            (body[i : i + max_chars], i) for i in range(0, len(body), max_chars)
        ]
    rx = re.compile(subsplit, re.MULTILINE)
    positions = [m.start() for m in rx.finditer(body)]
    if not positions:
        return [(body[i : i + max_chars], i) for i in range(0, len(body), max_chars)]
    positions = [0] + [p for p in positions if p > 0]
    out = []
    for j, start in enumerate(positions):
        end = positions[j + 1] if j + 1 < len(positions) else len(body)
        piece = body[start:end].strip()
        if piece:
            out.append((piece, start))
    return out


def resolve_xrefs(chunks: list[dict], profile: dict) -> None:
    """Attach cross-references, resolved to chunk IDs where the target exists."""
    patterns = profile["segmentation"].get("xref_patterns", [])
    if not patterns:
        return

    # Index chunks by (level, normalised number) for target lookup.
    index: dict[tuple[str, str], str] = {}
    for c in chunks:
        # Chunks are addressable by number (article 12) or by name (a recipe
        # title). Index both so either style of cross-reference resolves.
        for key in (c["number"], c["title"]):
            if key:
                index[(c["level"], key.replace(" ", "").lower())] = c["id"]

    for spec in patterns:
        rx = re.compile(spec["pattern"], re.IGNORECASE)
        target_level = spec.get("target_level")
        relation = spec.get("relation", "REFERS_TO")
        for c in chunks:
            for m in rx.finditer(c["text"]):
                raw = (m.group(1) or "").strip()
                key = (target_level, raw.replace(" ", "").lower())
                target = index.get(key)
                # A chunk's own heading matches the reference pattern. Self-loops
                # are noise and inflate degree, so drop them.
                if target == c["id"]:
                    continue
                c["xrefs"].append(
                    {
                        "relation": relation,
                        "raw": raw,
                        "target_level": target_level,
                        "target_chunk_id": target,
                        "char_start": m.start(),
                        "char_end": m.end(),
                    }
                )

    for c in chunks:
        c["xrefs"] = dedupe_xrefs(c["xrefs"])


def dedupe_xrefs(xrefs: list[dict]) -> list[dict]:
    """Collapse overlapping matches on the same target.

    "modifie l'article 12" matches both the generic reference pattern and the
    amendment pattern. The specific relation is the informative one, so a generic
    REFERS_TO to the same target is dropped whenever a specific relation exists.
    """
    specific_targets = {
        (x["target_level"], x["raw"].replace(" ", "").lower())
        for x in xrefs
        if x["relation"] != "REFERS_TO"
    }
    seen: set[tuple] = set()
    out = []
    for x in sorted(xrefs, key=lambda x: (x["relation"] == "REFERS_TO", x["char_start"])):
        key = (x["target_level"], x["raw"].replace(" ", "").lower())
        if x["relation"] == "REFERS_TO" and key in specific_targets:
            continue
        dedup_key = (x["relation"],) + key
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(x)
    return sorted(out, key=lambda x: x["char_start"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--input", required=True, help="file or directory of .txt/.md/.pdf")
    ap.add_argument("--out", default="chunks.jsonl")
    ap.add_argument("--limit", type=int, default=0, help="stop after N chunks (sampling)")
    ap.add_argument("--pdf-cache", default=".text-cache", help="where extracted PDF text lands")
    ap.add_argument("--pdf-engine", default="poppler", choices=["poppler", "pdfplumber", "pypdf"])
    args = ap.parse_args()

    profile = load_profile(args.profile)
    source = ensure_text(Path(args.input), Path(args.pdf_cache), args.pdf_engine)
    docs = read_documents(source)

    all_chunks: list[dict] = []
    for doc_id, text in docs:
        page_map = load_page_map(Path(doc_id))
        chunks = segment_document(doc_id, text, profile)
        for c in chunks:
            page = page_for_offset(page_map, c["char_start"])
            if page is not None:
                c["page"] = page
                c["path"] = f"{c['path']} (p. {page})"
        all_chunks.extend(chunks)

    resolve_xrefs(all_chunks, profile)

    if args.limit:
        all_chunks = all_chunks[: args.limit]

    out = Path(args.out)
    with out.open("w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    n_xref = sum(len(c["xrefs"]) for c in all_chunks)
    n_resolved = sum(1 for c in all_chunks for x in c["xrefs"] if x["target_chunk_id"])
    n_paged = sum(1 for c in all_chunks if c.get("page"))
    sizes = sorted(len(c["text"]) for c in all_chunks) or [0]

    print(f"documents      : {len(docs)}")
    print(f"chunks         : {len(all_chunks)}  -> {out}")
    print(f"chunk chars    : median {sizes[len(sizes)//2]}, max {sizes[-1]}")
    print(f"cross-refs     : {n_xref} found, {n_resolved} resolved to a chunk")
    if n_paged:
        print(f"page numbers   : {n_paged}/{len(all_chunks)} chunks carry one")
    if len(all_chunks) == len(docs):
        print("\nWARNING: one chunk per document. The hierarchy regexes probably")
        print("did not match. Check profile.segmentation.hierarchy against a sample.")
        print("For PDF sources, check the extracted .txt first: headings often")
        print("survive extraction but with different spacing than you expect.")


if __name__ == "__main__":
    main()
