# config.py —— 全项目唯一的配置文件
#
# 所有脚本零参数可跑，默认值全在这里。要改行为，改这个文件，不要改别的脚本。
#
# 安全约定（全项目通用）：
#   1. 只发 GET。本项目代码里根本没有 POST/PUT/DELETE 的函数。
#   2. URL 白名单集中在 ao3_client.py，不在名单里的一律拒绝。
#   3. 先存原始字节，再解析。
#   4. 绝不原地覆盖。
#   5. 联网前打印计划、等回车确认。
#   6. fail closed：只接受确实像目标页面的响应，可疑的存进 probe_raw/ 留给人看。

from __future__ import annotations

import re
import sys
from pathlib import Path

# Python 3.9 没有 PEP 604 的 `str | None` 运行时支持，靠上面的 future import 兜住。
# 版本太低就直接说清楚，而不是抛一句看不懂的 TypeError。
if sys.version_info < (3, 8):
    sys.exit(f"需要 Python 3.8 以上，当前是 {sys.version.split()[0]}（{sys.executable}）")

# ---------------------------------------------------------------- 路径
# 以本文件位置为基准往上找，所以整个项目文件夹可以整体搬家。
CODE_DIR = Path(__file__).resolve().parent          # …/main/code
MAIN_DIR = CODE_DIR.parent                          # …/main
DATA_DIR = MAIN_DIR / "data"
SECRETS_DIR = MAIN_DIR / "secrets"

AO3_DIR = DATA_DIR / "ao3"
WORKS_DIR = AO3_DIR / "works"
SERIES_DIR = AO3_DIR / "series"
# 索引页原始 HTML。**覆写同路径**，历史交给 git（与作品件同一策略，DESIGN-NOTES.md N-23）
INDEX_RAW_DIR = AO3_DIR / "index_raw"
TEXT_DIR = DATA_DIR / "text"
# `data/reports/` 已于 20260805 停用（内容与运行记录 100% 重复），
# 作者随后把它改名为 `reports_leftover/`。这里**只留常量、不再建目录**：
#   常量留着，是因为万一还有旧代码引用它，起码不会 NameError；
#   不建目录，是因为 ensure_dirs() 每次都跑，会把刚改完名的空 `reports/` 又造回来。
REPORTS_DIR = DATA_DIR / "reports_leftover"      # 只读留档，任何脚本都不再往里写
PROBE_RAW_DIR = DATA_DIR / "probe_raw"
COMMENTS_DIR = DATA_DIR / "comments"
PRIVATE_DIR = DATA_DIR / "private"

INDEX_DB = DATA_DIR / "index.sqlite"
COOKIE_FILE = SECRETS_DIR / "ao3_cookie.txt"

# ---------------------------------------------------------------- 身份
AO3_HOST = "archiveofourown.org"
AO3_BASE = f"https://{AO3_HOST}"
AO3_USERNAME = "YOUR_AO3_USERNAME"

# AO3 希望爬虫自报家门。这不是伪装成浏览器，是礼貌。
USER_AGENT = (
    "YOUR_AO3_USERNAME-personal-archive/0.1 "
    "(personal backup of own works; low volume; contact via AO3 inbox)"
)

# ---------------------------------------------------------------- 节流
# AO3 限流是跨连接共享的：跑脚本时自己别同时刷 AO3。
# 登录态下限流按账号计，比游客更容易撞，所以间隔放宽。
# 间隔从「上一次响应结束」开始算（不是从请求开始算）。
# 20260804 实测：按 8 秒设、请求本身耗 1–3 秒，真实间隔只有 5–7 秒，抓到第 13 页就吃 429。
# 所以既调大了间隔，也改了计时口径。
REQUEST_DELAY_SEC = 15.0         # 普通页面（索引页很重，20 篇 blurb + 全量 tag）
REQUEST_TIMEOUT_SEC = 60         # 单次请求超时
DOWNLOAD_DELAY_SEC = 20.0        # /downloads/ 是服务端现生成文件，更贵，单独放宽

# 撞上 429 时自动退避重试。有 Retry-After 头就听它的。
# 重试用完才放弃 —— 而放弃也不是失败，重跑会复用已完成的部分。
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_WAIT_SEC = 300        # 5 分钟

# ---- 临时性故障的自动重试（20260805 加）----
# 20260805 实测：3 小时的正文抓取被打断 8 次，**没有一次是 429**，
# 全是 AO3/Cloudflare 侧的临时故障：HTTP 525 ×5、503/502 ×2、读取超时 ×1。
# 原先只有 fetch_works 在**外层**兜这类错，别的脚本一遇上就直接停。
# 现在把重试下沉到 ao3_client.get() 里 —— 所有脚本（含探测脚本）都自动获得韧性。
TRANSIENT_RETRIES = 3
TRANSIENT_WAIT_SEC = 60          # 临时故障等 1 分钟就够，不必像限流那样等 5 分钟

