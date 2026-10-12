# video-distill

> 把 B 站视频「蒸馏」成可读、可检索的文字资产：逐字稿、AI 总结、结构化要点。

输入一个 B 站视频链接，自动产出带时间戳的逐字稿、分段摘要和结构化要点，所有内容均可点回原视频对应位置。

## ✨ 特性

- **逐字稿**：直接使用 B 站站内字幕（人工字幕优先，其次 AI 字幕），无需本地语音识别
- **AI 总结**：基于 DeepSeek 大模型生成分段摘要，保留视频逻辑脉络
- **结构化要点**：自动提取结论、论据、行动项，每条带时间戳锚点，点击即可跳转到原视频
- **Web 界面**：FastAPI 提供的任务管理界面，支持任务提交、进度查看、产物浏览与搜索
- **凭据安全**：B 站登录凭据使用 Fernet 加密落盘，操作号与管理员 UID 分离
- **配额管理**：基于视频时长的配额校验，防止资源浪费

## 📋 环境要求

- Python 3.11+
- 一个 B 站账号（用于获取字幕，建议使用小号）
- DeepSeek API Key（用于 AI 总结）

## 🚀 安装

1. **克隆仓库**

```bash
git clone https://github.com/fullstop113/video-distill.git
cd video-distill
```

2. **创建虚拟环境并安装依赖**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## ⚙️ 配置

复制 `.env.example` 为 `.env` 再填值。**每一项在 `.env.example` 里都写了
"是什么、不填会怎样、怎么生成"** —— 逐项说明另见
[docs/configuration.md](docs/configuration.md)。

最少只需要两个：

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥，用于 AI 总结 |
| `VD_SECRET_KEY` | **凭据加密密钥，需要你自己生成**（不会自动生成） |

生成 `VD_SECRET_KEY`：

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

它的作用是加密存在本机磁盘上的 B 站登录凭据（SESSDATA 等 cookie）——
密钥与数据分离，即使有人拿到 `data/credentials/` 里的文件也解不开。

> ⚠️ **丢了会怎样**：已保存的 B 站登录全部失效，需要重新扫码。
> 凭据文件还在，但解不开了。

处理**无字幕视频**还需要两个（可选）：`DASHSCOPE_API_KEY` +
`VD_PUBLIC_BASE_URL`（本服务的公网可达地址）。详见
[快速开始](docs/quickstart.md#五可选让无字幕视频也能处理)。

多人使用还需填 `VD_OPERATOR_BILI_UID` / `VD_OWNER_BILI_UID`
（用于**凭据隔离**：谁是取数号、谁是管理员），见
[配置项说明](docs/configuration.md#账号角色)。

## 🏃 快速开始

**完整的从零上手步骤见 [docs/quickstart.md](docs/quickstart.md)。** 最短路径：

1. **启动服务**

```powershell
.venv\Scripts\python.exe scripts/serve.py
```

2. **打开浏览器访问** [http://127.0.0.1:8788](http://127.0.0.1:8788)

3. **首次使用**：在 Web 界面扫码登录 B 站账号（凭据会加密保存在 `data/`，之后自动复用）。

4. **提交任务**：粘贴 BV 号，点击「开始蒸馏」即可。

> 想用命令行而不是网页：`scripts/distill.py`（取逐字稿，消耗配额）
> 和 `scripts/summarize.py`（做总结，消耗 DeepSeek 费用）分两步。

### 登录说明

- **本机访问**（localhost / 127.0.0.1）：磁盘上已存有凭据时，Web 界面不再要求重复扫码；若凭据不存在，仍需先扫码登录。
- **远程访问**（局域网 / 公网）：始终需要扫码登录，且仅管理员与受邀 UID 可提交任务。

### 环境自检（可选）

```powershell
.venv\Scripts\python.exe scripts/check_env.py
```

逐项检查 Python、ffmpeg、凭据、密钥等是否就绪。

## 🖥️ 服务页面

| 路径 | 用途 |
| --- | --- |
| `/` | 提交任务 + 任务列表 |
| `/artifacts` | 产物汇总，支持筛选与搜索 |
| `/tasks/<id>` | 任务详情：速览 / 总结 / 时间轴 / 要点 / 下载 |
| `/status` | 服务状态 |
| `/quota` | 配额使用情况 |

## 🛠️ 命令行工具

除了 Web 界面，也可以直接使用命令行：

```powershell
# 单视频取逐字稿（不走 Web）
.venv\Scripts\python.exe scripts/distill.py BV1xxxxxxxxx

# 对已有逐字稿生成总结
.venv\Scripts\python.exe scripts/summarize.py BV1xxxxxxxxx
```

## 📁 目录结构

```
video-distill/
├─ src/vd/        # 核心包：web / pipeline / transcript / summarize / credentials ...
├─ scripts/       # 入口脚本：serve / distill / summarize / check_env / check_deploy ...
├─ tests/         # 单元测试与端到端验收
├─ docs/          # 快速开始、配置说明、部署、设计文档
├─ data/          # 凭据密文、SQLite、缓存、音频中转（不入版本库）
├─ output/        # 产物
└─ .env           # 配置（已 gitignore）
```

## 📚 文档

| 文档 | 内容 |
| --- | --- |
| [快速开始](docs/quickstart.md) | 从零到跑通第一个视频 |
| [配置项说明](docs/configuration.md) | 全部环境变量逐项说明 |
| [部署](docs/deploy.md) | 部署到云服务器（安全组、公网地址） |
| [云 ASR](docs/asr-cloud.md) | 无字幕视频的云端转写：原理、成本、踩坑 |
| [合规与风险](docs/legal-review.md) | **建议先读** |
| [整体设计](docs/plan-bilibili-mvp.md) | 技术选型与方案 |

## 🧪 运行测试

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

改过配置相关代码后，顺手跑一下文档一致性检查
（它会对比"代码读取的环境变量"与"文档记录的"，防止文档悄悄过期）：

```powershell
.venv\Scripts\python.exe scripts/check_env_docs.py
```

## 📄 许可证

MIT
