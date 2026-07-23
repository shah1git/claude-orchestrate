#!/usr/bin/env python3
"""Печать недельной квота-сводки из append-only routing-log.jsonl.

Журнал намеренно читается снисходительно: его схема менялась несколько раз,
а незавершённая последняя JSONL-строка не должна лишать лида всей картины.
Этот инструмент ничего не исправляет и не дописывает — он только показывает
наблюдаемые факты, включая пробелы и неизвестные модели.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any


# Путь вычисляется от самого инструмента, а не от cwd: weekly checkpoint часто
# запускают из другого worktree, но дефолт всё равно должен указывать на журнал
# именно этого checkout.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOG = REPO_ROOT / "skill/orchestrate/telemetry/routing-log.jsonl"

PROVIDER_RULES = (
    ("Claude", ("fable", "opus", "sonnet", "haiku", "claude")),
    ("OpenAI", ("codex", "gpt", "sol", "terra", "luna", "o3", "o4")),
    ("xAI", ("grok",)),
    ("Moonshot", ("kimi", "k2", "k3")),
    ("Google", ("gemini", "agy", "antigravity")),
)
CLAUDE_TIERS = ("fable", "opus", "sonnet", "haiku")


def label(value: Any) -> str:
    """Дать счётчикам явную метку даже для старого отсутствующего поля."""
    if value is None or value == "":
        return "∅"
    return str(value)


def parse_date(value: Any) -> date | None:
    """Вернуть только настоящую ISO-дату, не угадывая значения старых полей."""
    if not (isinstance(value, str) and len(value) == 10
            and value[4] == "-" and value[7] == "-"):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def provider_for(model: Any) -> str:
    """Классифицировать модель по закреплённым подстрочным правилам тикета."""
    name = label(model).casefold()
    for provider, fragments in PROVIDER_RULES:
        if any(fragment in name for fragment in fragments):
            return provider
    return "?"


def claude_tier_for(model: Any) -> str:
    """Тир — более узкая ось, чем провайдер; generic Claude остаётся non-claude."""
    name = label(model).casefold()
    for tier in CLAUDE_TIERS:
        if tier in name:
            return tier
    return "non-claude"


def number(value: Any) -> int | float | None:
    """Не принимать bool за число: в JSON это отдельный тип, хотя int-подкласс."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def percent(part: int | float, total: int) -> float:
    return round(100 * part / total, 1) if total else 0.0


def ordered(counter: Counter[str], limit: int | None = None) -> list[dict[str, Any]]:
    """Стабильный порядок нужен, чтобы JSON-сводки были сравнимы между неделями."""
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    if limit is not None:
        items = items[:limit]
    return [{"name": name, "count": count} for name, count in items]


def reject_nonstandard_json(value: str) -> None:
    """Не принимать NaN/Infinity: они допускаются Python, но не JSON-стандартом."""
    raise ValueError(f"недопустимая JSON-константа {value}")