# 首次正式抓取只跑这么多篇。确认干净后再调大。
FIRST_RUN_WORK_LIMIT = 280 #20

# ---- 阶段 E（图片）----
# 推特图床与 AO3 是**两个独立的限流域**，节流必须分开计（DESIGN-NOTES.md N-19）。
# 图片是静态 CDN，比 AO3 轻得多，间隔可以短。
ASSET_DELAY_SEC = 3.0

# ---- 阶段 C（抓正文）的自动续跑 ----
# 被限流/网络出错而停下后，等一会儿自己接着跑，省得人守着一轮一轮点。
# ⚠️ 只对「等一等就能好」的错误生效（限流、网络抖动）。
#    登录失效**不自动重试** —— 等多久都没用，必须人去重取 cookie。
RESUME_WAIT_MIN = 10          # 停下后等多少分钟再续
RESUME_MAX_ROUNDS = 24 #6         # 最多自动续几轮（0 = 不自动续，跑一轮就退出）

# 索引页：若某页的原始文件是这么多小时以内抓的，重跑时直接复用、不再请求。
# 目的是「跑到第 9 页断了，重跑不用把前 8 页再抓一遍」。设 0 则每次都重抓。
INDEX_RAW_FRESH_HOURS = 6

# ---------------------------------------------------------------- 成人内容
# 无条件带上，无害，且把「成人内容确认闸」这个变量整个消掉。
ALWAYS_VIEW_ADULT = True


# ---------------------------------------------------------------- cookie
class CookieMissing(Exception):
    """cookie 文件不存在 / 是空的 / 格式明显不对时抛出，附带怎么修的说明。"""


_FIX_HINT = (
    "\n请按本仓库 README 的「Step 0 · 放好 cookie」 重取（**整条 Cookie 请求头**，不是单个值）：\n"
    "  1. Chrome 打开 https://archiveofourown.org ，确认右上角显示 Hi, YOUR_AO3_USERNAME!\n"
    "  2. ⌥⌘I 打开开发者工具 → 切到 **Network（网络）** 标签\n"
    "  3. 按 ⌘R 刷新页面，在左边列表里点**最上面那条**（名字通常就是 archiveofourown.org）\n"
    "  4. 右边选 **Headers** → 往下找 **Request Headers** → 找到 `Cookie:` 那一行\n"
    "  5. 右键那一行 → Copy value（或手动全选复制 `Cookie:` 冒号后面的一整串）\n"
    f"  6. 整串粘贴进 {COOKIE_FILE}（一行，不要加引号）\n"
    "\n  为什么要整条：AO3 的下载接口会检查不止一个 cookie。只给 _otwarchive_session\n"
    "  会被判成「Lost Cookie / Forced Logout」——而且它返回 HTTP 200，页面上还照样写着\n"
    "  Hi, YOUR_AO3_USERNAME!，属于会静默污染数据的那类失败。（20260804 实测踩到）\n"
)

# AO3 会用到的 cookie 名。用来判断文件里是「整条请求头」还是「光秃秃一个值」。
_KNOWN_COOKIE_NAMES = (
    "_otwarchive_session", "user_credentials", "remember_user_token",
    "view_adult", "accepted_tos", "flash_message",
)

# 缺了会导致下载接口 Forced Logout 的关键 cookie
_CREDENTIAL_COOKIES = ("user_credentials", "remember_user_token")

# ---------------------------------------------------------------- Cloudflare
# AO3 前面挂着 Cloudflare，浏览器里会多出 cf_clearance / __cf_bm / _cfuvid 这几个。
# **默认不发给 AO3**，理由三条：
#   1. `cf_clearance` 是与 **User-Agent + IP** 绑定的。我们故意用自报家门的 UA
#      （不伪装浏览器），UA 对不上反而更容易触发 Cloudflare 质询。
#   2. `__cf_bm` 只活 30 分钟，带着一个过期的没有任何意义。
#   3. 20260804 首跑时**根本没带这些**，首页、索引页、navigate 全部正常
#      —— 说明 AO3 的 Cloudflare 对我们这个 UA 并没有开质询。
# 万一日后真撞上 Cloudflare 质询，把下面这个改成 True 再试。
SEND_CLOUDFLARE_COOKIES = False
_CLOUDFLARE_COOKIES = ("cf_clearance", "__cf_bm", "_cfuvid", "__cflb", "cf_chl_rc_m")


