# 分身（Fenshen）版本管理

> 产品：分身 · 群聊形态 AI 团队总管（桌面端引擎 + H5/LAN 终端）
> 工作区：`/Users/a13401098230/WorkBuddy/fenshen-v1/`
> 当前版本：**v4.0**（`0.40.0` / release `v4.0`）
> 基于：`v3.9-preaudit`（git `184fa98`）
> 分支：`fix/v4.0-audit`
> 最后更新：2026-08-09

---

## 版本链路（已签字/待签字）

| 版本 | 日期 | git | 状态 | 关键里程碑 |
|------|------|-----|------|-----------|
| v1.0 → v2.0 → v3.0 → v3.5 → v3.6 → v3.7 | — | — | 已归档 | 基础架构、元神对话、看板 |
| v3.8 | 2026-08-07 | `2bb9081` | 已签字 | 元神人格蒸馏、演示视频 |
| v3.9-preaudit | 2026-08-09 | `184fa98` | 基线 | 审查前最后一个 tag |
| **v4.0** | 2026-08-09 | 待打 tag | **待签字** | 全面安全重构 + 诚实性修复 + 能派状态流转 + 版本确认 |

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
| AI 执行确认 | `approval_mode = all` | `all`（每次确认）/ `danger`（仅危险）/ `off`（关闭） |
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

---

## 中枢回填

元神中枢 `~/Desktop/元神/VERSION_MANAGEMENT.md` 第 140 行「分身 / 分身桌面版」指针由 **待登记** 改为 **已登记**。
