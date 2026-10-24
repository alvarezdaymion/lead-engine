"""CSV import: businesses you already have, one per row.

Columns: `company`, `website` (required), `market`, `keyword` (optional). Rows whose domain is already
known are skipped, so the same file can be re-imported safely. Template: examples/import.csv.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..errors import InputError, MissingFile, sanitized
from . import Candidate


@sanitized
def read_candidates(path: Path | str, source: str = "csv") -> list[Candidate]:
    p = Path(path)
    if not p.exists():
        raise MissingFile("import CSV not found")
    out = []
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = {"company", "website"} - set(reader.fieldnames or [])
        if missing:
            raise InputError("import CSV is missing required columns: %s" % ", ".join(sorted(missing)))
        for row in reader:
            website = (row.get("website") or "").strip()
            if website and "://" not in website:
                website = "https://" + website
            out.append(Candidate(company=(row.get("company") or "").strip(), website=website,
                                 market=(row.get("market") or "").strip(), keyword=(row.get("keyword") or "").strip(),
                                 source=source))
    return out
