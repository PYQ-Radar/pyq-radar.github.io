#!/usr/bin/env python3
"""Conservative parser for official UPSC CSE Prelims GS-I PDFs.

Only records with a clear question number and four clearly labelled options
are emitted. Imports remain unverified; no answer key is inferred.
"""
from __future__ import annotations
import json, os, re, sys
from pathlib import Path
import fitz
import requests

MANIFEST = Path("upsc-data/manifest.json")
IMPORT_URL = os.environ.get("PYQ_IMPORT_URL", "").strip()
IMPORT_TOKEN = os.environ.get("PYQ_IMPORT_TOKEN", "").strip()
EXAM_ID = os.environ.get("UPSC_EXAM_ID", "").strip()

QUESTION_START = re.compile(r"(?m)^\s*(\d{1,3})[.)]\s+")
OPTION_LINE = re.compile(r"(?im)^\s*(?:\(([a-d])\)|([a-d])[.)])\s*(.+?)\s*$")

def subject_for(text: str):
    t = text.lower()
    groups = [
        ("Geography", ["latitude", "longitude", "river", "monsoon", "climate", "soil", "plateau", "mountain", "ocean", "island", "strait", "glacier", "rainfall", "drainage"]),
        ("Environment", ["biodiversity", "ecosystem", "wildlife", "species", "national park", "biosphere", "wetland", "pollution", "climate change", "carbon"]),
        ("Economy", ["inflation", "fiscal", "monetary", "repo rate", "gdp", "taxation", "banking", "currency", "budget", "export", "import"]),
        ("Polity", ["constitution", "parliament", "president", "fundamental right", "directive principle", "governor", "supreme court", "bill", "election commission"]),
        ("History", ["dynasty", "inscription", "archaeological", "buddhism", "jainism", "mughal", "harappan", "chola", "gupta", "freedom movement"]),
        ("Science & Technology", ["quantum", "vaccine", "virus", "satellite", "semiconductor", "artificial intelligence", "biotechnology", "nuclear"]),
    ]
    for name, words in groups:
        if any(w in t for w in words):
            return name
    return None

def parse_pdf(path: Path, paper: dict):
    doc = fitz.open(path)
    full = "\n".join(page.get_text("text") for page in doc)
    starts = list(QUESTION_START.finditer(full))
    records = []
    for i, match in enumerate(starts):
        number = int(match.group(1))
        if number < 1 or number > 100:
            continue
        end = starts[i+1].start() if i+1 < len(starts) else len(full)
        block = full[match.start():end].strip()
        option_matches = list(OPTION_LINE.finditer(block))
        if len(option_matches) != 4:
            continue
        keys = [((m.group(1) or m.group(2)).upper()) for m in option_matches]
        if keys != ["A", "B", "C", "D"]:
            continue
        stem = block[:option_matches[0].start()].strip()
        stem = re.sub(r"^\s*\d{1,3}[.)]\s*", "", stem)
        stem = re.sub(r"\s+", " ", stem).strip()
        if len(stem) < 25 or len(stem) > 3000:
            continue
        options = []
        for m in option_matches:
            key = (m.group(1) or m.group(2)).upper()
            value = re.sub(r"\s+", " ", m.group(3)).strip()
            if len(value) < 1:
                break
            options.append({"option_key": key, "option_text": value})
        if len(options) != 4:
            continue
        records.append({
            "exam_id": EXAM_ID,
            "year": int(paper["year"]),
            "paper": f"UPSC CSE Prelims {paper['year']} GS Paper I",
            "question_text": stem,
            "source_name": "Union Public Service Commission",
            "source_url": paper["source_url"],
            "options": options,
            "subject_name": subject_for(stem + " " + " ".join(o["option_text"] for o in options)),
        })
    return records

def main():
    if not MANIFEST.exists():
        raise SystemExit("upsc-data/manifest.json not found")
    if not (IMPORT_URL and IMPORT_TOKEN and EXAM_ID):
        raise SystemExit("Missing PYQ_IMPORT_URL, PYQ_IMPORT_TOKEN, or UPSC_EXAM_ID")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    records = []
    for paper in manifest.get("papers", []):
        if paper.get("fetch_status") != "downloaded" or paper.get("stage") != "Preliminary":
            continue
        if not re.search(r"(GS\s*[-–]?\s*I|General Studies Paper\s*[-–]?\s*I|Paper\s*[-–]?\s*I)", paper.get("paper", ""), re.I):
            continue
        pdf_path = Path(paper["file"])
        if pdf_path.exists():
            parsed = parse_pdf(pdf_path, paper)
            records.extend(parsed)
            print(f"Parsed {len(parsed)} candidates from {pdf_path.name}")
    if not records:
        print("No safely parsed question blocks found; nothing imported.")
        return
    # Send bounded batches. Edge Function caps each request at 100.
    imported_total = 0
    for start in range(0, len(records), 100):
        batch = records[start:start+100]
        resp = requests.post(
            IMPORT_URL,
            headers={"Content-Type": "application/json", "x-import-token": IMPORT_TOKEN},
            json={"records": batch},
            timeout=90,
        )
        print(f"Import response {resp.status_code}: {resp.text[:4000]}")
        resp.raise_for_status()
        imported_total += resp.json().get("imported", 0)
    print(f"Import complete: {imported_total} unverified records sent to review queue.")

if __name__ == "__main__":
    main()
