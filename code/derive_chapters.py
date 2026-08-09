# derive_chapters.py —— 阶段 D：从本地下载件派生**章级信息**（完全不联网）
#
# 20260805 改名自 derive_words.py。作者的意见是对的：
#   这一步不该以「字数」为中心，而该以**「章」这个单位**为中心。
#   字数只是章级信息之一；日后评论、图片、其它逐章的东西都要挂在同一个骨架上。
#
# 章的骨架已经有了 —— `chapters` 表，主键 (work_id, idx)，且带 **AO3 自己的 chapter_id**。
# 那个 chapter_id 是关键：**AO3 的评论就是按章挂的**，评论 URL 里带的正是 chapter_id
# （DESIGN-NOTES.md N-17 已实测到这种形式：`works/10000005/chapters/20000003`）。
# 所以评论子系统日后可以直接 JOIN 上来，不必再想办法对齐。
#
# 本脚本从每篇下载件里派生：
#   · words_local   分章字数        ← AO3 只给全文总数，分章必须自己算
#   · summary       Chapter Summary  ← DESIGN-NOTES.md N-04 当初点名「必须存」，此前一直没兑现
#   · notes         Chapter Notes    ← 同上
#   · end_notes     Chapter End Notes
#   · body_chars    正文字符数（含标点，与 words_local 口径不同，做校验用）
#   · title_in_file 下载件里的章标题（与 navigate 来的标题互相印证）
#   · images        本章正文里引用了几张图（与 assets 表挂钩）
#
# 联网请求数：**0**。只读 data/ao3/works/*/{id}.html
# 会写什么：index.sqlite 的 chapters 各列、work_words 表
# 不会写什么：不碰任何原始件，不删任何东西
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'derive_chapters'

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import html as htmllib
    import re
    import sqlite3
    from datetime import datetime
    import config
    import fetch_index
    import fetch_works
