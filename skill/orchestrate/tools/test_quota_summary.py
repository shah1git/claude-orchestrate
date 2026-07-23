"""CLI-тесты quota_summary.py на маленьком намеренно неоднородном JSONL.

Тест не берёт настоящий routing-log: он проверяет именно терпимость читателя к
исторической схеме, не превращая live-телеметрию в хрупкую фикстуру.
"""
import json
import subprocess
import sys
from pathlib import Path


TOOL = Path(__file__).parent / "quota_summary.py"


def write_log(tmp_path, rows):
    log = tmp_path / "routing-log.jsonl"
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    # Обрыв строки — нормальная авария append-only журнала, поэтому добавляем
    # его намеренно и проверяем, что остальные dispatch-записи не теряются.
    lines.append('{"date": "2026-07-99"')
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def run(log, *args):
    return subprocess.run(
        [sys.executable, str(TOOL), "--log", str(log), *args],
        capture_output=True, text=True,
    )


def sample_rows():
    return [
        {"date": "2026-07-04", "agent": "critic", "model": "opus",
         "verdict": "PASS", "retries": 0, "escalated_to": None,
         "entry": "full", "tokens": 100},
        {"date": "2026-07-05", "agent": "builder", "model": "gpt-5",
         "verdict": "FAIL_FIXED", "retries": 2, "escalated_to": "opus",
         "entry": "frontier", "tokens": 50},
        # Старый вариант схемы: отсутствуют retries, entry и tokens.
        {"date": "2026-07-13", "role": "scout", "model": "haiku",
         "verdict": "PASS"},
        {"agent": "mystery", "model": "strange-engine", "verdict": "FAIL",
         "retries": 1, "entry": "full", "tokens": "n/a"},
    ]


def test_text_report_has_all_eight_blocks_and_visible_gaps(tmp_path):
    result = run(write_log(tmp_path, sample_rows()))

    assert result.returncode == 0, result.stderr
    for block in range(1, 9):
        assert f"{block}." in result.stdout
    assert "битых строк: 1" in result.stdout
    assert "без даты: 1" in result.stdout
    assert "?: 1 (25.0%)" in result.stdout
    assert "неопознанные модели: strange-engine" in result.stdout
    assert "доля Claude: 2/4 (50.0%)" in result.stdout
    assert "сумма retries: 3; записей с escalated_to: 1" in result.stdout
    assert ("сумма: 150; записей с полем tokens: 3, из них численных: 2"
            in result.stdout)
    assert "2026-W27: 1/2 (50.0%)" in result.stdout


def test_json_and_cutoff_produce_two_periods_without_invalid_json(tmp_path):
    log = write_log(tmp_path, sample_rows())
    full_result = run(log, "--json")
    assert full_result.returncode == 0, full_result.stderr
    tokens = json.loads(full_result.stdout)["periods"][0]["metrics"]["tokens"]
    assert tokens["token_records"] == 3
    assert tokens["token_records_numeric"] == 2

    result = run(log, "--cutoff", "2026-07-10", "--json")

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["broken_lines"] == 1
    assert data["without_date"] == 1
    assert data["cutoff"] == "2026-07-10"
    assert [period["name"] for period in data["periods"]] == [
        "до 2026-07-10", "с 2026-07-10"]
    before, after = data["periods"]
    assert before["metrics"]["records"] == 2
    assert after["metrics"]["records"] == 1
    assert before["metrics"]["claude_share"] == {"count": 1, "percent": 50.0}
    assert after["metrics"]["claude_tiers"][3] == {
        "name": "haiku", "count": 1, "percent": 100.0}
