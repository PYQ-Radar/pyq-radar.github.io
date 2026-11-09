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

import fitz
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


def extract_pdf_text(pdf_path: Path, text_path: Path) -> dict:
    with fitz.open(pdf_path) as doc:
        pages = [
            f"\n\n--- PAGE {number} ---\n{page.get_text('text')}"
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
    for anchor in soup.find_all("a", href=True):
        label = " ".join(anchor.get_text(" ", strip=True).split())
        href = urljoin(SOURCE_URL, anchor["href"])
        path = urlparse(href).path
        if not label or urlparse(href).scheme not in ("http", "https"):
            continue
        if not (path.lower().endswith(".pdf") or "pdf" in label.lower()):
            continue

        # UPSC CSE Preliminary filenames use the CSP marker. Exclude CSM/Mains
        # and all other UPSC examinations; only select GS Paper-I.
        upper = path.upper()
        label_upper = label.upper().replace("–", "-").replace("—", "-")
        is_prelims = bool(re.search(r"QP[-_]?CSP", upper))
        is_gs1 = bool(
            re.search(r"GS[-_]?I(?:[-_.]|$)", upper)
            or re.search(r"GENERAL STUDIES\s*PAPER\s*[-–]?\s*(?:I|1)\b", label_upper)
            or re.search(r"GS\s*PAPER\s*[-–]?\s*(?:I|1)\b", label_upper)
        )
        if not (is_prelims and is_gs1):
            continue

        context = " ".join(anchor.parent.get_text(" ", strip=True).split())
        year_match = re.search(r"20\d{2}", context) or re.search(r"20\d{2}", path)
        if not year_match:
            # Some filenames encode the year as two digits, e.g. QP-CSP-24.
            short_year = re.search(r"QP[-_]?CSP[-_]?([0-9]{2})", upper)
            if not short_year:
                print(f"[SKIP] Could not identify year: {label}")
                continue
            year = 2000 + int(short_year.group(1))
        else:
            year = int(year_match.group(0))

        key = f"prelims-{year}-gs-paper-i"
        papers[key] = {
            "exam": "Civil Services Examination",
            "stage": "Preliminary",
            "year": year,
            "paper": "General Studies Paper-I",
            "title": label,
            "source_name": "Union Public Service Commission",
            "source_url": href,
            "listing_url": SOURCE_URL,
        }

    print(f"UPSC CSE Prelims GS-I PDFs discovered: {len(papers)}", flush=True)
    if not papers:
        print(
            "No matching CSP GS-I PDF links found. UPSC may have changed its link naming; "
            "stopping safely rather than downloading unrelated examinations.",
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
                    item["source_url"], headers=HEADERS, timeout=(20, 60)
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
