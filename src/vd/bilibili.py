"""B 站接口客户端（P0 探针用）。

设计原则直接来自方案 §4 的实测结论：
  - 匿名拿站内字幕是**静默失败**（code=0 + 空列表），所以「未登录」与「无字幕」
    必须在类型层面区分开（R1）。这里用异常而非返回值表达"未登录"。
  - 风控 -352 真实存在 → 维持会话 Cookie、限速、指数退避（R4）。
  - 音频 CDN 必须带 Referer + 浏览器 UA，且 HEAD 会 404，只有 GET 正常（§4.3）。
"""

from __future__ import annotations

import functools
import hashlib
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass, field

import requests

from .storage import Credentials

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


class BiliError(RuntimeError):
    """接口返回业务错误码。"""

    def __init__(self, message: str, code: int = -1, context: str = ""):
        super().__init__(message)
        self.code = code
        self.context = context


class NotLoggedInError(BiliError):
    """当前请求没有登录态。

    与「该视频没有字幕」是完全不同的两件事，绝不能合并处理（R1）。
    """


class RiskControlError(BiliError):
    """风控 -352 / 412（R4）。"""


class NoSubtitleError(BiliError):
    """确认该视频确实没有站内字幕 —— 此时才允许走 ASR 兜底。"""


class AudioUnavailableError(BiliError):
    """**取不到音频**的基类。

    单独成类是因为它必须与「接口报错」区分开：用户看到的提示、以及该不该重试，
    两者完全不同。
    """


class RestrictedVideoError(AudioUnavailableError):
    """视频有权限限制（充电专属 / 付费预览 / 会员），拿不到完整媒体流。

    ## 这类错误必须单独识别，否则会产出"看起来成功"的残废产物

    实测（2026，`scripts/probe_playurl.py` + `probe_durl.py`）：

        is_upower_exclusive = True          # 充电专属
        is_ugc_pay_preview  = True          # 只能预览
        preview_toast       = '为创作付费，购买观看完整视频|购买观看'
        playurl: code=0, 无 dash，有 durl[1 段]
        durl[0].length = 249987ms  ← 250 秒
        视频真实时长  = 1749s      ← 29 分钟

    即：**接口"成功"返回了一段可播放的媒体流，但它只是 4 分钟的预览。**
    如果不识别这一点而直接降级到 durl，就会生成一份 250 秒的逐字稿，
    使用者会以为它是完整视频的 —— 这正是方案 §5.2③ 禁止的静默残废产物。

    注意 `code == 0` 且 `message == 'OK'`，所以**不能靠错误码判断**，
    必须查权限标记 + 比对媒体流时长与视频时长。
    """


class NoAudioTrackError(AudioUnavailableError):
    """接口正常，但该视频确实没有可用的音频流（如纯静态/无人声投稿）。"""


@dataclass
class VideoInfo:
    bvid: str
    cid: int
    title: str = ""
    duration: int = 0
    owner_name: str = ""
    owner_mid: int = 0
    desc: str = ""
    # 权限标记（来自 view / player 接口）。默认 False = 无限制。
    is_upower_exclusive: bool = False   # 充电专属
    is_ugc_pay_preview: bool = False    # 付费预览
    is_upower_play: bool = False        # 已充电/已购
    preview_toast: str = ""             # 平台给用户的提示语
    need_login: bool = False            # 需要登录才能看

    @property
    def restricted(self) -> bool:
        """是否受权限限制。

        `is_upower_play=True` 表示当前账号**已经**充电/购买，此时不算受限。
        """
        if self.is_upower_play:
            return False
        return bool(self.is_upower_exclusive or self.is_ugc_pay_preview or self.need_login)

    def restriction_reason(self) -> str:
        parts = []
        if self.is_upower_exclusive:
            parts.append("充电专属")
        if self.is_ugc_pay_preview:
            parts.append("仅预览")
        if self.need_login:
            parts.append("需要登录")
        return "、".join(parts)


@dataclass
class SubtitleTrack:
    lan: str
    lan_doc: str
    url: str
    is_ai: bool = False
    cues: list[dict] = field(default_factory=list)


def _mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def ts_hms(seconds: float) -> str:
    """秒 → HH:MM:SS。放在这里是因为多处都要用（日志、错误信息、产物）。"""
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


