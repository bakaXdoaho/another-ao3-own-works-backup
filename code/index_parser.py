# index_parser.py —— 解析作品索引页（/users/{name}/works?page=N）的 blurb
#
# ⚠️ 这是**零件**，不是要跑的脚本。fetch_index.py 会自动 import 它。
#    （直接 Run 它也无妨：会拿本地已存的原始页做一次离线自检，不联网、不写任何数据。）
#
# 只做解析，不联网、不写盘。这样可以拿已经存下来的原始页离线反复验证。
# 结构依据：实测（DESIGN-NOTES.md N-02④）。
#
# 设计原则：
#   * 解析不出来的字段一律留 None，**绝不猜**。
#   * 每个 blurb 都算一个 blurb_hash，用来做「改了但没推进 updated」的检测。
#   * 保留标签原文（尤其 relationship 里的 `/` 与 `&` 语义不同，见DESIGN-NOTES.md N-06②）。

from __future__ import annotations

import hashlib
import json
import re

# 一个 blurb 的起点
_BLURB_SPLIT = re.compile(r'(?=<li id="work_\d+"[^>]*class="[^"]*work blurb group)')

_RE = {
    "work_id":     re.compile(r'<li id="work_(\d+)"'),
    "updated_at":  re.compile(r"<!--\s*updated_at=(\d+)\s*-->"),
    "title":       re.compile(r'<h4 class="heading">\s*<a href="/works/\d+"[^>]*>(.*?)</a>', re.S),
    "datetime":    re.compile(r'<p class="datetime">([^<]*)</p>'),
    "rating":      re.compile(r'class="rating-[\w\-]+ rating"[^>]*title="([^"]*)"'),
    "category":    re.compile(r'class="category-[\w\-]+ category"[^>]*title="([^"]*)"'),
    "req_warning": re.compile(r'class="warning-[\w\-]+ warnings"[^>]*title="([^"]*)"'),
    "iswip":       re.compile(r'class="complete-(yes|no) iswip"'),
    "fandom_bloc": re.compile(r'<h5 class="fandoms heading">(.*?)</h5>', re.S),
    "summary":     re.compile(r'<blockquote class="userstuff summary">(.*?)</blockquote>', re.S),
    "series":      re.compile(r'Part\s*<strong>(\d+)</strong>\s*of\s*<a href="/series/(\d+)"[^>]*>(.*?)</a>', re.S),
    "collections": re.compile(r'<dd class="collections">(.*?)</dd>', re.S),
    "tag_a":       re.compile(r'<a class="tag"[^>]*>(.*?)</a>', re.S),
    "tag_li":      re.compile(r"""<li class=['"](warnings|relationships|characters|freeforms)['"]>(.*?)</li>""", re.S),
    "chapters":    re.compile(r'<dd class="chapters">(.*?)</dd>', re.S),
}

_STAT_NUM = {k: re.compile(rf'<dd class="{k}">(?:<a[^>]*>)?\s*([\d,]+)')
             for k in ("words", "comments", "kudos", "hits", "bookmarks")}
_LANG = re.compile(r'<dd class="language"[^>]*>(.*?)</dd>', re.S)


