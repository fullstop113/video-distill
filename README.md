# video-distill

> 把 B 站视频「蒸馏」成可读、可检索的文字资产：逐字稿、AI 总结、结构化要点。

## 快速启动

```powershell
.venv\Scripts\python.exe scripts/serve.py
```

打开 [http://127.0.0.1:8788](http://127.0.0.1:8788) 即可使用。

本机访问免登录；远程访问需扫码登录。

### 环境自检（可选）

```powershell
.venv\Scripts\python.exe scripts/check_env.py
```

逐项检查 Python、CUDA、ffmpeg、凭据、密钥、操作号分离等是否就绪。

## 项目简介

输入一个 B 站视频链接，产出：

| 产出 | 说明 |
| --- | --- |
| **逐字稿** | 语音转文字，带时间戳，可点回原视频 |
| **AI 总结** | 分段摘要，保留逻辑脉络 |
| **结构化要点** | 结论、论据、行动项，每条带时间戳锚点 |

取数策略：**站内字幕优先 + FunASR 兜底**。字幕命中率约 60%，零 GPU 成本；
剩余视频走本地 ASR（RTX 4070 Ti 上 1 小时音频约 0.7 分钟）。

## 配置

复制 `.env.example` 为 `.env`，填写：

| 变量 | 说明 |
| --- | --- |
| `VD_SECRET_KEY` | 凭据加密密钥 |
| `VD_OPERATOR_BILI_UID` | 取数用的 B 站 UID |
| `VD_OWNER_BILI_UID` | 管理员 UID（仅配额，不取数） |
| `DEEPSEEK_API_KEY` | DeepSeek 总结用密钥 |

扫码登录后凭据会自动保存。

## 服务页面

| 路径 | 用途 |
| --- | --- |
| `/` | 提交任务 + 任务列表 |
| `/artifacts` | 产物汇总，支持筛选与搜索 |
| `/tasks/<id>` | 任务详情：速览 / 总结 / 时间轴 / 要点 / 下载 |
| `/status` | 服务状态 |
| `/quota` | 配额使用情况 |

## 目录结构

```
video-distill/
├─ src/vd/        # 核心包：web / pipeline / transcript / summarize / credentials ...
├─ scripts/       # 入口脚本：serve / distill / summarize / check_env ...
├─ tests/         # 单元测试（离线，256 项）
├─ docs/          # 设计文档与实测记录
├─ data/          # 凭据密文、SQLite、模型缓存（不入版本库）
├─ output/        # 产物
└─ .env           # 配置（已 gitignore）
```

## 常用命令

```powershell
# 启动 Web 服务
.venv\Scripts\python.exe scripts/serve.py

# 单视频取逐字稿（不走 Web）
.venv\Scripts\python.exe scripts/distill.py BV1xxxxxxxxx

# 对已有逐字稿生成总结
.venv\Scripts\python.exe scripts/summarize.py BV1xxxxxxxxx

# 环境自检
.venv\Scripts\python.exe scripts/check_env.py

# 单元测试（离线）
.venv\Scripts\python.exe -m pytest tests -q
```

## 当前进展

- ✅ P0 环境与探针
- ✅ P1 取数层（字幕优先 + ASR 兜底）
- ✅ P2 总结层（DeepSeek + 时间戳锚点校验）
- ✅ P3 Web 层（FastAPI + SQLite 队列 + 扫码登录 + 配额）
- 🔜 P4 部署与运维
