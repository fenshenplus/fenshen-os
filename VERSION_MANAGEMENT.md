# 分身（Fenshen）版本管理

> 产品：分身 · 群聊形态 AI 团队总管（桌面端引擎 + H5/LAN 终端）
> 工作区：`/Users/a13401098230/WorkBuddy/fenshen-v1/`
> 当前版本：**v4.1**（`0.41.0` / release `v4.1`）
> 基于：`v4.0`（git `c0607fd`，含批次 A 审计修复）
> 分支：`fix/v4.0-audit`
> 最后更新：2026-08-10

---

## 版本链路（已签字/待签字）

| 版本 | 日期 | git | 状态 | 关键里程碑 |
|------|------|-----|------|-----------|
| v1.0 → v2.0 → v3.0 → v3.5 → v3.6 → v3.7 | — | — | 已归档 | 基础架构、元神对话、看板 |
| v3.8 | 2026-08-07 | `2bb9081` | 已签字 | 元神人格蒸馏、演示视频 |
| v3.9-preaudit | 2026-08-09 | `184fa98` | 基线 | 审查前最后一个 tag |
| v4.0 | 2026-08-09 | `6c0e1ec`（批次A `c0607fd`） | 已签字 | 全面安全重构 + 诚实性修复 + 能派状态流转 |
| **v4.1** | 2026-08-10 | 待打 tag | **待签字** | 批次 B：任务完成标准 + 对照标准判定 + 工具分级 + 元神搭建 + autonomy 有界循环 + 护栏收敛；批次 C：角色动态加载 + 预设技能活配件 + 并行上限放开 |

---

## 核心配置资产清单

| 类型 | 路径 | 说明 |
|------|------|------|
| 后端主入口 | `backend/main.py` | FastAPI 服务、安全层、LLM 路由、业务 API |
| 蒸馏引擎 | `backend/meta_distill.py` | 元神访谈 / 画像 / 多源资料摄取 |
| 前端 SPA | `frontend/index.html` | 单文件 H5 客户端 |
| 启动脚本 | `start.sh` / `启动分身.command` | 默认 `127.0.0.1:8002`，LAN 模式需 opt-in |
| 回归测试 | `tests/smoke_v40.py` | 39 项冒烟用例（接口 + 安全 + 任务流转 + exec 真实） |
| 鉴权 Token | `data/.auth_token` | 启动时自动生成，文件权限 600 |
| 数据库 | `data/fenshen.db` | sqlite；破坏性操作前自动备份 |

---

## 安全与运行配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 绑定地址 | `127.0.0.1` | `start.sh` 默认；`FENSHEN_ALLOW_LAN=1` 才绑 `0.0.0.0` |
| 端口 | `8002` | 避开 `8000`（choice-power 生产占用） |
| 鉴权 | Token（Cookie + Header） | 无 Token 返回 401；Token 由服务端生成 |
| DNS Rebinding 防护 | Host 白名单 | 伪造 Host → 403 |
| CSRF 防护 | Origin/Referer 校验 | 跨站请求 → 403 |
| AI 执行确认 | `approval_mode = danger`（v4.1 起默认） | `all`（每次确认）/ `danger`（仅危险命令）/ `off`（关闭） |
| 确认超时 | 90 秒 | 超时 fail-closed，按拒绝处理 |
| 危险命令 | 黑名单 + 敏感路径正则 | 服务端弹 `osascript` 系统对话框，客户端 confirm 无效 |

---

## v4.0 修复清单（来源：GStack 五人团队审查 `deliverables/gstack/full-audit-fenshen-2026-08-09.md`）

### P0 安全（已修复并实测）
- [x] 本地 Token 鉴权 + Host/Origin 中间件
- [x] 危险命令黑名单扩充 + 敏感路径正则
- [x] macOS 系统级授权对话框（fail-closed）
- [x] `/api/cleanup` scope 白名单 + 破坏性范围强制真人确认 + 自动备份
- [x] `/api/exec` 返回真实 `ok: exit_code == 0`
- [x] 启动脚本默认绑定 `127.0.0.1`

### P0 前端安全（子 agent 修复，已复核）
- [x] 70+ 处 `innerHTML` 替换为 `escapeHtml`/`textContent`，堵死 XSS → RCE
- [x] `.hidden` CSS 定义补齐，解决元神面板堆叠
- [x] 深浅色混用统一为浅色 token
- [x] 元神「中止」按钮改为真 `AbortController`

### P1 诚实性（已修复）
- [x] `write_file` 截断根因：`max_tokens 800` → `8192`
- [x] 工具 JSON 解析失败如实报错，不再静默 fallback 空 dict
- [x] `_call_provider_tools` 统一合并 `system_prompt`
- [x] LLM 无产出时返回诚实提示（不再伪造承诺）
- [x] 清理中心移除假自动开关，改为手动入口 + 二次确认

### P1 架构真实化（已修复）
- [x] 多模型降级链真实实现（角色配置优先 → FALLBACK_ORDER → Ollama 探活）
- [x] 三种蒸馏（memory/skills/experiences）改为真 LLM 抽取 + method 标注
- [x] `system_prompt` 在 deepseek/openai/ollama 分支不再被静默丢弃

### P1 能派支柱（本次新增）
- [x] dispatch 派单后任务自动从 `todo/idea` → `doing`
- [x] 角色执行结束后按真实产出判定 `done`/`review`/`todo`
- [x] 元神调度 `/api/meta/dispatch` 同步更新状态并落系统消息