except Exception as _import_error:
    session_log.crash_dump("derive_chapters", _import_error)
    raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS work_words (
    work_id       INTEGER PRIMARY KEY,
    words_ao3     INTEGER,
    words_local   INTEGER,
    delta         INTEGER,
    chapters_seen INTEGER,
    method        TEXT,
    computed_at   TEXT
);
"""

# chapters 表上要补的列（老库平滑升级，ALTER 失败说明已经有了）
NEW_COLS = [
    ("words_local", "INTEGER"),
    ("body_chars", "INTEGER"),
    ("summary", "TEXT"),
    ("notes", "TEXT"),
    ("end_notes", "TEXT"),
    ("title_in_file", "TEXT"),
    ("images", "INTEGER"),
    ("derived_at", "TEXT"),
]

# 计数口径版本号。改规则**必须换版本号**，否则历史数字无从对照。
METHOD = "han-chars+latin-tokens v1"

_HAN = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_LATIN_TOKEN = re.compile(r"[A-Za-zÀ-ɏ]+(?:['’-][A-Za-zÀ-ɏ]+)*")
_KANA = re.compile(r"[぀-ゟ゠-ヿ]+")
_IMG = re.compile(r"<img\b", re.I)


def count_words(text: str) -> int:
    """DESIGN-NOTES.md N-09 的口径：汉字按字，拉丁与假名按词；标点空白不计。"""
    return (len(_HAN.findall(text))
            + len(_LATIN_TOKEN.findall(text))
            + len(_KANA.findall(text)))


def strip_tags(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p>", "\n", html, flags=re.I)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", html))


def _clean(html: str | None) -> str | None:
    if not html:
        return None
    t = re.sub(r"\s+", " ", strip_tags(html)).strip()
    return t or None


def _blockquote_after(header_html: str, label: str) -> str | None:
    """章头里形如：<p>Chapter Summary</p><blockquote class="userstuff">…</blockquote>"""
    m = re.search(
        rf"<p>\s*{re.escape(label)}\s*</p>\s*<blockquote[^>]*>(.*?)</blockquote>",
        header_html, re.S | re.I)
    return m.group(1) if m else None


def parse_chapters(html: str) -> list[dict]:
    """把 div#chapters 切成一章一条记录。

    ⚠️ 两个都得靠「**直接**子节点」才能分清（DESIGN-NOTES.md N-04）：
       · `class="userstuff"` 在 5 章的作品里出现 **19 次**，只有 5 次是正文；
         其余 14 次是 Chapter Summary / Chapter Notes 的 blockquote，
         它们嵌在 `div.meta.group` 里面，同样带 userstuff 类。
       · 顶层 `div.meta group` = 该章的**头**（h2 标题 + Summary + Notes）；
         顶层 `div.meta`（不带 group）= 上一章的**章末注**。

    所以必须扫顶层 div 并记住顺序，不能全文搜关键词。
    """
    if '<div id="chapters"' not in html:
        return []
    ch = html.split('<div id="chapters"', 1)[1].rsplit('<div id="afterword"', 1)[0]

    out: list[dict] = []
    pending_header: str | None = None
    depth, start = 0, None

    for m in re.finditer(r"<(/?)div\b([^>]*)>", ch):
        if m.group(1) != "/":
            if depth == 0:
                start = m
            depth += 1
            continue
        depth -= 1
        if depth != 0 or start is None:
            continue

        attrs = start.group(2)
        inner = ch[start.end():m.start()]
        cls = re.search(r'class="([^"]*)"', attrs)
        cls = cls.group(1).strip() if cls else ""
        start = None

        if cls == "meta group":
            pending_header = inner
        elif cls == "userstuff":
            head = pending_header or ""
            title = re.search(r"<h2[^>]*>(.*?)</h2>", head, re.S)
            out.append({
                "title_in_file": _clean(title.group(1)) if title else None,
                "summary": _clean(_blockquote_after(head, "Chapter Summary")),
                "notes": _clean(_blockquote_after(head, "Chapter Notes")),
                "end_notes": None,
                "words_local": count_words(strip_tags(inner)),
                "body_chars": len(re.sub(r"\s+", "", strip_tags(inner))),
                "images": len(_IMG.findall(inner)),
            })
            pending_header = None
        elif cls == "meta" and out:
            # 不带 group 的 meta = 上一章的章末注
            out[-1]["end_notes"] = _clean(
                re.sub(r"<p>\s*Chapter End Notes\s*</p>", "", inner, flags=re.I))
    return out


def main() -> int:
    session_log.start("derive_chapters")
    config.ensure_dirs()

    if not config.INDEX_DB.exists():
        print("✗ 还没有 data/index.sqlite。先跑 fetch_index.py。")
        return 1

    con = fetch_index.open_db()
    con.executescript(fetch_works.SCHEMA)
    con.executescript(SCHEMA)
    for name, typ in NEW_COLS:
        try:
            con.execute(f"ALTER TABLE chapters ADD COLUMN {name} {typ}")
        except sqlite3.OperationalError:
            pass                     # 已经有这列了
    now = datetime.now().isoformat(timespec="seconds")

    print(f"计数口径：{METHOD}")
    print("（汉字按字、拉丁与假名按词；标点、空白、summary/notes 均不计入字数）\n")

    files = sorted(config.WORKS_DIR.glob("*/[0-9]*.html"))
    print(f"待处理 {len(files)} 个下载件 …\n")

    ok = mismatch = skipped = 0
    n_sum = n_notes = n_end = n_img = 0
    worst: list[tuple] = []
    tot_local = tot_ao3 = 0

    for i, p in enumerate(files, 1):
        wid = int(p.parent.name)
        html = p.read_text(encoding="utf-8", errors="ignore")
        chs = parse_chapters(html)
        if not chs:
            skipped += 1
            print(f"  ⚠ {wid}：切不出章，跳过（未写入任何东西）")
            continue

        for n, c in enumerate(chs, 1):
            con.execute(
                "UPDATE chapters SET words_local=?, body_chars=?, summary=?, notes=?, "
                "end_notes=?, title_in_file=?, images=?, derived_at=? "
                "WHERE work_id=? AND idx=?",
                (c["words_local"], c["body_chars"], c["summary"], c["notes"],
                 c["end_notes"], c["title_in_file"], c["images"], now, wid, n))
            n_sum += bool(c["summary"])
            n_notes += bool(c["notes"])
            n_end += bool(c["end_notes"])
            n_img += c["images"]

        local = sum(c["words_local"] for c in chs)
        m = fetch_works._STAT_WORDS.search(html)
        ao3 = int(m.group(1).replace(",", "")) if m else None
        con.execute(
            "INSERT OR REPLACE INTO work_words "
            "(work_id, words_ao3, words_local, delta, chapters_seen, method, computed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (wid, ao3, local, (local - ao3) if ao3 else None, len(chs), METHOD, now))

        if ao3:
            tot_local += local
            tot_ao3 += ao3
            rel = abs(local - ao3) / ao3
            (worst.append((rel, wid, ao3, local, local - ao3, len(chs)))
             if rel > 0.02 else None)
            mismatch += rel > 0.02
            ok += rel <= 0.02
        if i % 50 == 0:
            print(f"  … 已处理 {i}/{len(files)}")

    con.commit()

    print("\n" + "=" * 62)
    print("章级信息（本次派生）")
    print(f"  有 Chapter Summary   {n_sum:>4} 章")
    print(f"  有 Chapter Notes     {n_notes:>4} 章")
    print(f"  有 Chapter End Notes {n_end:>4} 章")
    print(f"  正文内嵌图片合计     {n_img:>4} 处")
    print("\n字数对账")
    print(f"  误差 ≤2%   {ok} 篇｜误差 >2%  {mismatch} 篇｜切不出章 {skipped} 篇")
    print(f"  全库合计：本地 {tot_local:,}｜AO3 {tot_ao3:,}｜差 {tot_local - tot_ao3:+,}"
          f"（{abs(tot_local - tot_ao3) / max(tot_ao3, 1) * 100:.2f}%）")

    if worst:
        print(f"\n  误差最大的几篇：")
        print(f"  {'work_id':>10} {'AO3':>9} {'本地':>9} {'差':>8} {'相对':>7}  章数")
        for rel, wid, a, l, d, n in sorted(worst, reverse=True)[:10]:
            print(f"  {wid:>10} {a:>9,} {l:>9,} {d:>+8,} {rel*100:>6.1f}%  {n}")
        print("\n  → 本地口径与 AO3 口径本来就不同，**不必强求对齐**；")
        print("    要紧的是这个差可解释、可复现，且带版本号可追溯。")

    print("\n所有章级字段已写入 `chapters` 表，主键 (work_id, idx)，另带 AO3 的 chapter_id。")
    print("日后评论按 chapter_id 挂进来即可，不需要再对齐一次。")
    print("联网请求数：0")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
