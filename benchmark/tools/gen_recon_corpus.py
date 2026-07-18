#!/usr/bin/env python3
"""Детерминированный генератор корпуса + ключа для дисциплины RECON.

Зачем отдельная дисциплина: recon-лейн Kimi (k3, 1M контекст) оправдан ровно
на ОДНОМ — большой корпус, который не влезает в скромное окно. scout-фикстура
(depot/) для этого слишком мала. Здесь корпус синтезируется по правилу и
масштабируется тиром, а истина вычисляется тем же генератором — кандидату
недоступна (DESIGN §4).

Дисциплина — та же, что у scout: чисто механическая экстракция «мест возбуждения»
и точный ключ; грейд = precision/recall против ключа (порог 0.95). Отличие
единственное — РАЗМЕР входа:

  t1  ~40 файлов   (влезает в 256K — калибровочный, все тиры должны брать)
  t2  ~200 файлов  (за пределами скромного окна — cheap-тиры начинают ронять)
  t3  ~600 файлов  (только большой контекст держит корпус в одном проходе)

Корпус НЕ коммитится в git (объём): генератор детерминирован, харнесс
материализует корпус в staged workspace перед прогоном командой `corpus`.
Коммитятся только генератор и запечатанный ключ (seal_keys.sh).

Маркер (одна строка, как scout t1 `raise DepotError`):
    raise ReconError("RC-#####", "message ... {maybe_subst}")
Ключ — {file, line, code, message} по каждому маркеру, порядок: файл (алфавит),
затем строка (возр.). Детерминизм: фиксированный seed + чистая конструкция,
никакого сетевого/временного ввода.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

BM = Path(__file__).resolve().parent.parent
TASKS = BM / "tasks" / "recon"

# Тиры: файлы * функции — размер ВХОДА (контекст); marker_every — плотность
# маркеров (умеренный ВЫХОД, чтобы лимит выходных токенов не спутать с потерей
# контекста). Цель по входу: t1 влезает всюду; t2 давит 200K-окна (haiku/sonnet);
# t3 превышает 372K (luna) — держат только 1M-лейны (k3, gemini-flash).
TIERS = {
    "t1": {"files": 40, "funcs": 6, "marker_every": 5, "seed": 1101},
    "t2": {"files": 200, "funcs": 8, "marker_every": 8, "seed": 2202},
    "t3": {"files": 600, "funcs": 9, "marker_every": 12, "seed": 3303},
}

# Тот же извлекающий regex, что у scout t1 — одна строка, второй аргумент как
# записан между кавычками, без префикса f.
RAISE_RE = re.compile(
    r'raise\s+ReconError\(\s*"(?P<code>RC-\d{5})"\s*,\s*f?"(?P<msg>.*?)"\s*\)'
)

_WORDS = ("kit", "serial", "tariff", "ledger", "booking", "quarantine",
          "registry", "report", "compat", "window", "cursor", "batch",
          "mirror", "slice", "handle", "quota", "cap", "coef", "rate")


def _msg(rng: random.Random, code_n: int) -> str:
    w = rng.choice(_WORDS)
    # Половина сообщений — с f-подстановкой (проверяем точную экстракцию как в исходнике).
    if code_n % 2 == 0:
        return f"{w} operation {code_n} exceeds the configured {w}_limit of {{CAP_{w.upper()}}}"
    return f"{w} state for record {code_n} is not reconcilable"


def build(tier: str):
    """Возвращает (files: dict[relpath->text], key: list[dict])."""
    cfg = TIERS[tier]
    rng = random.Random(cfg["seed"])
    files: dict[str, str] = {}
    key: list[dict] = []
    code_seq = 0
    for fi in range(cfg["files"]):
        name = f"mod_{fi:04d}.py"
        lines = [f'"""Synthetic recon module {fi:04d} — filler, not real logic."""',
                 "from .errors import ReconError", ""]
        for fn in range(cfg["funcs"]):
            lines.append(f"def op_{fi:04d}_{fn}(x, y=0):")
            lines.append(f"    # deterministic filler to inflate context window")
            lines.append(f"    acc = x * {fn + 1} + {fi}")
            # Маркер сажаем по правилу marker_every, помечая точную строку.
            if (fi * cfg["funcs"] + fn) % cfg["marker_every"] == 0:
                code_seq += 1
                code = f"RC-{code_seq:05d}"
                msg = _msg(rng, code_seq)
                lines.append(f'    if acc < 0:')
                marker_line_idx = len(lines) + 1  # 1-based номер следующей строки
                lines.append(f'        raise ReconError("{code}", "{msg}")')
                key.append({"file": name, "line": marker_line_idx,
                            "code": code, "message": msg})
            lines.append(f"    return acc")
            lines.append("")
        files[name] = "\n".join(lines) + "\n"
    # errors.py — определение ReconError (НЕ место возбуждения; проверяет, что
    # кандидат не считает определение/импорты за маркер, как в scout BOUNDARIES).
    files["errors.py"] = (
        '"""Recon error type — declaration only, never a raise site."""\n'
        "class ReconError(Exception):\n"
        "    def __init__(self, code: str, message: str):\n"
        "        super().__init__(f'{code}: {message}')\n"
        "        self.code = code\n"
    )
    return files, key


def cmd_corpus(tier: str, out: Path):
    files, key = build(tier)
    dest = out / "corpus"
    dest.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (dest / rel).write_text(text)
    print(f"{tier}: {len(files)} файлов, {len(key)} маркеров -> {dest}")


def cmd_gen():
    total = 0
    for tier in TIERS:
        _, key = build(tier)
        hidden = TASKS / tier / "hidden"
        hidden.mkdir(parents=True, exist_ok=True)
        (hidden / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2))
        print(f"{tier}: ключ {len(key)} маркеров -> {hidden/'key.json'}")
        total += len(key)
    print(f"итого маркеров: {total}")


def cmd_grade(tier: str, cand_path: str):
    key = json.loads((TASKS / tier / "hidden" / "key.json").read_text())
    cand = json.loads(Path(cand_path).read_text())
    kf = lambda r: (r["file"], r["code"])
    kset = {kf(r): r for r in key}
    cset = {kf(r): r for r in cand}
    tp = kset.keys() & cset.keys()
    fields = ("line", "code", "message")
    correct = {k for k in tp if all(str(kset[k].get(f)) == str(cset[k].get(f)) for f in fields)}
    precision = len(correct) / len(cset) if cset else 0.0
    recall = len(correct) / len(kset) if kset else 0.0
    print(json.dumps({
        "tier": tier, "key_size": len(kset), "cand_size": len(cset),
        "correct": len(correct), "precision": round(precision, 4),
        "recall": round(recall, 4),
        "bar": "PASS" if precision >= 0.95 and recall >= 0.95 else "FAIL",
        "missed": [kset[k] for k in list(kset.keys() - correct)][:10],
        "wrong_or_extra": [cset[k] for k in list(cset.keys() - correct)][:10],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen")  # (пере)собрать ключи всех тиров
    c = sub.add_parser("corpus")  # материализовать корпус тира в staged workspace
    c.add_argument("tier", choices=list(TIERS))
    c.add_argument("out", type=Path)
    g = sub.add_parser("grade")
    g.add_argument("tier", choices=list(TIERS))
    g.add_argument("candidate")
    a = ap.parse_args()
    if a.cmd == "gen":
        cmd_gen()
    elif a.cmd == "corpus":
        cmd_corpus(a.tier, a.out)
    elif a.cmd == "grade":
        cmd_grade(a.tier, a.candidate)
