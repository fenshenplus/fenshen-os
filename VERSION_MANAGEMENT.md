# 分身（Fenshen）版本管理

> 产品：分身 · 群聊形态 AI 团队总管（桌面端引擎 + H5/LAN 终端）
> 工作区：`/Users/a13401098230/WorkBuddy/fenshen-v1/`
> 当前版本：**v6.4**（`0.61.0` / release `v6.1`，矩阵看板 M1 + 元神长续航调度器 M2/M3/M4 + 6 meta 端点 + autopilot 控制台；v6.0 新 UI 外壳已收口）
> 基于：`v4.1`（git `f5b1929`）
> 分支：`fix/v4.0-audit`
> 最后更新：2026-08-25（v6.1 已发布：8002 切换新二进制 · git tag `v6.1` · dmg 已生成 · S4 官网推送待网络恢复）

---

## 版本链路（已签字/待签字）

| 版本 | 日期 | git | 状态 | 关键里程碑 |
|------|------|-----|------|-----------|
| v1.0 → v2.0 → v3.0 → v3.5 → v3.6 → v3.7 | — | — | 已归档 | 基础架构、元神对话、看板 |
| v3.8 | 2026-08-07 | `2bb9081` | 已签字 | 元神人格蒸馏、演示视频 |
| v3.9-preaudit | 2026-08-09 | `184fa98` | 基线 | 审查前最后一个 tag |
| v4.0 | 2026-08-09 | `6c0e1ec`（批次A `c0607fd`） | 已签字 | 全面安全重构 + 诚实性修复 + 能派状态流转 |
| v4.1 | 2026-08-10 | 待打 tag | **待签字** | 批次 B：任务完成标准 + 对照标准判定 + 工具分级 + 元神搭建 + autonomy 有界循环 + 护栏收敛；批次 C：角色动态加载 + 预设技能活配件 + 并行上限放开；安装包 v4.1 + 官网推送 + 签字文档 |
| **v4.2** | 2026-08-10 | `afb164a`（tag `v4.2`） | **✅ 已签字（2026-08-10 岳衡「我签」）** |
| **v5.0** | 2026-08-11 | tag `v5.0` | **✅ 已签字（2026-08-11 岳衡「我签」）** |
| **v5.1** | 2026-08-11 | tag `v5.1` | **✅ 已签字（2026-08-11 岳衡「我签」）** |
| **v5.2** | 2026-08-11 | tag `v5.2` | **✅ 已签字（2026-08-12 岳衡「我签」）** |
| **v5.3** | 2026-08-12 | tag `v5.3` | **✅ 已签字（2026-08-12 岳衡「我签」）** |
| **v5.4** | 2026-08-12 | tag `v5.4` | **✅ 已签字（2026-08-12 岳衡「我签」）** |
| **v5.5** | 2026-08-13 | 待打 tag | **待签字** | 战略修正 P0（首启不阻塞/技能只读/市场折叠/局域网一键开关）+ P1 token 节约（自主 60s）+ 双平台 v5.5 包；冒烟 72/0 | Windows 跨平台：start.bat 启动器 + PowerShell 审批弹窗 + paramiko 部署（实测 S4 备份+上传）；官网 macOS/Windows 双平台安装包；冒烟 72/0 | 运营中心转正（公开反馈→转看板 + SEO）+ 12 技能执行验证（番茄钟全链路真实产出）+ 测试产物落盘规范 + 移动端真机清单；冒烟 72/0 | 移动端遥控器（群聊首页/元神置顶/左滑看板/语音）+ 部署一键化 + 监控自动兜底 + 首登引导/软门槛；12 技能核对 12/12；冒烟 72/0 | 应用市场上架（内测）：一键上架/公开产品页/访问统计；审批超时可配置（≤3s fail-closed 直接拒绝）；冒烟 72/0 | 移动端体验闭环：异步派单+进度轮询 / PWA 可安装离线 / 移动端看板快捷操作 / 手机默认群聊；WAL 并发写修复；演示视频 v4 重录 | 自主闭环：立项自动拆解（/plan）+ 看板完成度 + 阶段自动流转 + 团队自主推进循环（元神不提问）+ 派单并行化；DeepSeek 迁移 v4-flash + 关闭思考模式；安装包 v4.2 已上线官网 |
| **v5.6** | 2026-08-14 | `b41fc24` | 已合入（未单独签字） | 借鉴 DeepSeek Harness——工作区限定 + PTC 批处理（落地+评估） | — | — | — | — | — | — | — |
| **v5.7** | 2026-08-14 | 未打 tag | 已合入（未单独签字） | 元神 PM 定位 + 蒸馏重排为绑定（利益/情感，非增强能力）+ 手机号+密码长效登录（v5.7）+ 「磨」skill 分段（以话题边界为主）；架构基线就绪，待 v5.8 一并收口 | — | — | — | — | — | — | — |
| **v5.8** | 2026-08-14 | tag `v5.8` | **✅ 已签字（2026-08-14 随 v6.0 一并签字）** | 四项重构收口：P0-A 元神PM定位+蒸馏绑定 ✅、P0-B 磨skill分段 ✅、P1 双维度存储（项目/成员/模块文件夹+点模块开文件+旧项目迁移）✅、P2 UI 设计规范库（apple_hig/wechat，建项自选无默认，注入角色执行）✅；安装包 v5.8 macOS dmg + 官网推送；冒烟 76/0 | — | — | — | — | — | — | — |
| **v5.9** | 2026-08-14 | tag `v5.9`（已推送 S4） | **✅ 已签字（2026-08-14 随 v6.0 一并签字）** | 候选增强收口：Trajectory 事件级回放（一次派单/对话=一个 run，run 内记录 run_start/plan/role_start/tool×N/role_done；/api/projects/{pid}/runs + /trajectory?run_id=，前端「🎬 回放」面板）✅、底部实时 token 条（进程级 LIVE_TOKENS 累加器 + /api/token/usage + /api/token/reset，前端 2.5s 轮询展示 输入/输出/合计/调用）✅、扩展内置供方（通义 qwen / 月之暗面 moonshot / 智谱 zhipu，均 OpenAI 兼容，PROVIDER_PRESETS 新增 3 项）✅、model_usage 补 input_tokens/output_tokens 列迁移 ✅；安装包 v5.9 macOS dmg + 官网推送（v5.8 dmg 下架）✅；冒烟 76/0 | — | — | — | — | — | — | — |
| **v6.0** | 2026-08-14 | tag `v6.0`（已打，commit `313f6e4`） | **✅ 已签字（2026-08-14 岳衡「我签」，含 v5.8 / v5.9 一并收口）** | 新 UI 外壳落地真实前端（图标栏+设置抽屉13项+联系人列元神置顶+分栏进程轨迹/看板+聊天内思考链 inline）；保留全部既有 ~120 JS 函数/~84 fetch 调用逐字不变（最小回归）；版本 0.60.0/v6.0；安装包 v6.0 macOS dmg + 官网推送（v5.9 dmg 下架）✅；jsdom 端到端 0 报错 + 冒烟 76/0 | — | — | — | — | — | — | — | — |
| **v6.1** | 2026-08-14 | tag `v6.1`（待打 commit） | **✅ 已签字（2026-08-14 岳衡「发布」指令）** | 矩阵看板 M1（模块×阶段数据模型 + `/matrix` + 前端矩阵默认视图）+ 元神长续航调度器 M2（三模式 autopilot/normal/rest + 事件回调 + 护栏）+ M3 关键路径优先级 + M4 多轨道补空白 + 休息窗口 + 6 meta 端点（interview/ingest/profile/mirror，LLM 可选降级）+ autopilot 控制台（前端 sec-autopilot + 后端 state/set）；版本 0.61.0/v6.1；安装包 v6.1 macOS dmg + 官网推送（v6.0 dmg 下架）；单测 28/28 + 冒烟 76/0 + jsdom 9/9 + 6 meta 端点 HTTP 端到端全绿 | — | — | — | — | — | — | — | — |✅；jsdom 端到端 0 报错 + 冒烟 76/0 | — | — | — | — | — | — | — |

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

