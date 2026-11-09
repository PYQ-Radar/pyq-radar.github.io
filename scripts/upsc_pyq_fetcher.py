#!/usr/bin/env python3
"""Fetch only official UPSC CSE Prelims GS-I PDFs and extract searchable text."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pymupdf
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOURCE_URL = "https://www.upsc.gov.in/examinations/previous-question-papers"
OUT = Path("upsc-data")
PDF_DIR = OUT / "pdf"
TEXT_DIR = OUT / "text"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PYQ-Radar/1.0; educational indexer)"}

session = requests.Session()
retry = Retry(
    total=1, connect=1, read=1, status=1,
    backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
)
session.mount("https://", HTTPAdapter(max_retries=retry))
session.mount("http://", HTTPAdapter(max_retries=retry))

EXAM_RE = re.compile(
    r"^Civil Services\s*\(\s*Preliminary\s*\)\s*Examination\s*,?\s*(20\d{2})$",
    re.I,
)
GS1_RE = re.compile(
    r"^General Studies\s*Paper\s*[-–—]?\s*(?:I|1)$",
    re.I,
)


def get_prelims_gs1_context(anchor):
    """Use the nearest preceding paper label and exam heading, never broad ancestors."""
    previous = []
    for node in anchor.find_all_previous(string=True, limit=80):
        value = " ".join(str(node).split())
        if value:
            previous.append(value)

    # In nearest-first order, the first exam heading is the one owning this link.
    exam_index = next(
        (i for i, value in enumerate(previous) if EXAM_RE.fullmatch(value)),
        None,
    )
    if exam_index is None:
        return None

    exam_match = EXAM_RE.fullmatch(previous[exam_index])
    # The exact GS-I label must occur between this link and its owning exam heading.
    paper_label = next(
        (value for value in previous[:exam_index] if GS1_RE.fullmatch(value)),
        None,
    )
    if not paper_label:
        return None
    return int(exam_match.group(1))


def extract_pdf_text(pdf_path: Path, text_path: Path) -> dict:
    with pymupdf.open(pdf_path) as doc:
        pages = [
            f"\n\n--- PAGE {number} ---\n{page.get_text()}"
            for number, page in enumerate(doc, start=1)
        ]
    text = "".join(pages).strip()
    text_path.write_text(text, encoding="utf-8")
    return {"pages": len(pages), "characters": len(text), "has_text": bool(text)}


def main() -> int:
    OUT.mkdir(exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    response = session.get(SOURCE_URL, headers=HEADERS, timeout=(15, 45))
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    papers = {}
    pdf_links_seen = 0
    for anchor in soup.find_all("a", href=True):
        href = urljoin(SOURCE_URL, anchor["href"])
        if not urlparse(href).path.lower().endswith(".pdf"):
            continue
        pdf_links_seen += 1

        year = get_prelims_gs1_context(anchor)
        if year is None:
            continue

        key = f"prelims-{year}-gs-paper-i"
        papers[key] = {
            "exam": "Civil Services Examination",
            "stage": "Preliminary",
            "year": year,
            "paper": "General Studies Paper-I",
            "title": f"Civil Services (Preliminary) Examination {year} - General Studies Paper-I",
            "source_name": "Union Public Service Commission",
            "source_url": href,
            "listing_url": SOURCE_URL,
        }

    print(f"UPSC page PDF links inspected: {pdf_links_seen}", flush=True)
    print(f"UPSC CSE Prelims GS-I PDFs discovered: {len(papers)}", flush=True)
    for item in sorted(papers.values(), key=lambda p: p["year"]):
        print(f"[MATCH] {item['year']} | {item['paper']} | {item['source_url']}", flush=True)

    if not papers:
        print("No exact Civil Services Prelims GS-I links found; stopping safely.", file=sys.stderr, flush=True)
        return 2

    downloaded = []
    for index, (key, item) in enumerate(sorted(papers.items()), start=1):
        print(f"[{index}/{len(papers)}] Fetching {item['year']} Prelims GS-I", flush=True)
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", key).strip("-")
        pdf_path = PDF_DIR / f"{safe}.pdf"
        text_path = TEXT_DIR / f"{safe}.txt"
        try:
            if not pdf_path.exists():
                pdf_response = session.get(
                    item["source_url"], headers=HEADERS, timeout=(15, 45)
                )
                pdf_response.raise_for_status()
                if not pdf_response.content.startswith(b"%PDF"):
                    raise ValueError("The linked document did not return a PDF file")
                pdf_path.write_bytes(pdf_response.content)

            extraction = extract_pdf_text(pdf_path, text_path)
            digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            downloaded.append({
                **item, "file": str(pdf_path), "text_file": str(text_path),
                "sha256": digest, **extraction, "fetch_status": "downloaded"
            })
            print(
                f"[OK] {item['year']} GS-I ({extraction['pages']} pages, "
                f"{extraction['characters']} characters)",
                flush=True,
            )
        except Exception as exc:
            downloaded.append({**item, "fetch_status": "error", "error": str(exc)})
            print(f"[ERROR] {item['year']} GS-I: {exc}", file=sys.stderr, flush=True)
        time.sleep(1)

    manifest = {
        "source": SOURCE_URL,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "Only official UPSC CSE Prelims GS-I PDFs. Extracted questions remain unverified.",
        "papers_found": len(papers),
        "papers_processed": len(downloaded),
        "papers": downloaded,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    success_count = sum(p.get("fetch_status") == "downloaded" for p in downloaded)
    (OUT / "summary.txt").write_text(
        f"UPSC CSE Prelims GS-I PDFs discovered: {len(papers)}\n"
        f"Downloaded and extracted: {success_count}\n"
        f"Errors: {len(downloaded) - success_count}\n"
        "Review extracted text before importing individual questions.\n",
        encoding="utf-8",
    )
    return 0 if success_count else 2


if __name__ == "__main__":
    raise SystemExit(main())
