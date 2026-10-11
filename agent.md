# video-distill — AI 助手工作指南

> 本文件供 AI 助手快速理解项目并正确工作。修改代码前请先读完。

## 一、项目是什么

把 B 站视频「蒸馏」成文字资产：**逐字稿**（带时间戳）、**AI 总结**、**结构化要点**。产物以 Web 页面交付，每条结论可点回原视频时间点。

- 入口：`scripts/serve.py`（Web 服务）、`scripts/distill.py`（CLI 单视频）、`scripts/summarize.py`（CLI 总结）
- 包目录：`src/vd/`（通过 `package-dir = { "" = "src" }` 安装）
- 产物输出：`output/*.md`
- 数据目录：`data/`（凭据密文、SQLite、音频、模型缓存）

## 二、技术栈

| 层 | 选型 |
|----|------|
| Web | FastAPI + uvicorn |
| 队列 | SQLite + 进程内单工作线程 |
| 取数 | requests（B 站 API）+ 风控退避 |
| 转写 | B 站站内字幕优先，FunASR/Paraformer 兜底（GPU） |
| 总结 | DeepSeek API（OpenAI 兼容协议） |
| 加密 | cryptography.Fernet（凭据落盘） |
| 测试 | pytest（256 项，全部离线） |

## 三、目录结构

```
video-distill/
├─ src/vd/                # 核心包
│  ├─ web.py              # FastAPI 路由 + 会话 + 访问控制 (P3)
│  ├─ worker.py           # 单线程任务消费者 (GPU 串行)
│  ├─ pipeline.py         # 编排层：meta → 逐字稿 → 总结 → 落盘
│  ├─ transcript.py       # 取数层 (P1)：字幕优先 + ASR 兜底
│  ├─ bilibili.py         # B 站 API 客户端（风控/限速/退避）
│  ├─ asr.py              # FunASR 单例模型缓存
│  ├─ summarize.py        # 总结层 (P2)：DeepSeek + 时间戳锚点校验
│  ├─ credentials.py      # 凭据白名单（操作号/大号分离）
│  ├─ storage.py          # 凭据加密落盘 (Fernet)
│  ├─ quota.py            # 配额（按 B 站 UID，下载前判定）
│  ├─ tasks.py            # SQLite 任务队列
│  ├─ cache.py            # SQLite KV 缓存（TTL）
│  ├─ hotwords.py         # 共用热词修正表
│  ├─ disclaimer.py       # 免责声明单一事实源
│  └─ login.py            # B 站扫码登录状态机
├─ scripts/               # CLI 入口 + 探针脚本
├─ tests/                 # 单元测试 + 验收测试
├─ docs/                  # 设计文档与实测记录
├─ data/                  # 凭据密文、SQLite、音频、模型
├─ output/                # 产物
├─ .env / .env.example    # 配置（.env 已 gitignore）
└─ requirements.txt
```

## 四、核心数据流

```
用户提交 BV
  → web.py: can_submit() 校验身份 + 配额预检
  → tasks.sqlite3: QUEUED（按自增 seq 排序）
  → worker.py: 认领 → load_operator_credentials()（白名单）
  → pipeline.run_pipeline():
      ① cache 或 BiliClient.get_info()（缓存含权限标记）
      ② transcript.fetch_transcript()（字幕 → ASR → 热词修正）
      ③ summarize.summarize() + validate_points()（锚点校验）
      ④ 写 output/*.md
      ⑤ tasks: DONE + artifacts 清单
  → 用户浏览 /tasks/<id> 或 /artifacts
```

## 五、必须遵守的设计约束

这些不是风格偏好，是踩过坑后定的硬规则，改之前先看对应模块顶部的 docstring。

