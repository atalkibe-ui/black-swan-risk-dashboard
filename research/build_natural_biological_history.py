#!/usr/bin/env python3
"""Build a weekly natural-biological risk index from open public data."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree


WHO_DON = "https://www.who.int/api/emergencies/diseaseoutbreaknews"
WHO_FLUNET = "https://xmart-api-public.who.int/FLUMART/VIW_FNT"
OPEN_METEO_FLOOD = "https://flood-api.open-meteo.com/v1/flood"
GOOGLE_NEWS = "https://news.google.com/rss/search"
USER_AGENT = "Kazakhstan-development-bank-risk-research/1.0"
DATA_START = "/* NATURAL_BIOLOGICAL_DATA_START */"
DATA_END = "/* NATURAL_BIOLOGICAL_DATA_END */"
ALERT_THRESHOLD = 70.0
EVENTS = [
    ("2009-06-11", "Объявление пандемии гриппа A(H1N1)"),
    ("2014-08-08", "WHO PHEIC: вспышка Ebola в Западной Африке"),
    ("2016-02-01", "WHO PHEIC: вирус Zika"),
    ("2020-01-30", "WHO PHEIC: новый коронавирус"),
    ("2022-07-23", "WHO PHEIC: глобальная вспышка mpox"),
    ("2024-04-06", "Крупнейшие паводки в Казахстане"),
    ("2024-08-14", "WHO PHEIC: новая вспышка mpox в Африке"),
]

COUNTRIES = {
    "KAZ": 0.40,
    "RUS": 0.20,
    "CHN": 0.20,
    "KGZ": 0.08,
    "UZB": 0.08,
    "TKM": 0.04,
}

# Coordinates were snapped to the nearest GloFAS river-network cell by selecting
# the highest-flow cell within roughly 20 km of each reference city.
BASINS = {
    "Esil / Petropavl": (54.67, 68.95),
    "Tobol / Kostanay": (53.41, 63.82),
    "Ural / Oral": (51.03, 51.27),
    "Irtysh / Pavlodar": (52.09, 76.95),
    "Syr Darya / Kyzylorda": (44.75, 65.60),
    "Ili / Qonaev": (43.97, 76.96),
}

NOVEL_TERMS = (
    "undetermined etiology", "unknown", "novel", "new variant", "new strain",
    "emerging", "unexplained", "disease x",
)
RESPIRATORY_TERMS = (
    "influenza", "coronavirus", "mers", "sars", "pneumonia", "respiratory",
)
SEVERE_TERMS = (
    "ebola", "marburg", "nipah", "plague", "cholera", "haemorrhagic", "hemorrhagic",
)
ZOONOTIC_TERMS = (
    "avian", "swine", "zoonotic", "mpox", "monkeypox", "rabies", "hantavirus",
)
REGIONAL_TERMS = (
    "kazakhstan", "russian federation", "russia", "china", "kyrgyzstan",
    "uzbekistan", "turkmenistan", "tajikistan", "mongolia", "afghanistan",
)
NEWS_QUERY = (
    '(outbreak OR epidemic) (ebola OR marburg OR nipah OR "lassa fever" OR plague '
    'OR "avian influenza" OR "bird flu" OR H5N1 OR "unknown disease" OR "novel virus")'
)
NEWS_SPLIT_QUERIES = (
    '(outbreak OR cases OR spread) (ebola OR marburg OR nipah OR "lassa fever")',
    '(outbreak OR cases OR spread) ("avian influenza" OR "bird flu" OR H5N1)',
    '(outbreak OR epidemic OR cases) (plague OR "unknown disease" OR "novel virus" OR "disease x")',
)
NEWS_PATHOGEN_TERMS = (
    "ebola", "marburg", "nipah", "lassa", "plague", "avian influenza", "bird flu",
    "h5n1", "h5n5", "unknown disease", "novel virus", "disease x",
)
NEWS_CONTEXT_TERMS = (
    "outbreak", "epidemic", "case", "death", "fatal", "spread", "transmission",
    "infected", "infection", "confirmed", "cluster", "surge", "strikes",
)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def request_json(url: str, cache_path: Path, retries: int = 3) -> object:
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.load(response)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache_path)
            return payload
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def request_text(url: str, cache_path: Path, retries: int = 3) -> str:
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read().decode("utf-8")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(cache_path)
            return payload
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def sundays(start: date, end: date) -> list[date]:
    start += timedelta(days=(6 - start.weekday()) % 7)
    end -= timedelta(days=(end.weekday() - 6) % 7)
    return [start + timedelta(days=7 * index) for index in range((end - start).days // 7 + 1)]


def causal_percentiles(values: list[float | None], lookback: int = 260, minimum: int = 52) -> list[float]:
    scores: list[float] = []
    for index, value in enumerate(values):
        history = [item for item in values[max(0, index - lookback):index] if item is not None]
        if value is None or len(history) < minimum:
            scores.append(50.0)
            continue
        lower = sum(previous < value for previous in history)
        equal = sum(previous == value for previous in history)
        scores.append(min(99.0, max(1.0, 100.0 * (lower + 0.5 * equal) / len(history))))
    return scores


def percentile(value: float, history: list[float], minimum: int = 8) -> float:
    if len(history) < minimum:
        return 50.0
    lower = sum(previous < value for previous in history)
    equal = sum(previous == value for previous in history)
    return min(99.0, max(1.0, 100.0 * (lower + 0.5 * equal) / len(history)))


def fetch_don(cache_dir: Path, history_start: date, end: date) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for skip in range(0, 4000, 100):
        query = {
            "sf_provider": "dynamicProvider372",
            "sf_culture": "en",
            "$top": "100",
            "$skip": str(skip),
            "$orderby": "PublicationDateAndTime desc",
            "$expand": "EmergencyEvent",
            "$select": "Title,regionscountries,ItemDefaultUrl,PublicationDateAndTime,EmergencyEvent",
        }
        payload = request_json(
            f"{WHO_DON}?{urllib.parse.urlencode(query)}",
            cache_dir / f"who-don-{end.isoformat()}-{skip}.json",
        )
        page = payload.get("value", [])
        if not page:
            break
        records.extend(page)
        oldest = datetime.fromisoformat(page[-1]["PublicationDateAndTime"].replace("Z", "+00:00")).date()
        if oldest < history_start:
            break
    return records


def outbreak_weight(title: str) -> float:
    lowered = title.lower()
    weight = 1.0
    weight += 2.5 if any(term in lowered for term in NOVEL_TERMS) else 0.0
    weight += 1.2 if any(term in lowered for term in RESPIRATORY_TERMS) else 0.0
    weight += 1.0 if any(term in lowered for term in SEVERE_TERMS) else 0.0
    weight += 0.8 if any(term in lowered for term in ZOONOTIC_TERMS) else 0.0
    weight += 1.5 if any(term in lowered for term in REGIONAL_TERMS) else 0.0
    weight += 0.5 if "multi-country" in lowered or "global" in lowered else 0.0
    return weight


def don_series(records: list[dict[str, object]], weeks: list[date]) -> tuple[list[float], list[float], list[int]]:
    reports = []
    for record in records:
        observed = datetime.fromisoformat(str(record["PublicationDateAndTime"]).replace("Z", "+00:00")).date()
        title = str(record.get("Title") or "")
        reports.append((observed, title, outbreak_weight(title)))
    levels: list[float] = []
    breadth: list[float] = []
    counts: list[int] = []
    for week in weeks:
        active = [(observed, title, weight) for observed, title, weight in reports if 0 <= (week - observed).days <= 56]
        levels.append(sum(weight * math.exp(-math.log(2) * (week - observed).days / 21.0) for observed, _, weight in active))
        event_keys = {
            (title.rsplit(" - ", 1)[-1] if " - " in title else title).strip().lower()
            for _, title, _ in active
        }
        breadth.append(float(len(event_keys)))
        counts.append(sum(week - timedelta(days=6) <= observed <= week for observed, _, _ in active))
    return levels, breadth, counts


def fetch_flunet(cache_dir: Path, history_start: date, end: date) -> dict[str, list[dict[str, object]]]:
    output = {}
    for country in COUNTRIES:
        query = {
            "$filter": f"COUNTRY_CODE eq '{country}' and ISO_WEEKSTARTDATE ge {history_start.isoformat()}",
            "$orderby": "ISO_WEEKSTARTDATE asc",
            "$top": "10000",
        }
        payload = request_json(
            f"{WHO_FLUNET}?{urllib.parse.urlencode(query)}",
            cache_dir / f"flunet-{country}-{history_start.isoformat()}-{end.isoformat()}.json",
        )
        output[country] = payload.get("value", [])
    return output


def flunet_weekly(rows: dict[str, list[dict[str, object]]]) -> dict[str, dict[date, float]]:
    output: dict[str, dict[date, float]] = {}
    for country, country_rows in rows.items():
        totals: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for row in country_rows:
            observed = date.fromisoformat(str(row["ISO_WEEKSTARTDATE"]))
            processed = float(row.get("SPEC_PROCESSED_NB") or 0)
            positive = row.get("INF_ALL")
            if positive is None:
                positive = float(row.get("INF_A") or 0) + float(row.get("INF_B") or 0)
            totals[observed][0] += processed
            totals[observed][1] += float(positive or 0)
        output[country] = {
            observed: min(1.0, positive / processed)
            for observed, (processed, positive) in totals.items()
            if processed >= 10
        }
    return output


def seasonal_percentile_series(series: dict[date, float], weeks: list[date]) -> list[float | None]:
    output: list[float | None] = []
    for week in weeks:
        monday = week - timedelta(days=6)
        value = series.get(monday)
        if value is None:
            output.append(None)
            continue
        iso_week = monday.isocalendar().week
        history = [
            previous
            for observed, previous in series.items()
            if observed < monday and abs(observed.isocalendar().week - iso_week) <= 2
        ]
        output.append(percentile(value, history))
    return output


def respiratory_score(rows: dict[str, list[dict[str, object]]], weeks: list[date]) -> tuple[list[float], list[float]]:
    weekly = flunet_weekly(rows)
    country_scores = {country: seasonal_percentile_series(values, weeks) for country, values in weekly.items()}
    scores = []
    coverage = []
    for index in range(len(weeks)):
        available = [(country_scores[country][index], weight) for country, weight in COUNTRIES.items() if country_scores[country][index] is not None]
        covered = sum(weight for _, weight in available)
        weighted = sum(float(value) * weight for value, weight in available)
        scores.append(weighted + 50.0 * (1.0 - covered))
        coverage.append(covered)
    return scores, coverage


def google_news_url(query: str, start: date, end: date) -> str:
    parameters = {
        "q": f"{query} after:{start.isoformat()} before:{end.isoformat()}",
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }
    return f"{GOOGLE_NEWS}?{urllib.parse.urlencode(parameters)}"


def parse_news_items(payload: str, start: date, end: date) -> list[tuple[str, str]]:
    output = []
    root = ElementTree.fromstring(payload)
    for item in root.findall("./channel/item"):
        published = item.findtext("pubDate")
        if not published:
            continue
        observed = parsedate_to_datetime(published).astimezone(timezone.utc).date()
        if not start <= observed < end:
            continue
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "unknown").strip()
        suffix = f" - {source}"
        if title.endswith(suffix):
            title = title[:-len(suffix)]
        lowered = title.lower()
        if not any(term in lowered for term in NEWS_PATHOGEN_TERMS):
            continue
        if not any(term in lowered for term in NEWS_CONTEXT_TERMS):
            continue
        output.append((title, source))
    return output


def fetch_news_week(cache_dir: Path, week: date) -> tuple[float, int, int, bool]:
    start = week - timedelta(days=6)
    end = week + timedelta(days=1)
    payload = request_text(
        google_news_url(NEWS_QUERY, start, end),
        cache_dir / f"google-news-{week.isoformat()}-all.xml",
    )
    root = ElementTree.fromstring(payload)
    raw_count = len(root.findall("./channel/item"))
    items = parse_news_items(payload, start, end)
    split_used = raw_count >= 100
    if split_used:
        items = []
        for query_index, query in enumerate(NEWS_SPLIT_QUERIES):
            split_payload = request_text(
                google_news_url(query, start, end),
                cache_dir / f"google-news-{week.isoformat()}-{query_index}.xml",
            )
            items.extend(parse_news_items(split_payload, start, end))

    unique: dict[tuple[str, str], tuple[str, str]] = {}
    for title, source in items:
        normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        unique[(normalized, source.lower())] = (title, source)

    weighted_count = 0.0
    sources = set()
    for title, source in unique.values():
        lowered = title.lower()
        weight = 1.0
        weight += 1.0 if any(term in lowered for term in ("unknown disease", "novel virus", "disease x", "human-to-human")) else 0.0
        weight += 0.6 if any(term in lowered for term in ("death", "deadly", "fatal")) else 0.0
        weight += 0.4 if any(term in lowered for term in ("spread", "surge", "transmission")) else 0.0
        weight += 0.2 if any(term in lowered for term in ("case", "confirmed", "infected")) else 0.0
        weighted_count += weight
        sources.add(source.lower())
    return weighted_count, len(unique), len(sources), split_used


def news_signal(
    cache_dir: Path, weeks: list[date], start: date, workers: int
) -> tuple[list[float], list[int], list[int], list[bool]]:
    indices = [index for index, week in enumerate(weeks) if week >= start]
    fetched: dict[int, tuple[float, int, int, bool]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_news_week, cache_dir, weeks[index]): index for index in indices}
        for future in as_completed(futures):
            fetched[futures[future]] = future.result()

    raw = [fetched[index][0] for index in indices]
    breadth = [float(fetched[index][2]) for index in indices]
    acceleration = [
        value - statistics.mean(raw[max(0, position - 4):position]) if position else 0.0
        for position, value in enumerate(raw)
    ]
    level_score = causal_percentiles(raw)
    breadth_score = causal_percentiles(breadth)
    acceleration_score = causal_percentiles(acceleration)
    scored = [
        0.55 * level + 0.25 * sources + 0.20 * change
        for level, sources, change in zip(level_score, breadth_score, acceleration_score)
    ]

    scores = [50.0] * len(weeks)
    counts = [0] * len(weeks)
    sources = [0] * len(weeks)
    split_used = [False] * len(weeks)
    for position, index in enumerate(indices):
        scores[index] = scored[position]
        counts[index] = fetched[index][1]
        sources[index] = fetched[index][2]
        split_used[index] = fetched[index][3]
    return scores, counts, sources, split_used


def fetch_flood(cache_dir: Path, history_start: date, end: date) -> dict[str, dict[date, float]]:
    query = {
        "latitude": ",".join(str(value[0]) for value in BASINS.values()),
        "longitude": ",".join(str(value[1]) for value in BASINS.values()),
        "daily": "river_discharge",
        "start_date": history_start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "UTC",
    }
    payload = request_json(
        f"{OPEN_METEO_FLOOD}?{urllib.parse.urlencode(query)}",
        cache_dir / f"glofas-{history_start.isoformat()}-{end.isoformat()}.json",
    )
    if isinstance(payload, dict):
        payload = [payload]
    output = {}
    for name, series in zip(BASINS, payload):
        output[name] = {
            date.fromisoformat(observed): float(value)
            for observed, value in zip(series["daily"]["time"], series["daily"]["river_discharge"])
            if value is not None
        }
    return output


def fetch_flood_forecast(cache_dir: Path) -> dict[str, list[float]]:
    query = {
        "latitude": ",".join(str(value[0]) for value in BASINS.values()),
        "longitude": ",".join(str(value[1]) for value in BASINS.values()),
        "daily": "river_discharge_max",
        "forecast_days": "30",
        "timezone": "UTC",
    }
    payload = request_json(
        f"{OPEN_METEO_FLOOD}?{urllib.parse.urlencode(query)}",
        cache_dir / f"glofas-forecast-{date.today().isoformat()}.json",
    )
    if isinstance(payload, dict):
        payload = [payload]
    return {
        name: [float(value) for value in series["daily"]["river_discharge_max"] if value is not None]
        for name, series in zip(BASINS, payload)
    }


def weekly_max(series: dict[date, float], weeks: list[date]) -> list[float | None]:
    return [
        max((series.get(week - timedelta(days=offset)) for offset in range(7) if series.get(week - timedelta(days=offset)) is not None), default=None)
        for week in weeks
    ]


def seasonal_ranks(basin_weekly: dict[str, list[float | None]], weeks: list[date], baseline_end: date) -> dict[int, float]:
    basin_profiles: dict[str, dict[int, float]] = {}
    for basin, values in basin_weekly.items():
        grouped: dict[int, list[float]] = defaultdict(list)
        for week, value in zip(weeks, values):
            if value is not None and week <= baseline_end:
                grouped[min(52, week.isocalendar().week)].append(value)
        means = {week_number: statistics.mean(observations) for week_number, observations in grouped.items()}
        ranked = {week_number: percentile(value, list(means.values()), minimum=26) for week_number, value in means.items()}
        basin_profiles[basin] = ranked
    raw = {}
    for week_number in range(1, 53):
        scores = sorted((profile.get(week_number, 50.0) for profile in basin_profiles.values()), reverse=True)
        raw[week_number] = 0.65 * scores[0] + 0.35 * scores[1]
    distribution = list(raw.values())
    return {week_number: percentile(value, distribution, minimum=26) for week_number, value in raw.items()}


def flood_scores(
    series: dict[str, dict[date, float]], forecast: dict[str, list[float]], weeks: list[date],
    seasonal_baseline_end: date,
) -> tuple[list[float], list[float], list[float], float]:
    basin_weekly = {basin: weekly_max(values, weeks) for basin, values in series.items()}
    basin_scores: dict[str, list[float]] = {basin: [] for basin in BASINS}
    for basin, values in basin_weekly.items():
        for index, (week, value) in enumerate(zip(weeks, values)):
            if value is None:
                basin_scores[basin].append(50.0)
                continue
            week_number = week.isocalendar().week
            seasonal_history = [
                previous
                for previous_week, previous in zip(weeks[:index], values[:index])
                if previous is not None and abs(min(52, previous_week.isocalendar().week) - min(52, week_number)) <= 2
            ]
            level = percentile(value, seasonal_history)
            previous = [item for item in values[max(0, index - 4):index] if item is not None]
            acceleration = value / max(1e-9, statistics.mean(previous)) if previous else 1.0
            acceleration_history = []
            for prior_index in range(4, index):
                prior_window = [item for item in values[max(0, prior_index - 4):prior_index] if item is not None]
                if values[prior_index] is not None and prior_window:
                    acceleration_history.append(float(values[prior_index]) / max(1e-9, statistics.mean(prior_window)))
            change = percentile(acceleration, acceleration_history, minimum=26)
            basin_scores[basin].append(0.75 * level + 0.25 * change)

    dynamic_raw = []
    for index in range(len(weeks)):
        scores = sorted((values[index] for values in basin_scores.values()), reverse=True)
        dynamic_raw.append(0.65 * scores[0] + 0.35 * scores[1])
    dynamic = causal_percentiles(dynamic_raw)

    season_profile = seasonal_ranks(basin_weekly, weeks, seasonal_baseline_end)
    seasonal = [season_profile[min(52, week.isocalendar().week)] for week in weeks]

    forecast_scores = []
    for basin, values in forecast.items():
        historical = [item for item in basin_weekly[basin] if item is not None]
        forecast_scores.append(percentile(max(values, default=0.0), historical, minimum=52))
    forecast_raw = 0.65 * sorted(forecast_scores, reverse=True)[0] + 0.35 * sorted(forecast_scores, reverse=True)[1]
    forecast_score = percentile(forecast_raw, dynamic_raw, minimum=52)

    combined = [0.20 * season + 0.80 * observed for season, observed in zip(seasonal, dynamic)]
    combined[-1] = 0.20 * seasonal[-1] + 0.60 * dynamic[-1] + 0.20 * forecast_score
    return combined, dynamic, seasonal, forecast_score


def earthquake_seasonality(week: date) -> float:
    # This is only a cold-weather consequence modifier, not an earthquake forecast.
    phase = 2.0 * math.pi * ((week - date(week.year, 1, 15)).days % 365) / 365.0
    return 20.0 + 60.0 * (1.0 + math.cos(phase)) / 2.0


def update_dashboard(path: Path, records: list[dict[str, object]]) -> None:
    source = path.read_text(encoding="utf-8")
    start = source.find(DATA_START)
    end = source.find(DATA_END)
    if start < 0 or end <= start:
        raise RuntimeError(f"Natural-biological data markers not found in {path}")
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(source[:start + len(DATA_START)] + payload + source[end:], encoding="utf-8")
    temporary.replace(path)


def event_week_index(records: list[dict[str, object]], event: date) -> int:
    eligible = [index for index, record in enumerate(records) if date.fromisoformat(str(record["date"])) <= event]
    if not eligible:
        raise ValueError(f"Event {event} precedes index history")
    return eligible[-1]


def contiguous_episodes(mask: list[bool]) -> int:
    return sum(value and (index == 0 or not mask[index - 1]) for index, value in enumerate(mask))


def roc_auc(labels: list[int], scores: list[float]) -> float:
    positives = [score for label, score in zip(labels, scores) if label]
    negatives = [score for label, score in zip(labels, scores) if not label]
    if not positives or not negatives:
        return float("nan")
    wins = sum(
        1.0 if positive > negative else 0.5 if positive == negative else 0.0
        for positive in positives for negative in negatives
    )
    return wins / (len(positives) * len(negatives))


def backtest(records: list[dict[str, object]]) -> dict[str, object]:
    scores = [float(record["composite"]) for record in records]
    dates = [date.fromisoformat(str(record["date"])) for record in records]
    pre_event = [False] * len(records)
    near_event = [False] * len(records)
    reaction = [False] * len(records)
    event_rows = []
    for event_text, name in EVENTS:
        event = date.fromisoformat(event_text)
        event_index = event_week_index(records, event)
        window = list(range(max(0, event_index - 12), event_index))
        for index in window:
            pre_event[index] = True
        for index in range(max(0, event_index - 4), event_index):
            near_event[index] = True
        for index in range(event_index, min(len(records), event_index + 5)):
            reaction[index] = True
        peak_index = max(window, key=lambda index: scores[index])
        peak = scores[peak_index]
        event_rows.append({
            "event_date": event_text,
            "event": name,
            "event_week": dates[event_index].isoformat(),
            "pre_event_peak": round(peak, 1),
            "peak_date": dates[peak_index].isoformat(),
            "lead_weeks": max(1, round((dates[event_index] - dates[peak_index]).days / 7)),
            "warning_70": peak >= ALERT_THRESHOLD,
            "score_at_event": round(scores[event_index], 1),
        })

    labels = [int(value) for value in near_event]
    alerts = [score >= ALERT_THRESHOLD for score in scores]
    false_alerts = [alert and not pre and not after for alert, pre, after in zip(alerts, pre_event, reaction)]
    hits = sum(bool(item["warning_70"]) for item in event_rows)
    sensitivity = {}
    for threshold in (70.0, 75.0, 80.0):
        threshold_alerts = [score >= threshold for score in scores]
        threshold_false = [
            alert and not pre and not after
            for alert, pre, after in zip(threshold_alerts, pre_event, reaction)
        ]
        threshold_hits = sum(float(item["pre_event_peak"]) >= threshold for item in event_rows)
        sensitivity[str(int(threshold))] = {
            "events_warned": threshold_hits,
            "event_hit_rate": round(threshold_hits / len(event_rows), 3),
            "alert_weeks": sum(threshold_alerts),
            "false_alert_weeks": sum(threshold_false),
            "false_alert_episodes": contiguous_episodes(threshold_false),
        }
    return {
        "method": {
            "warning_window_weeks": 12,
            "warning_window_excludes_event_week": True,
            "threshold": ALERT_THRESHOLD,
            "reaction_exclusion_weeks": 4,
            "auc_window_weeks": 4,
        },
        "summary": {
            "events_warned": hits,
            "events_total": len(event_rows),
            "event_hit_rate": round(hits / len(event_rows), 3),
            "roc_auc_4w": round(roc_auc(labels, scores), 3),
            "alert_weeks": sum(alerts),
            "false_alert_weeks": sum(false_alerts),
            "false_alert_episodes": contiguous_episodes(false_alerts),
            "weeks_tested": len(records),
        },
        "threshold_sensitivity": sensitivity,
        "events": event_rows,
    }


def markdown_report(result: dict[str, object], first_date: str, last_date: str) -> str:
    summary = result["summary"]
    lines = [
        "# Бэк-тест природно-биологического индекса",
        "",
        "Расчет причинный: нормирование каждой недели использует только предшествующие наблюдения.",
        "Паводковая сезонность зафиксирована по калибровочному периоду до начала отображаемой истории.",
        "",
        "## Итог",
        "",
        f"- Предупреждение при пороге 70: **{summary['events_warned']} из {summary['events_total']} событий**.",
        f"- Event hit rate: **{summary['event_hit_rate']:.1%}**.",
        f"- ROC AUC для четырех недель перед событием: **{summary['roc_auc_4w']:.3f}**.",
        f"- Ложные тревоги: **{summary['false_alert_episodes']} эпизодов / {summary['false_alert_weeks']} недель**.",
        "",
        "## События",
        "",
        "| Событие | Пик до события | Дата пика | Лаг | Сигнал ≥70 | На событии |",
        "|---|---:|---|---:|:---:|---:|",
    ]
    for item in result["events"]:
        lines.append(
            f"| {item['event']} ({item['event_date']}) | {item['pre_event_peak']:.1f} | "
            f"{item['peak_date']} | {item['lead_weeks']} нед. | "
            f"{'да' if item['warning_70'] else 'нет'} | {item['score_at_event']:.1f} |"
        )
    lines.extend([
        "",
        "## Чувствительность к порогу",
        "",
        "| Порог | Предупреждено событий | Hit rate | Ложные эпизоды | Ложные недели |",
        "|---:|---:|---:|---:|---:|",
    ])
    for threshold, item in result["threshold_sensitivity"].items():
        lines.append(
            f"| {threshold} | {item['events_warned']} из {summary['events_total']} | "
            f"{item['event_hit_rate']:.1%} | {item['false_alert_episodes']} | "
            f"{item['false_alert_weeks']} |"
        )
    lines.extend([
        "",
        "## Ограничения",
        "",
        "- Список событий задан до расчета метрик; веса и порог под него не оптимизировались.",
        "- Google News RSS является прокси интенсивности публикаций и зависит от архивной выдачи.",
        "- Сезонный сейсмический луч оценивает тяжесть возможных последствий, а не вероятность землетрясения.",
        "- Исторический прогноз GloFAS недоступен; прогнозный компонент применяется только к последнему срезу.",
        "",
        f"История: {first_date} — {last_date}.",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=parse_date, default=date(2008, 1, 1))
    parser.add_argument("--history-start", type=parse_date, default=date(2000, 1, 1))
    parser.add_argument("--end", type=parse_date, default=date.today())
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/natural-biological"))
    parser.add_argument("--output", type=Path, default=Path("research/natural-biological-history.json"))
    parser.add_argument("--backtest", type=Path, default=Path("research/natural-biological-backtest.json"))
    parser.add_argument("--report", type=Path, default=Path("research/natural-biological-backtest.md"))
    parser.add_argument("--dashboard", type=Path)
    parser.add_argument("--news-workers", type=int, default=10)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    history_start = args.history_start
    if history_start >= args.start:
        raise ValueError("history-start must precede start")
    if args.refresh and args.cache_dir.exists():
        for path in args.cache_dir.iterdir():
            if path.is_file():
                path.unlink()

    all_weeks = sundays(history_start, args.end)
    don = fetch_don(args.cache_dir, history_start, args.end)
    don_level, don_breadth, don_counts = don_series(don, all_weeks)
    level_score = causal_percentiles(don_level)
    breadth_score = causal_percentiles(don_breadth)
    acceleration_raw = [
        value - statistics.mean(don_level[max(0, index - 4):index]) if index else 0.0
        for index, value in enumerate(don_level)
    ]
    acceleration_score = causal_percentiles(acceleration_raw)
    official_outbreak = [
        0.55 * level + 0.25 * breadth + 0.20 * change
        for level, breadth, change in zip(level_score, breadth_score, acceleration_score)
    ]

    flu_rows = fetch_flunet(args.cache_dir, history_start, args.end)
    respiratory, flu_coverage = respiratory_score(flu_rows, all_weeks)
    news_start = args.start - timedelta(weeks=53)
    news, news_counts, news_sources, news_split = news_signal(
        args.cache_dir, all_weeks, news_start, args.news_workers
    )

    flood_series = fetch_flood(args.cache_dir, history_start, args.end)
    flood_forecast = fetch_flood_forecast(args.cache_dir)
    flood, flood_dynamic, flood_seasonal, forecast_score = flood_scores(
        flood_series, flood_forecast, all_weeks, args.start - timedelta(days=1)
    )

    records = []
    for index, week in enumerate(all_weeks):
        if week < args.start:
            continue
        quake = earthquake_seasonality(week)
        values = [
            round(official_outbreak[index], 1),
            round(respiratory[index], 1),
            round(news[index], 1),
            round(flood[index], 1),
            round(quake, 1),
        ]
        pandemic = 0.60 * values[0] + 0.25 * values[1] + 0.15 * values[2]
        base = 0.65 * pandemic + 0.25 * values[3] + 0.10 * values[4]
        composite = 0.55 * max(pandemic, values[3]) + 0.45 * base
        records.append({
            "date": week.isoformat(),
            "values": values,
            "composite": round(composite, 1),
            "pandemic": {
                "official_outbreak": round(official_outbreak[index], 1),
                "respiratory": round(respiratory[index], 1),
                "news": round(news[index], 1),
                "flunet_coverage": round(flu_coverage[index], 2),
                "don_reports_7d": don_counts[index],
                "news_mentions_7d": news_counts[index],
                "news_sources_7d": news_sources[index],
                "news_query_split": news_split[index],
            },
            "flood": {
                "dynamic": round(flood_dynamic[index], 1),
                "seasonality": round(flood_seasonal[index], 1),
                "forecast": round(forecast_score, 1) if index == len(all_weeks) - 1 else None,
            },
            "source": "factual",
        })

    output = {
        "method": {
            "formula": "P=0.60O+0.25R+0.15M; B_N=0.65P+0.25F+0.10Q; N=0.55*max(P,F)+0.45*B_N",
            "earthquake_note": "Q is a seasonal cold-weather consequence modifier only; it is not an earthquake forecast.",
            "news_note": "M is a cached Google News RSS search-intensity proxy; it has no provider SLA and is intentionally low-weighted.",
            "display_start": records[0]["date"],
            "calibration_start": history_start.isoformat(),
            "red_threshold": ALERT_THRESHOLD,
            "sources": ["WHO Disease Outbreak News", "WHO FluNet", "Google News RSS", "GloFAS via Open-Meteo"],
        },
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    backtest_result = backtest(records)
    args.backtest.write_text(json.dumps(backtest_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(
        markdown_report(backtest_result, records[0]["date"], records[-1]["date"]), encoding="utf-8"
    )
    if args.dashboard:
        update_dashboard(args.dashboard, records)
    print(json.dumps({"records": len(records), **backtest_result["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
