# fetch_assets.py —— 阶段 E：备份正文里的外链图片
#
# 为什么急：正文里的图基本都在推特图床（pbs.twimg.com）。这类链接可能会失效
# （改版、需登录、账号状态变化都可能），所以这一步越早做越好。
#
# 实测规模：
#   含图片的作品数、img 标签数、去重后的 URL 数（本项目实测：几十个量级）
#   域名：绝大多数是同一个图床，另有个别是网页链接而非直链
#   → 是个小活儿，不是大工程。
#
# 联网请求数：正文里内嵌了几个图片 URL 就几次（已抓成功的会跳过）
# 会写什么：data/ao3/works/{id}/assets/ 下的图片；index.sqlite 的 assets 表
# 不会写什么：**绝不改动 {id}.html 原件**；不删任何已下载的图
#
# 关键设计（DESIGN-NOTES.md N-18）：**append-only 资产池**。
#   图一旦下载就永久保留。日后你把某张图从文里撤掉，本地照样留着，
#   只是标 referenced=0 —— 与「已删作品保留」是同一条原则。
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'fetch_assets'

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import hashlib
    import html as htmllib
    import re
    import time
    from datetime import datetime
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    import config
    import fetch_index
    from ao3_client import confirm
    import requests
except Exception as _import_error:
    session_log.crash_dump("fetch_assets", _import_error)
    raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    original_url TEXT PRIMARY KEY,   -- 正文里写的原始 URL，**原样保存**
    fetch_url    TEXT,               -- 实际请求的 URL（可能换成了更高清的变体）
    local_path   TEXT,
    sha256       TEXT,
    bytes        INTEGER,
    content_type TEXT,
    http_status  INTEGER,
    status       TEXT,               -- ok / http_error / not_image / network_error / skipped
    note         TEXT,
    fetched_at   TEXT
);
CREATE TABLE IF NOT EXISTS asset_refs (
    original_url TEXT,
    work_id      INTEGER,
    referenced   INTEGER DEFAULT 1,  -- 0 = 正文里已不再引用，但**文件仍保留**（DESIGN-NOTES.md N-18）
    last_seen    TEXT,
    PRIMARY KEY (original_url, work_id)
);
"""

# ────────────────────────────────────────────────────────────────
# 图床白名单。AO3 的白名单在 ao3_client 里，这里是**另一套**、互不影响。
#
# ⚠️ **这份名单大概率不够你用，请自己往里加。**
#    本项目最初只测过推特图床（写这套代码的那个库里恰好都是），
#    但同人圈常用的图床远不止一家：Tumblr、Lofter、Imgur、Discord、
#    Poipiku、自建站……你的正文里挂在哪儿，就把哪个域名加进来。
#
# 怎么知道该加什么：**直接跑一遍**。脚本会把不在名单里的域名逐个报出来
#    （「域名是 xxx，不在图床白名单里」），照着加即可，然后重跑。
#
# 加进来是安全的：这份名单只决定「允许去哪些域名取图片字节」，
#    脚本仍然只发 GET，抓不到的会记状态、**不会当成功处理**。
#
# 标注说明：✅ = 本项目实测抓成功过；· = 常见但**没实测过**，
#    留在这里是为了省你查域名的功夫，不保证一定能抓到（有些站点会挡外链）。
ALLOWED_HOSTS = (
    # ✅ 实测通过
    "pbs.twimg.com", "pbs.twimg.com.", "video.twimg.com",   # Twitter / X
    # · 以下未实测，按需保留或删除
    "i.imgur.com", "imgur.com",                              # Imgur
    "64.media.tumblr.com", "media.tumblr.com",               # Tumblr
    "cdn.discordapp.com", "media.discordapp.net",            # Discord
    "img-original.poipiku.com", "poipiku.com",               # Poipiku
    # ← 把你自己的图床加在这里
)

_IMG_SRC = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_IMG_TAG = re.compile(r'<img[^>]*>', re.I)

MANUAL_FILE = config.CODE_DIR / "assets_manual.tsv"


def load_manual() -> tuple:
    """读人工补的图片直链（见 assets_manual.tsv 里的说明）。

    返回 (映射表, 被注释掉但看起来是真映射的行)。

    第二项是给人看的提示：文件里的示例行本来就是注释掉的，
    照着改完很容易忘了删行首那个 `#` —— 那样脚本会读到 0 条，
    看起来像「没生效」。与其让人困惑，不如直接指出来。
    """
    out, commented, ignored = {}, [], {}
    if not MANUAL_FILE.exists():
        return out, commented, ignored
    for line in MANUAL_FILE.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        is_comment = raw.startswith("#")
        body = raw.lstrip("#").strip() if is_comment else raw
        parts = re.split(r"\s*\t\s*|\s{2,}", body, maxsplit=1)
        if len(parts) != 2 or not parts[0].startswith("http"):
            continue
        src, dst = parts[0].strip(), parts[1].strip()

        # IGNORE = 这个 URL 不需要抓，也不要再提醒（例如失败的旧插入、后来另插了一张）
        is_ignore = dst.upper().startswith("IGNORE")
        looks_like_pair = dst.startswith("http") and "XXXX" not in dst

        if is_comment:
            if looks_like_pair:
                commented.append((src, dst))
            continue
        if is_ignore:
            ignored[src] = dst[len("IGNORE"):].lstrip(" :：#") or "（未注明原因）"
        elif looks_like_pair:
            out[src] = dst
    return out, commented, ignored


def missing_dimensions(html: str, url: str) -> bool:
    """这个 <img> 有没有 width/height 属性。**纯属附带观察，不作任何结论。**

    这里有两种很自然、但都站不住的推断，记下来免得再犯：

      ✗ 「缺 width/height ⇒ 图是坏的」
         反例：work 10000004 的图同样没有这两个属性，却下载正常（33 KB）。
         区别只是插入写法不同（自闭合 `<img ... />` vs 带尺寸的写法）。

      ✗ 「src 指向网页而非图片 ⇒ 在 AO3 上渲染不出来」
         作者实测：那张 x.com 的图在 AO3 页面上**显示正常**。
         为什么能显示没有查清 —— 但显然那个先验推理不成立。

    教训与DESIGN-NOTES.md N-02② 同类：**没验证过的推断不要写成结论。**
    本项目里唯一站得住的判断依据，是「实际请求过、看到了什么」。
    """
    for tag in _IMG_TAG.findall(html):
        if url.replace("&", "&amp;") in tag or url in tag:
            return "width=" not in tag and "height=" not in tag
    return False

EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif",
}


def collect_refs() -> tuple:
    """扫描所有下载件。原件只读，绝不修改。

    返回 ({url: set(work_id)}, {url: set(work_id) 疑似在 AO3 上已坏})
    """
    refs: dict[str, set] = {}
    broken: dict[str, set] = {}
    for p in sorted(config.WORKS_DIR.glob("*/[0-9]*.html")):
        wid = int(p.parent.name)
        t = p.read_text(encoding="utf-8", errors="ignore")
        for raw in _IMG_SRC.findall(t):
            url = htmllib.unescape(raw).strip()      # &amp; → &
            refs.setdefault(url, set()).add(wid)
            if missing_dimensions(t, url):
                broken.setdefault(url, set()).add(wid)
    return refs, broken


def upgrade_to_orig(url: str) -> str:
    """推特图床的 ?name=small/medium/900x900 都是缩略图。
    既然要备份，当然取**最高清的那一版**，所以统一换成 name=orig。
    原始 URL 仍原样记录在 original_url 里。

    ⚠️ 这一段**只对推特图床生效**（靠下面那个 endswith 判断）。
       别的图床有各自的缩略图规则（Tumblr 的 `_500.jpg`、Imgur 的 `xxxs.jpg` 等），
       没实测过就没写 —— **宁可存下缩略图，也不要拼一个猜出来的 URL**：
       猜错了会抓到 404，或者更糟：抓到一张不相干的图而毫无察觉。"""
    u = urlparse(url)
    if not u.netloc.endswith("twimg.com"):
        return url
    q = dict(parse_qsl(u.query))
    if "name" in q:
        q["name"] = "orig"
    return urlunparse(u._replace(query=urlencode(q)))


def main() -> int:
    session_log.start("fetch_assets")
    config.ensure_dirs()

    con = fetch_index.open_db()
    con.executescript(SCHEMA)
    now = datetime.now().isoformat(timespec="seconds")

    report: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        report.append(line)

    say(f"# 图片备份报告 · {datetime.now():%Y-%m-%d %H:%M}")
    say("")

    # ---- 1. 扫描引用（不联网）----
    refs, broken = collect_refs()
    manual, commented_out, ignored = load_manual()
    # ⚠️ 篇数要**现数**，不能写死 —— 写死的话，库一变（或换个人跑）报告就会说谎。
    n_files = len(list(config.WORKS_DIR.glob("*/[0-9]*.html")))
    say(f"扫描 {n_files} 篇下载件：**{len(refs)}** 个不同图片 URL，"
        f"分布在 **{len({w for ws in refs.values() for w in ws})}** 篇里")
    if broken:
        say("")
        say(f"（附带信息：**{len(broken)}** 张的 `<img>` 没有 width/height 属性。"
            f"这只说明插入方式不同，**不代表图坏了** —— 实测其中就有下载正常的。）")
    say("")

    # 记录引用关系；本次没扫到的标 referenced=0 但**不删文件**
    con.execute("UPDATE asset_refs SET referenced=0")
    for url, wids in refs.items():
        for wid in wids:
            con.execute(
                "INSERT OR REPLACE INTO asset_refs (original_url, work_id, referenced, last_seen) "
                "VALUES (?,?,1,?)", (url, wid, now))
    con.commit()

    # ---- 2. 挑要下的 ----
    done = {r[0] for r in con.execute("SELECT original_url FROM assets WHERE status='ok'")}
    todo = [(u, sorted(w)) for u, w in refs.items() if u not in done]

    # 人工补过直链的，用直链判断是否可抓
    def target(u: str) -> str:
        return manual.get(u, u)

    # 明确标了 IGNORE 的：记档并从待办里剔除，报告里不再当成「待补」
    for u, reason in ignored.items():
        con.execute(
            "INSERT OR REPLACE INTO assets "
            "(original_url, status, note, fetched_at) VALUES (?,?,?,?)",
            (u, "ignored", reason, now))
    if ignored:
        con.commit()
        say(f"## 已标记为无需抓取（{len(ignored)} 个）")
        say("")
        for u, reason in ignored.items():
            say(f"- `{u[:78]}`")
            say(f"  原因：{reason}")
        say("")

    todo = [(u, w) for u, w in todo if u not in ignored]

    needs_manual = [(u, w) for u, w in todo
                    if urlparse(target(u)).netloc not in ALLOWED_HOSTS]
    todo = [(u, w) for u, w in todo if urlparse(target(u)).netloc in ALLOWED_HOSTS]

    if manual:
        say(f"已从 `assets_manual.tsv` 读到 **{len(manual)}** 条人工直链")
        say("")

    if commented_out:
        say(f"## ⚠️ `assets_manual.tsv` 里有 {len(commented_out)} 行**被 `#` 注释掉了**")
        say("")
        say("这些行看起来是填好的映射，但行首有 `#`，所以脚本**没有读取它们**：")
        say("")
        for a, b in commented_out:
            say(f"- `{a[:70]}`")
            say(f"  → `{b[:70]}`")
        say("")
        say("**把行首那个 `# ` 删掉再重跑即可。**（文件里的示例行本来就是注释状态，"
            "照着改完很容易忘了删。）")
        say("")

    # 人工直链指向的图，如果**已经作为别的 original_url 存在库里**，多半是复制错了图
    dup = {}
    for src, dst in manual.items():
        row = con.execute(
            "SELECT original_url, bytes FROM assets WHERE original_url=? AND status='ok'",
            (dst,)).fetchone()
        if row:
            dup[src] = row
    if dup:
        say("## ⚠️ 有人工直链指向了**已经存过的另一张图**")
        say("")
        say("下面这些直链，库里已经以别的 `original_url` 存在了。可能是两处引用同一张图"
            "（那没问题），也可能是复制时点错了图（同一条推文里往往有好几张）。")
        say("")
        for src, (dst, size) in dup.items():
            say(f"- 原始：`{src[:66]}`")
            say(f"  直链：`{dst[:66]}`（库里已有，{size:,} 字节）")
        say("")
        say("→ **请对一眼**：如果那确实是同一张图，就不用管；如果不是，重新复制正确的图片地址。")
        say("")

    if needs_manual:
        say(f"## 需要你补一个图片直链（{len(needs_manual)} 个）")
        say("")
        say("这些 `<img src>` 不是图床直链，机器没法自动抓 —— 但**它们确实是正文里的图**，")
        say("不是无关链接。补上直链后重跑本脚本即可抓到。")
        say("")
        for u, w in needs_manual:
            say(f"- work **{w[0]}**：`{u}`")
            host = urlparse(u).netloc
            if host not in ALLOWED_HOSTS:
                say(f"  域名是 `{host}`，**不在图床白名单里**，所以脚本拿不到图片字节。")
                say(f"  → 若这是个正常图床，把 `{host}` 加进 `fetch_assets.py` 的 "
                    f"`ALLOWED_HOSTS` 再重跑就行。")
                say(f"  （**这不代表它在 AO3 上显示有问题** —— 那是另一回事，不要据此下结论。）")
            con.execute(
                "INSERT OR REPLACE INTO assets "
                "(original_url, status, note, fetched_at) VALUES (?,?,?,?)",
                (u, "needs_manual", "不是图床直链，等人工在 assets_manual.tsv 里补直链", now))
        say("")
        say("**怎么补**：打开那条推文 → 在图片上点右键 →「复制图片地址」→")
        say("把链接填进 `code/assets_manual.tsv`（文件里有格式说明和示例）。")
        say("")
        con.commit()

    say(f"已下载过 {len(done)} 个｜本次待下 **{len(todo)}** 个")
    say("")

    if not todo:
        say("没有需要下载的图片。")
        _write(report)
        return 0

    confirm(
        plan_lines=[
            f"下载 {len(todo)} 张图片（推特图床，与 AO3 是**两个独立的限流域**）",
            "尺寸参数统一升到 name=orig，备份最高清版本",
            "存到 data/ao3/works/{id}/assets/，文件名 = sha256(原URL) 前 16 位",
            "**绝不改动 {id}.html 原件**；下载失败会记录状态，日后可重试",
            f"间隔 {config.ASSET_DELAY_SEC:.0f} 秒",
        ],
        request_count=len(todo),
    )

    sess = requests.Session()
    sess.headers.update({"User-Agent": config.USER_AGENT})

    ok = failed = 0
    fail_rows = []
    for i, (url, wids) in enumerate(todo, 1):
        if i > 1:
            time.sleep(config.ASSET_DELAY_SEC)
        fetch_url = upgrade_to_orig(manual.get(url, url))
        print(f"\n[{i}/{len(todo)}] work {wids[0]}  {url[-46:]}")
        if url in manual:
            print(f"    → 用人工补的直链")
        elif fetch_url != url:
            print(f"    → 升到 orig 清晰度")

        status = http = ctype = None
        try:
            r = sess.get(fetch_url, timeout=config.REQUEST_TIMEOUT_SEC)
            http = r.status_code
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if r.status_code != 200:
                status, note = "http_error", f"HTTP {r.status_code}"
            elif not ctype.startswith("image/"):
                status, note = "not_image", f"Content-Type={ctype!r}"
            else:
                stem = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                ext = EXT_BY_TYPE.get(ctype, ".bin")
                adir = config.WORKS_DIR / str(wids[0]) / "assets"
                adir.mkdir(parents=True, exist_ok=True)
                dst = adir / f"{stem}{ext}"
                tmp = dst.with_suffix(dst.suffix + ".tmp")
                tmp.write_bytes(r.content)
                tmp.replace(dst)
                con.execute(
                    "INSERT OR REPLACE INTO assets (original_url, fetch_url, local_path, "
                    "sha256, bytes, content_type, http_status, status, note, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (url, fetch_url, str(dst.relative_to(config.MAIN_DIR)),
                     hashlib.sha256(r.content).hexdigest(), len(r.content),
                     ctype, http, "ok", None, now))
                ok += 1
                print(f"    ✓ {len(r.content):,} 字节 {ctype} → {dst.name}")
                con.commit()
                continue
        except requests.RequestException as e:
            status, note = "network_error", str(e)[:200]

        failed += 1
        fail_rows.append((url, wids[0], status, note))
        con.execute(
            "INSERT OR REPLACE INTO assets (original_url, fetch_url, http_status, "
            "status, note, fetched_at) VALUES (?,?,?,?,?,?)",
            (url, fetch_url, http, status, note, now))
        con.commit()
        print(f"    ✗ {status}：{note}")

    # ---- 报告 ----
    say("| 项 | 数 |")
    say("|---|---|")
    say(f"| 成功备份 | **{ok}** |")
    say(f"| 失败 | {failed} |")
    say("")

    if fail_rows:
        say("## 没抓下来的（**已记录状态，日后可重试；不当成功处理**）")
        say("")
        for u, w, st, note in fail_rows:
            say(f"- `{u[:80]}`")
            say(f"  work {w}｜{st}｜{note[:80]}")
        say("")
        say("→ 若大量 404 / 403，说明推特图床链接已失效。")
        say("  **备选来源**：Google Drive 的 docx 里内嵌着 387 MB 图片（占 dump 的 83%），")
        say("  很可能就是同一批图的幸存副本。见DESIGN-NOTES.md N-19。")
        say("")

    tot = con.execute("SELECT COUNT(*), SUM(bytes) FROM assets WHERE status='ok'").fetchone()
    say(f"资产池累计：**{tot[0]}** 张，{(tot[1] or 0) / 1e6:.1f} MB")
    say("")
    say("---")
    say("原件 `{id}.html` 一个字节都没改。已下载的图**永不删除**（DESIGN-NOTES.md N-18 append-only）。")

    _write(report)
    con.close()
    return 0


def _write(report: list[str]) -> None:
    # 不再写 reports/ 文件：内容与运行记录完全重复。
    print("\n（不再单独写报告文件 —— 完整输出已存进运行记录。）")


if __name__ == "__main__":
    sys.exit(main())
