# ao3_client.py —— 唯一允许联网的模块
#
# 这个文件的存在意义就是：把「能对 AO3 做什么」这件事收拢到一处，从结构上不可能做坏事。
#
#   * 只有 GET。整个文件里没有 requests.post / put / delete / patch 的调用，
#     连 import 都没有把它们暴露出来。想删东西也没有代码可用。
#   * URL 白名单。不匹配 ALLOWED_PATHS 的一律拒绝，直接抛异常。
#   * 每次请求之间强制等待，全局共享计时器。
#   * 所有响应先原样落盘（save_raw），再交给别人解析。

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlencode, urlunparse, parse_qsl

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit(
        "缺少 requests 库。请在终端里跑一次：\n"
        "  python3 -m pip install requests\n"
        "（或在 PyCharm 里：设置 → Project → Python Interpreter → + → requests）"
    )

import config


# ---------------------------------------------------------------- 白名单
# 只允许这些路径。任何别的东西——尤其是任何带 edit / delete / new 的路径——都会被拒。
ALLOWED_PATHS = [
    re.compile(r"^/$"),                                   # 首页（登录检测用）
    re.compile(r"^/users/[A-Za-z0-9_\-]+$"),              # 用户页
    re.compile(r"^/users/[A-Za-z0-9_\-]+/works$"),        # 作品列表
    re.compile(r"^/users/[A-Za-z0-9_\-]+/gifts$"),        # 收到的赠文
    re.compile(r"^/works/\d+$"),                          # work 页
    re.compile(r"^/works/\d+/navigate$"),                 # 分章日期
    re.compile(r"^/works/\d+/chapters/\d+$"),             # 单章
    re.compile(r"^/works/\d+/comments$"),                 # 评论
    re.compile(r"^/downloads/\d+/[^/]+\.html$"),          # 官方下载件
    re.compile(r"^/series/\d+$"),                         # 系列页
]

# 出现这些字样的路径直接拒绝，属于第二道保险（白名单已经挡住了，但写出来更放心）
FORBIDDEN_SUBSTRINGS = ("edit", "delete", "new", "confirm_delete", "logout", "admin")


class UrlNotAllowed(Exception):
    pass


class FetchFailed(Exception):
    pass


class CloudflareChallenge(FetchFailed):
    """AO3 前面的 Cloudflare 拦下了我们。与 AO3 自己的限流是两回事，分开报。"""


_CF_HINTS = ("just a moment", "cf-browser-verification", "cf_chl_", "attention required")


def is_cloudflare_challenge(html: str) -> bool:
    low = html[:4000].lower()
    return any(h in low for h in _CF_HINTS)


def _check_path(path: str) -> None:
    low = path.lower()
    for bad in FORBIDDEN_SUBSTRINGS:
        if bad in low:
            raise UrlNotAllowed(f"路径里含禁用词 {bad!r}，拒绝访问：{path}")
    for pat in ALLOWED_PATHS:
        if pat.match(path):
            return
    raise UrlNotAllowed(f"路径不在白名单里，拒绝访问：{path}")


