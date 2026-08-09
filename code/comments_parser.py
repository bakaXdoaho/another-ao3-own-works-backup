# comments_parser.py —— 解析 AO3 评论区（**纯解析，不联网**）
#
# 单独成一个模块，理由和 index_parser.py 一样：
#   原始 HTML 才是存档，数据库只是派生物。解析器改了、字段加了，
#   拿 data/comments/*.html 重跑一遍就行，不必再去 AO3 抓。
#
# ────────────────────────────────────────────────────────────────
# 实测得到的结构（样本：某篇四章的短作，11 条评论）
#
#   <ol class="thread">
#     <li class="odd comment group user-40000002" id="comment_30000001" role="article">
#       <h4 class="heading byline">
#         <a href="/users/ReaderName/pseuds/ReaderName">ReaderName</a>
#         <span class='parent'>on <a href="/works/10000003/chapters/20000002">Chapter 2</a></span>
#         <span class="posted datetime">
#           <abbr class="day" title="Tuesday">Tue</abbr> <span class="date">07</span>
#           <abbr class="month" title="October">Oct</abbr> <span class="year">2025</span>
#           <span class="time">08:11AM</span> <abbr class="timezone" title="…">PDT</abbr>
#         </span>
#       </h4>
#       <blockquote class="userstuff"><p>…</p></blockquote>
#       <ul class="actions">…</ul>
#     </li>
#     <li>                          ← ⚠️ 注意这里
#       <ol class="thread">         ← 回复挂在**另一个** <li> 里
#         <li class="… comment …" id="comment_30000002">   ← 这才是上一条的回复
#
# ⚠️ 最要命的一点，也是最容易写错的地方：
#   **回复并不嵌在被回复的那条 `<li>` 里面。** 被回复的 `<li>` 先闭合，
#   然后跟一个**没有 id 的空壳 `<li>`**，回复的 `<ol class="thread">` 才在那里面。
#   所以「数栈上有几个评论 li」来判断层级，**永远得 0**——
#   跑出来「11 条全是顶层」就是这么来的，看着还挺像回事。
#   正确做法：**层级由嵌套的 `<ol class="thread">` 决定**，与 `<li>` 无关。
#   改正后同一份文件解析出：**2 串、11 条、最深 7 层**，
#   与 AO3 stats 页的「Comment Threads」口径正好对得上（串数约为条数的三分之一）。
#
# 这条教训值得留着：**结构假设必须拿真实文件验一遍**，
#   而且「结果看起来合理」不等于「结果正确」——全是顶层也很像一份正常输出。
# ────────────────────────────────────────────────────────────────

from __future__ import annotations

import html as htmllib
import re
from html.parser import HTMLParser

# 评论区容器。AO3 把整个评论区放在这个 id 底下。
_PLACEHOLDER = 'id="comments_placeholder"'

_COMMENT_ID = re.compile(r"^comment_(\d+)$")
_LI_COMMENT = re.compile(r'<li[^>]*\bid="comment_(\d+)"[^>]*>')
_USER_CLASS = re.compile(r"\buser-(\d+)\b")
_PSEUD_LINK = re.compile(r'<a href="/users/([^/"]+)(?:/pseuds/([^"]+))?"[^>]*>(.*?)</a>', re.S)
_PARENT_CH = re.compile(
    r"<span class='parent'>\s*on\s*<a href=\"/works/\d+/chapters/(\d+)\"[^>]*>(.*?)</a>", re.S)
_POSTED = re.compile(r'<span class="posted datetime">(.*?)</span>\s*</h4>', re.S)
_BQ_OPEN = re.compile(r'<blockquote[^>]*class="[^"]*userstuff[^"]*"[^>]*>', re.I)
_BQ_ANY = re.compile(r"</?blockquote\b[^>]*>", re.I)
_PAGINATION = re.compile(r'<ol[^>]*class="[^"]*pagination[^"]*"', re.I)

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def comments_segment(html: str) -> str:
    """只取评论区那一段。取不到就返回空串（**绝不拿整页硬解**）。"""
    i = html.find(_PLACEHOLDER)
    return "" if i < 0 else html[i:]


def has_pagination(segment: str) -> bool:
    """评论区里有没有分页。有的话说明这一页**不是全部**，必须继续翻。"""
    return bool(_PAGINATION.search(segment))


def next_page_href(segment: str) -> str | None:
    """取 AO3 自己给的「下一页」链接。

    **不自己拼 URL。** 评论分页的参数没有实测过，
    拼错了要么抓空要么抓错篇，还不会报错。用页面上现成的 href 最稳。
    """
    m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>\s*Next\s*(?:&#8594;|→|»)?\s*</a>',
                  segment, re.I)
    return htmllib.unescape(m.group(1)) if m else None