@functools.lru_cache(maxsize=8)
def _cached_keys(bili_jct: str) -> tuple[str, str]:
    """wbi 密钥按 cookie 缓存（lru_cache 以便同一登录态只取一次）。"""
    resp = requests.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"User-Agent": UA, "Referer": REFERER},
        timeout=15,
    )
    data = resp.json()
    wbi = (data.get("data") or {}).get("wbi_img") or {}
    img_url = wbi.get("img_url", "")
    sub_url = wbi.get("sub_url", "")
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        raise BiliError("无法从 nav 接口取得 wbi 密钥", context="nav")
    return img_key, sub_key


def wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    """按 B 站 wbi 规则签名（参数按 key 排序、过滤 !'()* 字符）。"""
    signed = dict(params)
    signed["wts"] = int(time.time())
    items = sorted(signed.items())
    query = urllib.parse.urlencode(
        [(k, "".join(ch for ch in str(v) if ch not in "!'()*")) for k, v in items]
    )
    signed["w_rid"] = hashlib.md5((query + _mixin_key(img_key, sub_key)).encode()).hexdigest()
    return signed


class BiliClient:
    """带限速与退避的 B 站客户端。"""

    def __init__(self, creds: Credentials | None = None, min_interval: float = 0.8):
        self.creds = creds
        self.min_interval = min_interval
        self._last_call = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Referer": REFERER,
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        if creds:
            for k, v in creds.cookies.items():
                self.session.cookies.set(k, v, domain=".bilibili.com")
        else:
            # 匿名也要先访问首页拿 buvid3，否则 -352（§4.4 实测）
            self.session.get("https://www.bilibili.com", timeout=15)

    # ---------- 底层 ----------

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap + random.uniform(0, 0.2))
        self._last_call = time.monotonic()

    def _get_json(
        self,
        url: str,
        params: dict | None = None,
        retries: int = 3,
        tolerate_codes: tuple[int, ...] = (),
    ) -> dict:
        """取 JSON。

        tolerate_codes 里的业务错误码会被**原样返回**而不是抛异常 —— 诊断脚本
        需要看到接口原文（例如匿名时 nav 返回 -101），否则"看不到真相"正是 R1
        这类静默陷阱的温床。
        """
        last: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=20)
            except requests.RequestException as exc:  # 网络层
                last = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 412:
                # 风控封禁，退避要更狠
                time.sleep(3 * (2**attempt))
                last = RiskControlError("HTTP 412 风控拦截", code=412, context=url)
                continue
            if resp.status_code != 200:
                last = BiliError(f"HTTP {resp.status_code}", code=resp.status_code, context=url)
                time.sleep(2**attempt)
                continue
            data = resp.json()
            code = data.get("code", 0)
            if code == 0 or code in tolerate_codes:
                return data
            if code == -352:
                last = RiskControlError("风控校验失败 (-352)", code=code, context=url)
                time.sleep(3 * (2**attempt))
                continue
            if code in (-101, -400, -403):
                raise NotLoggedInError(
                    f"接口要求登录态：code={code} message={data.get('message')}",
                    code=code,
                    context=url,
                )
            raise BiliError(
                f"接口错误 code={code} message={data.get('message')}",
                code=code,
                context=url,
            )
        raise last or BiliError("请求失败", context=url)

    def _signed(self, url: str, params: dict) -> dict:
        bili_jct = self.session.cookies.get("bili_jct", "")
        img_key, sub_key = _cached_keys(bili_jct)
        return self._get_json(url, wbi_sign(params, img_key, sub_key))

    @property
    def is_logged_in(self) -> bool:
        return bool(self.session.cookies.get("SESSDATA"))

    # ---------- 业务 ----------

    def nav(self) -> dict:
        """导航信息。未登录时返回的 data.isLogin=False —— 判别登录态最可靠的方式。

        注意：匿名调用时接口可能直接返回 code=-101，这里容忍该码，把 data 交给
        调用方判断（返回体里 isLogin 为 False）。
        """
        return self._get_json(
            "https://api.bilibili.com/x/web-interface/nav",
            tolerate_codes=(-101,),
        ).get("data") or {}

    def video_info(self, bvid: str) -> VideoInfo:
        data = self._get_json(
            "https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}
        )["data"]
        pages = data.get("pages") or []
        cid = pages[0]["cid"] if pages else data.get("cid", 0)
        info = VideoInfo(
            bvid=bvid,
            cid=cid,
            title=data.get("title", ""),
            duration=data.get("duration", 0),
            owner_name=(data.get("owner") or {}).get("name", ""),
            owner_mid=(data.get("owner") or {}).get("mid", 0),
            desc=data.get("desc", ""),
            is_upower_exclusive=bool(data.get("is_upower_exclusive")),
        )
        self._fill_restriction(info)
        return info

    def _fill_restriction(self, info: VideoInfo) -> None:
        """从 player 接口补充权限标记。

        为什么要多打一次请求：`view` 接口只给 `is_upower_exclusive`，
        而"是否只能看预览"（`is_ugc_pay_preview`）与平台提示语只在 player 接口里。
        这两者决定了要不要**提前**拒绝 —— 提前拒绝比下载完才发现只有 250 秒好得多。
        """
        try:
            params = {
                "bvid": info.bvid,
                "cid": info.cid,
                "isGaiaAvoided": "false",
                "web_location": 1315873,
            }
            data = self._signed(
                "https://api.bilibili.com/x/player/wbi/v2", params
            ).get("data") or {}
        except Exception as exc:  # noqa: BLE001
            # 补不到标记不算致命错误：后面的"实际时长比对"是更硬的兜底
            log.debug("补充权限标记失败（不影响主流程）：%s", exc)
            return
        info.is_upower_exclusive = bool(
            data.get("is_upower_exclusive") or info.is_upower_exclusive
        )
        info.is_ugc_pay_preview = bool(data.get("is_ugc_pay_preview"))
        info.is_upower_play = bool(data.get("is_upower_play"))
        info.preview_toast = data.get("preview_toast") or ""
        # ⚠️ permission 在不同视频上类型不一致（这里实测到的是字符串，不是字典）。
        # 直接 .get() 会 AttributeError —— 接口字段类型不是契约，必须防御。
        perm = data.get("permission")
        if isinstance(perm, dict):
            info.need_login = bool(perm.get("need_login"))
        elif isinstance(perm, str):
            info.need_login = perm.lower() in ("need_login", "login")
        if info.restricted:
            log.info(
                "视频受权限限制：%s（%s）", info.restriction_reason(), info.preview_toast
            )

    def subtitle_tracks(self, info: VideoInfo) -> list[SubtitleTrack]:
        """取站内字幕轨道。

        Raises:
            NotLoggedInError: 当前无登录态 → **无法判定**是否有字幕（R1 高危陷阱）。
            NoSubtitleError:  已确认**没有**站内字幕 → 才允许走 ASR 兜底。

        R1 的真实行为（本项目实测，见 docs/p0-findings.md）：
        匿名请求返回的是 `code=0` + `message='OK'`，且 `subtitle` 字段**存在**，
        内容为 `{'allow_submit': False, 'subtitles': []}` —— 也就是说它既不报错、
        也不是字段缺失，而是"体面地"返回一个空列表。任何只看 code 的实现都会把
        它当成"该视频没有字幕"，然后一路绿灯产出空文件。

        判定依据是 nav 的 isLogin（服务端权威），而不是本地有没有 Cookie：
        本地 Cookie 可能已失效，此时同样只能说"无法判定"，不能说"没有字幕"。
        """
        params = {
            "bvid": info.bvid,
            "cid": info.cid,
            "isGaiaAvoided": "false",
            "web_location": 1315873,
        }
        data = self._signed(
            "https://api.bilibili.com/x/player/wbi/v2", params
        )["data"]

        sub = data.get("subtitle")
        tracks = []
        if isinstance(sub, dict):
            for item in sub.get("subtitles") or []:
                url = item.get("subtitle_url") or ""
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = "https://aisubtitle.hdslb.com" + url
                tracks.append(
                    SubtitleTrack(
                        lan=item.get("lan", ""),
                        lan_doc=item.get("lan_doc", ""),
                        url=url,
                        is_ai=bool(item.get("ai_type")) or item.get("type") == 1,
                    )
                )
        if tracks:
            return tracks

        # 走到这里 = 没有可用轨道。到底是"真没有"还是"没权限"，必须查清。
        if not self.is_logged_in:
            raise NotLoggedInError(
                "接口返回空字幕列表，但当前请求是匿名的（无 SESSDATA）→ "
                "**无法区分「该视频没有字幕」与「未登录/无权限」**，按 R1 判定为"
                "「无法判定」。请先登录后重试。",
                context="player/wbi/v2",
            )
        if self.creds is not None:
            nav = self.nav()
            if not nav.get("isLogin"):
                raise NotLoggedInError(
                    "本地有 SESSDATA 但服务端判定未登录（凭证已失效）→ 无法判定字幕"
                    "情况，请重新扫码登录。",
                    context="player/wbi/v2",
                )
        if sub is None:
            raise BiliError(
                "已登录但接口未返回 subtitle 字段，无法判定。可能是接口变更。",
                context="player/wbi/v2",
            )
        raise NoSubtitleError(
            f"已确认该视频没有站内字幕（allow_submit={sub.get('allow_submit')}，"
            f"need_login_subtitle={data.get('need_login_subtitle')}）。",
            context="player/wbi/v2",
        )

    def fetch_cues(self, track: SubtitleTrack) -> list[dict]:
        """下载字幕 JSON。注意：字幕 CDN 的 Referer 必须带，UA 不能是默认 python-requests。"""
        if not track.url:
            return []
        resp = requests.get(
            track.url, headers={"User-Agent": UA, "Referer": REFERER}, timeout=20
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("body") or []

    def audio_url(self, info: VideoInfo) -> tuple[str, float]:
        """取音频流地址，返回 `(url, 该流的时长秒数)`。

        ## 为什么返回时长

        实测发现：**受限视频会"成功"返回一段预览媒体流**，`code=0`、`message='OK'`，
        播放起来完全正常，但只有 4 分钟。如果只看"有没有 url"，就会生成一份
        250 秒的逐字稿而使用者以为它是 29 分钟视频的 —— 静默残废产物。

        所以这里把时长一并返回，由调用方与视频时长比对；不一致就明确失败。

        ## 三种失败要分开

        之前这里只有一句 `BiliError("playurl 未返回 DASH 音频流")`，
        把权限受限、真的没有音频、接口变更糊在一起，用户和排查者都只能猜：

        | 情况 | 异常 | 用户该看到什么 |
        | --- | --- | --- |
        | 充电专属/付费预览 | `RestrictedVideoError` | 该视频需要充电/购买才能取完整内容 |
        | 真的没有音频轨 | `NoAudioTrackError` | 该视频没有可用音频 |
        | 既有 dash 又有 durl 都没有 | `AudioUnavailableError` | 接口异常，请反馈 |
        """
        # 已经知道受限就别浪费一次请求，直接给可读的原因
        if info.restricted:
            raise RestrictedVideoError(
                f"该视频受权限限制（{info.restriction_reason()}），无法获取完整音频。"
                + (f"平台提示：{info.preview_toast}" if info.preview_toast else ""),
                context="playurl",
            )

        raw = self._signed(
            "https://api.bilibili.com/x/player/wbi/playurl",
            {"bvid": info.bvid, "cid": info.cid, "fnval": 16, "fnver": 0, "fourk": 1},
        )
        data = raw.get("data") or {}

        dash = data.get("dash") or {}
        audios = dash.get("audio") or []
        if audios:
            best = max(audios, key=lambda a: a.get("bandwidth", 0))
            url = best.get("baseUrl") or best.get("base_url", "")
            # dash.duration 单位是秒；缺失时退回外层 timelength（毫秒）
            dur = dash.get("duration")
            if not dur:
                dur = (data.get("timelength") or 0) / 1000
            if url:
                return url, float(dur or 0)

        durl = data.get("durl") or []
        if durl:
            # 老格式/受限视频：单段 mp4。**必须校验时长**
            seg = durl[0]
            dur_ms = seg.get("length") or data.get("timelength") or 0
            return (seg.get("url") or ""), float(dur_ms) / 1000.0

        # 两种格式都没有 —— 这时才可能是"真的没有音频"或接口变更
        if info.is_upower_exclusive or info.is_ugc_pay_preview:
            raise RestrictedVideoError(
                f"该视频受权限限制（{info.restriction_reason()}），接口未返回任何媒体流。",
                context="playurl",
            )
        raise AudioUnavailableError(
            "playurl 既没有 dash 也没有 durl —— 可能是接口变更或该视频没有音频。"
            f"（code={raw.get('code')} message={raw.get('message')!r}，"
            f"data 字段={sorted(data.keys())[:12]}）",
            context="playurl",
        )