# ---------------------------------------------------------------- 客户端
class AO3Client:
    """极简的只读客户端。用法：
           c = AO3Client()
           html = c.get("/users/YOUR_AO3_USERNAME/works", params={"page": 1})
    """

    def __init__(self, cookies: dict | None = None, delay: float | None = None):
        self.cookies = cookies if cookies is not None else config.load_cookies()
        self.delay = config.REQUEST_DELAY_SEC if delay is None else delay
        self._last_request_at = 0.0
        self.request_count = 0

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en,zh;q=0.8",
        })
        for name, value in config.cookies_to_send(self.cookies).items():
            self._session.cookies.set(name, value, domain=config.AO3_HOST)

    # ---- 内部：节流 ----
    def _wait(self) -> None:
        """从**上一次响应结束**开始算间隔，不是从上一次请求开始算。

        20260804 实测教训：原先从「请求开始」计时，请求本身耗 1–3 秒，
        于是真实间隔只有 5–7 秒而不是设定的 8 秒 —— 抓到第 13 页就吃了 429。
        改成从响应结束计时，设多少就真的隔多少。
        """
        if self._last_request_at <= 0:
            return
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            print(f"    （等待 {remaining:.0f} 秒，遵守限流）", flush=True)
            time.sleep(remaining)

    # ---- 唯一的对外方法 ----
    def get(self, path: str, params: dict | None = None) -> str:
        """发一次 GET，返回 HTML 文本。路径必须以 / 开头，且在白名单内。

        撞上 429 会**自动退避重试**（默认 3 次，每次等 5 分钟或听 Retry-After 的），
        重试完还不行才抛异常交给调用方优雅收尾。
        """
        _check_path(path)

        params = dict(params or {})
        if config.ALWAYS_VIEW_ADULT:
            params.setdefault("view_adult", "true")

        url = urlunparse(("https", config.AO3_HOST, path, "", urlencode(params), ""))

        rate_tries = trans_tries = 0
        while True:
            self._wait()
            self.request_count += 1
            print(f"  [{self.request_count}] GET {url}", flush=True)

            try:
                resp = self._session.get(url, timeout=config.REQUEST_TIMEOUT_SEC)
            except requests.RequestException as e:
                self._last_request_at = time.monotonic()
                # 网络抖动 / 读取超时：等一会儿再来，别直接放弃整次运行
                if trans_tries < config.TRANSIENT_RETRIES:
                    trans_tries += 1
                    print(f"    ⚠ 网络出错（{type(e).__name__}）。等 "
                          f"{config.TRANSIENT_WAIT_SEC} 秒后重试"
                          f"（第 {trans_tries}/{config.TRANSIENT_RETRIES} 次）…", flush=True)
                    time.sleep(config.TRANSIENT_WAIT_SEC)
                    self._last_request_at = time.monotonic()
                    continue
                raise FetchFailed(f"网络请求失败（已重试 {trans_tries} 次）：{e}") from e

            self._last_request_at = time.monotonic()   # 从「响应结束」开始计间隔

            # ---- 5xx：AO3/Cloudflare 侧的临时故障，等一会儿多半就好了 ----
            if resp.status_code >= 500 and trans_tries < config.TRANSIENT_RETRIES:
                trans_tries += 1
                print(f"    ⚠ AO3 服务端错误（HTTP {resp.status_code}）。等 "
                      f"{config.TRANSIENT_WAIT_SEC} 秒后重试"
                      f"（第 {trans_tries}/{config.TRANSIENT_RETRIES} 次）…", flush=True)
                time.sleep(config.TRANSIENT_WAIT_SEC)
                self._last_request_at = time.monotonic()
                continue

            if resp.status_code != 429:
                break

            # ---- 429：退避重试 ----
            wait = config.RATE_LIMIT_WAIT_SEC
            hdr = resp.headers.get("Retry-After")
            if hdr and hdr.strip().isdigit():
                wait = max(int(hdr.strip()), 5)
            rate_tries += 1
            if rate_tries > config.RATE_LIMIT_RETRIES:
                raise FetchFailed(
                    f"被限流了（HTTP 429），已重试 {config.RATE_LIMIT_RETRIES} 次仍未通过。\n"
                    "  这不是故障，是正常现象。过一阵子直接重跑本脚本即可，"
                    "已完成的部分会被复用、不会重来。"
                )
            print(f"    ⚠ 被限流（HTTP 429）。等 {wait // 60} 分 {wait % 60} 秒后重试"
                  f"（第 {rate_tries}/{config.RATE_LIMIT_RETRIES} 次）…", flush=True)
            print("      期间请不要自己刷 AO3 —— 限流是跨连接共享的。", flush=True)
            time.sleep(wait)
            self._last_request_at = time.monotonic()

        if resp.status_code >= 500:
            raise FetchFailed(f"AO3 服务端错误（HTTP {resp.status_code}）。稍后重试。")

        # Cloudflare 质询：403 + 特征页。单独识别，免得当成 AO3 的错误去猜。
        if resp.status_code in (403, 503) and is_cloudflare_challenge(resp.text):
            raise CloudflareChallenge(
                f"撞上 Cloudflare 质询（HTTP {resp.status_code}）。\n"
                "  可以试：把 config.py 里的 SEND_CLOUDFLARE_COOKIES 改成 True，\n"
                "  并重新用 Copy as cURL 取一次 cookie（cf_clearance 与浏览器 UA 绑定且很快过期）。\n"
                "  若仍不行，隔一阵子再跑——质询通常是临时的。"
            )

        resp.encoding = "utf-8"          # 显式 UTF-8，不信自动探测
        return resp.text


# ---------------------------------------------------------------- 落盘
def save_raw(text: str, label: str, directory: Path | None = None) -> Path:
    """把原始响应存进 probe_raw/（或指定目录），文件名带时间戳，绝不覆盖已有文件。

    先存原始字节再解析 —— 这样解析写错了只要重跑解析，不用重新下载。
    """
    directory = directory or config.PROBE_RAW_DIR
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", label)[:60]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}_{safe}.html"
    n = 1
    while path.exists():                     # 绝不原地覆盖
        path = directory / f"{stamp}_{safe}_{n}.html"
        n += 1
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 限流页识别
_RATE_LIMIT_HINTS = ("retry later", "please try again", "rate limit")