def read_log(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Прочитать все целые object-строки и посчитать строки, которым нельзя верить."""
    rows: list[dict[str, Any]] = []
    broken = 0
    try:
        with path.open(encoding="utf-8") as stream:
            for raw in stream:
                if not raw.strip():
                    # Пустая разделительная строка не является оборванным JSON.
                    continue
                try:
                    value = json.loads(raw, parse_constant=reject_nonstandard_json)
                except (json.JSONDecodeError, ValueError):
                    broken += 1
                    continue
                if isinstance(value, dict):
                    rows.append(value)
                else:
                    # JSONL здесь означает "одна диспетчеризация = object";
                    # массив или скаляр не дают честной метрики и тоже битые.
                    broken += 1
    except OSError as exc:
        raise RuntimeError(f"не удалось прочитать журнал {path}: {exc}") from exc
    return rows, broken


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Собрать одну самодостаточную метрику периода из неоднородных записей."""
    providers: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    agents: Counter[str] = Counter()
    models: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    entries: Counter[str] = Counter()
    weeks: dict[str, Counter[str]] = defaultdict(Counter)
    unknown_models: set[str] = set()
    dates: list[date] = []
    retries = 0
    escalated = 0
    token_sum = 0
    token_records = 0
    token_records_numeric = 0

    for row in rows:
        model = label(row.get("model"))
        provider = provider_for(row.get("model"))
        providers[provider] += 1
        tiers[claude_tier_for(row.get("model"))] += 1
        agents[label(row.get("agent"))] += 1
        models[model] += 1
        verdicts[label(row.get("verdict"))] += 1
        entries[label(row.get("entry"))] += 1
        if provider == "?":
            unknown_models.add(model)

        retry_value = number(row.get("retries"))
        if retry_value is not None:
            retries += retry_value
        if row.get("escalated_to") is not None:
            escalated += 1
        if "tokens" in row:
            token_records += 1
            token_value = number(row["tokens"])
            if token_value is not None:
                token_records_numeric += 1
                token_sum += token_value

        row_date = parse_date(row.get("date"))
        if row_date is not None:
            dates.append(row_date)
            iso_year, iso_week, _ = row_date.isocalendar()
            week = f"{iso_year}-W{iso_week:02d}"
            weeks[week]["total"] += 1
            if provider == "Claude":
                weeks[week]["claude"] += 1

    total = len(rows)
    active_days = len(set(dates))
    provider_rows = [
        {**item, "percent": percent(item["count"], total)}
        for item in ordered(providers)
    ]
    # Все тиры печатаются даже с нулём: отсутствие Fable важно отличать от
    # отсутствующей строки в отчёте, когда настраивают квотную калибровку.
    tier_rows = [
        {"name": tier, "count": tiers[tier], "percent": percent(tiers[tier], total)}
        for tier in (*CLAUDE_TIERS, "non-claude")
    ]
    weekly = []
    for week in sorted(weeks):
        total_week = weeks[week]["total"]
        claude_week = weeks[week]["claude"]
        weekly.append({"week": week, "claude": claude_week, "total": total_week,
                       "percent": percent(claude_week, total_week)})

    # В ADR-0004 есть ровно три ожидаемых значения. Нулевые тоже показываем,
    # иначе "frontier не было" неотличим от "строку про frontier забыли".
    entry_names = ("frontier", "full", "∅") + tuple(
        name for name in sorted(entries) if name not in {"frontier", "full", "∅"})
    return {
        "records": total,
        "dated_records": len(dates),
        "date_range": ({"from": min(dates).isoformat(), "to": max(dates).isoformat()}
                       if dates else None),
        "active_days": active_days,
        # Бездатную запись нельзя честно отнести ко дню, поэтому она не
        # участвует в среднем; её отдельный счётчик остаётся на уровне отчёта.
        "records_per_active_day": round(len(dates) / active_days, 2) if active_days else 0.0,
        "providers": provider_rows,
        "claude_share": {"count": providers["Claude"],
                         "percent": percent(providers["Claude"], total)},
        "unknown_models": sorted(unknown_models),
        "claude_tiers": tier_rows,
        "fable_opus_share": {"count": tiers["fable"] + tiers["opus"],
                              "percent": percent(tiers["fable"] + tiers["opus"], total)},
        "top_agents": ordered(agents, 8),
        "top_models": ordered(models, 8),
        "verdicts": ordered(verdicts),
        "retries": retries,
        "escalated_records": escalated,
        "entries": [{"name": name, "count": entries[name]} for name in entry_names],
        "tokens": {"sum": token_sum, "token_records": token_records,
                   "token_records_numeric": token_records_numeric},
        "weekly_claude_share": weekly,
    }


def report(rows: list[dict[str, Any]], broken: int, cutoff: date | None,
           log_path: Path) -> dict[str, Any]:
    """Разделить журнал на сравнимые периоды, не приписывая бездатные записи."""
    dated = [(row, parse_date(row.get("date"))) for row in rows]
    without_date = sum(row_date is None for _, row_date in dated)
    if cutoff is None:
        periods = [{"name": "все записи", "metrics": metrics(rows)}]
    else:
        before = [row for row, row_date in dated if row_date is not None and row_date < cutoff]
        after = [row for row, row_date in dated if row_date is not None and row_date >= cutoff]
        periods = [
            {"name": f"до {cutoff.isoformat()}", "metrics": metrics(before)},
            {"name": f"с {cutoff.isoformat()}", "metrics": metrics(after)},
        ]
    return {
        "log": str(log_path),
        "broken_lines": broken,
        "without_date": without_date,
        "unknown_models": sorted({label(row.get("model")) for row in rows
                                  if provider_for(row.get("model")) == "?"}),
        "cutoff": cutoff.isoformat() if cutoff else None,
        "periods": periods,
    }


def count_text(items: list[dict[str, Any]], with_percent: bool = False) -> str:
    if not items:
        return "  — нет данных"
    lines = []
    for item in items:
        suffix = f" ({item['percent']:.1f}%)" if with_percent else ""
        lines.append(f"  {item['name']}: {item['count']}{suffix}")
    return "\n".join(lines)


def format_period(period: dict[str, Any]) -> str:
    """Сохранить восемь осей отчёта видимыми и в пустом периоде."""
    data = period["metrics"]
    date_range = data["date_range"]
    span = (f"{date_range['from']} — {date_range['to']}" if date_range else "нет дат")
    weekly = data["weekly_claude_share"]
    weekly_text = ("\n".join(
        f"  {item['week']}: {item['claude']}/{item['total']} ({item['percent']:.1f}%)"
        for item in weekly) if weekly else "  — нет датированных записей")
    unknown = ", ".join(data["unknown_models"]) or "нет"
    return "\n".join((
        f"Период: {period['name']}",
        "1. Объём",
        f"  записей: {data['records']}; диапазон дат: {span}; активных дней: "
        f"{data['active_days']}; записей/активный день: {data['records_per_active_day']:.2f}",
        "2. Провайдерный сплит",
        count_text(data["providers"], with_percent=True),
        f"  доля Claude: {data['claude_share']['count']}/{data['records']} "
        f"({data['claude_share']['percent']:.1f}%)",
        f"  неопознанные модели: {unknown}",
        "3. Тиры Claude",
        count_text(data["claude_tiers"], with_percent=True),
        f"  Fable+Opus от всех вызовов: {data['fable_opus_share']['count']}/"
        f"{data['records']} ({data['fable_opus_share']['percent']:.1f}%)",
        "4. Топ агентов (8) и моделей (8)",
        " агенты:", count_text(data["top_agents"]),
        " модели:", count_text(data["top_models"]),
        "5. Вердикты, ретраи и эскалации",
        count_text(data["verdicts"]),
        f"  сумма retries: {data['retries']}; записей с escalated_to: "
        f"{data['escalated_records']}",
        "6. Entry (ADR-0004)",
        count_text(data["entries"]),
        "7. Токены",
        f"  сумма: {data['tokens']['sum']}; записей с полем tokens: "
        f"{data['tokens']['token_records']}, из них численных: "
        f"{data['tokens']['token_records_numeric']}",
        "8. Понедельная динамика доли Claude (ISO-неделя)",
        weekly_text,
    ))


def format_text(data: dict[str, Any]) -> str:
    parts = [
        "Квота-чекпоинт",
        f"журнал: {data['log']}",
        f"битых строк: {data['broken_lines']}",
        f"без даты: {data['without_date']}",
        "неопознанные модели во всём журнале: "
        + (", ".join(data["unknown_models"]) or "нет"),
    ]
    if data["cutoff"]:
        parts.append("Записи без даты не входят в периоды сравнения.")
    parts.extend(format_period(period) for period in data["periods"])
    return "\n\n".join(parts)


def cutoff_arg(value: str) -> date:
    parsed = parse_date(value)
    if parsed is None:
        raise argparse.ArgumentTypeError("ожидается дата YYYY-MM-DD")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG,
                        help="путь к routing-log.jsonl")
    parser.add_argument("--cutoff", type=cutoff_arg,
                        help="дата: до неё и с неё будут отдельные периоды")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="вывести ту же сводку как JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rows, broken = read_log(args.log)
    except RuntimeError as exc:
        print(f"quota_summary: {exc}", file=sys.stderr)
        return 1
    data = report(rows, broken, args.cutoff, args.log)
    if args.as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(format_text(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
