# 分身 v6.4（元神驾驶舱）—— 代码审查 Handoff 文档

> 对象：负责代码审查的独立 Agent
> 基线分支：`fix/v4.0-audit`（与 `main` 并存）
> 仓库：`~/WorkBuddy/fenshen-v1`
> 本文档由「测试 + 版本确认」流程产出，供审查 Agent 直接拿去逐项核对。

---

## 0. 结论速览（审查前必读）

- **版本**：`SEMVER=0.64.10` / `RELEASE=v6.4` / `SCHEMA_VERSION=1` / `BUILD_DATE=2026-08-18` / git HEAD `db6a056`。
- **三源一致**：`backend/version.py`、运行实例 `/api/version`、构建元数据 **一致**（SCHEMA_VERSION 为 int 1）。
- **未提交 WIP（当前工作树）**：`CHANGELOG.md` / `backend/main.py`(+2 行 `in_app_guide_enabled` 开关) / `backend/version.py`(0.64.8→0.64.10) / `frontend/index.html`(+392/-33，v6.5 产品内引导 + P0 模块 UI)。这些改动**连贯、非破坏性、可编译**，但**尚未提交**，审查时请单独评估其质量。
- **测试结果**：主回归套件 **76/0 全绿**；全功能 e2e **50 项全绿**，4 个 403 为设计内功能开关门禁（已开关注入验证可 200），`/api/meta/code_stream` 的 422 为入参 schema 校验。**无任何 5xx / 无崩溃**。
- **LLM 真实调用已验证**：环境 `llm=deepseek`（密钥在 `~/.workbuddy/config/secrets/deepseek.key`），distill / reviews/auto / auto-upgrade / search / attribution/refresh 等端点均实际跑通，非离线空转。

---

## 1. 架构与关键文件

| 层 | 文件 | 角色 | 关键行 |
|---|---|---|---|
| 单一真源 | `backend/version.py` | 版本常量 + `_git_commit()` | SEMVER L18 / RELEASE L21 / SCHEMA L24 / BUILD L27 |
| 入口/路由 | `backend/main.py` | FastAPI 单文件后端（**175 个 `@app` 路由**） | — |
| 人格/宪法 | `backend/main.py` `META_SYSTEM` | 元神最高宪法（用户利益至上、未经授权不蒸馏他人） | L59-89 |
| 鉴权 | `backend/main.py` `local_guard` | 本机令牌中间件 + Host/Origin 白名单 | L171-203 |
| DB | `backend/main.py` `DB` / `get_db()` | SQLite，路径固定 `backend/.. /data/fenshen.db` | L44 / L606 |
| 前端 | `frontend/index.html` | 单页 SPA（含驾驶舱/看板/引导） | — |
| 启动 | `start.sh` | `uvicorn backend.main:app --host 127.0.0.1 --port 8002`（默认仅本机回环） | L50 |

### 1.1 我（本轮前序）落地的核心功能锚点
- **元神自动驾驶汇报（P3）**：`_meta_autopilot_report()` `backend/main.py:9481`（纯 DB 聚合，**不调 LLM**，同步写 `messages.report_json`）；触发端点 `POST /api/meta/report/{pid}:10112`；读取端点 `GET /api/projects/{pid}/report/latest:10121`。
- **结构化卡片（P3+）**：前端 `renderReportCard()` `frontend/index.html:1522`（`.report-card` 卡片）；驾驶舱加载 `loadCockpitReport()` `:4534`；`tag==='自动驾驶汇报'` 路由到卡片渲染 `:1559`。
- **autopilot 续航/预算**：`GET /api/autopilot/state:9794`、`POST /api/autopilot/set:10042`、`AUTONOMY_MODES:9198`、`_autopilot_state_dict():9754`。
- **蒸馏充足度 / meta 状态**：`GET /api/meta/sufficiency:10542`、`GET /api/meta/state:10500`、`GET /api/meta/overview:8794`、`GET /api/meta/morning-brief:9830`。

---

## 2. 功能清单（审查覆盖面）

