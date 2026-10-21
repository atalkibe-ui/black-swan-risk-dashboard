#!/usr/bin/env python3
"""Build a weekly stress signal from official Fed Chair and ECB President communications."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path


FED = "https://www.federalreserve.gov"
ECB = "https://www.ecb.europa.eu"
ECB_STATEMENTS = f"{ECB}/press/press_conference/monetary-policy-statement/html/index.en.html"
USER_AGENT = "Kazakhstan-development-bank-risk-research/1.0"
DATA_START = "/* CENTRAL_BANK_COMMUNICATION_DATA_START */"
DATA_END = "/* CENTRAL_BANK_COMMUNICATION_DATA_END */"

RISK_GROUPS = (
    (2.0, ("financial instability", "systemic risk", "market dysfunction", "bank failure", "debt crisis", "credit crunch")),
    (1.5, ("downside risk", "sharp slowdown", "hard landing", "recession", "severe stress", "liquidity strain", "funding stress")),
    (1.0, ("vulnerabil", "fragil", "deteriorat", "weaken", "contraction", "adverse scenario", "tail risk", "disorderly")),
    (0.7, ("uncertain", "restrictive", "policy restraint", "tight financial conditions", "geopolitical risk", "shock")),
)
MITIGATING = (
    "remain resilient", "remains resilient", "solid pace", "robust", "well capitalized", "contained",
    "risks have diminished", "risks have receded", "unlikely", "stable", "soft landing",
)


@dataclass(frozen=True)
class Document:
    institution: str
    observed: date
    title: str
    url: str


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav", "footer"}:
            self.skip += 1
        elif not self.skip and tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self.parts.append(". ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav", "footer"} and self.skip:
            self.skip -= 1
        elif not self.skip and tag in {"p", "h1", "h2", "h3", "li"}:
            self.parts.append(". ")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", html.unescape("".join(self.parts))).strip()


def request_text(url: str, cache_dir: Path, retries: int = 3) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[-180:]
    path = cache_dir / f"{key}.html"
    if path.exists():
        return path.read_text(encoding="utf-8")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                source = response.read().decode("utf-8", errors="replace")
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(source, encoding="utf-8")
            temporary.replace(path)
            return source
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def fed_documents(start_year: int, end_year: int, cache_dir: Path) -> list[Document]:
    documents: dict[str, Document] = {}
    for year in range(start_year, end_year + 1):
        for kind, path in (("speech", "speeches"), ("testimony", "testimony")):
            url = f"{FED}/newsevents/{kind}/{year}-{path}.htm"
            try:
                source = request_text(url, cache_dir)
            except Exception:
                continue
            # Each archive item can contain additional "watch live" links between
            # its document URL and speaker, so parse one event row at a time.
            for row in source.split('<div class="row">')[1:]:
                relative_match = re.search(rf'href="(/newsevents/{kind}/[^"?#]+\.htm)"', row, re.IGNORECASE)
                speaker_match = re.search(
                    r'<p class="news__speaker">(Chair(?:man|woman)?\s+[^<]+)</p>', row, re.IGNORECASE
                )
                if not relative_match or not speaker_match:
                    continue
                relative = relative_match.group(1)
                date_match = re.search(r"(20\d{6})", relative)
                if not date_match:
                    continue
                observed = date.fromisoformat(
                    f"{date_match.group(1)[:4]}-{date_match.group(1)[4:6]}-{date_match.group(1)[6:]}"
                )
                full_url = urllib.parse.urljoin(FED, relative)
                documents[full_url] = Document("Fed Chair", observed, speaker_match.group(1).strip(), full_url)
    return list(documents.values())


def ecb_documents(start_year: int, end_year: int, cache_dir: Path) -> list[Document]:
    source = request_text(ECB_STATEMENTS, cache_dir)
    snippets = set(re.findall(r'(\.\./20\d{2}/html/index_include\.en\.html)', source))
    snippets = {
        urllib.parse.urljoin(ECB_STATEMENTS, snippet)
        for snippet in snippets
        if start_year <= int(re.search(r"20\d{2}", snippet).group()) <= end_year
    }
    links: set[str] = set()
    for snippet in snippets:
        year_source = request_text(snippet, cache_dir)
        links.update(re.findall(
            r'href="([^"?#]*monetary-policy-statement/20\d{2}/html/ecb\.is[^"?#]+\.en\.html)"',
            year_source,
        ))
    documents = []
    for relative in links:
        date_match = re.search(r"ecb\.is(\d{6})", relative)
        if not date_match:
            continue
        compact = date_match.group(1)
        observed = date.fromisoformat(f"20{compact[:2]}-{compact[2:4]}-{compact[4:]}")
        documents.append(Document("ECB President", observed, "ECB monetary policy statement", urllib.parse.urljoin(ECB, relative)))
    return documents


def body_text(source: str, institution: str) -> str:
    if institution == "Fed Chair" and 'id="article"' in source:
        source = source[source.index('id="article"'):]
    elif institution == "ECB President" and "<main" in source:
        source = source[source.index("<main"):]
    extractor = TextExtractor()
    extractor.feed(source)
    text = extractor.text()
    text = re.split(r"\b(?:References|Related topics|Our website uses cookies)\b", text, maxsplit=1)[0]
    return text


def risk_density(text: str) -> tuple[float, int, float]:
    words = re.findall(r"[a-z]+", text.lower())
    if len(words) < 100:
        return 0.0, len(words), 0.0
    weighted_hits = 0.0
    for sentence in re.split(r"(?<=[.!?])\s+", text.lower()):
        sentence_hits = 0.0
        for weight, terms in RISK_GROUPS:
            sentence_hits += weight * sum(sentence.count(term) for term in terms)
        if sentence_hits and any(term in sentence for term in MITIGATING):
            sentence_hits *= 0.35
        weighted_hits += sentence_hits
    return 1000.0 * weighted_hits / len(words), len(words), weighted_hits


def mondays(start: date, end: date) -> list[date]:
    start += timedelta(days=(7 - start.weekday()) % 7)
    end -= timedelta(days=end.weekday())
    return [start + timedelta(days=7 * index) for index in range((end - start).days // 7 + 1)]


def rolling_raw(documents: list[dict[str, object]], week: date, institution: str) -> float | None:
    available = [
        document for document in documents
        if document["institution"] == institution and 0 <= (week - date.fromisoformat(str(document["date"]))).days <= 84
    ]
    if not available:
        return None
    weighted = []
    for document in available:
        age = (week - date.fromisoformat(str(document["date"]))).days
        weight = math.exp(-math.log(2) * age / 28.0)
        weighted.append((float(document["density"]), weight))
    return sum(value * weight for value, weight in weighted) / sum(weight for _, weight in weighted)


def causal_percentiles(values: list[float | None], lookback: int = 104, minimum: int = 26) -> list[float]:
    scores = []
    for index, value in enumerate(values):
        history = [item for item in values[max(0, index - lookback):index] if item is not None]
        if value is None or len(history) < minimum:
            scores.append(50.0)
            continue
        lower = sum(previous < value for previous in history)
        equal = sum(previous == value for previous in history)
        scores.append(min(99.0, max(1.0, 100.0 * (lower + 0.5 * equal) / len(history))))
    return scores


def build_records(documents: list[dict[str, object]], weeks: list[date]) -> list[dict[str, object]]:
    fed = [rolling_raw(documents, week, "Fed Chair") for week in weeks]
    ecb = [rolling_raw(documents, week, "ECB President") for week in weeks]
    combined = []
    for fed_value, ecb_value in zip(fed, ecb):
        available = [value for value in (fed_value, ecb_value) if value is not None]
        combined.append(sum(available) / len(available) if available else None)
    level = causal_percentiles(combined)
    acceleration_values = []
    for index, value in enumerate(combined):
        previous = [item for item in combined[max(0, index - 4):index] if item is not None]
        acceleration_values.append(value - sum(previous) / len(previous) if value is not None and previous else None)
    acceleration = causal_percentiles(acceleration_values)
    records = []
    for index, week in enumerate(weeks):
        score = 0.7 * level[index] + 0.3 * acceleration[index]
        records.append({
            "date": week.isoformat(),
            "score": round(score, 1),
            "level_score": round(level[index], 1),
            "acceleration_score": round(acceleration[index], 1),
            "fed_density": round(fed[index], 4) if fed[index] is not None else None,
            "ecb_density": round(ecb[index], 4) if ecb[index] is not None else None,
            "coverage": sum(value is not None for value in (fed[index], ecb[index])),
        })
    return records


def update_dashboard(path: Path, records: list[dict[str, object]]) -> None:
    source = path.read_text(encoding="utf-8")
    start = source.find(DATA_START)
    end = source.find(DATA_END)
    if start < 0 or end <= start:
        raise RuntimeError(f"Central-bank data markers not found in {path}")
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    updated = source[:start + len(DATA_START)] + payload + source[end:]
    temporary = path.with_suffix(".tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=parse_date, default=date(2024, 1, 1))
    parser.add_argument("--end", type=parse_date, default=date.today())
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/central-bank-communications"))
    parser.add_argument("--output", type=Path, default=Path("research/central-bank-communication-stress.json"))
    parser.add_argument("--dashboard", type=Path)
    args = parser.parse_args()

    discovered = fed_documents(args.start.year, args.end.year, args.cache_dir) + ecb_documents(
        args.start.year, args.end.year, args.cache_dir
    )
    discovered = sorted({document.url: document for document in discovered}.values(), key=lambda document: document.observed)
    scored_documents = []
    for index, document in enumerate(discovered, start=1):
        if not (args.start - timedelta(days=90) <= document.observed <= args.end):
            continue
        source = request_text(document.url, args.cache_dir)
        density, words, hits = risk_density(body_text(source, document.institution))
        scored_documents.append({
            "institution": document.institution,
            "date": document.observed.isoformat(),
            "title": document.title,
            "url": document.url,
            "density": round(density, 4),
            "words": words,
            "weighted_hits": round(hits, 2),
        })
        if index % 10 == 0:
            print(f"Documents: {index}/{len(discovered)}", flush=True)
    records = build_records(scored_documents, mondays(args.start, args.end))
    output = {
        "method": {
            "sources": ["Federal Reserve official speeches and testimony", "ECB monetary policy statements"],
            "window_days": 84,
            "half_life_days": 28,
            "formula": "CB = 0.70 * causal percentile(level) + 0.30 * causal percentile(4-week acceleration)",
        },
        "documents": scored_documents,
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.dashboard:
        update_dashboard(args.dashboard, records)
    print(f"Documents: {len(scored_documents)}; latest {records[-1]['date']} score={records[-1]['score']}")


if __name__ == "__main__":
    main()