def _extract_cookie_from_curl(text: str) -> str | None:
    """从 Chrome「Copy as cURL」粘贴的整段命令里挖出 cookie 串。

    Chrome 有时用 `-b '…'`，有时用 `-H 'cookie: …'`，两种都认。
    不是 cURL 就返回 None，交给别的分支处理。
    """
    if "curl " not in text[:200].lower():
        return None
    m = re.search(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", text, re.S)
    if m:
        return m.group(2)
    m = re.search(r"-H\s+(['\"])\s*cookie\s*:\s*(.*?)\1", text, re.S | re.I)
    if m:
        return m.group(2)
    return None


def load_cookies() -> dict:
    """读出 cookie。支持两种写法：

    1. **整条 Cookie 请求头**（推荐）：`_otwarchive_session=xxx; user_credentials=1; ...`
    2. 光一个 session 值（旧写法，向后兼容，但下载接口很可能会失败）

    有任何问题都给出可操作的报错，而不是让脚本带着坏 cookie 乱跑。
    """
    if not COOKIE_FILE.exists():
        raise CookieMissing(f"找不到 cookie 文件：{COOKIE_FILE}{_FIX_HINT}")

    raw = COOKIE_FILE.read_text(encoding="utf-8", errors="ignore").strip()
    if not raw:
        raise CookieMissing(f"cookie 文件是空的：{COOKIE_FILE}{_FIX_HINT}")

    # ---- 三种粘贴形式都认，怎么省事怎么来 ----
    # A. 整份 "Copy as cURL"（最省事，推荐）
    # B. 整条 Cookie 请求头，带不带 "Cookie:" 前缀都行
    # C. 光一个 _otwarchive_session 值（旧写法，下载接口很可能失败）
    from_curl = _extract_cookie_from_curl(raw)
    if from_curl:
        raw = from_curl
    if re.match(r"(?i)^cookie\s*:", raw):
        raw = re.split(r"(?i)^cookie\s*:", raw, maxsplit=1)[1]

    raw = raw.strip().strip("'\"").strip()
    raw = re.sub(r"\\\s*\n\s*", "", raw)      # 去掉 shell 续行
    raw = re.sub(r"\s*\n\s*", " ", raw).strip()

    if not raw:
        raise CookieMissing(f"从 {COOKIE_FILE} 里没解析出任何 cookie 内容。{_FIX_HINT}")

    is_header = ";" in raw or any(raw.startswith(n + "=") for n in _KNOWN_COOKIE_NAMES)

    if is_header:
        jar = {}
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
        if "_otwarchive_session" not in jar:
            raise CookieMissing(
                "这串 cookie 里没有 `_otwarchive_session`，可能复制的不是 AO3 的请求头。"
                + _FIX_HINT
            )
    else:
        if any(ch.isspace() for ch in raw):
            raise CookieMissing("cookie 中间有空格或换行，可能没复制完整。" + _FIX_HINT)
        if len(raw) < 50:
            raise CookieMissing(
                f"cookie 只有 {len(raw)} 个字符，太短了（正常几百字符）。" + _FIX_HINT
            )
        jar = {"_otwarchive_session": raw}

    return jar


def cookies_to_send(jar: dict) -> dict:
    """实际发出去的那一份。默认滤掉 Cloudflare 的几个（原因见上）。"""
    if SEND_CLOUDFLARE_COOKIES:
        return dict(jar)
    return {k: v for k, v in jar.items() if k not in _CLOUDFLARE_COOKIES}


def describe_cookies(jar: dict) -> str:
    """人可读的摘要——只报名字和长度，**永远不打印值**。"""
    send = cookies_to_send(jar)
    held = [k for k in jar if k not in send]

    lines = [f"      解析出 {len(jar)} 个 cookie，实际发送 {len(send)} 个（值不打印）："]
    for k, v in send.items():
        star = " ←关键" if k in _CREDENTIAL_COOKIES or k == "_otwarchive_session" else ""
        lines.append(f"        发送 · {k}（{len(v)} 字符）{star}")
    for k in held:
        lines.append(f"        保留不发 · {k}（{len(jar[k])} 字符）— Cloudflare，与 UA 绑定")

    missing = [c for c in _CREDENTIAL_COOKIES if c not in jar]
    if missing:
        lines.append(
            f"      ⚠ 缺少 {' / '.join(missing)} —— /downloads/ 很可能会被判 Forced Logout。"
        )
    return "\n".join(lines)


# 旧名字保留，免得别的脚本忘了改而炸掉
def load_cookie() -> str:
    return load_cookies()["_otwarchive_session"]


def ensure_dirs() -> None:
    """建好所有需要的目录。git 不跟踪空目录，所以每次跑都确认一下。"""
    # 注意：**REPORTS_DIR 不在这个名单里**（20260805 起）。
    # 那个文件夹已停用并被作者改名为 reports_leftover/，
    # 若还留在这里，每次跑都会把空的旧目录重新造出来。
    for d in (
        AO3_DIR, WORKS_DIR, SERIES_DIR, INDEX_RAW_DIR, TEXT_DIR,
        PROBE_RAW_DIR, COMMENTS_DIR, PRIVATE_DIR, SECRETS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
