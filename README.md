# orchestrate

**Languages:** [Русский](#русский) · [中文](#中文) · [English](#english)

A Claude Code skill that turns one AI session into a **lead orchestrator** — it
breaks a task down, hands each piece to the model best suited for it, and refuses to
accept any result until three independent reviewers have checked it.

> **The point of this project is not to save tokens.** It is to embed a correct
> engineering method — thorough task work and quality code on the output. Cost is a
> legitimate argument only when choosing *who* executes a ticket; it never overrides
> a rule of the method.

---

## Русский

### Суть проекта

`/orchestrate` превращает основную сессию Claude Code в **ведущего оркестратора**. Он
разбирает задачу, раскладывает её на части и раздаёт каждую часть тому исполнителю,
чей уровень действительно нужен для этой работы. Затем — не принимает результат, пока
его не проверят три независимых ревьюера.

У оркестрации **два входа** (единое исполнительное ядро и тонкие головы, [ADR-0006](docs/adr/0006-execution-core-and-thin-heads.md); третья голова `orca_orchestrate` выведена из проекта, см. [ADR-0008](docs/adr/0008-retire-orca-transport.md)).
Полный — `/orchestrate`: он сам проводит подготовительную фазу (грилл → спецификация →
тикеты) в своей же сессии и остаётся входом для сырых задач и расследований. Второй,
тонкий — `/orchestrate-frontier` ([ADR-0004](docs/adr/0004-frontier-entry-skill.md)): если владелец уже прошёл подготовку в сольной сессии
и в трекере лежат готовые тикеты, исполнение стартует сразу с маршрутизации — в свежем
контекстном окне и без прозы подготовительной фазы; без опубликованной спеки и
восходящих к ней тикетов этот вход отказывается работать.

Четыре штатных исполнителя, каждый закреплён за своим тиром модели:

| Агент | Модель | Роль |
|---|---|---|
| `architect` | Fable (Claude Fable 5, старшая модель Anthropic) | Проектирование, архитектурные решения, поиск первопричины, оценка риска |
| `builder` | Sonnet | Реализация по чёткой спецификации: код, тесты, рефакторинг, документация |
| `scout` | Haiku, read-only | Механическая разведка: поиск файлов, инвентаризация, классификация |
| `critic` | Opus | Состязательная проверка любого результата, в свежем контексте |

### Философия — и почему это главное

**Задача проекта — не экономия токенов и не подмена дорогих моделей дешёвыми.** Цель —
внедрить *правильный подход к разработке*: глубокую проработку задачи и качественный код
на выходе. Цена остаётся законным аргументом, но только на слое исполнителя — при выборе,
*кто* возьмёт готовый тикет (какая модель, какой провайдер). Правило метода цена отменить
не может: тест пишется первым, срез — вертикальный (полный путь через все слои), проверка —
всегда независимым ревьюером. «Правильный подход по вторникам» — это не подход.

Из этого следует критерий входа: в оркестратор попадает только работа, где **есть над чем
подумать**. Тривиальное сюда не отдают — а значит, внутри тривиального не бывает, и полный
метод применяется ко всему безусловно.

### Метод разработки

Каждая содержательная задача идёт по единому хребту (заимствован у Мэтта Покока и вшит
в скилл):

**грилл → спецификация → тикеты → реализация → гейт.**

- **Грилл** — ведущий допрашивает человека по одному вопросу, снимая неоднозначность;
  решения остаются за человеком, факты ведущий добывает сам.
- **Спецификация и тикеты** — задача нарезается на вертикальные срезы; каждый срез —
  узкий, но полный путь через все слои, демонстрируемый сам по себе, размером в одно
  свежее окно контекста, с явными рёбрами блокировки.
- **Реализация** — исполнитель ведёт разработку через тесты (красный → зелёный).
- **Гейт** — фиксированные **три линзы** на любой принимаемый результат: **Standards**
  (хорошо ли сделано — по документированным стандартам репозитория и запахам кода),
  **Spec** (то ли сделано и нет ли лишнего) и **critic** (можно ли верить — единственная
  линза с вердиктом PASS/FAIL). Каждая линза работает в свежем контексте и не видит
  рассуждений исполнителя. Одна из трёх — всегда чужой провайдер, чтобы модель не хвалила
  собственную работу.

### Кросс-провайдерный слой

По умолчанию скилл самодостаточен и работает только на Claude. Если у пользователя
подключены другие модели — Codex (OpenAI), Gemini через `agy` (Google), Grok (xAI),
Kimi (Moonshot) — ведущий может маршрутизировать и к ним: как чужую линзу Standards (постоянного не-Claude участника
гейта против самопредпочтения) и как кодовую руку для хорошо специфицированных тикетов.
Это определяется автоматически: нет коннектора — любой маршрут сводится к Claude, и линза
Standards никогда не пропускается молча.

Смысл подключения чужих моделей — **дополнительный независимый угол** и распределение
квоты между уже оплаченными подписками, а не «дешевле». Качество при этом не сдвигается:
класс задач-суждений и первичный проверяющий каждого гейта всегда остаются на Claude.

### Исполнительный движок `run-lane`

Оба входа опираются на единое исполнительное ядро — тонкий исполнитель `run-lane` (`skill/orchestrate/tools/run_lane/`, см. [ADR-0005](docs/adr/0005-lane-invocation-unification.md)):

- **Транспорт** — какой CLI вендора запускается (`agy` — консольный клиент Google Antigravity, транспорт Google-пула, форма вызова задокументирована в [`skill/orchestrate/references/cross-provider.md`](skill/orchestrate/references/cross-provider.md) / `codex` / `grok` / `kimi` / `claude-print`) — единственная ось выбора; подложка запуска одна (обычный subprocess) и осью выбора больше не является — вторая подложка, терминал Orca, выведена (см. [ADR-0008](docs/adr/0008-retire-orca-transport.md)).
- Результат работы всегда снимается **с файла на диске по `--out`**, а не из печати модели в консоли (печать усекается и пересказывается по памяти); фактически запустившаяся модель сверяется с запрошенной (проверка свидетеля / witness check).
- Единый JSON-конверт возвращает итог (`ok`, `model_declared` против `model_observed`, свойства артефакта, класс ошибки).
- Два вспомогательных режима: `run-lane detect` (проверка входа и доступности лейнов) и `run-lane smoke` (выполнение одного тривиального вызова на каждый лейн для проверки сквозного пути).

### Полигон

`benchmark/` — античит-полигон для сравнения моделей по ролям на синтетическом,
вымышленном домене (чтобы обучающие данные не помогали). Ключи-ответы на время прогонов
шифруются, каждый кандидат работает в изолированной песочнице, грейдинг детерминирован.
Именно на нём принимаются решения о маршрутизации: какая модель на какую роль годится —
по живым экзаменам, а не по рекламным бенчмаркам вендоров.

### Состояние

Рабочее, в активной разработке. Конфигурация маршрутизации живёт в
`skill/orchestrate/config.yaml` (единственный источник истины, версионируется;
детерминированный валидатор ловит рассогласование настроек и прозы). Сырые результаты
полигона (`runs/`, `scores.jsonl`) и телеметрия прогонов хранятся локально и в публичный
репозиторий не входят; синтез опубликован в [`benchmark/RESULTS.md`](benchmark/RESULTS.md).

---

## 中文

### 项目本质

`/orchestrate` 把一个 Claude Code 主会话变成**首席编排者**：它拆解任务，把每一部分交给
最适合该工作的模型层级来执行，然后在三位独立评审全部通过之前，拒绝接受任何结果。

编排有**两个入口**（单一执行核心与轻量入口，参见 [ADR-0006](docs/adr/0006-execution-core-and-thin-heads.md)；第三个入口 `orca_orchestrate` 已从项目中移除，参见 [ADR-0008](docs/adr/0008-retire-orca-transport.md)）。完整入口是
`/orchestrate`：它在同一会话中亲自走完筹备阶段（盘问 → 规格 → 工单），仍是原始任务与
排查类工作的入口。第二个是轻量入口 `/orchestrate-frontier`（[ADR-0004](docs/adr/0004-frontier-entry-skill.md)）：若负责人已在独立会话中完成
筹备、工单追踪器中已有就绪工单，执行便直接从路由开始——使用全新的上下文窗口，且不加载
筹备阶段的说明文字；缺少已发布的规格及其工单时，它会拒绝启动。

四个常设执行者，各自绑定其真正需要的模型层级：

| 智能体 | 模型 | 职责 |
|---|---|---|
| `architect` | Fable | 系统设计、架构决策、根因排查、风险评估 |
| `builder` | Sonnet | 按明确规格实现：代码、测试、重构、文档 |
| `scout` | Haiku，只读 | 机械侦察：查找文件、清点、分类 |
| `critic` | Opus | 在全新上下文中对任何交付物进行对抗式验证 |

### 理念——以及为何这是核心

**本项目的目标不是节省 token，也不是用廉价模型替代昂贵模型。** 目标是植入一套*正确的
工程方法*：对任务的深入打磨，以及输出的代码质量。成本仍是正当的考量，但仅限于执行者
层面——即选择*由谁*来完成一张工单（哪个模型、哪个供应商）。成本不能推翻方法的规则：
测试先行，切片必须是纵向的（贯穿所有层次的完整路径），验证永远由独立评审完成。「只在
周二讲究的正确方法」根本不是方法。

由此得出准入标准：只有**值得思考**的工作才进入编排器。琐碎的任务不会交进来——因此内部
不存在琐碎，完整的方法无条件适用于一切。

### 开发方法

每一项实质性任务都沿同一条主干推进（借鉴自 Matt Pocock，并内建于该技能）：

**盘问 → 规格 → 工单 → 实现 → 质量门。**

- **盘问**——首领每次向人提出一个问题，消除歧义；决策权归人，事实由首领自行查证。
- **规格与工单**——任务被切成纵向切片；每片是贯穿所有层次的窄而完整的路径，本身可演示，
  大小恰好装进一个全新的上下文窗口，并带有明确的阻塞关系。
- **实现**——执行者以测试驱动开发（红 → 绿）。
- **质量门**——对任何待接收的交付物固定使用**三重视角**：**Standards**（做得好不好——
  依据仓库规范与代码坏味道）、**Spec**（做的是不是对的、有没有多余）、**critic**（能否
  信任——唯一给出 PASS/FAIL 裁决的视角）。每重视角都在全新上下文中工作，看不到执行者的
  推理过程。三者之一始终来自其他供应商，以防模型自夸其作。

### 跨供应商层

默认情况下，该技能自成一体，仅使用 Claude。若用户接入了其他模型——OpenAI Codex、
Google Gemini、xAI Grok——首领也可路由至它们：作为 Standards 的跨模型视角（质量门中
固定的非 Claude 成员，用以消除自我偏好），以及作为规格明确工单的编码之手。这一切按能力
自动探测：没有连接器时，一切路由回落到 Claude，且 Standards 视角绝不会被悄悄跳过。

接入其他模型的意义在于**额外的独立视角**与在已付费订阅之间分摊配额，而非「更便宜」。
质量因此不受影响：判断类任务与每道质量门的首要评审，始终留在 Claude。

### 执行引擎 `run-lane`

两个入口均构建在同一个执行核心之上——轻量执行器 `run-lane`（`skill/orchestrate/tools/run_lane/`，参见 [ADR-0005](docs/adr/0005-lane-invocation-unification.md)）：

- **传输方式 (transport)**——运行哪个供应商的 CLI（`agy`/`codex`/`grok`/`kimi`/`claude-print`）——唯一可选维度；底层启动方式只有一种（普通子进程 subprocess），不再是可选维度——第二种底层（Orca 终端）已移除（参见 [ADR-0008](docs/adr/0008-retire-orca-transport.md)）。
- 交付物始终**从磁盘上的 `--out` 文件获取**，绝不从模型在终端打印的回复中提取（打印输出会被截断或凭记忆复述）；并且会针对请求的模型验证实际运行的模型（“见证人”检查 / witness check）。
- 单一 JSON 封套返回执行结果（`ok`、`model_declared` 对比 `model_observed`、交付物属性、错误类别）。
- 提供两个辅助模式：`run-lane detect`（探测哪些 lane 已登录且可用）和 `run-lane smoke`（对每个 lane 发起一次真实的简单调用以确认端到端通路）。

### 竞技场（Polygon）

`benchmark/` 是一个防作弊竞技场，用于在合成的、虚构的领域上按角色对比模型（使训练数据
无从帮忙）。运行期间答案密钥被加密，每个候选者在隔离沙箱中作业，评分是确定性的。路由
决策正是据此做出：哪个模型胜任哪个角色——依据真实考试，而非供应商的宣传跑分。

### 现状

可用，处于活跃开发中。路由配置存于 `skill/orchestrate/config.yaml`（唯一真相来源，带
版本；确定性校验器捕捉配置与文字之间的不一致）。运行遥测与竞技场结果刻意保留在本地，
不进入公开仓库。

---

## English

### What it is

`/orchestrate` turns the main Claude Code session into a **lead orchestrator**: it
triages a task, decomposes it, and hands each piece to the model tier that piece
actually needs — then refuses to accept the result until three independent reviewers
have checked it.

There are **two entries** into orchestration (a single execution core + thin heads, see
[ADR-0006](docs/adr/0006-execution-core-and-thin-heads.md); a third entry, `orca_orchestrate`,
has been retired from the project, see [ADR-0008](docs/adr/0008-retire-orca-transport.md)).
The full one is `/orchestrate`: it runs the preparatory phase (grill → spec → tickets)
inside its own session and remains the door for raw tasks and investigations. The
second, thin one is `/orchestrate-frontier` ([ADR-0004](docs/adr/0004-frontier-entry-skill.md)): when the owner has already done the preparation in a solo
session and ready tickets sit in the tracker, execution starts straight at routing —
with a fresh context window and without the preparatory prose; with no published spec
and tickets tracing to it, this entry refuses to run.

Four standing executors, each pinned to its tier:

| Agent | Model | Role |
|---|---|---|
| `architect` | Fable (Claude Fable 5, Anthropic's lead tier) | System design, architecture decisions, root-cause debugging, risk assessment |
| `builder` | Sonnet | Implementation to a clear spec: code, tests, refactors, docs |
| `scout` | Haiku, read-only | Mechanical recon: find files/usages, inventories, classification |
| `critic` | Opus | Adversarial verification of any deliverable, in a fresh context |

### Philosophy — and why it is the point

**This project is not about saving tokens, nor about swapping expensive models for
cheap ones.** Its goal is to embed a *correct engineering method*: thorough task work
and quality code on the output. Cost stays a legitimate argument, but only at the
executor layer — when choosing *who* takes a finished ticket (which model, which
provider). Cost cannot overturn a rule of the method: the test comes first, the slice
is vertical (a complete path through every layer), verification is always by an
independent reviewer. "The right approach on Tuesdays" is not an approach.

From this follows an entry criterion: only work that has **something to think about**
enters the orchestrator. Trivial work isn't handed in — so trivial work never exists
inside it, and the full method applies to everything, unconditionally.

### The development method

Every substantive task travels the same spine (adopted from Matt Pocock, wired into
the skill):

**grill → spec → tickets → implement → gate.**

- **Grill** — the lead interviews the human one question at a time, resolving
  ambiguity; decisions stay the human's, facts the lead finds itself.
- **Spec & tickets** — the task is cut into vertical slices; each slice is a narrow
  but complete path through every layer, demoable on its own, sized to one fresh
  context window, with explicit blocking edges.
- **Implement** — the executor drives development test-first (red → green).
- **Gate** — a fixed **three-lens** review on any accepted deliverable: **Standards**
  (is it well made — against the repo's documented standards and code smells),
  **Spec** (is it the right thing, nothing extra), and **critic** (can it be trusted —
  the only lens with a PASS/FAIL verdict). Each lens works in a fresh context and never
  sees the producer's reasoning. One of the three is always a different provider, so a
  model never grades its own work.

### Cross-provider layer

By default the skill is self-contained and Claude-only. When the user has other models
wired — Codex (OpenAI), Gemini via `agy` (Google), Grok (xAI), Kimi (Moonshot) — the lead can route to them too: as the
cross-model Standards lens (a permanent non-Claude member of the gate, closing
self-preference bias) and as a coding hand for well-specified tickets. It is
capability-detected: with no connector, every route falls back to Claude, and the
Standards lens is never silently skipped.

The point of adding other models is an **extra independent angle** and spreading quota
across already-paid subscriptions — not "cheaper." Quality does not shift for it: the
judgment-class of tasks and the primary grader of every gate always stay on Claude.

### Execution engine `run-lane`

Both heads sit on one execution core — the thin executor `run-lane` (`skill/orchestrate/tools/run_lane/`, see [ADR-0005](docs/adr/0005-lane-invocation-unification.md)):

- **Transport** — which vendor CLI runs (`agy` — Google Antigravity's console client, the Google-pool transport, its calling form documented in [`skill/orchestrate/references/cross-provider.md`](skill/orchestrate/references/cross-provider.md) / `codex` / `grok` / `kimi` / `claude-print`) — the only remaining axis; the launch substrate is now a single one (a plain subprocess), no longer a choice — the second substrate, an Orca terminal, has been retired (see [ADR-0008](docs/adr/0008-retire-orca-transport.md)).
- The deliverable is always taken **from the `--out` file on disk**, never from the model's printed reply in the console (printed output truncates and gets paraphrased); and the model that actually ran is verified against the one requested (a "witness" check).
- One JSON envelope carries the outcome (`ok`, `model_declared` vs `model_observed`, artifact properties, error class).
- Two auxiliary modes exist: `run-lane detect` (probe which lanes are logged in and available) and `run-lane smoke` (fire one trivial real call per lane to confirm the path end-to-end).

### The polygon

`benchmark/` is a cheat-proof polygon for comparing models by role on a synthetic,
fictional domain (so training data can't help). Answer keys are encrypted during runs,
each candidate works in an isolated sandbox, grading is deterministic. Routing
decisions are made here — which model fits which role, by live exams rather than a
vendor's marketing benchmarks.

### State

Working, under active development. Routing configuration lives in
`skill/orchestrate/config.yaml` (the single source of truth, versioned; a deterministic
validator catches drift between config and prose). Raw polygon results (`runs/`,
`scores.jsonl`) and run telemetry are kept local and are not part of the public repo;
the synthesis is published in [`benchmark/RESULTS.md`](benchmark/RESULTS.md).

---

## Requirements

- [Claude Code](https://claude.com/claude-code) CLI
- Python ≥ 3.12 with the `PyYAML` package (`pip install pyyaml`)
- `git`
- `gh` (GitHub CLI, for the issue-tracker workflow)
- macOS only: version ≥ 12.3, or GNU coreutils — the installers use `readlink -f`

Vendor CLIs (`codex`, `grok`, `kimi`, `agy`) are optional — without them, orchestration
degrades to Claude-only workers (transport is chosen by presence, never preference).

## Install

```bash
git clone https://github.com/shah1git/claude-orchestrate /opt/claude-orchestrate
cd /opt/claude-orchestrate
./install.sh          # symlink mode: edits here are live in Claude Code immediately
./install.sh --copy   # copy mode: independent copies under ~/.claude
```

`/opt/` is only the author's own convention — any directory works; point the clone
above wherever you keep checkouts.

## Repo layout

```
skill/orchestrate/          the skill itself (SKILL.md + config.yaml + references/)
skill/orchestrate-frontier/ thin second entry: runs a prepared ticket frontier (ADR-0004)
skill/orchestrate/tools/    config validator (deterministic seam over config + prose)
skill/orchestrate/tools/run_lane/ the lane-invocation executor (transport; artifact from disk; model witness) (ADR-0005)
agents/                     the four agent definitions
benchmark/                  the role-comparison polygon (fixtures, tasks, grader tools)
benchmark/DESIGN.md         the polygon's protocol and anti-cheat design
benchmark/RESULTS.md        published synthesis of polygon runs
docs/                       architecture decision records (docs/adr/) and agent docs (docs/agents/)
CONTEXT.md                  the project's domain glossary
scripts/                    maintenance scripts (e.g. doctrine sync)
bootstrap-mac.sh            one-shot machine setup (macOS)
telemetry-sync.sh           run-telemetry sync to the owner's private store
LICENSE                     MIT
.doctrine-version           doctrine sync marker (see AGENTS.md)
install.sh                  wires ~/.claude/skills and ~/.claude/agents to this repo,
                             and mirrors both into ~/.agents if that shared store exists
```

## Usage

Invoke explicitly with `/orchestrate <task>`, or let Claude Code auto-invoke it on
large, multi-part tasks that decompose into independent subtasks. If the preparatory
phase already happened in a solo session and the tracker holds ready tickets, use the
thin frontier entry instead: `/orchestrate-frontier` (ADR-0004). The four agents are
also directly invocable on their own, without going through the skill.

See [skill/orchestrate/README.md](skill/orchestrate/README.md) for the detailed docs
(in Russian) and the changelog.

## License

MIT — see [LICENSE](LICENSE).
