#!/usr/bin/env python3
"""Build factual weekly de-escalation indicators from public data.

Financial prices come from Yahoo Finance's public chart endpoint. Text signals
come from Google News RSS. The script uses only observations available up to
each week and can replace the marked snapshots array in the standalone dashboard.
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
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
USER_AGENT = "Kazakhstan-development-bank-risk-research/1.0"

MARKET_SERIES = {
    "eurpln": "EURPLN=X",
    "eurhuf": "EURHUF=X",
    "eurczk": "EURCZK=X",
    "vstoxx": "^VIX",
    "eurusd": "EURUSD=X",
    "cee_equity": "EPOL",
    "europe_equity": "VGK",
    "rheinmetall": "RHM.DE",
    "saab": "SAAB-B.ST",
    "leonardo": "LDO.MI",
    "thales": "HO.PA",
    "bae": "BA.L",
}

GENERAL_QUERY = 'Ukraine Russia (ceasefire OR truce OR negotiations OR "peace talks")'
RUSSIAN_ACTORS = ("russia", "russian", "moscow", "kremlin", "putin", "lavrov")
UKRAINIAN_ACTORS = ("ukraine", "ukrainian", "kyiv", "kiev", "zelensky", "zelenskiy", "zelenskyy")
CONSTRUCTIVE_TERMS = (
    "agree", "accept", "ready", "propos", "support", "open to", "progress", "resume talks",
    "talks resume", "ceasefire holds", "truce holds", "framework", "deal reached", "signed",
    "commit", "welcome", "peace plan",
)
ADVERSE_TERMS = (
    "reject", "unwilling", "violat", "collapse", "fail", "snub", "no ceasefire", "attack",
    "strike", "war ", "offensive", "breakdown", "shatter", "accus",
)
SPECIFIC_TERMS = (
    "agreement", "framework", "memorandum", "monitoring", "guarantee", "deadline",
    "prisoner exchange", "corridor", "signed", "terms", "proposal", "partial ceasefire",
    "energy ceasefire", "meeting date", "summit",
)


@dataclass(frozen=True)
class Week:
    end: date

    @property
    def start(self) -> date:
        return self.end - timedelta(days=6)


def request_json(url: str, params: dict[str, object] | None = None, retries: int = 3) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def unix_seconds(value: date) -> int:
    stamp = datetime.combine(value, datetime_time.min, tzinfo=timezone.utc)
    return int(stamp.timestamp())


def yahoo_history(symbol: str, start: date, end: date) -> dict[date, float]:
    payload = request_json(
        YAHOO_CHART.format(symbol=urllib.parse.quote(symbol, safe="")),
        {
            "period1": unix_seconds(start),
            "period2": unix_seconds(end + timedelta(days=1)),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        },
    )
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    indicators = result.get("indicators", {})
    adjusted = indicators.get("adjclose", [{}])[0].get("adjclose")
    closes = adjusted or indicators.get("quote", [{}])[0].get("close", [])
    history: dict[date, float] = {}
    for timestamp, close in zip(timestamps, closes):
        observed = datetime.fromtimestamp(timestamp, timezone.utc).date()
        if start <= observed <= end and close is not None and math.isfinite(float(close)):
            history[observed] = float(close)
    if not history:
        raise RuntimeError(f"Yahoo returned no prices for {symbol}")
    return history


def last_value(history: dict[date, float], on_or_before: date) -> tuple[date, float] | None:
    candidates = [item for item in history.items() if item[0] <= on_or_before]
    return max(candidates, default=None, key=lambda item: item[0])


def log_return(history: dict[date, float], week_end: date) -> float | None:
    current = last_value(history, week_end)
    previous = last_value(history, week_end - timedelta(days=7))
    if not current or not previous or current[1] <= 0 or previous[1] <= 0:
        return None
    return math.log(current[1] / previous[1])


def median_available(values: list[float | None]) -> float | None:
    available = [value for value in values if value is not None and math.isfinite(value)]
    return statistics.median(available) if available else None


def market_raw(prices: dict[str, dict[date, float]], week: Week) -> dict[str, float | None]:
    returns = {name: log_return(history, week.end) for name, history in prices.items()}
    cee_fx = median_available([-returns[name] if returns[name] is not None else None for name in ("eurpln", "eurhuf", "eurczk")])
    vstoxx = -returns["vstoxx"] if returns["vstoxx"] is not None else None
    eurusd = returns["eurusd"]
    cee_equity = None
    if returns["cee_equity"] is not None and returns["europe_equity"] is not None:
        cee_equity = returns["cee_equity"] - returns["europe_equity"]
    defense_return = median_available([returns[name] for name in ("rheinmetall", "saab", "leonardo", "thales", "bae")])
    peace_trade = None
    if defense_return is not None and returns["europe_equity"] is not None:
        peace_trade = -(defense_return - returns["europe_equity"])
    return {
        "cee_fx": cee_fx,
        "vstoxx": vstoxx,
        "eurusd": eurusd,
        "cee_equity": cee_equity,
        "peace_trade": peace_trade,
    }


def google_news_items(query: str, week: Week, retries: int = 3) -> list[dict[str, str]]:
    dated_query = f"{query} after:{week.start.isoformat()} before:{(week.end + timedelta(days=1)).isoformat()}"
    params = urllib.parse.urlencode({"q": dated_query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
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
        published = (node.findtext("pubDate") or "").strip()
        try:
            observed = parsedate_to_datetime(published).date()
        except (TypeError, ValueError):
            continue
        if not (week.start <= observed <= week.end):
            continue
        normalized = re.sub(r"\W+", " ", title.lower()).strip()
        if normalized:
            items[normalized] = {"title": title, "source": source, "date": observed.isoformat()}
    return list(items.values())


def cached_google_news_items(query: str, week: Week, cache_dir: Path | None) -> list[dict[str, str]]:
    if cache_dir is None:
        return google_news_items(query, week)
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
    cache_path = cache_dir / f"{week.end.isoformat()}-{query_hash}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    items = google_news_items(query, week)
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    temporary.replace(cache_path)
    return items


def classify_text_week(week: Week, cache_dir: Path | None) -> dict[str, float]:
    general = cached_google_news_items(GENERAL_QUERY, week, cache_dir)
    classified = []
    for item in general:
        title = re.sub(r"[^a-z0-9 ]+", " ", item["title"].lower())
        constructive = any(term in title for term in CONSTRUCTIVE_TERMS) and not any(
            term in title for term in ADVERSE_TERMS
        )
        classified.append(
            {
                "constructive": constructive,
                "russian_actor": any(term in title for term in RUSSIAN_ACTORS),
                "ukrainian_actor": any(term in title for term in UKRAINIAN_ACTORS),
                "specific": any(term in title for term in SPECIFIC_TERMS),
                "source": item["source"].lower(),
            }
        )
    total = len(classified)
    constructive = [item for item in classified if item["constructive"]]
    ru_count = sum(item["russian_actor"] for item in constructive)
    ua_count = sum(item["ukrainian_actor"] for item in constructive)
    balance = 0.0 if ru_count + ua_count == 0 else 2.0 * min(ru_count, ua_count) / (ru_count + ua_count)
    constructive_share = 0.0 if total == 0 else len(constructive) / total
    mutuality = 100.0 * balance * min(1.0, 3.0 * constructive_share)
    specific_count = sum(item["specific"] for item in classified)
    specificity = 0.0 if total == 0 else 100.0 * min(1.0, 2.0 * specific_count / total)
    constructive_sources = len({item["source"] for item in constructive if item["source"]})
    convergence = 100.0 * min(1.0, constructive_sources / 20.0) * min(1.0, 4.0 * constructive_share)
    return {
        "mutuality": mutuality,
        "specificity": specificity,
        "convergence": convergence,
        "general_articles": float(total),
        "constructive_articles": float(len(constructive)),
        "specific_articles": float(specific_count),
        "independent_sources": float(constructive_sources),
        "russian_signals": float(ru_count),
        "ukrainian_signals": float(ua_count),
    }


def text_raw(weeks: list[Week], cache_dir: Path | None = None, workers: int = 4) -> list[dict[str, float]]:
    output: list[dict[str, float] | None] = [None] * len(weeks)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(classify_text_week, week, cache_dir): index for index, week in enumerate(weeks)}
        for completed, future in enumerate(as_completed(futures), start=1):
            output[futures[future]] = future.result()
            if completed % 25 == 0 or completed == len(weeks):
                print(f"Google News: {completed}/{len(weeks)} weeks", flush=True)
    return [item for item in output if item is not None]


def robust_scores(values: list[float | None], lookback: int = 52, minimum: int = 26) -> list[float | None]:
    scores: list[float | None] = []
    smoothed: float | None = None
    normal = statistics.NormalDist()
    for index, value in enumerate(values):
        history = [item for item in values[max(0, index - lookback):index] if item is not None and math.isfinite(item)]
        if value is None or len(history) < minimum:
            scores.append(None)
            continue
        center = statistics.median(history)
        mad = statistics.median(abs(item - center) for item in history)
        scale = 1.4826 * mad
        if scale <= 1e-12:
            scale = statistics.pstdev(history) or 1.0
        z_score = max(-3.0, min(3.0, (value - center) / scale))
        raw_score = 100.0 * normal.cdf(z_score)
        smoothed = raw_score if smoothed is None else 0.4 * raw_score + 0.6 * smoothed
        scores.append(smoothed)
    return scores


def sunday_weeks(end: date, count: int) -> list[Week]:
    last_sunday = end - timedelta(days=(end.weekday() + 1) % 7)
    return [Week(last_sunday - timedelta(days=7 * offset)) for offset in reversed(range(count))]


def build_history(
    end: date,
    history_weeks: int,
    output_weeks: int,
    cache_dir: Path | None = None,
    news_workers: int = 4,
) -> dict:
    if output_weeks > history_weeks:
        raise ValueError("output_weeks cannot exceed history_weeks")
    weeks = sunday_weeks(end, history_weeks)
    market_start = weeks[0].start - timedelta(days=14)
    prices = {name: yahoo_history(symbol, market_start, weeks[-1].end) for name, symbol in MARKET_SERIES.items()}
    market = [market_raw(prices, week) for week in weeks]
    market_keys = ["cee_fx", "vstoxx", "eurusd", "cee_equity", "peace_trade"]
    text_keys = ["mutuality", "specificity", "convergence"]
    keys = market_keys + text_keys
    raw_columns = {key: [row[key] for row in market] for key in market_keys}
    score_columns = {key: robust_scores(raw_columns[key]) for key in market_keys}
    text_weeks = weeks[-output_weeks:]
    text = text_raw(text_weeks, cache_dir=cache_dir, workers=news_workers)
    snapshots = []
    first_output_index = len(weeks) - output_weeks
    for text_index, (week, text_row) in enumerate(zip(text_weeks, text)):
        market_index = first_output_index + text_index
        values = [score_columns[key][market_index] for key in market_keys]
        values.extend(text_row[key] for key in text_keys)
        if any(value is None for value in values):
            continue
        raw = {key: raw_columns[key][market_index] for key in market_keys}
        raw.update({key: text_row[key] for key in text_keys})
        raw.update(
            {
                "general_articles": text_row["general_articles"],
                "constructive_articles": text_row["constructive_articles"],
                "specific_articles": text_row["specific_articles"],
                "independent_sources": text_row["independent_sources"],
                "russian_signals": text_row["russian_signals"],
                "ukrainian_signals": text_row["ukrainian_signals"],
            }
        )
        snapshots.append({"date": week.end.isoformat(), "values": [round(float(value), 1) for value in values], "raw": raw})
    if len(snapshots) < output_weeks:
        raise RuntimeError(f"Only {len(snapshots)} complete weeks were produced; need {output_weeks}")
    snapshots = snapshots[-output_weeks:]
    for snapshot in snapshots:
        snapshot["source"] = "factual"
    latest_market_dates = {name: max(history).isoformat() for name, history in prices.items()}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "factual_open_data",
        "axes": keys,
        "snapshots": snapshots,
        "latest_market_dates": latest_market_dates,
        "sources": {
            "market": "Yahoo Finance public chart endpoint",
            "text": "Google News RSS",
            "market_symbols": MARKET_SERIES,
            "news_queries": {
                "general": GENERAL_QUERY,
            },
        },
        "method": {
            "financial": "Weekly log returns; direction aligned so higher means de-escalation; robust trailing 52-week z-score using only prior observations; normal-CDF score; EWMA 0.4.",
            "text": "Title-level classification of one deduplicated weekly Google News RSS corpus: balanced constructive signals, concrete-term share, and constructive independent-source breadth.",
        },
    }


def javascript_snapshots(snapshots: list[dict]) -> str:
    reduced = [{"date": item["date"], "values": item["values"], **({"source": item["source"]} if "source" in item else {})} for item in snapshots]
    return json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))


def update_dashboard(path: Path, snapshots: list[dict]) -> None:
    content = path.read_text(encoding="utf-8")
    pattern = re.compile(r"(/\* DEESCALATION_DATA_START \*/).*?(/\* DEESCALATION_DATA_END \*/)", re.DOTALL)
    replacement = rf"\1 {javascript_snapshots(snapshots)} \2"
    updated, count = pattern.subn(replacement, content, count=1)
    if count != 1:
        raise RuntimeError(f"Could not find one de-escalation data marker pair in {path}")
    path.write_text(updated, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today(), help="Last calendar date, YYYY-MM-DD")
    parser.add_argument("--start", type=date.fromisoformat, help="First output date; rounded forward to Sunday")
    parser.add_argument("--history-weeks", type=int, default=90, help="Weeks fetched before trimming")
    parser.add_argument("--output-weeks", type=int, default=9, help="Complete weeks written to the dashboard")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/deescalation-google-news"))
    parser.add_argument("--news-workers", type=int, default=4, help="Concurrent Google News requests")
    parser.add_argument("--output", type=Path, default=Path("research/deescalation-history.json"))
    parser.add_argument("--dashboard", type=Path, help="Optional standalone HTML file to update")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    history_weeks, output_weeks = args.history_weeks, args.output_weeks
    if args.start:
        first_sunday = args.start + timedelta(days=(6 - args.start.weekday()) % 7)
        last_sunday = args.end - timedelta(days=(args.end.weekday() + 1) % 7)
        if first_sunday > last_sunday:
            raise ValueError("start must precede the last complete Sunday")
        output_weeks = (last_sunday - first_sunday).days // 7 + 1
        history_weeks = max(history_weeks, output_weeks + 52)
    payload = build_history(
        args.end,
        history_weeks,
        output_weeks,
        cache_dir=args.cache_dir,
        news_workers=args.news_workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.dashboard:
        update_dashboard(args.dashboard, payload["snapshots"])
    print(f"Wrote {len(payload['snapshots'])} factual weekly snapshots to {args.output}")


if __name__ == "__main__":
    main()
