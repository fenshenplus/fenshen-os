# 分身 · Goal-Mode + 质量门设计补丁（v1）

> 来源：Hermes（NousResearch）`/goal` 机制 —— worker 干活 + 独立 judge 模型验收的 **Ralph loop**。
> 目标：把"你当外层循环"变成"分身自己跑循环"，且不与分身既有架构（`tasks.stage/track` 看板真源、`backend/native/` 原生能力、元神宪法）冲突。
> 状态：设计补丁（待评审）。本补丁**不改动任何已发布运行时**，仅作为下一批次实现的规格。

---

## 0. 背景：Hermes /goal 是什么

Hermes 的 `/goal` 是一个**单会话内持久目标**机制，核心伪代码：

```
while not done:
    work()                       # worker 干活（本会话内自主迭代）
    ralph = judge(work)          # 独立 judge：只看「目标 + 当前状态」判定 done / keep going
```

关键机制（提炼）：

| 能力 | 说明 | 分身可借鉴度 |
|---|---|---|
| 独立 judge 验收 | worker 不适合当自己的裁判，单独轻量 LLM 调用判定 `done` | ★★★ 最强 |
| `/goal draft` | 自然语言目标 → 结构化"完成契约"（每条带 `Done when:`） | ★★★ |
| `/subgoal` | 运行中追加验收标准，不必重启 | ★★☆ |
| `/goal gate add` | 加一个"必须通过的 shell 命令"质量门（如 `pytest` 退出 0） | ★★★ |
| `max_turns` 预算 | 默认 20，重构可设 50，防失控 | ★★☆（元神宪法护栏） |
| `pause/resume` | 循环可挂起 / 恢复 | ★★☆ |
| `/goal` vs Kanban 边界 | `/goal` 单任务自主迭代；Kanban 多任务排程，互不扇出 | ★★★（哲学咬合） |
| cron agent | 定时自主推进（"你睡觉时它在干活"） | ★☆☆（延伸） |

**核心理念**：Agent 与 chatbox 的分水岭是**主动推进**——一个目标交出去，它自己迭代到满足验收条件，而不是每轮等你确认。

---

## 1. 设计原则（与分身架构咬合）

本补丁严格遵循分身既有定位，不引入新范式：

1. **元神只搭基建不直接执行**：Goal-Mode 是看板卡片的一个**运行开关**，worker 仍由现有角色（owner_role）经元神调度执行（复用 `_run_dispatch_job` 路径），元神本身不亲自写代码。
2. **原生基础能力强大（Native Stdlib）**：质量门**优先用 `backend/native/` 确定性校验**，不烧 LLM；只有主观验收（"是否优雅"）才调轻量 judge。
3. **看板真源 `tasks.stage/track`**：Goal-Mode 是 `tasks` 表上的一层状态机，不另起炉灶；`done_criteria` 字段（已存在）即"完成契约"的天然载体。
4. **元神宪法护栏**：`max_turns` 预算 + `pause/resume` 是"钱/隐私/声誉保守"的具体实现——自主循环必须可控、可停、有成本上限。
5. **单任务 vs 多任务边界清晰**：Goal-Mode **只在单张卡内**跑 Ralph 循环；**永不自动建卡、永不扇出到群聊**。多卡依赖/跨群派发仍归看板与元神调度。

---

## 2. 概念模型

```
┌─────────────────────────────────────────────────────────────┐
│ 看板卡片 task（已有 tasks 表）                                  │
│   name, owner_role, status, stage, track, done_criteria       │
│                                                               │
│  + Goal-Mode 开关（新增 goal_mode=1）                          │
│     │                                                         │
│     ▼                                                         │
│  ┌──────────── Ralph loop（该卡自己的会话上下文）────────────┐ │
│  │  turn 1..N (≤ max_turns)                                  │ │
│  │   worker = 元神调度 owner_role 执行一步                     │ │
│  │   judge  = 验收（native gate 优先 → 轻量 judge 兜底）       │ │
│  │     判定: PASS → 标 done，结束                             │ │
│  │            FAIL → 返回 gap 清单，worker 下一轮补           │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                               │
│  + 完成契约（done_criteria + subgoals[] + gates[]）            │
│  + 控制：pause / resume / clear / append-subgoal / add-gate   │
└─────────────────────────────────────────────────────────────┘
```