| 模块 | 代表端点 | 状态 |
|---|---|---|
| 认证/账号 | `/api/auth/*`、`/api/account/*` | 含恢复密钥、导出、注销 |
| 项目/看板/任务 | `/api/projects`、`/api/projects/{pid}/modules`、`/api/topics/{tid}/tasks`、`/api/tasks/{tid}/move` | 阶段自动流转、完成度聚合 |
| 自动驾驶汇报（结构化） | `/api/meta/report/{pid}`、`/api/projects/{pid}/report/latest` | P3 已验 |
| 元神战队（成员） | `/api/projects/{pid}/members`、`/api/members/{mid}/upgrade|auto-upgrade|experience` | P0 已验 |
| 蒸馏/记忆/进化 | `/api/memory/distill`、`/api/experiences/distill|tree|recall`、`/api/skills/distill`、`/api/meta/evolution/{cell|promote|lineage}` | 后三者受开关门控（默认关→403） |
| 记忆面板/归档（E6/E7） | `/api/meta/memory/archive`、`/api/meta/memory/panel` | 受开关门控（默认关→403） |
| 质量/工程 | `/api/meta/quality_gate|codebase|vcs|code_stream|code_scan` | 已验 |
| 评审/打磨 | `/api/reviews/auto`、`/api/grind/rules` | 已验 |
| 矩阵/就绪/搜索/私聊 | `/api/projects/{pid}/matrix|readiness`、`/api/search`、`/api/direct/{peer}` | 已验 |
| 阶段链/快照/变更单 | `/api/projects/{pid}/stage-chain|snapshots|change-orders` | 已验 |
| 导出/令牌 | `/api/export/vault`、`/api/token/usage` | 已验 |
| 安全/护栏 | `local_guard`、`/api/cleanup`（白名单+破坏性确认）、工作区越界拦截 | 已验（smoke 批次 2/批次 B） |

---

## 3. 测试结果（供审查 Agent 复核）

### 3.1 主回归 `tests/smoke_v40.py` —— **76/0 通过**
- 覆盖：GET 接口可用性、P0 安全防护（401/403/400/坏 JSON）、exec 真实性、写链路与任务状态流转、批次 A/B/C/D（完成标准、看板↔群聊绑定、bootstrap、工具分级、技能活配件、角色动态加载、看板完成度、阶段自动流转、autonomy、市场）。
- 纪律：必须从**源码根**起 uvicorn（`data/fenshen.db`），不可放 `/tmp`（否则批次 C 因 import 直连 DB 与服务端不一致而假失败）。已遵守。

### 3.2 全功能 e2e `tests/e2e_full_sandbox.py` —— **50 项全绿**
- 新增回归资产，覆盖 P2/P3/P3+ 及 v6.4/v6.5 端点。
- 4 个 403（`evolution/promote`、`evolution/lineage`、`memory/archive`、`memory/panel`）经开启对应开关复测**均 200 且返回真实数据**（memory/panel 返回 125 条经验），属设计内门禁，非缺陷。
- `/api/meta/code_stream` 返回 422 = FastAPI 入参 Pydantic 校验拒绝（端点存活，未崩溃）；审查时建议确认其预期 body 形状是否与前端调用一致。

### 3.3 沙箱纪律
- 测试前备份 `data/` → 测试后**还原**（已还原并重启服务，health 正常、版本 0.64.10）。本次会话未污染用户工作数据。

---

## 4. 红线 / 技术债（审查 Agent 必须守住）

