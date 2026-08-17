# 分身 v6.3 · 实装版

群聊形态 AI 团队总管：每个项目 = 一个群聊；元神（Meta-Agent）调度角色、跟踪进度、交付。桌面托管，本地 SQLite 持久化。

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

### 换机器 / 发给同事（独立部署）
打包 `分身-启动器包-v6.3.zip` 发给对方，**不要求**对方装 WorkBuddy。对方解压后：

```bash
# macOS / Linux
bash start.sh
# 或： python3 start.py

# Windows
双击 start.bat
# 或： py -3 start.py
```

首次运行会自动联网安装 `backend/requirements.txt` 里的依赖（fastapi / uvicorn / requests，约 1 分钟），之后直接启动并打开浏览器到 `http://localhost:8002/`。
> 可选功能（SSH 终端、浏览器自动化、元神蒸馏）需额外 `paramiko` / `playwright`，为按需懒加载，缺失不影响核心启动。

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

## 维护者：Windows 安装版构建与签名（GitHub Actions）

源码启动器包（`fenshen-launcher-v6.3.zip`）适合高级用户；给普通用户的"下载安装即用"安装版由 CI 产出：

- 工作流：`.github/workflows/build-windows.yml`（打 `v*` tag 或手动触发 → `windows-latest` 构建 → 签名 → 发布到 GitHub Release）。
- 构建产物：`分身.exe`（one-file，数据持久化于用户目录 `~/.fenshen`，退出不丢）。

**证书（在阿里云云市场向 锐成/亚数 等代购 Sectigo/DigiCert/GlobalSign 代码签名证书）有两种交付形态，决定签名方式：**

1. **可导出 PFX**（多为 OV，或少数允许导出的场景）
   - 仓库 Secrets 配 `CODESIGN_PFX`（证书 Base64）+ `CODESIGN_PWD`。CI 用 `signtool` 自动签名（含双时间戳）。
   - 注意：2023-06 起 **EV 证书私钥必须存硬件**，通常**不能**导出 PFX。

2. **USB 硬件令牌**（Sectigo/DigiCert EV 常见，如 SafeNet eToken）
   - CI 无法签名（无令牌）。需在一台插着令牌的 Windows 机器上本地签名，或选下面的云签名。

3. **云 HSM / 云签名**（推荐，CI 无需硬件）
   - 把你的 CA 提供的签名命令整段填进 Secret `CODESIGN_CMD`，CI 直接执行（命令内务必带 RFC3161 时间戳）。厂商中立，适配 SSL.com eSigner / DigiCert Software Trust Manager / 锐成云签名等。

> 选型建议：想要用户下载**零 SmartScreen 警告**，优先 **EV 或云签名**；OV 新证初期会被 SmartScreen 拦。法律主体名（UAC 显示）用分发主体（如「安徽叒叕创业投资有限公司」）。

## 元神 LLM（人格分身）
- 引擎：`backend/main.py` 调 DeepSeek `deepseek-chat`，系统提示含岳衡人格 grounding（中文、coding 聚焦、砍臃肿、授权红线）。
- 密钥：读取 `~/.workbuddy/config/secrets/deepseek.key`。
- ⚠️ 当前该 key 返回 **402 Payment Required（余额不足/失效）**，引擎自动降级为"记录输入 + 降级提示"，接线正确、换有效 key 即恢复真实对话。

## 数据层
SQLite 表：`projects` / `roles` / `resources` / `messages(project_id, sender, kind, text, tag)` / `meta_files`。首次启动自动写入种子数据（4 项目 + 角色 + 资源 + 元神欢迎语）。