**完成契约（Completion Contract）**的组成：
- `done_criteria`（已有字段）：人类可读的"做到什么算完"。
- `subgoals[]`（新增）：机器可判定的验收清单，每条 `{id, text, check, status}`。`check` 可为 `native:<cap>` 或 `llm:free-text` 或 `cmd:<shell>`。
- `gates[]`（新增）：完成前的硬性质量门，每条 `{id, kind, spec, required_exit}`。例如 `{kind:"cmd", spec:"python -m pytest tests/", required_exit:0}`。

---

## 3. 数据模型变更

在 `backend/main.py` 的 schema 迁移段（`ALTER TABLE tasks ...` 附近）追加：

```sql
-- Goal-Mode 状态机（vX 新增）
ALTER TABLE tasks ADD COLUMN goal_mode      INTEGER DEFAULT 0;   -- 0=关 1=开
ALTER TABLE tasks ADD COLUMN goal_status    TEXT    DEFAULT '';   -- running|paused|done|failed|''
ALTER TABLE tasks ADD COLUMN goal_max_turns INTEGER DEFAULT 20;
ALTER TABLE tasks ADD COLUMN goal_subgoals  TEXT    DEFAULT '[]'; -- JSON: [{id,text,check,status}]
ALTER TABLE tasks ADD COLUMN goal_gates     TEXT    DEFAULT '[]'; -- JSON: [{id,kind,spec,required_exit}]

-- Ralph 循环逐轮记录（新表）
CREATE TABLE IF NOT EXISTS goal_runs (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    turn        INTEGER NOT NULL,
    worker_out  TEXT,                -- worker 本轮产物/摘要
    verdict     TEXT,                -- pass|fail
    reason      TEXT,                -- judge 给的 gap 清单
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_goal_runs_task ON goal_runs(task_id, turn);
```

> 注：`done_criteria` 字段已存在于 `tasks`（line 890），直接复用为完成契约的人类可读主描述。

---

## 4. 核心引擎：Ralph 循环

新增 `backend/goal_mode.py`，由元神在卡片开启 Goal-Mode 时拉起。**worker 复用现有 `_run_dispatch_job` 调度链路**，judge 走"原生 gate 优先、轻量 LLM 兜底"。

```python
# backend/goal_mode.py（新增，伪代码骨架）
import json, asyncio
from backend.native import NATIVE_CAPABILITIES   # 原生能力注册表
from backend.main import get_db, run_worker_step, call_llm, META_PID

async def run_goal_loop(task_id: str):
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task or not task["goal_mode"]:
        return
    contract = build_contract(task)          # done_criteria + subgoals + gates
    turn = 0
    max_turns = task["goal_max_turns"] or 20
    while turn < max_turns:
        turn += 1
        # 1) worker：派给该卡 owner_role，带本轮上下文（含上轮 judge 的 gap）
        gap = last_gap(task_id)
        worker_out = await run_worker_step(task, context={"gap": gap, "turn": turn})
        # 2) judge：原生 gate 优先，主观项才调 LLM
        verdict, reason = await judge(task, worker_out, contract)
        db.execute("INSERT INTO goal_runs ...", (..., turn, worker_out, verdict, reason))
        if verdict == "pass":
            db.execute("UPDATE tasks SET status='done', goal_status='done' WHERE id=?", (task_id,))
            return
        # FAIL：把 gap 写入下一轮上下文，继续
        await asyncio.sleep(0)   # 让出事件循环
    # 超出预算：按元神宪法，标记 failed 并通知用户，绝不静默
    db.execute("UPDATE tasks SET goal_status='failed' WHERE id=?", (task_id,))


async def judge(task, worker_out, contract) -> (str, str):
    # 质量门：原生确定性校验优先（不烧 LLM）
    for g in contract["gates"]:
        if g["kind"] == "cmd":
            code = await run_shell(g["spec"])            # 沙箱内执行
            if code != g["required_exit"]:
                return "fail", f"质量门未过: {g['spec']} 退出码 {code}"
        elif g["kind"] == "native":
            ok, msg = run_native_check(g["spec"], worker_out)  # 见 §5
            if not ok:
                return "fail", f"原生校验未过: {msg}"
    # 完成契约：逐条验收
    for s in contract["subgoals"]:
        if s["check"].startswith("native:"):
            ok, msg = run_native_check(s["check"][7:], worker_out)
            if not ok: return "fail", f"验收项未过: {s['text']} ({msg})"
        # 主观项或自由文本验收：轻量 judge（小模型，开销低）
        elif s["check"].startswith("llm:"):
            ok = await llm_judge(s["text"], worker_out, task)
            if not ok: return "fail", f"验收项未过: {s['text']}"
    return "pass", "全部验收通过"
```

