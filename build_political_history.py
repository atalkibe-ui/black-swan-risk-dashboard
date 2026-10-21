#!/usr/bin/env python3
"""Build the Kazakhstan political risk index from factual open news data.

The direction is Kazakhstan-specific: de-escalation involving Russia or the
Strait of Hormuz raises the index, while escalation in other theatres and
direct Kazakhstan involvement raise it. Google News queries are dated by week;
normalization uses only observations available before the scored week.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from build_deescalation_history import Week, cached_google_news_items, robust_scores, sunday_weeks


ROOT = Path(__file__).resolve().parent
DEFAULT_RUSSIA = ROOT / "deescalation-history.json"
DEFAULT_OUTPUT = ROOT / "political-index-history.json"
DEFAULT_DASHBOARD = ROOT.parent / "dashboard" / "black-swan-dashboard.html"

QUERIES = {
    "hormuz_deescalation": (
        '"Strait of Hormuz" (ceasefire OR truce OR agreement OR de-escalation '
        'OR diplomacy OR talks OR reopen OR security)'
    ),
    "taiwan_escalation": (
        '(Taiwan OR "Taiwan Strait") (invasion OR blockade OR "military drills" '
        'OR missile OR attack OR war OR escalation)'
    ),
    "other_escalation": (
        '("armed conflict" OR "military escalation" OR invasion OR war OR blockade) '
        '(attack OR threat OR offensive OR tensions)'
    ),
    "kazakhstan_involvement": (
        'Kazakhstan (war OR conflict OR military OR sanctions OR CSTO OR border '
        'OR mobilization OR mobilisation)'
    ),
}

EXCLUDED_OTHER_THEATRES = (
    "russia", "russian", "ukraine", "ukrainian", "moscow", "kremlin",
    "israel", "israeli", "gaza", "iran", "iranian", "iraq", "syria",
    "yemen", "lebanon", "hormuz", "taiwan", "taiwanese",
)

HORMUZ_CONTEXT = ("hormuz",)
HORMUZ_CONSTRUCTIVE = (
    "ceasefire", "truce", "reopen", "reopening", "agreement", "deal", "diplomacy",
    "diplomatic", "talks", "negotiat", "de-escalat", "normalization", "normalisation",
    "safe passage", "security plan",
)
TAIWAN_CONTEXT = ("taiwan", "taiwanese", "taiwan strait")
TAIWAN_ESCALATION = (
    "china", "chinese", "beijing", "pla ", "blockade", "invasion", "war ", "warfare",
    "military drill", "military exercise", "missile", "attack", "coerc", "threat",
)
KAZAKHSTAN_CONTEXT = ("kazakhstan", "kazakh")
KAZAKHSTAN_DIRECT_RISK = (
    "sanction", "csto", "collective security", "troop", "deploy", "mobiliz", "mobilis",
    "invasion", "armed conflict", "military operation", "border clash", "border incident",
    "peacekeep", "war risk", "war threat", "attack on kazakhstan", "attack kazakhstan",
)
OTHER_ESCALATION = (
    "war ", "warfare", "invasion", "attack", "offensive", "blockade", "military",
    "armed conflict", "escalat", "clash", "missile", "drone strike", "threat",
)

WEIGHTS = [0.30, 0.20, 0.25, 0.15, 0.10]
CONFIDENCE_ARTICLES_4W = {
    "hormuz_deescalation": 12.0,
    "taiwan_escalation": 20.0,
    "other_escalation": 25.0,
    "kazakhstan_involvement": 6.0,
}


def russia_risk(snapshot: dict) -> float:
    values = snapshot["values"]
    market = sum(value * weight for value, weight in zip(values[:5], [0.375, 0.15, 0.15, 0.20, 0.125]))
    text = sum(value * weight for value, weight in zip(values[5:8], [0.46, 0.38, 0.16]))
    return 0.50 * math.sqrt(market * text) + 0.25 * market + 0.25 * text


def directional_volume(items: list[dict[str, str]], key: str) -> dict[str, float]:
    def contains(title: str, terms: tuple[str, ...]) -> bool:
        return any(term in title for term in terms)

    filtered = []
    for item in items:
        title = f" {item['title'].lower()} "
        relevant = False
        if key == "hormuz_deescalation":
            relevant = contains(title, HORMUZ_CONTEXT) and contains(title, HORMUZ_CONSTRUCTIVE)
        elif key == "taiwan_escalation":
            relevant = contains(title, TAIWAN_CONTEXT) and contains(title, TAIWAN_ESCALATION)
        elif key == "other_escalation":
            relevant = not contains(title, EXCLUDED_OTHER_THEATRES) and contains(title, OTHER_ESCALATION)
        elif key == "kazakhstan_involvement":
            relevant = contains(title, KAZAKHSTAN_CONTEXT) and contains(title, KAZAKHSTAN_DIRECT_RISK)
        if relevant:
            filtered.append(item)
    items = filtered
    sources = {item["source"].strip().lower() for item in items if item["source"].strip()}
    count = len(items)
    breadth = len(sources)
    # Log volume prevents a single crowded news week from dominating the history.
    signal = math.log1p(count) * (0.60 + 0.40 * min(20, breadth) / 20.0)
    return {"signal": signal, "articles": float(count), "sources": float(breadth)}


def fetch_week(week: Week, cache_dir: Path) -> dict[str, dict[str, float]]:
    result = {}
    for key, query in QUERIES.items():
        items = cached_google_news_items(query, week, cache_dir)
        result[key] = directional_volume(items, key)
    return result


def rolling_mean(values: list[float], window: int = 4) -> list[float]:
    return [sum(values[max(0, i - window + 1):i + 1]) / min(window, i + 1) for i in range(len(values))]


def composite(values: list[float]) -> float:
    baseline = sum(value * weight for value, weight in zip(values, WEIGHTS))
    return min(99.0, max(1.0, 0.65 * baseline + 0.35 * max(values)))


def build(russia_path: Path, cache_dir: Path, workers: int) -> dict:
    russia_payload = json.loads(russia_path.read_text(encoding="utf-8"))
    russia_snapshots = russia_payload["snapshots"]
    weeks = [Week(date.fromisoformat(item["date"])) for item in russia_snapshots]
    news_rows: list[dict[str, dict[str, float]] | None] = [None] * len(weeks)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_week, week, cache_dir): index for index, week in enumerate(weeks)}
        for completed, future in enumerate(as_completed(futures), start=1):
            news_rows[futures[future]] = future.result()
            if completed % 25 == 0 or completed == len(weeks):
                print(f"Political news: {completed}/{len(weeks)} weeks", flush=True)

    rows = [row for row in news_rows if row is not None]
    keys = list(QUERIES)
    score_columns = {}
    for key in keys:
        smoothed_raw = rolling_mean([row[key]["signal"] for row in rows])
        causal = robust_scores(smoothed_raw)
        article_counts = [row[key]["articles"] for row in rows]
        article_volume = [sum(article_counts[max(0, i - 3):i + 1]) for i in range(len(rows))]
        adjusted = []
        for value, volume in zip(causal, article_volume):
            if value is None:
                adjusted.append(50.0)
                continue
            confidence = min(1.0, volume / CONFIDENCE_ARTICLES_4W[key])
            adjusted.append(20.0 + (value - 20.0) * confidence)
        score_columns[key] = adjusted

    snapshots = []
    for index, (week, russia_snapshot, news_row) in enumerate(zip(weeks, russia_snapshots, rows)):
        values = [russia_risk(russia_snapshot)] + [score_columns[key][index] for key in keys]
        rounded = [round(float(value), 1) for value in values]
        snapshots.append(
            {
                "date": week.end.isoformat(),
                "values": rounded,
                "composite": round(composite(values), 1),
                "raw": {
                    "russia_deescalation": round(russia_risk(russia_snapshot), 4),
                    **{key: news_row[key] for key in keys},
                },
                "source": "factual",
            }
        )

    return {
        "generated_at": date.today().isoformat(),
        "status": "factual_open_data",
        "axes": [
            "russia_deescalation", "hormuz_deescalation", "taiwan_escalation",
            "other_escalation", "kazakhstan_involvement",
        ],
        "snapshots": snapshots,
        "sources": {
            "russia_deescalation": russia_payload["sources"],
            "directional_news": "Google News RSS",
            "queries": QUERIES,
        },
        "method": {
            "direction": (
                "Russia and Hormuz de-escalation increase Kazakhstan political risk; "
                "Taiwan, other-theatre escalation, and Kazakhstan involvement increase risk."
            ),
            "normalization": (
                "Four-week mean of title volume and independent-source breadth; robust trailing "
                "52-week score using prior observations only; sparse volume is attenuated toward "
                "20 and the first 26 weeks are neutral 50."
            ),
            "formula": "B=.30R+.20H+.25T+.15G+.10K; P=.65B+.35max(R,H,T,G,K)",
        },
    }


def update_dashboard(path: Path, snapshots: list[dict]) -> None:
    content = path.read_text(encoding="utf-8")
    reduced = [
        {"date": item["date"], "values": item["values"], "composite": item["composite"], "source": item["source"]}
        for item in snapshots
    ]
    payload = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
    pattern = re.compile(
        r"(?<=/\* DEESCALATION_DATA_START \*/).*?(?=/\* DEESCALATION_DATA_END \*/)",
        re.DOTALL,
    )
    updated, count = pattern.subn(f" {payload} ", content, count=1)
    if count != 1:
        raise RuntimeError(f"Expected one political data marker in {path}; found {count}")
    path.write_text(updated, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--russia", type=Path, default=DEFAULT_RUSSIA)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/political-google-news"))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    args = parser.parse_args()
    payload = build(args.russia, args.cache_dir, args.workers)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update_dashboard(args.dashboard, payload["snapshots"])
    latest = payload["snapshots"][-1]
    print(json.dumps({"weeks": len(payload["snapshots"]), "latest": latest}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
