#!/usr/bin/env python3
"""Build a weekly economic narrative stress indicator from public news.

The historical segment uses the sampled GDELT 2.1 GKG archive proxy; the current
segment uses dated Google News RSS results. Scores are causal: each week is
normalized only against observations that were available before that week.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
USER_AGENT = "Kazakhstan-development-bank-risk-research/1.0"

ECONOMIES = "US OR China OR Eurozone OR Japan OR Britain OR India OR Russia"
GENERAL_TERMS = "economy OR GDP OR growth OR inflation"
GENERAL_QUERY = f"({ECONOMIES}) ({GENERAL_TERMS})"
CRISIS_QUERIES = (
    f'({ECONOMIES}) (economy OR economic) (recession OR "hard landing" OR stagflation OR contraction)',
    f'({ECONOMIES}) (economy OR economic) ("financial crisis" OR "credit crunch" OR "negative outlook" OR downgrade)',
    f'({ECONOMIES}) (economy OR economic) ("banking crisis" OR "debt crisis" OR default OR "systemic risk")',
)
CRISIS_PATTERN = re.compile(
    r"\b(?:negative outlook|downgrad\w*|recession\w*|hard landing|financial crisis|banking crisis|"
    r"debt crisis|credit crunch|default\w*|systemic risk|stagflation|contraction|growth warning)\b",
    re.IGNORECASE,
)
DATA_START = "/* ECONOMIC_NARRATIVE_DATA_START */"
DATA_END = "/* ECONOMIC_NARRATIVE_DATA_END */"
DEFAULT_GDELT_HISTORY = Path("research/gdelt-economic-narrative-history.json")

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in", "is",
    "it", "of", "on", "or", "that", "the", "this", "to", "was", "will", "with",
}


@dataclass(frozen=True)
class Week:
    end: date

    @property
    def start(self) -> date:
        return self.end - timedelta(days=6)

    @property
    def dashboard_date(self) -> date:
        return self.end + timedelta(days=1)


def sundays(start: date, end: date) -> list[Week]:
    start += timedelta(days=(6 - start.weekday()) % 7)
    end -= timedelta(days=(end.weekday() - 6) % 7)
    return [Week(start + timedelta(days=7 * index)) for index in range((end - start).days // 7 + 1)]


def normalized_title(title: str, source: str) -> str:
    suffix = f" - {source}" if source else ""
    if suffix and title.lower().endswith(suffix.lower()):
        title = title[: -len(suffix)]
    return re.sub(r"[^a-z0-9 ]+", " ", title.lower()).strip()


def title_tokens(title: str) -> set[str]:
    return {token for token in title.split() if len(token) > 2 and token not in STOPWORDS}


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def deduplicate(items: list[dict[str, str]]) -> list[dict[str, str]]:
    clusters: list[tuple[set[str], dict[str, str]]] = []
    for item in sorted(items, key=lambda value: value["title"]):
        tokens = title_tokens(item["title"])
        if tokens and any(jaccard(tokens, existing) >= 0.72 for existing, _ in clusters):
            continue
        clusters.append((tokens, item))
    return [item for _, item in clusters]


def rss_items(query: str, week: Week, retries: int = 3) -> list[dict[str, str]]:
    dated = f"{query} after:{week.start.isoformat()} before:{(week.end + timedelta(days=1)).isoformat()}"
    params = urllib.parse.urlencode({"q": dated, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    request = urllib.request.Request(f"{GOOGLE_NEWS_RSS}?{params}", headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                root = ET.fromstring(response.read())
            break
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))

    items: dict[str, dict[str, str]] = {}
    for node in root.findall("./channel/item"):
        title = (node.findtext("title") or "").strip()
        source = (node.findtext("source") or "").strip()
        try:
            observed = parsedate_to_datetime((node.findtext("pubDate") or "").strip()).astimezone(timezone.utc).date()
        except (TypeError, ValueError):
            continue
        if not (week.start <= observed <= week.end):
            continue
        normalized = normalized_title(title, source)
        if normalized:
            items[normalized] = {"title": normalized, "source": source.lower(), "date": observed.isoformat()}
    return list(items.values())


def cached_items(query: str, week: Week, cache_dir: Path) -> list[dict[str, str]]:
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
    path = cache_dir / f"{week.end.isoformat()}-{query_hash}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    items = rss_items(query, week)
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return items


def calculate_week(week: Week, cache_dir: Path) -> dict[str, object]:
    general_raw = cached_items(GENERAL_QUERY, week, cache_dir)
    crisis_feeds = [cached_items(query, week, cache_dir) for query in CRISIS_QUERIES]
    crisis_raw_by_title = {
        item["title"]: item
        for feed in crisis_feeds
        for item in feed
        if CRISIS_PATTERN.search(item["title"])
    }
    crisis_raw = list(crisis_raw_by_title.values())
    general = deduplicate(general_raw)
    crisis = deduplicate(crisis_raw)
    general_sources = {item["source"] for item in general if item["source"]}
    crisis_sources = {item["source"] for item in crisis if item["source"]}
    article_share = min(1.0, len(crisis) / max(1, len(general)))
    source_share = min(1.0, len(crisis_sources) / max(1, len(general_sources)))
    intensity = 100.0 * math.sqrt(article_share * source_share)
    return {
        "date": week.dashboard_date.isoformat(),
        "week_end": week.end.isoformat(),
        "intensity": round(intensity, 4),
        "crisis_articles": len(crisis),
        "economic_articles": len(general),
        "crisis_sources": len(crisis_sources),
        "economic_sources": len(general_sources),
        "crisis_feed_capped": any(len(feed) >= 100 for feed in crisis_feeds),
        "economic_feed_capped": len(general_raw) >= 100,
    }


def causal_percentiles(values: list[float], lookback: int = 104, minimum: int = 26) -> list[float]:
    scores: list[float] = []
    for index, value in enumerate(values):
        history = values[max(0, index - lookback):index]
        if len(history) < minimum:
            scores.append(50.0)
            continue
        lower = sum(previous < value for previous in history)
        equal = sum(previous == value for previous in history)
        percentile = 100.0 * (lower + 0.5 * equal) / len(history)
        scores.append(min(99.0, max(1.0, percentile)))
    return scores


def score(records: list[dict[str, object]]) -> None:
    intensity = [float(record["intensity"]) for record in records]
    acceleration = []
    for index, value in enumerate(intensity):
        previous = intensity[max(0, index - 4):index]
        acceleration.append(value - statistics.median(previous) if previous else 0.0)
    level_scores = causal_percentiles(intensity)
    acceleration_scores = causal_percentiles(acceleration)
    for record, level, change in zip(records, level_scores, acceleration_scores):
        record["level_score"] = round(level, 1)
        record["acceleration_score"] = round(change, 1)
        record["score"] = round(0.75 * level + 0.25 * change, 1)


def median_absolute_deviation(values: list[float]) -> float:
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def hybrid_records(
    google_records: list[dict[str, object]],
    gdelt_path: Path,
    cutover: date,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    if not gdelt_path.exists():
        records = [dict(record, source="Google News RSS") for record in google_records]
        return records, {"mode": "Google News RSS only", "gdelt_history_found": False}

    payload = json.loads(gdelt_path.read_text(encoding="utf-8"))
    gdelt_records = payload.get("records", [])
    gdelt_by_week = {record["week_end"]: record for record in gdelt_records}
    google_by_week = {record["week_end"]: record for record in google_records}
    overlap_weeks = sorted(
        week for week in gdelt_by_week.keys() & google_by_week.keys()
        if parse_date(week) <= cutover
    )
    if len(overlap_weeks) < 13:
        raise RuntimeError("At least 13 overlap weeks are required to calibrate GDELT and Google News.")

    gdelt_log = [math.log1p(float(gdelt_by_week[week]["intensity"])) for week in overlap_weeks]
    google_log = [math.log1p(float(google_by_week[week]["intensity"])) for week in overlap_weeks]
    gdelt_center = statistics.median(gdelt_log)
    google_center = statistics.median(google_log)
    gdelt_mad = median_absolute_deviation(gdelt_log)
    google_mad = median_absolute_deviation(google_log)
    scale = gdelt_mad / google_mad if google_mad > 1e-9 else 1.0
    scale = min(4.0, max(0.25, scale))

    historical = [dict(record) for record in gdelt_records if parse_date(record["week_end"]) <= cutover]
    current = []
    for record in google_records:
        if parse_date(record["week_end"]) <= cutover:
            continue
        native_intensity = float(record["intensity"])
        mapped_log = gdelt_center + (math.log1p(native_intensity) - google_center) * scale
        mapped = max(0.0, math.expm1(mapped_log))
        current.append({
            **record,
            "native_intensity": round(native_intensity, 4),
            "intensity": round(mapped, 4),
            "source": "Google News RSS, calibrated to GDELT overlap",
        })
    combined = sorted(historical + current, key=lambda record: record["week_end"])
    return combined, {
        "mode": "GDELT archive before cutover; Google News RSS after cutover",
        "gdelt_history_found": True,
        "cutover_week_end": cutover.isoformat(),
        "overlap_weeks": len(overlap_weeks),
        "calibration": "robust log-intensity location/scale mapping using overlap medians and MAD",
        "google_log_scale": round(scale, 6),
        "gdelt_method": payload.get("method", {}),
    }


def update_dashboard(path: Path, records: list[dict[str, object]]) -> None:
    source = path.read_text(encoding="utf-8")
    start = source.find(DATA_START)
    end = source.find(DATA_END)
    if start < 0 or end < 0 or end <= start:
        raise RuntimeError(f"Narrative data markers not found in {path}")
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    updated = source[: start + len(DATA_START)] + payload + source[end:]
    temporary = path.with_suffix(".tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=parse_date, default=date(2024, 1, 1))
    parser.add_argument("--end", type=parse_date, default=date.today())
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/economic-narrative"))
    parser.add_argument("--output", type=Path, default=Path("research/economic-narrative-stress.json"))
    parser.add_argument("--dashboard", type=Path)
    parser.add_argument("--gdelt-history", type=Path, default=DEFAULT_GDELT_HISTORY)
    parser.add_argument("--gdelt-cutover", type=parse_date, default=date(2024, 6, 30))
    args = parser.parse_args()

    weeks = sundays(args.start, args.end)
    records: list[dict[str, object] | None] = [None] * len(weeks)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(calculate_week, week, args.cache_dir): index for index, week in enumerate(weeks)}
        for completed, future in enumerate(as_completed(futures), start=1):
            records[futures[future]] = future.result()
            if completed % 25 == 0 or completed == len(weeks):
                print(f"Google News: {completed}/{len(weeks)} weeks", flush=True)
    google_records = [record for record in records if record is not None]
    completed_records, hybrid_method = hybrid_records(google_records, args.gdelt_history, args.gdelt_cutover)
    score(completed_records)

    output = {
        "method": {
            "source": "GDELT 2.1 GKG archive and Google News RSS",
            "frequency": "weekly",
            "economies": ["United States", "China", "euro area", "Japan", "United Kingdom", "India", "Russia"],
            "formula": "N = 0.75 * causal percentile(intensity) + 0.25 * causal percentile(4-week acceleration)",
            "intensity": "100 * sqrt(crisis article share * crisis source share)",
            "hybrid": hybrid_method,
            "note": "GDELT history is a systematic GKG archive proxy; Google News RSS is capped and ranking-based.",
        },
        "records": completed_records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.dashboard:
        update_dashboard(args.dashboard, completed_records)
    latest = completed_records[-1]
    print(f"Latest: {latest['date']} score={latest['score']} intensity={latest['intensity']}")


if __name__ == "__main__":
    main()