**与现有代码复用点**：
- `run_worker_step` 应包装现有 `_run_dispatch_job(job_id, pid, user_text, target=owner_role)`（line 6327）——目标角色即 `task.owner_role`，pid 取该卡所属 `project_id`。
- `call_llm` 已有（line 5580 等），轻量 judge 用更小/更快的模型配置（参考 `MODEL_ROUTER` 中 META_PID 的 `deepseek-chat` 平衡档，或单独配置 judge 档）。
- 循环由 `asyncio.create_task` 拉起，**不阻塞**元神主对话（与现有 dispatch 异步化一致）。

---

## 5. 质量门（native gate registry）

质量门优先落到 `backend/native/`，与 `NATIVE_CAPABILITIES` 注册表同构。新增一个 `verify` 模块：

```python
# backend/native/verify.py（新增）
"""分身原生标准库 · 验收与质量门模块（verify）。

提供「可确定性判定」的常见验收原语，供 Goal-Mode judge 直接调用，不依赖 LLM。
返回 dict: {ok: bool, msg: str}。
"""
from sqlite3 import Connection

def file_exists(conn: Connection, path: str) -> dict: ...
def test_passed(conn: Connection, cmd: str) -> dict: ...        # 运行并返回退出码
def page_reachable(conn: Connection, url: str) -> dict: ...     # HTTP 200
def db_row_count(conn: Connection, table: str, expect_ge: int) -> dict: ...
def build_ok(conn: Connection, project_root: str) -> dict: ...  # 前端/后端构建无错

# 注册进 NATIVE_CAPABILITIES
VERIFY_CAPS = [
    {"name": "文件存在", "tool": "file_exists", "pure_stdlib": True},
    {"name": "测试通过", "tool": "test_passed", "pure_stdlib": True},
    {"name": "页面可达", "tool": "page_reachable", "pure_stdlib": True},
    {"name": "构建无误", "tool": "build_ok", "pure_stdlib": True},
]
```

`NATIVE_CAPABILITIES`（line 16）追加一项 `verify` 模块，使元神系统提示能枚举"本机可确定性验收的能力"，调度前先判是否命中原生快路径。

---

## 6. API 与前端交互

### 后端 API（`backend/main.py`）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/tasks/{task_id}/goal/start` | POST | 开启 Goal-Mode，拉起 `run_goal_loop` |
| `/api/tasks/{task_id}/goal/pause` | POST | 挂起循环（`goal_status=paused`） |
| `/api/tasks/{task_id}/goal/resume` | POST | 恢复循环 |
| `/api/tasks/{task_id}/goal/clear` | POST | 关闭 Goal-Mode，停循环 |
| `/api/tasks/{task_id}/goal/subgoal` | POST | 追加验收项 `{text, check}`（`/subgoal`） |
| `/api/tasks/{task_id}/goal/gate` | POST | 追加质量门 `{kind, spec, required_exit}`（`/goal gate add`） |
| `/api/tasks/{task_id}/goal/runs` | GET | 返回 `goal_runs` 逐轮记录（供用户查看进度） |

