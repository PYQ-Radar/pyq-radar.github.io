#!/usr/bin/env python3
"""Discover official UPSC CSE question-paper PDFs and extract searchable text."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import fitz
import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://www.upsc.gov.in/examinations/previous-question-papers"
OUT = Path("upsc-data")
PDF_DIR = OUT / "pdf"
TEXT_DIR = OUT / "text"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PYQ-Radar/1.0; educational paper indexer)"
}
EXAM_RE = re.compile(r"Civil Services\s*\((?:Preliminary|Main)\)", re.I)
PAPER_RE = re.compile(
    r"(General Studies Paper\s*[-–]?\s*[I1-4]|GS Paper\s*[-–]?\s*[I1-4]|Essay)",
    re.I,
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def nearest_context(anchor) -> str:
    """Find the smallest ancestor block containing the exam name and its year."""
    node = anchor
    fallback = []
    for _ in range(24):
        if node is None:
            break
        text = " ".join(node.get_text(" ", strip=True).split())
        if text and text not in fallback:
            fallback.append(text)
        if text and EXAM_RE.search(text) and YEAR_RE.search(text):
            return text
        node = node.parent

    # Some UPSC page layouts put the exam title in a preceding sibling.
    previous = anchor.find_previous(["h2", "h3", "h4", "h5"])
    if previous:
        heading_text = " ".join(previous.get_text(" ", strip=True).split())
        if heading_text:
            fallback.append(heading_text)
    return " | ".join(fallback)


def classify(context: str, label: str):
    exam_match = EXAM_RE.search(context)
    paper_match = PAPER_RE.search(label) or PAPER_RE.search(context)
    if not exam_match or not paper_match:
        return None
    year_match = YEAR_RE.search(context)
    if not year_match:
        return None
    stage = "Preliminary" if "Preliminary" in exam_match.group(0) else "Main"
    return stage, int(year_match.group(1)), paper_match.group(0)


def extract_pdf_text(pdf_path: Path, text_path: Path) -> dict:
    doc = fitz.open(pdf_path)
    pages = [
        f"\n\n--- PAGE {page_number} ---\n{page.get_text('text')}"
        for page_number, page in enumerate(doc, start=1)
    ]
    text = "".join(pages).strip()
    text_path.write_text(text, encoding="utf-8")
    return {"pages": len(doc), "characters": len(text), "has_text": bool(text)}


def main() -> int:
    OUT.mkdir(exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    papers = {}
    for anchor in soup.find_all("a", href=True):
        label = " ".join(anchor.get_text(" ", strip=True).split())
        href = urljoin(SOURCE_URL, anchor["href"])
        if not label or urlparse(href).scheme not in ("http", "https"):
            continue

        context = nearest_context(anchor)
        classification = classify(context, label)
        if not classification:
            continue
        stage, year, paper = classification

        # Discover only Civil Services exam papers, not other UPSC examinations.
        if stage == "Preliminary" and not re.search(
            r"(?:Paper\s*[-–]?\s*[I1]|Paper\s*[-–]?\s*II|GS)", paper, re.I
        ):
            continue
        if stage == "Main" and not re.search(r"(General Studies|Essay|GS)", paper, re.I):
            continue
        if not (
            href.lower().endswith(".pdf")
            or ".pdf?" in href.lower()
            or "pdf" in label.lower()
        ):
            continue

        key = f"{stage}-{year}-{paper}".lower().replace(" ", "-")
        papers[key] = {
            "exam": "Civil Services Examination",
            "stage": stage,
            "year": year,
            "paper": paper,
            "title": label,
            "source_name": "Union Public Service Commission",
            "source_url": href,
            "listing_url": SOURCE_URL,
        }

    print(f"Official UPSC CSE papers discovered: {len(papers)}")
    if not papers:
        print("No papers matched the current page structure; refusing to report a successful empty fetch.")
        return 2

    downloaded = []
    for key, item in sorted(papers.items()):
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", key).strip("-")
        pdf_path = PDF_DIR / f"{safe}.pdf"
        text_path = TEXT_DIR / f"{safe}.txt"
        try:
            if not pdf_path.exists():
                r = requests.get(item["source_url"], headers=HEADERS, timeout=90)
                r.raise_for_status()
                if not r.content.startswith(b"%PDF"):
                    raise ValueError("The linked document did not return a PDF file")
                pdf_path.write_bytes(r.content)
                time.sleep(1.0)
            extraction = extract_pdf_text(pdf_path, text_path)
            digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            downloaded.append({
                **item, "file": str(pdf_path), "text_file": str(text_path),
                "sha256": digest, **extraction, "fetch_status": "downloaded"
            })
            print(f"[OK] {item['year']} {item['stage']} {item['paper']} ({extraction['pages']} pages)")
        except Exception as exc:
            downloaded.append({**item, "fetch_status": "error", "error": str(exc)})
            print(f"[WARN] {item['title']}: {exc}", file=sys.stderr)

    manifest = {
        "source": SOURCE_URL,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "Official PDF discovery and text extraction only. No questions are auto-verified.",
        "papers_found": len(papers),
        "papers_processed": len(downloaded),
        "papers": downloaded,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (OUT / "summary.txt").write_text(
        f"Official UPSC CSE papers discovered: {len(papers)}\n"
        f"Processed: {sum(p.get('fetch_status') == 'downloaded' for p in downloaded)}\n"
        f"Errors: {sum(p.get('fetch_status') == 'error' for p in downloaded)}\n"
        "Review extracted text before converting it into individual PYQ records.\n",
        encoding="utf-8",
    )
    return 0 if downloaded else 2


if __name__ == "__main__":
    raise SystemExit(main())
