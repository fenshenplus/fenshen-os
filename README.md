# 分身 v1 · 实装版（阶段 1）

coding 版群聊团队助手。手机端 H5 客户端，桌面托管，本地 SQLite 持久化。

## 架构
- 前端：`frontend/index.html`（单文件 H5，响应式，手机可用）
- 后端：`backend/main.py`（FastAPI + SQLite，端口 **8002**，规避 8000）
- 数据：`data/fenshen.db`（本地，不落云）

四机制：元神·我的分身（1:1 私聊 + 资料库）／项目群聊（房间=团队）／角色库（Agent Schema）／公共资源库（授权开关）。

## 安装与启动（本机使用）

**无需下载安装包、无需联网装依赖**——运行环境（Python 受管 venv，已含 fastapi/uvicorn）已随 WorkBuddy 就绪，代码就在本机 `~/WorkBuddy/fenshen-v1/`。

### 方式 A：双击启动（最省事，推荐）
在 Finder 中打开 `~/WorkBuddy/fenshen-v1/`，**双击 `启动分身.command`**：
- 自动拉起后端服务（8002），并打开默认浏览器到 `http://localhost:8002/`
- 若已在运行，直接打开浏览器，不会重复启动
- 关闭终端窗口不影响服务（后台运行）

> 首次双击若 macOS 提示"无法验证开发者"：右键该文件 →「打开」→「打开」即可；或终端执行 `chmod +x 启动分身.command`。

### 方式 B：命令行
```bash
cd ~/WorkBuddy/fenshen-v1
bash start.sh
# 等价：~/.workbuddy/binaries/python/envs/default/bin/python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8002
```
启动后访问 `http://localhost:8002/`。

### 停止服务
双击 `停止分身.command`，或 `pkill -f "uvicorn.*8002"`。

### 换机器 / 发给同事
整目录 `~/WorkBuddy/fenshen-v1/` 直接打包（zip）拷到另一台 Mac 即可，**前提**对方也装了 WorkBuddy（提供受管 venv）；否则需先 `pip install -r backend/requirements.txt`。

## 手机访问
桌面与手机同一局域网，手机浏览器打开 `http://<桌面局域网IP>:8002/`
（端口守纪律避 8000；日常手机↔桌面走局域网，不经公网服务器）。

## API 清单
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查（含 LLM 状态） |
| GET/POST | `/api/projects` | 项目列表 / 成立项目 |
| PATCH | `/api/projects/{pid}` | 更新项目状态等 |
| GET/POST | `/api/roles` | 角色库 / 新建角色 |
| GET | `/api/resources` | 资源库 |
| POST | `/api/resources/{rid}/auth` | 资源授权切换 |
| GET/POST | `/api/messages` | 消息列表 / 发送（按 project_id） |
| GET/POST | `/api/meta/files` | 元神资料库 |
| POST | `/api/meta/chat` | 元神对话（接 LLM） |

元神私聊消息使用特殊 project_id：`__meta__`。

## 元神 LLM（人格分身）
- 引擎：`backend/main.py` 调 DeepSeek `deepseek-chat`，系统提示含岳衡人格 grounding（中文、coding 聚焦、砍臃肿、授权红线）。
- 密钥：读取 `~/.workbuddy/config/secrets/deepseek.key`。
- ⚠️ 当前该 key 返回 **402 Payment Required（余额不足/失效）**，引擎自动降级为"记录输入 + 降级提示"，接线正确、换有效 key 即恢复真实对话。

## 数据层
SQLite 表：`projects` / `roles` / `resources` / `messages(project_id, sender, kind, text, tag)` / `meta_files`。首次启动自动写入种子数据（4 项目 + 角色 + 资源 + 元神欢迎语）。
