#!/usr/bin/env python3
"""Build a pre-Google economic narrative proxy from archived GDELT 2.1 GKG files.

The archive is very large, so the reproducible pilot uses one rotating 15-minute
snapshot per week and a four-week rolling window. It classifies documents from
their GKG themes and tone; it does not claim to be a full-text census.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import io
import json
import math
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path


ARCHIVE_ROOT = "https://data.gdeltproject.org/gdeltv2"
USER_AGENT = "Kazakhstan-development-bank-risk-research/1.0"
csv.field_size_limit(sys.maxsize)

# GDELT uses FIPS-style country codes in V2Locations.
MAJOR_ECONOMY_CODES = {
    "US", "CH", "JA", "UK", "IN", "RS",  # US, China, Japan, UK, India, Russia
    "AU", "BE", "CY", "EI", "EN", "FI", "FR", "GM", "GR", "IT", "LG",
    "LH", "LO", "LU", "MT", "NL", "PO", "SI", "SP",  # euro-area coverage
}
MAJOR_ECONOMY_NAMES = (
    "united states", "u.s.", "china", "chinese", "eurozone", "euro area",
    "european central bank", "japan", "japanese", "united kingdom", "britain",
    "british", "india", "indian", "russia", "russian", "federal reserve",
)
ECONOMIC_THEME_FRAGMENTS = (
    "ECON_", "EPU_ECONOMY", "MACROECONOMIC", "TAX_ECON_PRICE",
    "FINANCIAL_SECTOR", "FINANCIAL_STABILITY",
)
STRESS_THEME_FRAGMENTS = (
    "DEBT", "INFLATION", "COST_OF_LIVING", "INTEREST_RATES", "STOCKMARKET",
    "BANKRUPT", "RECESS", "UNEMPLOY", "MACROECONOMIC_VULNERABILITY",
    "FINANCIAL_STABILITY", "CURRENCY", "CREDIT", "POVERTY",
)
ROTATING_SAMPLES = ((0, 0), (2, 6), (4, 12), (6, 18))


@dataclass(frozen=True)
class Week:
    end: date
    sample_at: datetime


def sundays(start: date, end: date) -> list[date]:
    start += timedelta(days=(6 - start.weekday()) % 7)
    end -= timedelta(days=(end.weekday() - 6) % 7)
    return [start + timedelta(days=7 * index) for index in range((end - start).days // 7 + 1)]


def weeks(start: date, end: date) -> list[Week]:
    result = []
    for index, week_end in enumerate(sundays(start, end)):
        day_offset, hour = ROTATING_SAMPLES[index % len(ROTATING_SAMPLES)]
        week_start = week_end - timedelta(days=6)
        sample_date = week_start + timedelta(days=day_offset)
        result.append(Week(week_end, datetime.combine(sample_date, datetime_time(hour))))
    return result


def cache_path(cache_dir: Path, sample_at: datetime) -> Path:
    return cache_dir / f"{sample_at:%Y%m%d%H%M%S}.json"


def archive_url(sample_at: datetime) -> str:
    return f"{ARCHIVE_ROOT}/{sample_at:%Y%m%d%H%M%S}.gkg.csv.zip"


def themes(row: list[str]) -> set[str]:
    return {value for value in row[7].split(";") if value}


def average_tone(row: list[str]) -> float | None:
    try:
        return float(row[15].split(",", 1)[0])
    except (IndexError, ValueError):
        return None


def in_major_economy_scope(row: list[str]) -> bool:
    try:
        for location in row[9].split(";"):
            fields = location.split("#")
            if len(fields) > 2 and fields[2] in MAJOR_ECONOMY_CODES:
                return True
    except IndexError:
        pass
    searchable = " ".join(row[index] for index in (4, 13, 23) if index < len(row)).lower()
    return any(name in searchable for name in MAJOR_ECONOMY_NAMES)


def has_fragment(values: set[str], fragments: tuple[str, ...]) -> bool:
    return any(fragment in value for value in values for fragment in fragments)


def classify_archive(payload: bytes, sample_at: datetime) -> dict[str, object]:
    general_documents: set[str] = set()
    crisis_documents: set[str] = set()
    general_sources: set[str] = set()
    crisis_sources: set[str] = set()
    rows_seen = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise RuntimeError(f"Unexpected GDELT archive contents for {sample_at:%Y-%m-%d %H:%M}")
        with archive.open(names[0]) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
            for row in csv.reader(stream, delimiter="\t"):
                rows_seen += 1
                if len(row) < 16 or not in_major_economy_scope(row):
                    continue
                document_themes = themes(row)
                if not has_fragment(document_themes, ECONOMIC_THEME_FRAGMENTS):
                    continue
                tone = average_tone(row)
                if tone is None:
                    continue
                identifier = row[4] or f"{row[3]}:{rows_seen}"
                source = row[3].lower()
                general_documents.add(identifier)
                if source:
                    general_sources.add(source)
                stress_theme = has_fragment(document_themes, STRESS_THEME_FRAGMENTS)
                if tone <= -5.0 or (stress_theme and tone <= -1.5):
                    crisis_documents.add(identifier)
                    if source:
                        crisis_sources.add(source)
    return {
        "sample_at": sample_at.isoformat(),
        "rows_seen": rows_seen,
        "general_documents": len(general_documents),
        "crisis_documents": len(crisis_documents),
        "general_sources": sorted(general_sources),
        "crisis_sources": sorted(crisis_sources),
    }


def fetch_sample(week: Week, cache_dir: Path, retries: int = 3) -> tuple[date, dict[str, object]]:
    path = cache_path(cache_dir, week.sample_at)
    if path.exists():
        return week.end, json.loads(path.read_text(encoding="utf-8"))
    offsets = (
        0, 15, -15, 30, -30, 45, -45, 60, -60, 120, -120, 360, -360,
        720, -720, 1440, -1440, 2880, -2880, 4320, -4320,
    )
    last_error: Exception | None = None
    for offset in offsets:
        candidate = week.sample_at + timedelta(minutes=offset)
        request = urllib.request.Request(archive_url(candidate), headers={"User-Agent": USER_AGENT})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    result = classify_archive(response.read(), candidate)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
                return week.end, result
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code == 404:
                    break
                if attempt + 1 < retries:
                    time.sleep(1.5 * (attempt + 1))
            except (OSError, http.client.HTTPException, urllib.error.URLError, zipfile.BadZipFile) as error:
                last_error = error
                if attempt + 1 < retries:
                    time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"No usable GDELT archive near {week.sample_at.isoformat()}") from last_error


def rolling_records(samples: list[tuple[date, dict[str, object]]], window: int = 4) -> list[dict[str, object]]:
    records = []
    for index, (week_end, sample) in enumerate(samples):
        history = samples[max(0, index - window + 1):index + 1]
        general_documents = sum(int(item[1]["general_documents"]) for item in history)
        crisis_documents = sum(int(item[1]["crisis_documents"]) for item in history)
        general_sources = {source for _, item in history for source in item["general_sources"]}
        crisis_sources = {source for _, item in history for source in item["crisis_sources"]}
        article_share = crisis_documents / max(1, general_documents)
        source_share = len(crisis_sources) / max(1, len(general_sources))
        intensity = 100.0 * math.sqrt(article_share * source_share)
        records.append({
            "date": (week_end + timedelta(days=1)).isoformat(),
            "week_end": week_end.isoformat(),
            "intensity": round(intensity, 4),
            "crisis_articles": crisis_documents,
            "economic_articles": general_documents,
            "crisis_sources": len(crisis_sources),
            "economic_sources": len(general_sources),
            "sample_at": sample["sample_at"],
            "rows_seen": sum(int(item[1]["rows_seen"]) for item in history),
            "source": "GDELT 2.1 GKG archive proxy",
        })
    return records


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=parse_date, default=date(2015, 3, 1))
    parser.add_argument("--end", type=parse_date, default=date(2024, 6, 30))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/gdelt-economic-narrative"))
    parser.add_argument("--output", type=Path, default=Path("research/gdelt-economic-narrative-history.json"))
    args = parser.parse_args()

    requested = weeks(args.start, args.end)
    completed: dict[date, dict[str, object]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(fetch_sample, week, args.cache_dir): week for week in requested}
        for index, future in enumerate(as_completed(futures), start=1):
            week_end, result = future.result()
            completed[week_end] = result
            if index % 25 == 0 or index == len(futures):
                print(f"GDELT archive: {index}/{len(futures)} weeks", flush=True)

    ordered = sorted(completed.items())
    output = {
        "method": {
            "source": "GDELT 2.1 Global Knowledge Graph archive files",
            "archive_root": ARCHIVE_ROOT,
            "sample": "one rotating 15-minute snapshot per week",
            "rolling_window_weeks": 4,
            "classification": "major-economy economic themes; crisis if tone <= -5 or stress theme and tone <= -1.5",
            "limitation": "Systematic archive sample and GKG theme/tone proxy, not a full-text census.",
        },
        "records": rolling_records(ordered),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(output['records'])} weeks to {args.output}")


if __name__ == "__main__":
    main()