1. **版本单一真源**：禁止在代码中硬编码版本字符串（语义/对外/构建日期）。任何版本展示必须走 `backend/version.py` 的 `as_dict()` / `*_git_commit()`。发现硬编码即判缺陷。
2. **本机令牌鉴权不可弱化**：`local_guard`（`backend/main.py:171`）对所有 `/api/` 强制 `x-fenshen-token`；`/api/health` 等 PUBLIC_API 例外。不得为"方便调试"移除令牌校验或放宽为任意 Host。
3. **元神宪法恒定锚定**：`META_SYSTEM`（`backend/main.py:59`）的最高宪法（用户利益至上、未经授权不蒸馏他人、危险操作先确认）不可被改动或降级。
4. **密钥外置**：已去除硬编码密钥，deepseek key 仅从 `~/.workbuddy/config/secrets/deepseek.key` 读取（`backend/main.py:53`）。审查时若发现任何内联 key/secret/token，判严重缺陷。
5. **破坏性操作门禁**：`/api/cleanup` 的破坏性 scope（chat/memory/all）必须真人确认 + 删前备份（`backend/main.py:5185` 起）。不得把 scope 默认值改回破坏力最大的 `all`。
6. **公开物料措辞**：面向用户的文案禁用"傻瓜"等贬损词（用户明确红线）。
7. **DB 路径**：当前固定 `data/fenshen.db`，无 env 覆盖。若需支持多实例，应新增 env 注入而非硬编码多路径；现状审查标记为"已知约束"，非缺陷。

---

## 5. 审查验收标准（请逐项给出 PASS/FAIL + 行号证据）

- [ ] **可编译**：`python -m py_compile backend/*.py` 全过；`frontend/index.html` 内 `<script>` 块无语法错误（可用 `node --check` 抽取校验）。
- [ ] **版本纪律**：全仓 grep 不出硬编码的 `0.64.10` / `v6.4` / `2026-08-18` 字符串（除 `version.py` 与 `CHANGELOG.md`）。
- [ ] **鉴权**：随机抽 5 个 `/api/` 写端点，去掉 `x-fenshen-token` 必须等于 401；伪造 `Host`/`Origin` 必须等于 403。
- [ ] **P3 汇报闭环**：`POST /api/meta/report/{pid}` → `GET /api/projects/{pid}/report/latest` 返回的 `report_json` 含 `progress`/`critical_path`/`readiness` 字段；前端 `renderReportCard` 正确渲染 `.report-card`。
- [ ] **蒸馏/记忆/进化门控**：`evolution_heldout_enabled` / `evolution_lineage_enabled` / `memory_archive_enabled` / `memory_panel_enabled` 关闭时返回 403、开启时返回 200（本次已验证）。
- [ ] **无密钥泄露**：grep `sk-` / `api_key\s*=\s*["']` / 明文 token，应为 0 命中（仅 `version.py`/`.gitignore` 例外）。
- [ ] **破坏性清理**：`/api/cleanup` scope 缺省/非法/破坏性时分别 400/400/需确认，不能静默全删。
- [ ] **未提交 WIP 质量**：单独评审 `backend/main.py`(+2 行)、`frontend/index.html`(+392/-33)、`version.py`、`CHANGELOG.md` 的改动是否自洽、是否引入回归。
- [ ] **回归不退化**：重跑 `tests/smoke_v40.py` 须 76/0；重跑 `tests/e2e_full_sandbox.py` 须 50 绿。

---

## 6. 已知外部阻塞（非代码问题，供参考，不在本次审查范围）

以下为运营/合规侧阻塞，需人工或另一 Agent 推进，不在代码审查内：
- Apple Dev ID + 公证；Windows EV 证书 + CI 签名流水线。
- 域名 + ICP 备案 + HTTPS；公安备案；软著；应用备案；公众号 AppID。
- S4 公网修复（39.105.6.135）；SMS_507335285 模板审核。
- `p1` 临时演示数据清理；推广源文件（8 短文 + 3 软文 + 视频脚本）确认/重写（另一推广 handoff 文档承接）。

---

## 7. 给审查 Agent 的交付清单

- 测试结论：见本文 §3（76/0 + 50 绿）。
- 版本确认：见本文 §0（三源一致，WIP 未提交）。
- 回归资产：`tests/smoke_v40.py`（既有）、`tests/e2e_full_sandbox.py`（本次新增）。
- 你的产出：一份带行号证据的问题清单（按严重度排序）+ 总体 PASS/FAIL 结论 + 对未提交 WIP 的专项意见。
