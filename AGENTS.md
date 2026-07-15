# claude-orchestrate

Репозиторий скилла оркестрации `/orchestrate` и полигона сравнения ролей
(`benchmark/`). Этот файл читают все агент-CLI, которым раздаётся работа —
не только Claude Code, но и codex- и gemini-лейны (config v9 `promoted`).

## Agent skills

### Issue tracker

Задачи живут в GitHub Issues этого репозитория (`shah1git/claude-orchestrate`),
операции — через `gh` CLI. Нативные зависимости issue и sub-issues на репозитории
включены и проверены, поэтому карты `/wayfinder` и рёбра блокировки тикетов
выражаются машинно, а не текстовой пометкой в теле. См. `docs/agents/issue-tracker.md`.

### Triage labels

Пять канонических triage-ролей, строка метки совпадает с именем роли. Из них в
трекере пока заведена только `wontfix` (дефолтная гитхабовская); остальные четыре
создаются по мере первого применения. См. `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` и `docs/adr/` в корне репозитория. Сигналов монорепо
нет. См. `docs/agents/domain.md`.