def looks_like_rate_limit(html: str) -> bool:
    """限流页的特征：极短，且含提示语。这是启发式，不是断言——
    真正的保护是调用方 fail closed（只接受确实像目标页面的响应）。"""
    if len(html) > 20000:
        return False
    low = html.lower()
    return any(h in low for h in _RATE_LIMIT_HINTS)


# ---------------------------------------------------------------- 会话守卫
# 实测确定（20260804，check_login 首次运行）：AO3 已登录页面的头部长这样——
#     <nav id="greeting" aria-label="User">
#       <a href="/users/YOUR_AO3_USERNAME">Hi, YOUR_AO3_USERNAME!</a>
# 用带用户名的那一句做主判据，比只看 id="greeting" 强：它同时断言「登录的是谁」。
_GREETING_RE = re.compile(r'id="greeting"')


def _hi_marker() -> str:
    return f"Hi, {config.AO3_USERNAME}!"


class SessionLost(Exception):
    """登录标记消失。这不是故障，是 session 过期，重取 cookie 即可续跑。"""


class ForcedLogout(SessionLost):
    """AO3 的「Lost Cookie / Forced Logout」页。

    20260804 实测踩到：只带 _otwarchive_session 去请求 /downloads/ 时，AO3 返回
      · HTTP **200**
      · <title>Lost Cookie Home</title>，正文写着 "Forced Logout"
      · 而页首**照样**渲染着 "Hi, YOUR_AO3_USERNAME!" 和 id="greeting"
    所以它能骗过登录标记检查 —— 属于DESIGN-NOTES.md N-01 头号杀手的同一类：
    看起来成功、实则是错误页，只看状态码或只看登录标记都会把它当正文存进库。
    必须单独识别。
    """


_FORCED_LOGOUT_RE = re.compile(r"Lost Cookie|Forced Logout", re.I)


def is_forced_logout(html: str) -> bool:
    return bool(_FORCED_LOGOUT_RE.search(html))


def assert_logged_in(html: str, where: str = "页面") -> None:
    """DESIGN-NOTES.md N-22 的运行期会话守卫。

    掉登录后会静默拿到游客视图，受限作品直接 404 —— 会被误记成「已从 AO3 消失」。
    所以每次抓索引页都断言一次，一旦失守就**立刻停止本次运行**，绝不以游客身份继续。
    """
    # 顺序要紧：Forced Logout 页同时含有 "Hi, 用户名!"，所以必须先查它。
    if is_forced_logout(html):
        raise ForcedLogout(
            f"{where}：AO3 返回了「Lost Cookie / Forced Logout」页。\n"
            "  典型原因：cookie 给得不全。AO3 的部分接口（已知 /downloads/）会检查\n"
            "  user_credentials 等 cookie，只给 _otwarchive_session 不够。\n"
            + config._FIX_HINT
        )

    if _hi_marker() in html:
        return

    if _GREETING_RE.search(html):
        raise SessionLost(
            f"{where}：有 greeting 区块但不是 {config.AO3_USERNAME} —— "
            "登录的可能是别的账号。已停止，请检查 cookie 来源。"
        )

    raise SessionLost(
        f"{where}：找不到登录标记 {_hi_marker()!r}，说明当前是游客视图。\n"
        "  这不是故障，是 session 过期了，属于正常现象。\n"
        "  请重做本仓库 README 的「Step 0 · 放好 cookie」 重取 cookie，然后直接重跑本脚本——\n"
        "  已完成的部分不会重来。"
    )


def confirm(plan_lines: list[str], request_count: int) -> None:
    """联网前把计划打印出来，等回车。直接关掉窗口 / Ctrl-C 就是取消。"""
    print("\n" + "=" * 60)
    print("即将执行：")
    for line in plan_lines:
        print("  · " + line)
    print(f"\n  预计联网请求数：{request_count} 次")
    print(f"  请求间隔：{config.REQUEST_DELAY_SEC:.0f} 秒")
    print("  本脚本只发 GET，不会修改或删除 AO3 上的任何东西。")
    print("=" * 60)
    try:
        input("\n按回车继续，或按 Ctrl-C 取消： ")
    except (KeyboardInterrupt, EOFError):
        print("\n已取消，什么都没做。")
        sys.exit(0)