# ---------------------------------------------------------------- 层级
class _ThreadDepth(HTMLParser):
    """只干一件事：算出每条评论嵌在第几层 `<ol class="thread">` 里。

    见文件头的说明——层级**只能**由 ol.thread 的嵌套决定。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.last_at: dict[int, int] = {}   # 层级 -> 该层最近一条评论 id
        self.order: list[tuple[int, int, int | None]] = []   # (cid, depth, parent)

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        if tag == "ol" and "thread" in (a.get("class") or ""):
            self.depth += 1
            return
        if tag != "li":
            return
        m = _COMMENT_ID.match(a.get("id") or "")
        if not m:
            return                       # 是 actions 里的 li，不是评论
        cid = int(m.group(1))
        d = max(self.depth - 1, 0)
        self.order.append((cid, d, self.last_at.get(d - 1)))
        self.last_at[d] = cid
        for k in [k for k in self.last_at if k > d]:
            del self.last_at[k]          # 更深的层已经收束，别再当父节点

    def handle_endtag(self, tag: str) -> None:
        if tag == "ol" and self.depth > 0:
            self.depth -= 1


# ---------------------------------------------------------------- 字段
def _strip(h: str) -> str:
    h = re.sub(r"<(script|style)\b.*?</\1>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"</p>", "\n", h, flags=re.I)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", h))


def _clean(h: str | None) -> str | None:
    if not h:
        return None
    t = re.sub(r"[ \t]+", " ", _strip(h)).strip()
    return t or None


def _blockquote(chunk: str) -> str | None:
    """取第一个 blockquote.userstuff 的**内层 HTML**。

    读者正文里可能自己也用 blockquote（引用作品原文），所以要数层级配对，
    不能拿最近的 </blockquote> 了事。
    """
    m = _BQ_OPEN.search(chunk)
    if not m:
        return None
    depth = 1
    for t in _BQ_ANY.finditer(chunk, m.end()):
        depth += -1 if t.group(0).startswith("</") else 1
        if depth == 0:
            return chunk[m.end():t.start()]
    return chunk[m.end():]               # 没配上就整段带走，宁可多不可少


def _posted(chunk: str) -> tuple[str | None, str | None]:
    """把 `Tue 07 Oct 2025 08:11AM PDT` 拼成 ISO 时间 + 时区名。

    时区**单独存**：AO3 按查看者的时区渲染，不留着就说不清这是谁的 8:11。
    """
    m = _POSTED.search(chunk)
    if not m:
        return None, None
    seg = m.group(1)

    def g(pat):
        mm = re.search(pat, seg, re.S)
        return mm.group(1).strip() if mm else None

    day = g(r'<span class="date">(\d+)</span>')
    mon = g(r'<abbr class="month" title="([^"]+)"')
    yr = g(r'<span class="year">(\d+)</span>')
    tm = g(r'<span class="time">([^<]+)</span>')
    tz = g(r'<abbr class="timezone"[^>]*>([^<]+)</abbr>')
    if not (day and mon and yr and tm):
        return None, tz
    try:
        h, mi = tm[:-2].split(":")
        h = int(h) % 12 + (12 if tm[-2:].upper() == "PM" else 0)
        return (f"{int(yr):04d}-{_MONTHS[mon]:02d}-{int(day):02d}"
                f"T{h:02d}:{int(mi):02d}:00"), tz
    except Exception:
        return None, tz                 # 解不出就留空，**不猜**


def parse_comments(html: str, my_user_id: int | None = None) -> list[dict]:
    """把一页评论区解析成一条一条记录。解不出就返回空列表。"""
    seg = comments_segment(html)
    if not seg:
        return []

    p = _ThreadDepth()
    p.feed(seg)
    if not p.order:
        return []
    meta = {cid: (d, par) for cid, d, par in p.order}

    # 按 `<li id="comment_N">` 把评论区切块：每条评论的 byline 与正文
    # 都在它自己那一块里（回复出现在文档后面，所以不会被切进来）。
    marks = list(_LI_COMMENT.finditer(seg))
    rows: list[dict] = []
    for n, m in enumerate(marks):
        cid = int(m.group(1))
        chunk = seg[m.start():(marks[n + 1].start() if n + 1 < len(marks) else len(seg))]
        d, parent = meta.get(cid, (0, None))

        uid = _USER_CLASS.search(m.group(0))
        uid = int(uid.group(1)) if uid else None
        who = _PSEUD_LINK.search(chunk)
        ch = _PARENT_CH.search(chunk)
        posted_at, tz = _posted(chunk)
        body_html = _blockquote(chunk)

        rows.append({
            "comment_id": cid,
            "chapter_id": int(ch.group(1)) if ch else None,
            "chapter_label": _clean(ch.group(2)) if ch else None,
            "parent_id": parent,
            "depth": d,
            "seq": n + 1,
            "user_id": uid,
            "username": who.group(1) if who else None,
            "pseud": (who.group(2) or who.group(1)) if who else None,
            "display_name": _clean(who.group(3)) if who else None,
            # 访客评论没有 /users/ 链接，也没有 user-N 类
            "is_guest": 0 if uid else 1,
            "is_mine": 1 if (my_user_id and uid == my_user_id) else 0,
            "posted_at": posted_at,
            "posted_tz": tz,
            "body": _clean(body_html),
            "body_html": body_html,
            "body_chars": len(re.sub(r"\s+", "", _clean(body_html) or "")),
        })

    # thread 归属：顺着 parent 一路往上找根。根就是这一串的主评论。
    by_id = {r["comment_id"]: r for r in rows}
    for r in rows:
        root, seen = r["comment_id"], set()
        while by_id.get(root, {}).get("parent_id") and root not in seen:
            seen.add(root)
            root = by_id[root]["parent_id"]
        r["thread_root_id"] = root
    return rows
