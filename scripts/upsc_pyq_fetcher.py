#!/usr/bin/env python3
"""Fetch official UPSC CSE Prelims GS-I PDFs and extract searchable text."""
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
    total=3, connect=3, read=3, status=3,
    backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
)
session.mount("https://", HTTPAdapter(max_retries=retry))
session.mount("http://", HTTPAdapter(max_retries=retry))


def exam_context(anchor):
    """Walk up the DOM until the containing block identifies CSE Prelims and year."""
    node = anchor
    for _ in range(20):
        if node is None:
            break
        text = " ".join(node.get_text(" ", strip=True).split())
        exam = re.search(
            r"Civil Services\s*\(\s*Preliminary\s*\)\s*Examination",
            text,
            re.I,
        )
        year = re.search(r"\b20\d{2}\b", text)
        gs1 = re.search(
            r"General Studies\s*Paper\s*[-–—]?\s*(?:I|1)\b",
            text,
            re.I,
        )
        if exam and year and gs1:
            return int(year.group(0)), text
        node = node.parent
    return None, ""


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
        path = urlparse(href).path.lower()
        if not path.endswith(".pdf"):
            continue
        pdf_links_seen += 1

        year, context = exam_context(anchor)
        if year is None:
            continue

        # The UPSC page groups each paper link under its exam heading.
        # Match the surrounding exam block, not the PDF filename or tiny link label.
        if not re.search(
            r"Civil Services\s*\(\s*Preliminary\s*\)\s*Examination",
            context,
            re.I,
        ):
            continue
        if not re.search(
            r"General Studies\s*Paper\s*[-–—]?\s*(?:I|1)\b",
            context,
            re.I,
        ):
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
    if not papers:
        print(
            "No matching papers found in the page's exam sections. "
            "Stopping safely; no unrelated PDFs will be downloaded.",
            file=sys.stderr,
            flush=True,
        )
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
                    item["source_url"], headers=HEADERS, timeout=(20, 90)
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
        "note": "Official UPSC CSE Prelims GS-I only. Imported questions remain unverified.",
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
