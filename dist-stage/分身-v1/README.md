# 分身 v1（macOS 发行版）

本地优先的 AI 团队群聊助手。本包自带运行环境，无需 WorkBuddy。

## 安装与启动
1. 解压本包
2. 双击 **`安装.command`**（首次会联网装依赖，约几十秒）
3. 浏览器自动打开 `http://localhost:8002/`

之后每次使用，双击 **`启动.command`** 即可；停止双击 **`停止.command`**。

## 要求
- macOS 10.15+，已装 `python3`（Xcode 命令行工具或 Homebrew 均可）
- 首次安装需联网（拉取 FastAPI / uvicorn）；之后离线可用

## macOS 打开 .command 提示「无法验证」怎么办？
这是苹果对网上下载文件的统一安全拦截（Gatekeeper），与本软件无关。二选一即可：

**方法 A（推荐，永久生效）**——打开「终端」粘贴运行（路径按你实际解压位置改）：
```
xattr -cr ~/Downloads/分身-v1
```
回车后再双击 `安装.command` 即可正常打开。

**方法 B（单次信任）**——右键 `安装.command` → 打开 → 弹窗中点「打开」。之后 `启动.command` / `停止.command` 也用同样方式首次打开一次即可。

## 说明
- 数据存于本包内 `data/fenshen.db`（本地 SQLite），不落云
- 服务端口 **8002**（规避 8000 生产占用）
- 元神 LLM 接 DeepSeek，密钥失效时自动降级（记录输入 + 提示），换有效 key 即恢复