### 仍遗留 / 需用户决策
- [ ] 官网安装包下架（审查 P0-1，dist 目录在 S4 `/var/www/fenshen/`；S4 当前 SSH 无法登录）
- [ ] 数据恢复：审查中误删的 `long_term_memory` / `messages`，备份文件 `fenshen.db.leadbackup-20260809-105950` 仍保留在 `data/`

---

## v4.1 批次 B 改造清单（2026-08-10，来源：设计改进方案 v1「批次 B：P1-1~3 / P2-1~4」）

### P1 目标与标准（完成判定可对照）
- [x] P1-1 `tasks` 表新增 `done_criteria` 列 + 老库兼容迁移；dispatch / 话题提炼均落任务级完成标准；聚合接口与看板卡片透出
- [x] P1-2 向导完成标准输入（批次 A 已落地 `wStandards` → 项目级 `standards`，沿用）
- [x] P1-3 `_judge_role_output` 由长度启发式改为对照标准 LLM 判定：任务 done_criteria → 项目 standards → 无标准时退回保守长度启发式；返回 (status, reason)，未达标原因进群聊与看板

### P2 元神职责收敛 + autonomy 循环
- [x] P2-1 工具分级：`META_TOOLS`（元神只读+搭建，无 write_file）/ `ROLE_TOOLS`（角色含 write_file 可动手产出）；`_chat_with_tools` 按 agent_id 默认分级
- [x] P2-2 `_bootstrap_project(pid, goal, standards, roles)`：项目成立即由元神搭建基础设施——群聊开场消息定格目标/完成标准/团队/模块看板
- [x] P2-3 autonomy 有界循环：执行 → 对照完成标准判定 → 未达标由元神重新规划补充动作 → 最多 `autonomy_max_rounds`（默认 3）轮；响应返回 `rounds` / `all_done`
- [x] P2-4 护栏收敛：`approval_mode` 默认 `all` → `danger`（普通命令不再弹窗，危险命令仍 fail-closed）；`write_file` 纳入确认框架（`all` 模式拦截，`danger` 不拦但始终 file_log 审计）

### 验证
- [x] 冒烟回归 `tests/smoke_v40.py`：45 / 45 通过（新增批次 A 用例后基线）
- [x] 端到端：bootstrap 开场消息落库、任务 done_criteria 透出、角色 write_file 真实产出、LLM 对照标准判定 done、autonomy 单轮达标、approval_mode=danger

---

## v4.1 批次 C 改造清单（2026-08-10，来源：设计改进方案 v1「批次 C：P3-1~3」）

### P3 角色与技能（活配件）
- [x] P3-1 角色从表动态加载：`_roles_from_db()` 从 roles 表加载 (systems, names)（静态种子兜底，角色库 POST /api/roles 即时生效）；`_role_id_by_name()` 中文名反查 id，消灭 `ROLE_ID_MAP`；project_chat / _bootstrap_project / topic_chat 全部改用动态角色
- [x] P3-2 预设技能活配件：`BUILTIN_SKILLS` 12 种（需求拆解/技术选型/UI 组件库/API 设计/DB Schema/前端脚手架/后端脚手架/测试用例/代码审查/部署上线/文档生成/Bug 修复）启动 upsert 种入 skills 表；`_match_skill_steps()` 按 trigger_words 命中 enabled 技能 → 注入 steps 到角色 system prompt；删除 `_auto_after_chat` 关键词正则自动生成垃圾技能逻辑（保留记忆提炼）；create_skill 配件上限 ≤20
- [x] P3-3 团队规模上限放开：`max_actions = max(3, min(6, len(role_systems)))` 按角色数动态（PAD 协议 ≤3 并行由串行执行天然满足）；dispatch / replan 提示词同步动态上限与角色枚举

### 验证
- [x] 冒烟回归 `tests/smoke_v40.py`：61 / 61 通过（新增批次 C 7 例：12 技能 seed/触发注入/角色动态加载/中文名反查）
- [x] 端到端：真 LLM 派单 autonomy 循环真实跑到第 2 轮（首轮未达标 → 元神重规划）；P1-3 判定准确识别"产出仅为异常信息未交付文档"；元神调度前用只读工具查目录（P2-1 分级生效）
- [x] 浏览器验证：技能库页面渲染 12 条内置技能

---

## 验收测试结果

- 测试脚本：`tests/smoke_v40.py`
- 结果：**39 / 39 通过**
- 覆盖：全部 23 个 GET 接口、P0 安全 6 项、`/api/exec` 真实性 3 项、任务状态流转 6 项

---

## 部署日志

| 日期 | 操作 | 影响范围 | 快照/备注 |
|------|------|----------|----------|
| 2026-08-09 | v4.0 安全重构 + 诚实性修复 + 能派流转 | `backend/main.py`, `frontend/index.html`, `start.sh` | git `fix/v4.0-audit` |
| 2026-08-09 | 新增回归测试 `tests/smoke_v40.py` | 39 项冒烟通过 | — |
| 2026-08-09 | 官网改版 + 安装包更新为 v4.0 | `site/index.html`, `site/分身-v4.0-macOS.zip`, `dist-stage/分身-v1/*` | 默认 127.0.0.1 绑定；S4 不可达，线上未推送 |

---

## 中枢回填

元神中枢 `~/Desktop/元神/VERSION_MANAGEMENT.md` 第 140 行「分身 / 分身桌面版」指针由 **待登记** 改为 **已登记**。
