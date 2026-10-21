#!/usr/bin/env python3
"""Build the weekly economic risk index and a causal historical backtest.

The workbook contains Bloomberg exports without headers: column A is the
observation date and column B is the value. Monthly releases are shifted by a
conservative 14-day publication lag before they can affect the index.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score


ROOT = Path(__file__).resolve().parent
DEFAULT_WORKBOOK = ROOT / "Blackswan.xlsx"
DEFAULT_REOK = ROOT / "REOK.xlsx"
DEFAULT_OUTPUT = ROOT / "economic-index-history.json"
DEFAULT_BACKTEST = ROOT / "economic-index-backtest.json"
DEFAULT_REPORT = ROOT / "economic-index-backtest.md"
DEFAULT_DASHBOARD = ROOT.parent / "dashboard" / "black-swan-dashboard.html"

COMPONENT_WEIGHTS = {"F": 0.27, "T": 0.25, "X": 0.18, "D": 0.15, "N": 0.15}
ALERT_THRESHOLD = 70.0
REFERENCE_WEEKS = 260
MIN_REFERENCE_WEEKS = 52

EVENTS = [
    ("2008-09-15", "Банкротство Lehman Brothers"),
    ("2011-08-05", "Понижение рейтинга США и пик долгового кризиса Европы"),
    ("2014-11-27", "Решение OPEC и ускорение обвала цен на нефть"),
    ("2015-08-11", "Девальвация юаня и глобальная распродажа"),
    ("2020-03-11", "Объявление пандемии COVID-19"),
    ("2022-02-24", "Эскалация конфликта Россия–Украина"),
    ("2023-03-10", "Крах Silicon Valley Bank"),
    ("2024-08-05", "Глобальный carry unwind и всплеск волатильности"),
]


def read_two_column_sheet(workbook: Path, sheet: str) -> pd.Series:
    frame = pd.read_excel(
        workbook,
        sheet_name=sheet,
        header=None,
        usecols=[0, 1],
        names=["date", "value"],
        engine="openpyxl",
    )
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna().sort_values("date").drop_duplicates("date", keep="last")
    return frame.set_index("date")["value"].astype(float)


def read_reok(workbook: Path) -> pd.Series:
    frame = pd.read_excel(
        workbook,
        sheet_name="Ежемесячные",
        header=None,
        usecols=[1, 2],
        skiprows=3,
        names=["date", "value"],
        engine="openpyxl",
    )
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna().sort_values("date").drop_duplicates("date", keep="last")
    return frame.set_index("date")["value"].astype(float)


def asof_weekly(series: pd.Series, weeks: pd.DatetimeIndex, lag_days: int = 0) -> pd.Series:
    shifted = series.copy()
    shifted.index = shifted.index + pd.Timedelta(days=lag_days)
    combined = shifted.index.union(weeks)
    return shifted.reindex(combined).sort_index().ffill().reindex(weeks)


def last_weekly(series: pd.Series, weeks: pd.DatetimeIndex) -> pd.Series:
    return asof_weekly(series, weeks)


def causal_percentile(
    series: pd.Series,
    window: int = REFERENCE_WEEKS,
    min_periods: int = MIN_REFERENCE_WEEKS,
) -> pd.Series:
    values = series.to_numpy(dtype=float)
    result = np.full(len(values), np.nan)
    for index, value in enumerate(values):
        if not np.isfinite(value):
            continue
        history = values[max(0, index - window) : index]
        history = history[np.isfinite(history)]
        if len(history) < min_periods:
            result[index] = 50.0
            continue
        below = np.count_nonzero(history < value)
        equal = np.count_nonzero(history == value)
        percentile = 100.0 * (below + 0.5 * equal) / len(history)
        result[index] = float(np.clip(percentile, 1.0, 99.0))
    return pd.Series(result, index=series.index)


def weighted_score(signals: dict[str, tuple[pd.Series, float]]) -> pd.Series:
    numerator = None
    denominator = None
    for series, weight in signals.values():
        valid = series.notna().astype(float)
        contribution = series.fillna(0.0) * weight
        numerator = contribution if numerator is None else numerator + contribution
        denominator = valid * weight if denominator is None else denominator + valid * weight
    return (numerator / denominator.replace(0.0, np.nan)).clip(1.0, 99.0)


def load_narrative(weeks: pd.DatetimeIndex) -> pd.Series:
    news_payload = json.loads((ROOT / "economic-narrative-stress.json").read_text())
    bank_payload = json.loads((ROOT / "central-bank-communication-stress.json").read_text())
    news = pd.Series(
        {pd.Timestamp(item["week_end"]): float(item["score"]) for item in news_payload["records"]}
    ).sort_index()
    bank = pd.Series(
        {
            pd.Timestamp(item["date"]) - pd.Timedelta(days=1): float(item["score"])
            for item in bank_payload["records"]
        }
    ).sort_index()
    news_weekly = asof_weekly(news, weeks)
    bank_weekly = asof_weekly(bank, weeks)
    combined = 0.70 * news_weekly + 0.30 * bank_weekly
    first_observed = min(news.index.min(), bank.index.min())
    combined.loc[combined.index < first_observed] = 50.0
    return combined.fillna(50.0).clip(1.0, 99.0)


def build_history(workbook: Path, reok_workbook: Path) -> tuple[pd.DataFrame, dict]:
    daily_names = [
        "VIX Index",
        "DXY Curncy",
        "Move Index",
        "US High Yield OAS",
        "Brent CO1",
        "Brent CO6",
        "USDKZT Curncy",
        "KASE Trading Volume",
        "OVX Index",
    ]
    monthly_names = [
        "GSCPI",
        "Межд. резервы КЗ",
        "CPI Kazakhstan",
        "CPI Russia",
        "CPI China",
        "CPI EU",
    ]
    raw = {name: read_two_column_sheet(workbook, name) for name in daily_names + monthly_names}
    last_observation = max(series.index.max() for series in raw.values())
    last_complete_sunday = last_observation.to_period("W-SUN").start_time - pd.Timedelta(days=1)
    weeks = pd.date_range("2006-01-08", last_complete_sunday, freq="W-SUN")

    weekly = {name: last_weekly(raw[name], weeks) for name in daily_names}
    monthly = {name: asof_weekly(raw[name], weeks, lag_days=14) for name in monthly_names}
    reok = asof_weekly(read_reok(reok_workbook), weeks, lag_days=45)

    usd_daily_returns = raw["USDKZT Curncy"].pct_change(fill_method=None)
    usd_realized_vol = usd_daily_returns.rolling(20, min_periods=10).std() * math.sqrt(252) * 100
    weekly_usd_vol = asof_weekly(usd_realized_vol, weeks)
    kase_weekly_volume = raw["KASE Trading Volume"].resample("W-SUN").sum().reindex(weeks)
    kase_volume_ratio = kase_weekly_volume / kase_weekly_volume.rolling(52, min_periods=13).median()

    # F: global financial conditions. Each source appears in this channel only.
    f_signals = {
        "vix": (causal_percentile(weekly["VIX Index"]), 0.30),
        "move": (causal_percentile(weekly["Move Index"]), 0.25),
        "hy_oas": (causal_percentile(weekly["US High Yield OAS"]), 0.25),
        "dxy_rise_13w": (causal_percentile(weekly["DXY Curncy"].pct_change(13)), 0.20),
    }
    financial = weighted_score(f_signals)

    # T: Kazakhstan terms of trade and external demand.
    brent_return_13w = weekly["Brent CO1"].pct_change(13)
    brent_contango = weekly["Brent CO6"] / weekly["Brent CO1"] - 1.0
    t_signals = {
        "brent_decline_13w": (causal_percentile(-brent_return_13w), 0.30),
        "oil_volatility": (causal_percentile(weekly["OVX Index"]), 0.20),
        "brent_contango": (causal_percentile(brent_contango), 0.15),
        "gscpi": (causal_percentile(monthly["GSCPI"]), 0.15),
        "china_disinflation": (causal_percentile(-monthly["CPI China"]), 0.20),
    }
    trade = weighted_score(t_signals)

    # X: KZT pressure, reserves and REER. REOK is December 2016 = 100.
    reok_stress = 0.50 * causal_percentile(reok) + 0.50 * causal_percentile(reok.pct_change(52))
    x_signals = {
        "usdkzt_depreciation_4w": (causal_percentile(weekly["USDKZT Curncy"].pct_change(4)), 0.35),
        "reserve_decline_13w": (causal_percentile(-monthly["Межд. резервы КЗ"].pct_change(13)), 0.25),
        "reok": (reok_stress, 0.20),
        "usdkzt_realized_volatility": (causal_percentile(weekly_usd_vol), 0.20),
    }
    fx = weighted_score(x_signals)

    peers = (
        0.30 * monthly["CPI Russia"]
        + 0.30 * monthly["CPI China"]
        + 0.40 * monthly["CPI EU"]
    )
    d_signals = {
        "kazakhstan_cpi_yoy": (causal_percentile(monthly["CPI Kazakhstan"]), 0.40),
        "cpi_acceleration_3m": (causal_percentile(monthly["CPI Kazakhstan"].diff(13)), 0.20),
        "inflation_differential": (
            causal_percentile(monthly["CPI Kazakhstan"] - peers),
            0.20,
        ),
        "usdkzt_market_volume": (causal_percentile(kase_volume_ratio), 0.20),
    }
    domestic = weighted_score(d_signals)
    narrative = load_narrative(weeks)

    history = pd.DataFrame(
        {"F": financial, "T": trade, "X": fx, "D": domestic, "N": narrative},
        index=weeks,
    ).fillna(50.0)
    history["baseline"] = sum(history[key] * weight for key, weight in COMPONENT_WEIGHTS.items())
    ordered = np.sort(history[["F", "T", "X", "D"]].to_numpy(), axis=1)[:, ::-1]
    history["H1"] = ordered[:, 0]
    history["H2"] = ordered[:, 1]
    history["composite"] = (
        0.50 * history["H1"] + 0.30 * history["H2"] + 0.20 * history["baseline"]
    ).clip(1.0, 99.0)

    metadata = {
        "frequency": "weekly, Sunday period end",
        "first_date": history.index.min().date().isoformat(),
        "last_date": history.index.max().date().isoformat(),
        "monthly_publication_lag_days": 14,
        "reok_publication_lag_days": 45,
        "normalization": (
            "Causal percentile against up to 260 prior weeks; first 52 weeks use neutral 50."
        ),
        "cpi_unit": "year-over-year percent change",
        "reserves_unit": "USD billions, National Fund excluded",
        "kase_volume_security": "USDKZT KAZ2 Curncy",
        "narrative_before_observed_history": 50,
        "formula": "BE=.27F+.25T+.18X+.15D+.15N; E=.50H1+.30H2+.20BE",
    }
    return history, metadata


def event_week(index: pd.DatetimeIndex, event_date: pd.Timestamp) -> pd.Timestamp:
    eligible = index[index <= event_date]
    return eligible[-1]


def contiguous_episodes(mask: pd.Series) -> int:
    starts = mask & ~mask.shift(1, fill_value=False)
    return int(starts.sum())


def backtest(history: pd.DataFrame) -> dict:
    score = history["composite"]
    pre_event = pd.Series(False, index=history.index)
    near_event = pd.Series(False, index=history.index)
    reaction = pd.Series(False, index=history.index)
    event_rows = []
    for date_text, name in EVENTS:
        date = pd.Timestamp(date_text)
        week = event_week(history.index, date)
        window = score.loc[(score.index >= week - pd.Timedelta(weeks=12)) & (score.index < week)]
        pre_event.loc[window.index] = True
        near_event.loc[
            (near_event.index >= week - pd.Timedelta(weeks=4)) & (near_event.index < week)
        ] = True
        reaction.loc[(reaction.index >= week) & (reaction.index <= week + pd.Timedelta(weeks=4))] = True
        peak_date = window.idxmax()
        peak = float(window.max())
        lead_weeks = max(1, int(round((week - peak_date).days / 7)))
        event_rows.append(
            {
                "event_date": date_text,
                "event": name,
                "event_week": week.date().isoformat(),
                "pre_event_peak": round(peak, 1),
                "peak_date": peak_date.date().isoformat(),
                "lead_weeks": lead_weeks,
                "warning_70": bool(peak >= ALERT_THRESHOLD),
                "score_at_event": round(float(score.loc[week]), 1),
            }
        )

    valid = score.index >= pd.Timestamp("2007-01-01")
    y_true = near_event.loc[valid].astype(int)
    y_score = score.loc[valid]
    auc = float(roc_auc_score(y_true, y_score))
    alert = score >= ALERT_THRESHOLD
    false_alert = alert & ~pre_event & ~reaction
    hits = sum(item["warning_70"] for item in event_rows)
    return {
        "method": {
            "warning_window_weeks": 12,
            "warning_window_excludes_event_week": True,
            "threshold": ALERT_THRESHOLD,
            "reaction_exclusion_weeks": 4,
            "auc_window_weeks": 4,
            "auc_start": "2007-01-01",
        },
        "summary": {
            "events_warned": hits,
            "events_total": len(event_rows),
            "event_hit_rate": round(hits / len(event_rows), 3),
            "roc_auc_4w": round(auc, 3),
            "alert_weeks": int(alert.loc[valid].sum()),
            "false_alert_weeks": int(false_alert.loc[valid].sum()),
            "false_alert_episodes": contiguous_episodes(false_alert.loc[valid]),
            "weeks_tested": int(valid.sum()),
        },
        "events": event_rows,
    }


def history_payload(history: pd.DataFrame, metadata: dict) -> dict:
    records = []
    for date, row in history.iterrows():
        records.append(
            {
                "date": date.date().isoformat(),
                "values": [round(float(row[key]), 1) for key in ["F", "T", "X", "D", "N"]],
                "baseline": round(float(row["baseline"]), 1),
                "h1": round(float(row["H1"]), 1),
                "h2": round(float(row["H2"]), 1),
                "composite": round(float(row["composite"]), 1),
                "source": "factual" if date >= pd.Timestamp("2024-01-01") else "factual-neutral-narrative",
            }
        )
    return {"method": metadata, "records": records}


def markdown_report(backtest_result: dict, metadata: dict) -> str:
    summary = backtest_result["summary"]
    lines = [
        "# Бэк-тест экономического индекса",
        "",
        "Расчет использует только информацию, доступную на соответствующую историческую дату.",
        "Месячные показатели вводятся с консервативным лагом публикации 14 дней, РЭОК — 45 дней.",
        "",
        "## Итог",
        "",
        f"- Предупреждение при пороге 70: **{summary['events_warned']} из {summary['events_total']} событий**.",
        f"- Event hit rate: **{summary['event_hit_rate']:.1%}**.",
        f"- ROC AUC для четырех недель непосредственно перед событием: **{summary['roc_auc_4w']:.3f}**.",
        f"- Ложные тревоги: **{summary['false_alert_episodes']} эпизодов / {summary['false_alert_weeks']} недель**.",
        "",
        "## События",
        "",
        "| Событие | Пик до события | Дата пика | Лаг | Сигнал ≥70 | На событии |",
        "|---|---:|---|---:|:---:|---:|",
    ]
    for item in backtest_result["events"]:
        lines.append(
            f"| {item['event']} ({item['event_date']}) | {item['pre_event_peak']:.1f} | "
            f"{item['peak_date']} | {item['lead_weeks']} нед. | "
            f"{'да' if item['warning_70'] else 'нет'} | {item['score_at_event']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Ограничения",
            "",
            "- Нарративный компонент до начала текстовой истории равен нейтральным 50.",
            "- Бэк-тест проверяет режим рыночного и макроэкономического стресса, а не причинность события.",
            "- Порог не оптимизировался отдельно под перечисленные события.",
            "- USD/KZT implied volatility не используется: выгрузка заканчивается в мае 2014 года.",
            "- KAZ CDS остается диагностикой и не входит в ранний композит.",
            "",
            f"История: {metadata['first_date']} — {metadata['last_date']}.",
        ]
    )
    return "\n".join(lines) + "\n"


def update_dashboard(path: Path, records: list[dict]) -> None:
    html = path.read_text()
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    pattern = re.compile(
        r"(?<=/\* ECONOMIC_INDEX_DATA_START \*/).*?(?=/\* ECONOMIC_INDEX_DATA_END \*/)",
        re.DOTALL,
    )
    updated, count = pattern.subn(payload, html)
    if count != 1:
        raise RuntimeError(f"Expected one economic data marker in {path}, found {count}")
    path.write_text(updated)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--reok", type=Path, default=DEFAULT_REOK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--html", type=Path, default=DEFAULT_DASHBOARD)
    args = parser.parse_args()

    history, metadata = build_history(args.workbook, args.reok)
    backtest_result = backtest(history)
    payload = history_payload(history, metadata)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    args.backtest.write_text(json.dumps(backtest_result, ensure_ascii=False, indent=2))
    args.report.write_text(markdown_report(backtest_result, metadata))
    update_dashboard(args.html, payload["records"])
    print(json.dumps(backtest_result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
