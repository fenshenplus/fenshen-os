# 贡献指南（Contributing）

感谢参与分身（Fenshen）项目。以下为本地开发与提交流程。

## 本地运行

```bash
# 安装依赖（Python 3.13）
pip install -r backend/requirements.txt

# 一键启动（自动装依赖 + 起服务 + 开浏览器）
python start.py          # macOS / Linux
# 或： py -3 start.py    # Windows
# 或： bash start.sh
```

启动后访问 http://localhost:8002/ 。端口默认 8002，规避 8000。

## 分支与提交规范

- 主开发分支：`fix/v4.0-audit`（当前），稳定后合入 `main` / 打 `stable`。
- 提交信息采用 Conventional Commits：`feat:` / `fix:` / `build:` / `docs:` / `security:` / `chore:` 等。
- 重大发布打 `v*` tag（如 `v6.3`），CI 会自动构建 Windows 安装版并发布到 Release。

## 测试

```bash
# 隔离回归（自动起 8011 端口，不污染生产库）
python tests/smoke_v40.py --base
```

## 严禁事项

- ❌ **绝不提交任何密钥 / 口令 / AccessKey / AppSecret / 证书**（`.gitignore` 已忽略 `*.key` `*.pfx` `*.p12` `.env` `secrets/`）。
- ❌ 不要将 `data/`（含用户数据库）提交进仓库。
- ✅ 涉及密钥一律走环境变量或本地未跟踪文件。

## 代码组织

- `backend/main.py`：FastAPI 后端 + SQLite（端口 8002）。
- `frontend/index.html`：单文件 H5 前端（响应式，手机可用）。
- `packaging/`：各平台打包入口（macOS `run_app.py` / Windows `fenshen.spec` + `run.py`）。
- `.github/workflows/`：CI（Windows 构建与签名）。