1. **取数只能用操作号凭据** — `credentials.py` 白名单强制，不是「优先选小号」。Web 层不重新实现。
2. **配额在下载音频前判定** — 用视频真实时长，先拒绝再烧带宽和 GPU。
3. **绝不产出空文件** — 逐字稿低于 80 字抛异常，不返回「成功但空」。
4. **匿名取字幕是静默失败** — 用 `NotLoggedInError` 异常区分「未登录」与「无字幕」，不能合并。
5. **时间戳锚点必须本地校验** — 模型编造的时间戳直接丢弃，不写进产物。
6. **本机访问免登录，远程必须扫码** — 判定基于 socket 对端地址，不是 Host 头。
7. **缓存必须含权限标记** — 否则受限视频的预览会被当成完整逐字稿。
8. **sqlite3 连接必须用 `closing()`** — `with` 只管事务、不关连接，会泄漏句柄。
9. **队列顺序用自增 seq，不用 created_at** — created_at 秒级精度，同秒任务排序不确定。
10. **前端不暴露内部架构细节** — 「操作号」「大号/小号」「取数凭据」等概念不出现在用户可见文案中。

## 六、开发约定

### 测试

- 命令：`.venv\Scripts\python.exe -m pytest tests -q`
- 单元测试必须离线（不依赖网络/GPU/真实凭据）
- 新增功能必须带测试，且不能破坏现有测试
- 验收测试 `tests/acceptance_p0.py`、`tests/acceptance_p3.py` 依赖真实环境，日常不跑

### 代码风格

- Python 3.11+，用 `from __future__ import annotations`
- 类型注解：函数签名标注，局部变量按需
- 每个模块顶部写 docstring 说明「为什么这么做」和踩过的坑
- 不配置 ruff/black/mypy，保持与现有代码风格一致即可
- 不要随意格式化原有代码

### 修改前端文案

- 用户可见的文案集中在 `web.py`（HTML 模板字符串）和 `disclaimer.py`（免责声明）
- 涉及安全/法律的文案改动要同步考虑 `tests/test_web.py` 里的断言
- 内部概念（操作号、大号、取数凭据等）不应对外展示

## 七、配置项（.env）

| 变量 | 用途 |
|------|------|
| `VD_SECRET_KEY` | 凭据加密密钥（Fernet） |
| `VD_OPERATOR_BILI_UID` | 操作号 UID（唯一可取数的账号） |
| `VD_OWNER_BILI_UID` | 管理员大号 UID（仅配额，不取数） |
| `VD_ADMIN_UIDS` | 其他管理员 UID（逗号分隔） |
| `VD_INVITED_UIDS` | 受邀使用者 UID |
| `VD_OPERATOR_IS_ADMIN` | 操作号是否算管理员（默认 true） |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥 |
| `DEEPSEEK_MODEL` | 模型名（默认 deepseek-flash） |

## 八、常见任务指引

### 新增一个 API 路由

在 `web.py` 里加 `@app.get/post(...)`，复用现有的 `current_session()`、`can_submit()`、`_page()` 等辅助函数。不要在路由里直接调 B 站 API，走 `pipeline` 或对应模块。

### 修改任务处理流程

改 `pipeline.py` 的 `run_pipeline()`。CLI 和 Worker 都走它，不要在 `scripts/` 或 `worker.py` 里写第二份编排。

### 调整配额规则

改 `quota.py` 的 `QuotaRule.for_uid()`。配额判定在 `pipeline.py` 里下载音频前调用，别挪位置。

### 改免责声明

只改 `disclaimer.py`，Web 页和产物头部共用同一份文本。改完跑 `test_web.py` 里的 disclaimer 相关测试。

### 排查「为什么取数失败」

1. 看 `data/tasks.sqlite3` 里任务的 error 字段
2. 看服务端日志（`worker.py` 会打异常堆栈）
3. 用 `scripts/check_env.py` 查凭据和配额
4. 用 `scripts/transcript_probe.py` 做最小复现

## 九、安全红线

- 绝不把 `.env` 或 `data/credentials/*.enc` 提交到版本库（已在 `.gitignore`）
- 不在前端展示凭据、密钥、内部 UID
- 不绕过 `credentials.py` 的白名单直接用其他账号的凭据取数
- 文件下载接口（`/files/{name}`）必须做路径穿越防护