## v6.1 改造清单（2026-08-14，来源：M1 矩阵看板 + M2/M3/M4 元神长续航调度器 + meta 蒸馏端点 + autopilot 控制台）

### M1 矩阵看板数据模型（模块×阶段）
- [x] `tasks` 表新增 `stage` / `track` 列；`modules` 表新增 `track` / `weight` 列；`projects` 表新增 `tracks` / `stage_chains` 列（`init_db` 启动自动迁移，兼容旧库；线上库已含这些列）
- [x] `GET /api/projects/{pid}/matrix`：返回模块×阶段矩阵（阶段集按轨道预设 `STAGE_PRESETS` 配置 web/h5/app/mp/generic）
- [x] `GET /api/projects/{pid}/matrix/cell/tasks`：格子任务列表；`fill-blanks`：仅补「前置阶段已 done」的空白格
- [x] 前端矩阵默认视图（可切回状态看板）；格子状态由 tasks 派生，逐层聚合 格子→列(按 module_weight)→行→项目
- [x] `migrate_matrix_v61.py`：旧库数据回填空脚本（默认 --dry-run；--apply 前自动备份；本轮未对线上库 --apply，留待人工归位）

### M2 元神长续航调度器（三模式 + 事件回调 + 护栏）
- [x] 三模式：`autopilot`（15s·并发3·自动补空白）/ `normal`（60s·并发1）/ `rest`（300s·只巡检）
- [x] 由「定时轮询」改「事件回调」：任务转 done 即触发派单、执行返回即触发 tick，卡间毫秒级
- [x] 护栏：全局并发 / 小时+日 token 预算 / 连败熔断 / 卡死超时回收 / 单轮补空白上限 / 项目级暂停（`AUTONOMY_STATE` + `meta_settings`）
- [x] `GET /api/autopilot/state` + `POST /api/autopilot/set`（mode / 预算 / 休息窗口 / 项目暂停）

