# 快速开始

从零到跑通第一个视频，大约 10 分钟。

---

## 这个项目做什么

把一个 B 站视频变成**可读、可检索的文字资产**：

```
BV 号 ──► 逐字稿（.md） + 总结与要点（.summary.md）
```

产物不是"一段漂亮但没用的摘要"，而是**分段精炼陈述**：
去废话但不丢信息，目标是"只读这一份就够，不必再看视频"。

---

## 一、装依赖

需要 **Python 3.11+**。

```bash
git clone https://github.com/fullstop113/video-distill.git
cd video-distill

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

> 不需要 GPU，也不需要本地语音识别模型 —— 无字幕视频走云端 ASR
> （见第四节）。所以 2 核 2G 的服务器就够。

---

## 二、配两个必填项

```bash
cp .env.example .env          # Windows: Copy-Item .env.example .env
```

打开 `.env`，**只需要先填这两个**：

### 1. `DEEPSEEK_API_KEY`

去 https://platform.deepseek.com/api_keys 申请。它负责"逐字稿 → 总结"。

### 2. `VD_SECRET_KEY`

**这个不用申请，你自己生成。** 它的作用是加密存在本机磁盘上的
B 站登录凭据（SESSDATA 等 cookie）—— 设计意图是"密钥与数据分离"：
即使有人拿到了 `data/credentials/` 里的文件，没有这把密钥也解不开。

生成方式（复制到终端跑）：

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

把输出的那串字符填进 `VD_SECRET_KEY=`。

> ⚠️ **丢了会怎样**：已保存的 B 站登录会全部失效，需要重新扫码。
> 凭据文件还在，但解不开了。换机器时记得把它一起带走。
>
> ⚠️ **别提交到 git**。`.env` 已在 `.gitignore` 里，别改名或移出项目根目录。

填完自检：

```bash
python scripts/check_env.py
```

---

## 三、登录 B 站

本服务用你自己的 B 站账号取字幕（**免费**，不消耗任何付费额度）。

```bash
python scripts/login.py
```

会显示一个二维码，用 **B 站手机 App** 扫码即可。

> 💡 扫码用的是哪个账号，取数就用哪个账号。如果你在意风控风险，
> 请用**小号**登录，并把大号 UID 填进 `VD_OWNER_BILI_UID`
> （见 [configuration.md](configuration.md#账号角色)）。

---

## 四、跑第一个视频

命令行是**两步**（各管一件事，便于只重跑其中一步）：

```bash
# 第 1 步：BV → 逐字稿（会消耗配额）
python scripts/distill.py BV1Q3pe6GERF

# 第 2 步：逐字稿 → 总结与要点（会消耗 DeepSeek 费用）
python scripts/summarize.py BV1Q3pe6GERF
```

产物在 `output/` 下：

```
output/BV1Q3pe6GERF.md            ← 逐字稿
output/BV1Q3pe6GERF.summary.md    ← 总结与要点
```

想重跑总结而不重新取数（省时间与配额）：

```bash
python scripts/summarize.py BV1Q3pe6GERF --from-output
```

**有字幕的视频到这里就结束，不花钱**（只花 DeepSeek 那点费用）。

> 💡 嫌两步麻烦就用 Web 界面（第六节）—— 它一条流水线跑完全程。

---

## 五、可选：让"无字幕视频"也能处理

有些视频（尤其是直播回放、老视频）没有站内字幕。此时需要云端语音识别。

**代价**：约 0.288 元/小时，每月有 10 小时免费额度。
**有字幕的视频永远不花钱** —— 这是成本可控的关键。

要开启它，再加两个配置：

### 1. `DASHSCOPE_API_KEY`

去 https://bailian.console.aliyun.com/ 申请（阿里云百炼）。

### 2. `VD_PUBLIC_BASE_URL`

**这一项最容易搞错，所以解释一下为什么需要它。**

Paraformer 只接受**公网可访问的 URL**（官方 FAQ：不支持 Base64、
不支持二进制流、不支持本地文件）。而 B 站音频链接**外部拉不到** ——
B 站 CDN 校验 Referer，实测：

```
裸请求（无 UA、无 Referer）  → HTTP 403
带 Referer: bilibili.com     → HTTP 200
```

所以音频必须**由本服务中转一次**：

```
B站音频 → 本机下载 → https://你的地址/relay/<随机token> → 阿里云来取 → 取回即删
```

`VD_PUBLIC_BASE_URL` 就是那个"你的地址"：

```bash
# 有公网 IP
VD_PUBLIC_BASE_URL=http://123.45.67.89:8788

