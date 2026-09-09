#!/usr/bin/env python3
"""Stage 0: PDF to clean text, with a page map.

Naive PDF extraction quietly destroys the structure the whole pipeline depends
on: running headers land in the middle of articles, hyphenated line breaks split
words, and soft wraps put "Article" and "12" on different lines so the profile's
hierarchy regexes stop matching. That failure shows up much later as "the graph
came out empty", so this stage cleans up front and reports what it did.

    python scripts/extract_pdf.py --input ./pdfs --out ./corpus
    python scripts/extract_pdf.py --input code.pdf --out ./corpus --engine pdfplumber
    python scripts/extract_pdf.py --input ./pdfs --out ./corpus --diagnose-only

Writes <name>.txt plus <name>.pages.json (char offset -> page number) so
citations can carry a page number, which is what makes a legal answer checkable.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

PAGE_BREAK = "\f"


# --------------------------------------------------------------------------
# diagnosis
# --------------------------------------------------------------------------

def diagnose(pdf: Path) -> dict:
    """Decide whether a text layer exists before trying to use one."""
    info = {"path": str(pdf), "pages": None, "has_text_layer": False, "fonts": 0}
    try:
        out = subprocess.run(
            ["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=60
        ).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
        if m:
            info["pages"] = int(m.group(1))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        out = subprocess.run(
            ["pdffonts", str(pdf)], capture_output=True, text=True, timeout=60
        ).stdout
        # Two header lines, then one row per font. No rows means no text layer.
        rows = [l for l in out.splitlines()[2:] if l.strip()]
        info["fonts"] = len(rows)
        info["has_text_layer"] = len(rows) > 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # No poppler. Fall back to sampling with pypdf.
        try:
            from pypdf import PdfReader

            r = PdfReader(str(pdf))
            info["pages"] = len(r.pages)
            sample = "".join((p.extract_text() or "") for p in r.pages[:3])
            info["has_text_layer"] = len(sample.strip()) > 50
        except Exception:  # noqa: BLE001
            pass
    return info


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def extract_poppler(pdf: Path) -> list[str]:
    """pdftotext -layout. Best default: it preserves column structure, which
    matters because two-column statutes otherwise interleave into nonsense."""
    res = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "pdftotext failed")
    pages = res.stdout.split(PAGE_BREAK)
    # The final form feed yields a trailing empty page. Left in, it skews the
    # page count that header detection thresholds against.
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def extract_pdfplumber(pdf: Path) -> list[str]:
    """Slower, but gives better results on PDFs with irregular word spacing."""
    import pdfplumber

    pages = []
    with pdfplumber.open(str(pdf)) as doc:
        for page in doc.pages:
            pages.append(page.extract_text(layout=True) or "")
    return pages


def extract_pypdf(pdf: Path) -> list[str]:
    from pypdf import PdfReader

    return [(p.extract_text() or "") for p in PdfReader(str(pdf)).pages]


ENGINES = {"poppler": extract_poppler, "pdfplumber": extract_pdfplumber, "pypdf": extract_pypdf}


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------

def normalize_for_matching(line: str) -> str:
    """Strip digits and whitespace so 'Page 12 of 340' and 'Page 13 of 340'
    collapse to the same fingerprint."""
    return re.sub(r"\d+", "#", line).strip().lower()


def find_running_lines(pages: list[str], zone: int = 3, min_ratio: float = 0.5) -> set[str]:
    """Detect running headers and footers by fingerprint frequency.

    A line in the top or bottom few lines of at least half the pages is
    furniture, not content. Removing it is what stops 'JOURNAL OFFICIEL N 6789'
    from being swallowed into the middle of Article 12.
    """
    # Two pages is enough: a line appearing on both, at the same edge, is
    # furniture. The old 4-page floor silently disabled header removal on short
    # documents and behaved differently per engine because of page-count quirks.
    if len(pages) < 2:
        return set()
    counts: Counter[str] = Counter()
    for p in pages:
        lines = [l for l in p.splitlines() if l.strip()]
        if not lines:
            continue
        candidates = lines[:zone] + lines[-zone:]
        for c in set(normalize_for_matching(l) for l in candidates):
            if c and len(c) > 3:
                counts[c] += 1
    threshold = max(2, int(len(pages) * min_ratio))
    return {k for k, v in counts.items() if v >= threshold}


def strip_running_lines(page: str, running: set[str], zone: int = 3) -> str:
    """Remove detected furniture from the top and bottom of a page.

    Edge position is measured among non-blank lines, matching how the
    fingerprints were collected. Some engines emit runs of blank lines at the
    page top, which would otherwise push the header out of a raw-index window
    and leave it in the text.
    """
    if not running:
        return page
    lines = page.splitlines()
    nonblank = [i for i, l in enumerate(lines) if l.strip()]
    edge = set(nonblank[:zone]) | set(nonblank[-zone:])
    return "\n".join(
        l for i, l in enumerate(lines)
        if not (i in edge and normalize_for_matching(l) in running)
    )


def dehyphenate(text: str) -> str:
    """Join 'obliga-\ntion' into 'obligation'. Only when the continuation is
    lowercase, so hyphenated compounds at a line end survive."""
    return re.sub(r"(\w)[-\u2010\u2011]\s*\n\s*([a-zà-ÿ])", r"\1\2", text)


def measure_full_width(pages: list[str]) -> float:
    """The document's right margin, in characters.

    Must be computed on raw wrapped lines, before dehyphenation or reflow join
    anything. Measuring afterwards lets already-joined lines inflate the
    statistic, which then blocks every remaining join.
    """
    widths = sorted(
        len(l.rstrip()) for p in pages for l in p.split("\n") if len(l.strip()) > 20
    )
    if len(widths) < 5:
        return 0.0
    return widths[int(len(widths) * 0.9)]


def reflow(text: str, full_measure: float) -> str:
    """Join soft-wrapped lines using line width, not punctuation.

    A soft wrap happens because the line hit the right margin, so the tell is
    that the previous line runs close to the document's full measure. Headings,
    list items and paragraph-final lines are all short, which is exactly why
    width beats punctuation heuristics here: 'Article 12' ends in a digit and
    must never absorb the line beneath it, while 'prevue a l'article 12' running
    the full width must.
    """
    if not full_measure:
        return text
    threshold = full_measure * 0.85
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        prev = out[-1].rstrip() if out else ""
        if (
            prev
            and stripped
            and len(prev) >= threshold
            and not re.search(r"[.:;!?]$", prev)
            and re.match(r"^[a-zà-ÿ0-9(]", stripped)
        ):
            out[-1] = prev + " " + stripped
        else:
            out.append(line)
    return "\n".join(out)


def collapse_blank_runs(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def squeeze_layout_spaces(text: str) -> str:
    """pdftotext -layout pads with runs of spaces to fake column position.
    Keep leading indentation, squeeze the rest, or every regex needs \\s+."""
    out = []
    for line in text.split("\n"):
        if not line.strip():
            out.append("")  # a whitespace-only line is a blank line
            continue
        lead = len(line) - len(line.lstrip(" "))
        out.append(" " * min(lead, 4) + re.sub(r" {2,}", " ", line.strip()))
    return "\n".join(out)


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def build_document(pages: list[str], args) -> tuple[str, list[dict], int]:
    """Clean each page completely, then concatenate and record offsets.

    Order matters. Offsets must be taken on the final text: dehyphenation and
    reflow both shorten it, so recording positions first and cleaning after
    shifts every page boundary and silently misattributes citations by a page.
    """
    running = set() if args.keep_headers else find_running_lines(pages)

    stripped_pages = [
        squeeze_layout_spaces(strip_running_lines(p, running)) for p in pages
    ]
    full_measure = measure_full_width(stripped_pages)

    parts: list[str] = []
    page_map: list[dict] = []
    cursor = 0
    for i, page in enumerate(stripped_pages, 1):
        page = dehyphenate(page)
        if not args.no_reflow:
            page = reflow(page, full_measure)
        page = collapse_blank_runs(page).strip("\n")
        if not page.strip():
            continue
        page_map.append({"page": i, "char_start": cursor, "char_end": cursor + len(page)})
        parts.append(page)
        cursor += len(page) + 2  # the "\n\n" joiner below

    return "\n\n".join(parts), page_map, len(running)


def process(pdf: Path, out_dir: Path, args) -> dict | None:
    d = diagnose(pdf)
    if not d["has_text_layer"]:
        print(f"  {pdf.name}: NO TEXT LAYER ({d['pages']} pages). Scanned or raster.")
        print("    Run OCR first, then re-run this script:")
        print(f"      ocrmypdf --force-ocr -l fra+eng '{pdf}' '{pdf.stem}_ocr.pdf'")
        return None
    if args.diagnose_only:
        print(f"  {pdf.name}: {d['pages']} pages, {d['fonts']} fonts, text layer OK")
        return d

    engine = ENGINES[args.engine]
    try:
        pages = engine(pdf)
    except Exception as e:  # noqa: BLE001
        print(f"  {pdf.name}: {args.engine} failed ({e}); falling back to pypdf")
        pages = extract_pypdf(pdf)

    text, page_map, n_running = build_document(pages, args)

    out_txt = out_dir / f"{pdf.stem}.txt"
    out_txt.write_text(text, encoding="utf-8")
    (out_dir / f"{pdf.stem}.pages.json").write_text(
        json.dumps(page_map, ensure_ascii=False), encoding="utf-8"
    )

    chars_per_page = len(text) / max(len(page_map), 1)
    print(
        f"  {pdf.name}: {d['pages']} pages -> {len(text):,} chars "
        f"({chars_per_page:.0f}/page), {n_running} running line(s) removed"
    )
    if chars_per_page < 200:
        print("    LOW yield per page. Likely a partial text layer; consider OCR.")
    return d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="PDF file or directory of PDFs")
    ap.add_argument("--out", required=True, help="directory for .txt output")
    ap.add_argument("--engine", choices=list(ENGINES), default="poppler")
    ap.add_argument("--diagnose-only", action="store_true")
    ap.add_argument("--keep-headers", action="store_true", help="skip header/footer removal")
    ap.add_argument("--no-reflow", action="store_true", help="skip soft-wrap joining")
    args = ap.parse_args()

    src = Path(args.input)
    pdfs = [src] if src.is_file() else sorted(src.rglob("*.pdf"))
    if not pdfs:
        sys.exit(f"no PDFs found at {src}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{len(pdfs)} PDF(s), engine={args.engine}")
    ok = 0
    for pdf in pdfs:
        if process(pdf, out_dir, args):
            ok += 1

    if args.diagnose_only:
        return
    print(f"\n{ok}/{len(pdfs)} extracted -> {out_dir}")
    print("Open one .txt and check that headings survived before segmenting.")
    print("If the hierarchy regexes do not match, that is a cleanup problem here,")
    print("not a profile problem.")


if __name__ == "__main__":
    main()