### M3 关键路径优先级
- [x] `_project_critical_map`：按模块依赖推导关键路径；调度优先派关键路径模块（非关键模块仅在前置 done 后推进）

### M4 多轨道补空白 + 休息窗口
- [x] `_blank_proposals` 按轨道（web+app+…）遍历补空白；每轨道独立上限
- [x] 休息窗口（`rest_window_active`）：窗口内不派单、不补空白；关闭后恢复派单

### meta 蒸馏端点（LLM 可选降级）
- [x] `GET /api/meta/interview/next` + `POST /api/meta/interview/answer`（qid+answer）
- [x] `POST /api/meta/ingest`（多源资料摄取）
- [x] `GET /api/meta/profile`（画像叙述）
- [x] `POST /api/meta/mirror/generate`（镜像预测，LLM 可选降级）+ `POST /api/meta/mirror/judge`（agree+prediction 或 correction 强化画像）

### 前端 autopilot 控制台
- [x] 导航项「元神续航」`data-sec="autopilot"` + `sec-autopilot` 区块；`loadAutopilot` / `renderAutopilot` / `setAutopilot`
- [x] 旧 per-project `/api/projects/{pid}/autonomy` 端点移除，统一走 `/api/autopilot/*`

### 版本号
- [x] backend `FastAPI(version="0.61.0")` + `/api/health` 返回 `release: v6.1`
- [x] 前端 `<title>` + 版本徽标 `v6.1`
- [x] `tests/smoke_v40.py`：release 断言校正为 `v6.1`

## 验收测试结果

- 测试脚本：`tests/smoke_v40.py`（全回归）、`/tmp/sched_test.py`（M2-M4 调度器单测）、`/tmp/jsdom_frontend_check.js`（前端 jsdom 端到端）
- 结果：**冒烟 76 / 0 · 调度器单测 28 / 28 · 前端 jsdom 9 / 9 · 6 meta 端点 + autopilot HTTP 端到端全绿**
- 覆盖：全部 GET 接口、P0 安全、exec 真实性、任务/阶段流转、矩阵看板、调度器三模式/护栏/关键路径/休息窗口、meta 蒸馏端点、autopilot 控制台
- 验证方式：隔离测试实例（独立端口 8011 + 独立 DB 副本）跑全量，避免误测线上运行实例

---

## 部署日志