# 有域名（推荐，更安全）
VD_PUBLIC_BASE_URL=https://vd.example.com
```

> ⚠️ **不能**填 `127.0.0.1` 或 `10.x` / `192.168.x` —— 阿里云访问不到，
> 会得到 `FILE_403_FORBIDDEN`。

填完自检：

```bash
python scripts/check_deploy.py --remote
```

它会逐项告诉你：配置有没有、地址是不是公网的、端口通不通、
中转路由是否可达。**比你对着一个含糊的 403 猜要快得多。**

部署到服务器的完整步骤见 [deploy.md](deploy.md)。

---

## 六、可选：起 Web 界面

```bash
python scripts/serve.py                    # 只服务本机
python scripts/serve.py --host 0.0.0.0     # 对外开放（需配安全组）
```

打开 http://127.0.0.1:8788/ 即可提交任务、看进度、下载产物。

**本机访问免登录**（凭据就在本机磁盘上，登录只是仪式感）。
非本机访问需要扫码登录。

---

## 七、可选：给多人用（邀请制 + 余额）

默认只有管理员能用。要开放给别人：

1. **把对方的 B 站 UID 填进 `VD_INVITED_UIDS`**（逗号分隔）
   - 查自己 UID：登录 B 站后打开
     https://api.bilibili.com/x/web-interface/nav ，`data.mid` 就是
2. 对方扫码登录后即可提交任务
3. 对方在 `/balance` 页用**邀请码**充值；管理员在同一页生成邀请码

> **配额**与**余额**是两套独立的限制，都会生效：
> - **配额**限次数与总时长，防滥用（`VD_GUEST_DAILY_VIDEO_LIMIT` 等）
> - **余额**只被"无字幕视频"消耗（有字幕的不花钱）

---

## 常见问题

### 报"未配置 VD_PUBLIC_BASE_URL"

你要处理的是无字幕视频，但没配公网地址。见第五节。

### 报 `FILE_403_FORBIDDEN`

阿里云拉不到你的中转音频。三个可能：

1. `VD_PUBLIC_BASE_URL` 填的是内网地址 / `127.0.0.1`
2. 安全组没放行端口（服务器上）
3. 服务没监听 `0.0.0.0`（只在 `127.0.0.1` 上，外面进不来）

跑 `python scripts/check_deploy.py --remote` 能区分这三种。

### 报"该视频没有站内字幕"

这个视频确实没字幕。要么换一个视频，要么按第五节开启云 ASR。

### 产物里的专有名词转错了

语音识别的固有问题。本项目有热词表（`data/hotwords.json`），
可以修正特定的术语。对术语密集的视频（技术教程、专业分析）建议配上。

### 扫码一直不出图 / 扫不出来

B 站 App 的扫码通道偶尔不稳定。备用方案：

```bash
python scripts/login_from_browser.py
```

（在浏览器里登录后从浏览器读取凭据，不依赖 App 扫码。）

### 忘记 `VD_SECRET_KEY` 了

无法恢复 —— 加密凭据解不开了。重新生成一个，然后重新扫码登录。

---

## 配置项完整说明

见 [configuration.md](configuration.md)。想快速查"某个变量是干什么的"，
也可以直接看 `.env.example` —— 每个变量都写了"是什么、不填会怎样"。

---

## 更多文档

| 文档 | 内容 |
| --- | --- |
| [configuration.md](configuration.md) | 全部环境变量逐项说明 |
| [deploy.md](deploy.md) | 部署到云服务器（含安全组、公网地址） |
| [asr-cloud.md](asr-cloud.md) | 云 ASR 的原理、成本、踩坑记录 |
| [plan-bilibili-mvp.md](plan-bilibili-mvp.md) | 整体设计与技术选型 |
| [legal-review.md](legal-review.md) | 合规与风险说明（**建议先读**） |