### 前端（`frontend/index.html`）
- 看板卡片详情新增 **「Goal-Mode」开关**；开启后卡片出现：
  - 验收清单（`subgoals`，可运行时追加，对应 `/subgoal`）；
  - 质量门列表（`gates`，可追加命令，对应 `/goal gate add`）；
  - 控制条：`暂停 / 恢复 / 停止`（`pause/resume/clear`）；
  - 进度抽屉：展示 `goal_runs` 逐轮 worker 产物 + judge 判定（保留"暂停看进度"口子）。
- 视觉上沿用现有极简风，不新增顶部导航；卡片内局部扩展。

---

## 7. 与现有架构的边界（铁律）

1. **Goal-Mode 永不自动建卡**：它只在被开启的那张卡内迭代；需要多任务分解时，由元神（人/宪法授权下）显式派发新卡，而非循环自身扇出。
2. **Goal-Mode 永不扇出到群聊**：单卡会话上下文隔离；跨群/跨项目协作走既有「看板↔群聊联动」（批次 A 已锁）。
3. **worker ≠ judge**：同一任务里，执行角色与验收角色必须分离（验收见 §4 `judge`）。即使验收走 LLM，也应使用独立轻量配置，避免"自我满意"橡皮图章。
4. **看板真源不变**：`status/stage/track` 仍是唯一真相；Goal-Mode 只是在 `status` 到达 `done` 之前多了一层自动迭代状态机。

---

## 8. 元神宪法护栏

| 护栏 | 实现 | 对应宪法 |
|---|---|---|
| `max_turns` 预算 | `goal_max_turns` 默认 20，UI 可设（重构类建议 50） | 钱：限制 LLM 调用次数 |
| `pause/resume` | 控制端点 + 前端按钮 | 隐私/声誉：人工随时接管 |
| 超预算不静默 | 超出即 `goal_status=failed` 并通知用户，绝不自标 done | 用户完全控制权 |
| 原生门优先 | 可确定性验收不烧 LLM | 成本 + 可靠性 |
| 沙箱隔离 | `cmd` 类质量门在受限子进程执行，禁止写系统目录 | 隐私/安全 |

---

## 9. 分阶段落地

**MVP（建议先做，验证闭环）**
- `tasks` 加 `goal_mode/goal_status/goal_subgoals/goal_gates/goal_max_turns`；新建 `goal_runs` 表。
- `backend/goal_mode.py` + `native/verify.py`：实现 `run_goal_loop` / `judge`（native gate 优先 + 轻量 LLM 兜底）。
- 后端 7 个 API；前端卡片 Goal-Mode 开关 + 验收清单 + 控制条。
- smoke 回归（复用 `tests/smoke_v40.py` 门禁）。

**增强（MVP 验证后）**
- `/subgoal` 式运行中追加标准 UX 打磨；
- `goal_runs` 进度抽屉可视化；
- cron agent：定时推进某张卡（"每天凌晨推进 X 到可看程度"）。

**不急于做**
- 模糊目标（"更优雅"）的自主循环——此类 judge 无法稳定打分，走逐轮人工，不进 Goal-Mode。

---

## 10. 本补丁自身的验收标准（meta）

- [ ] `tasks` 表迁移在 `bump_version` 的 compile+smoke 门禁下通过；
- [ ] 一张卡开启 Goal-Mode 后，能在 `max_turns` 内仅凭 native gate 把"测试通过"类任务自标 `done`；
- [ ] 含一条主观 `llm:` 验收项的卡，能触发轻量 judge 且不自标 done 直到达标；
- [ ] `pause` 后循环挂起、`resume` 后从下一轮继续、`clear` 后状态归零；
- [ ] 超 `max_turns` 一律 `failed` 通知，无静默 done；
- [ ] 全程不新建看板卡、不向群聊发消息（边界铁律自动化断言）。