| 日期 | 操作 | 影响范围 | 快照/备注 |
|------|------|----------|----------|
| 2026-08-09 | v4.0 安全重构 + 诚实性修复 + 能派流转 | `backend/main.py`, `frontend/index.html`, `start.sh` | git `fix/v4.0-audit` |
| 2026-08-09 | 新增回归测试 `tests/smoke_v40.py` | 39 项冒烟通过 | — |
| 2026-08-09 | 官网改版 + 安装包更新为 v4.0 | `site/index.html`, `site/分身-v4.0-macOS.zip`, `dist-stage/分身-v1/*` | 默认 127.0.0.1 绑定；S4 不可达，线上未推送 |
| 2026-08-10 | 批次 A/B/C 全部落地（设计改进方案 v1 三批次） | `backend/main.py`, `frontend/index.html`, `tests/smoke_v40.py` | git `c0607fd` → `ef2b09a` → `48c418b`；冒烟 61/0 |
| 2026-08-10 | debug：LLM 连接中断自动重试（`_is_conn_error`） | `backend/main.py` | 连接类瞬时故障原地重试一次 |
| 2026-08-10 | 安装包重建 v4.1 + 官网推送 | `dist-stage/分身-v1/*`, `site/分身-v4.1-macOS.zip`, `site/index.html` | 线上备份 `/var/www/fenshen-backup-20260810-1720`；旧 v4.0 包下架；S4 HTTP 200 |
| 2026-08-10 | v4.2 自主闭环 + DeepSeek v4-flash 迁移 | `backend/main.py`, `frontend/index.html`, `tests/smoke_v40.py` | git `8e2d2e9`；冒烟 67/0 |
| 2026-08-10 | 派单并行化（PAD≤3 并发） | `backend/main.py` | git `86e57c1`；单任务派单 21-52s |
| 2026-08-13 | v5.5 战略修正 + token 节约 + 双平台 v5.5 包 + 官网推送 | `backend/main.py`, `frontend/index.html`, `site/分身-v5.5-macOS.dmg`+`-Windows-exe.zip` | 线上备份 `/var/www/fenshen-backup-20260813-2330`；v5.4 全下架（404）；v5.5 双包在线 |
| 2026-08-12 | v5.4 Windows 跨平台 + 双平台安装包 + 官网推送 | `backend/main.py`, `start.bat`, `dist-stage/分身-v1/*`, `site/分身-v5.4-macOS.zip`+`-Windows.zip` | 线上备份 `/var/www/fenshen-backup-20260812-1240`；v5.3 下架（404）；v5.4 双包 182878B 在线 |
| 2026-08-12 | v5.3 运营中心 + 执行验证 + 安装包 v5.3 + 官网推送 | `backend/main.py`, `frontend/index.html`, `tests/smoke_v40.py`, `site/分身-v5.3-macOS.zip` | 线上备份 `/var/www/fenshen-backup-20260812-1106`；v5.2 下架（404）；v5.3 包 181094B 在线 |
| 2026-08-11 | v5.2 移动端遥控器 + 部署兜底 + 首登引导 + 安装包 v5.2 + 官网推送 | `backend/main.py`, `frontend/index.html`, `tests/smoke_v40.py`, `site/分身-v5.2-macOS.zip` | 线上备份 `/var/www/fenshen-backup-20260811-2145`；v5.1 下架（404）；v5.2 包 178485B 在线 |
| 2026-08-11 | v5.1 应用市场上架（内测）+ 安全加固 + 安装包 v5.1 + 官网推送 | `backend/main.py`, `frontend/index.html`, `tests/smoke_v40.py`, `site/分身-v5.1-macOS.zip` | 线上备份 `/var/www/fenshen-backup-20260811-1900`；v5.0 下架（404）；v5.1 包 171869B 在线 |
| 2026-08-11 | v5.0 移动端体验闭环（异步派单/PWA/移动看板）+ 安装包 v5.0 + 官网推送 | `backend/main.py`, `frontend/*`, `dist-stage/分身-v1/*`, `site/分身-v5.0-macOS.zip` | 线上备份 `/var/www/fenshen-backup-20260811-1040`；v4.2 下架（404）；demo.mp4 更新 v5.0 演示视频 |
| 2026-08-10 | 安装包重建 v4.2 + 官网推送 | `dist-stage/分身-v1/*`, `site/分身-v4.2-macOS.zip`, `site/index.html`, `deliverables/gstack/v4.2-signoff.md` | 线上备份 `/var/www/fenshen-backup-20260810-1958`；v4.1 包下架（404）；v4.2 包 200 完整 |
| 2026-08-14 | v5.8 四项重构收口 + 安装包 v5.8 macOS dmg + 官网推送 | `backend/main.py`, `frontend/index.html`, `backend/design_specs/*`, `migrate_storage_v58.py`, `tests/smoke_v40.py`, `packaging/macos/run_app.py`, `site/分身-v5.8-macOS.dmg`, `site/index.html`, `deliverables/gstack/v5.8-signoff.md` | 线上备份 `/var/www/fenshen-backup-20260814-1021`；v5.5 macOS dmg 下架（404）；v5.8 dmg 200 在线；版本号 0.58.0/v5.8；冒烟 76/0 |
| 2026-08-14 | v6.0 新 UI 外壳落地 + 全功能测试 + 安装包 v6.0 macOS dmg + 官网推送 | `backend/main.py`, `frontend/index.html`, `packaging/macos/run_app.py`, `tests/smoke_v40.py`, `site/分身-v6.0-macOS.dmg`, `site/index.html`, `deliverables/gstack/v6.0-signoff.md` | 线上备份 `/var/www/fenshen-backup-20260814-1343`；v5.9 dmg 下架（404）；v6.0 dmg 200 在线（66MB） | 2026-08-14 | v6.1 矩阵看板 M1 + 元神长续航调度器 M2/M3/M4 + 6 meta 端点 + autopilot 控制台 + 安装包 v6.1 macOS dmg + 官网推送（S4 待推送） | `backend/main.py`, `frontend/index.html`, `frontend/fenshen.png`, `tests/smoke_v40.py`, `migrate_matrix_v61.py`, `packaging/macos/run_app.py`, `site/分身-v6.1-macOS.dmg`, `site/index.html`, `deliverables/gstack/v6.1-signoff.md`, `VERSION_MANAGEMENT.md` | 8002 切换新二进制（pid 62768）health=v6.1；v6.1 dmg 21.4MB 已生成；git commit `652093a` + tag `v6.1`；冒烟 76/0 · 调度单测 28/28 · jsdom 9/9 · meta/autopilot HTTP 全绿；S4（39.105.6.135）当前不可达，官网推送待网络恢复后执行 |；版本号 0.60.0/v6.0；jsdom 端到端 0 报错；冒烟 76/0 |