def _text(html: str) -> str:
    """去标签 + 还原实体 + 收拢空白。"""
    t = re.sub(r"<[^>]+>", "", html)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _int(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return None


def parse_blurb(b: str) -> dict | None:
    """解析一个 blurb。work_id 都取不到就返回 None（说明切错了）。"""
    m = _RE["work_id"].search(b)
    if not m:
        return None
    wid = int(m.group(1))

    # ---- 标签，按类别分组；保留原文（`/` vs `&` 的区别必须留住）----
    tags: dict[str, list[str]] = {"warnings": [], "relationships": [],
                                  "characters": [], "freeforms": []}
    for kind, inner in _RE["tag_li"].findall(b):
        for t in _RE["tag_a"].findall(inner):
            v = _text(t)
            if v and v not in tags[kind]:
                tags[kind].append(v)

    fandoms: list[str] = []
    fb = _RE["fandom_bloc"].search(b)
    if fb:
        fandoms = [_text(t) for t in _RE["tag_a"].findall(fb.group(1))]

    # ---- 章节 "2/?" 或 "5/5" ----
    ch_txt = ch_done = ch_total = None
    cm = _RE["chapters"].search(b)
    if cm:
        ch_txt = _text(cm.group(1))
        parts = ch_txt.split("/")
        if len(parts) == 2:
            ch_done = _int(parts[0])
            ch_total = _int(parts[1]) if parts[1].strip() != "?" else None

    # ---- 系列（一篇可属多个）----
    series = [{"part": int(p), "series_id": int(sid), "name": _text(nm)}
              for p, sid, nm in _RE["series"].findall(b)]

    # ---- 合集：**索引页只给数量，不给名字** ----
    # 实测：<dd class="collections"><a href="/works/ID/collections">1</a></dd>
    # 那个 "1" 是合集**个数**，不是合集名。第一版把它当名字存了，于是全库只有 "1"/"2" 两个
    # 假合集。合集名字只在官方下载件的 preface 里有（DESIGN-NOTES.md N-07）。
    collections_count = None
    cl = _RE["collections"].search(b)
    if cl:
        collections_count = _int(_text(cl.group(1)))

    def one(key: str) -> str | None:
        m2 = _RE[key].search(b)
        return _text(m2.group(1)) if m2 else None

    lang = _LANG.search(b)
    ua = _RE["updated_at"].search(b)
    wip = _RE["iswip"].search(b)
    summ = _RE["summary"].search(b)

    row = {
        "work_id": wid,
        "title": one("title"),
        "updated_at_unix": _int(ua.group(1)) if ua else None,
        "date_text": one("datetime"),
        "rating": one("rating"),
        "category": one("category"),
        "required_warning": one("req_warning"),
        "is_wip": (1 if wip and wip.group(1) == "no" else 0) if wip else None,
        "language": _text(lang.group(1)) if lang else None,
        "chapters_text": ch_txt,
        "chapters_done": ch_done,
        "chapters_total": ch_total,
        "fandoms": fandoms,
        "warnings": tags["warnings"],
        "relationships": tags["relationships"],
        "characters": tags["characters"],
        "freeforms": tags["freeforms"],
        "series": series,
        "collections_count": collections_count,
        "summary_html": summ.group(1).strip() if summ else None,
        "summary_text": _text(summ.group(1)) if summ else None,
    }
    for k, rx in _STAT_NUM.items():
        m3 = rx.search(b)
        row[k] = _int(m3.group(1)) if m3 else None

    # blurb 指纹：只挑「会变且需要关心」的字段，避免 hits/kudos 天天变导致全库误报
    # ⚠️ **`updated_at_unix` 已从指纹里去掉。**
    #    它是 AO3 给 blurb 片段做缓存时写下的时间戳，不是作品更新时间 ——
    #    实测 280 篇只有 22 个不同取值、99% 与别人共用、一次重抓 66 篇一起变。
    #    而这个哈希的用途是「改了但没推进 updated 的检测」（见文件头）：
    #    把一个会自己抖动的东西放进去，哈希就会在作品没变时照样变，检测因此失效。
    #    详见DESIGN-NOTES.md N-02③ 的更正。
    fingerprint = json.dumps({
        k: row[k] for k in (
            "title", "date_text", "rating", "category",
            "required_warning", "is_wip", "language", "chapters_text", "words",
            "fandoms", "warnings", "relationships", "characters", "freeforms",
            "series", "collections_count", "summary_text",
        )
    }, ensure_ascii=False, sort_keys=True)
    row["blurb_hash"] = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return row


def parse_page(html: str) -> list[dict]:
    """解析一整页，返回 blurb 列表（顺序即页面顺序）。"""
    out = []
    for chunk in _BLURB_SPLIT.split(html):
        if 'work blurb group' not in chunk[:300]:
            continue
        row = parse_blurb(chunk)
        if row:
            out.append(row)
    return out


def page_total_works(html: str) -> int | None:
    """页面顶部自报的作品总数。"""
    m = re.search(r"(\d[\d,]*)\s+Works? by", html) or re.search(r">\s*(\d[\d,]*)\s+Works?\s*<", html)
    return _int(m.group(1)) if m else None


def max_page(html: str) -> int | None:
    pages = re.findall(r'href="[^"]*[?&]page=(\d+)', html)
    return max(int(p) for p in pages) if pages else None


# ---------------------------------------------------------------- 筛选侧边栏
# 实测：索引页 HTML 里就带着 AO3 的筛选侧边栏，形如
#   <input ... id="include_work_search_relationship_ids_99999999" value="99999999" />
#   <span class="indicator"...></span><span>Yuris Leclair | Yuri Leclerc/Claude von Riegan (172)</span>
#
# 它白给三样 blurb 里没有的东西：
#   1. **AO3 内部 tag_id** —— 比标签名稳定（名字会被 wrangler 改，id 不会）
#   2. **canonical 标签名**
#   3. **canonical 计数** —— 已把 "implied X" 之类的同义/子标签归并进 X
#
# ⚠️ 限制：每个类别**只列前 10 个**。所以它是「权威但不完整」，
#    不能取代 blurb 标签，只能用来校验、并给大标签补上 tag_id。
_FACET_RE = re.compile(
    r'id="include_work_search_(\w+?)_(\d+)"[^>]*value="\d+"\s*/>\s*'
    r'<span class="indicator"[^>]*></span><span>(.*?)\s*\((\d+)\)</span>',
    re.S,
)


def parse_facets(html: str) -> list[dict]:
    """解析筛选侧边栏，返回 [{kind, tag_id, name, count}]。

    kind 取值：rating_ids / archive_warning_ids / category_ids /
              fandom_ids / character_ids / relationship_ids / freeform_ids
    """
    out = []
    for kind, tag_id, name, count in _FACET_RE.findall(html):
        out.append({
            "kind": kind,
            "tag_id": int(tag_id),
            "name": _text(name),
            "count": int(count),
        })
    return out


# ---------------------------------------------------------------- 离线自检
# 直接 Run 本文件时跑这个：拿本地已存的原始索引页解析一遍，打印字段完整率。
# **不联网、不写任何文件**，纯粹是「解析器还好使吗」的体检。
if __name__ == "__main__":
    import pathlib

    here = pathlib.Path(__file__).resolve().parent
    candidates = sorted((here.parent / "data" / "ao3" / "index_raw").glob("page-*.html"))
    candidates += sorted((here.parent / "data" / "probe_raw").glob("*index_p1*.html"))

    if not candidates:
        print("本地还没有任何索引页原始文件可供自检。")
        print("先跑一次 probe.py 或 fetch_index.py，之后再回来跑这个自检。")
        raise SystemExit(0)

    print("=" * 60)
    print("index_parser 离线自检（不联网、不写文件）")
    print("=" * 60)

    scalar = ["title", "updated_at_unix", "date_text", "rating", "category",
              "required_warning", "is_wip", "language", "chapters_text",
              "words", "kudos", "hits", "summary_text"]
    listy = ["fandoms", "relationships", "characters", "freeforms",
             "warnings", "series", "collections"]

    grand = 0
    for path in candidates:
        html = path.read_text(encoding="utf-8", errors="ignore")
        rows = parse_page(html)
        grand += len(rows)
        print(f"\n{path.name}：解析出 {len(rows)} 篇"
              f"｜页面自报总数 {page_total_works(html)}｜最大页码 {max_page(html)}")
        if not rows:
            print("  ⚠ 一篇都没解析出来 —— 页面结构可能变了，留着这个文件对照排查。")
            continue
        miss = [k for k in scalar
                if sum(1 for r in rows if r.get(k) not in (None, "", [])) == 0]
        print(f"  必有字段全空的：{miss or '无（都正常）'}")
        for k in listy:
            n = sum(1 for r in rows if r.get(k))
            print(f"    {k:16} {n:>3}/{len(rows)} 篇有值")
        first = rows[0]
        print(f"  样例：`{first['work_id']}` {first['title']}"
              f"｜{first['words']} 字｜{first['chapters_text']}｜{first['date_text']}")

    print(f"\n合计解析 {grand} 篇。自检结束，什么都没改动。")