---

## Schema 迁移兼容说明（前向 / 后向 / 回滚）

> 原则（来自 2026 SCM 实践）：**迁移一律 append-only**——只 `ALTER TABLE … ADD COLUMN` 带默认值，不删列、不删表、不改列类型、不做破坏性数据转换。任何破坏性变更必须走「新列 + 双写 + 旧列作废标记」的渐进路径。

### 兼容性约定
- **前向兼容（新代码跑老库）**：`init_db` 启动自动 `ALTER ADD COLUMN`（带默认值）；读取处用 `COALESCE(col, default)`，老库缺列不报错。旧库已含新列则跳过（`IF NOT EXISTS` 语义）。
- **后向兼容（老代码跑新库）**：新增列对老代码透明（被忽略）；老代码不引用新列即不报错。故回退代码版本不需要回退 DB。
- **回滚**：因迁移可加，回滚 = 代码 git revert；新增列**保留不删**（避免再次迁移时 `ADD COLUMN` 冲突）。作废数据以「标记列」隔离，不物理删除。
- **发布门禁**：`scripts/bump_version.py` 在 bump 前强制跑 `tests/smoke_v40.py`（隔离 8011 或本机 8002），不绿不发布（fail-closed）。

### 已落地迁移清单（兼容性 / 回滚）

| 迁移 | 新增列 / 表 | 前向兼容 | 后向兼容 | 回滚 |
|------|------------|----------|----------|------|
| 矩阵看板 M1 | `tasks.stage` / `tasks.track` / `modules.track` / `modules.weight` / `projects.tracks` / `projects.stage_chains` | ✅ 缺列自动补，读用默认值 | ✅ 老代码忽略新列 | 代码回退；列保留 |
| 双维度存储 v5.8 | `files` 表 + `storage_root` / `file_path`（模块目录树） | ✅ `migrate_storage_v58.py` 幂等，未 `--apply` 不写 | ✅ | 代码回退；`files` 表保留 |
| 蒸馏 v6.4 | `user_model.subject` / `authorization_status` / `material_type` / `quality_score`；新表 `distill_subjects` / `distill_authorizations` / `distill_materials` | ✅ 旧数据 `subject='self'` 回填；读用 `COALESCE` | ✅ 新表对老代码透明 | 代码回退；新表保留 |
| 用量统计 v5.9 | `model_usage.input_tokens` / `output_tokens` | ✅ 缺列补，读用 `COALESCE` | ✅ | 代码回退；列保留 |

### 新迁移提交检查单
- [ ] 仅 `ADD COLUMN`（带默认值）或新建表，无 `DROP` / 无类型变更
- [ ] 读取处用 `COALESCE` 兼容缺列
- [ ] 提供幂等迁移（重复执行安全）
- [ ] 破坏性意图必须走「新列 + 双写」，并登记到上表
- [ ] `bump_version.py` 门禁通过后才允许合并

---

## 中枢回填

元神中枢 `~/Desktop/元神/VERSION_MANAGEMENT.md` 第 140 行「分身 / 分身桌面版」指针由 **待登记** 改为 **已登记**。
