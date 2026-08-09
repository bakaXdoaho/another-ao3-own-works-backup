# rebuild_index.py —— 从已保存的原始页重建 index.sqlite（**完全不联网**）
#
# 为什么会有这个脚本：
#   DESIGN-NOTES.md N-23 的铁律是「原始文件才是存档，sqlite 只是派生物，可以随时重建」。
#   这个脚本就是那句话的兑现 —— 解析器改了、字段加了、发现解析 bug 了，
#   都不用重新去 AO3 抓一次，拿本地 data/ao3/index_raw/*.html 重跑一遍就行。
#
# 联网请求数：**0**
# 会写什么：data/index.sqlite（先备份旧的），data/reports/…_rebuild.md
# 不会写什么：不碰原始页，不碰 data/ao3/works/，不改 AO3
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'rebuild_index'

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import hashlib
    import re
    import shutil
    from datetime import datetime
    import config
    import index_parser
    import fetch_index
    import fetch_works
except Exception as _import_error:
    session_log.crash_dump("rebuild_index", _import_error)
    raise


def main() -> int:
    session_log.start("rebuild_index")
    config.ensure_dirs()

    pages = sorted(config.INDEX_RAW_DIR.glob("page-*.html"))
    if not pages:
        print(f"✗ {config.INDEX_RAW_DIR} 里没有原始页，没法重建。")
        print("  先跑一次 fetch_index.py。")
        return 1

    report: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        report.append(line)

    say(f"# 索引重建报告 · {datetime.now():%Y-%m-%d %H:%M}")
    say("")
    say(f"**不联网。** 从 {len(pages)} 个本地原始页重新解析。")
    say("")

    # ---- 1. 备份旧库（绝不原地覆盖）----
    if config.INDEX_DB.exists():
        bak = config.INDEX_DB.with_name(
            f"index.sqlite.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(config.INDEX_DB, bak)
        say(f"旧数据库已备份为 `{bak.name}`")
        # 记住旧的统计，好做前后对照
        import sqlite3
        old_con = sqlite3.connect(bak)
        try:
            old_count = old_con.execute("SELECT COUNT(*) FROM works_index").fetchone()[0]
        except Exception:
            old_count = None
        old_con.close()
    else:
        old_count = None
        say("（原先没有数据库，直接新建）")
    say("")

    # ---- 1b. 先把旧的 works_index 整表读进内存 ----
    # ⚠️ 20260805 实测发现的漏洞：
    #   本脚本会 DROP works_index 再从**当前**索引页重建。
    #   如果某篇作品已从 AO3 上删除，它就不在当前索引页里 ——
    #   于是重建后**这一行凭空消失**，虽然本地 HTML 文件还好好地躺着。
    #   这直接违反「只增不删」：文件在、库里却查不到，等于把已删作品弄丢了。
    # 修法：重建后，把「本地有文件、但当前索引页里没有」的作品原样补回去，
    #      并标上 missing_since —— 这正是 fetch_index 遇到作品消失时的做法。
    import sqlite3 as _sq
    _old = _sq.connect(config.INDEX_DB)
    _old.row_factory = _sq.Row
    try:
        previous = {r["work_id"]: dict(r) for r in _old.execute("SELECT * FROM works_index")}
    except Exception:
        previous = {}
    _old.close()

    # 清空重建：**不删文件**，只 DROP 表再建。
    # 删文件在某些环境（网络盘、同步盘、只读挂载）会失败，而 DROP TABLE 到处都能用；
    # 备份已经在上一步复制好了，真出事也能回退。
    import sqlite3
    _c = sqlite3.connect(config.INDEX_DB)
    # 注意：**不 DROP `stats_observations`** —— 它是时间序列，是真正的历史，
    # 不是派生物。重建时必须保留。
    for t in ("works_index", "changes", "facets", "chapters", "work_files"):
        _c.execute(f"DROP TABLE IF EXISTS {t}")
    _c.commit()
    _c.close()

    # ---- 2. 解析 ----
    parsed: dict[int, dict] = {}
    for p in pages:
        page_no = int(p.stem.split("-")[1])
        html = p.read_text(encoding="utf-8", errors="ignore")
        rows = index_parser.parse_page(html)
        print(f"  {p.name}：{len(rows)} 篇")
        if not rows:
            say(f"⚠ `{p.name}` 解析出 0 篇 —— 请检查这个文件。")
        for r in rows:
            parsed[r["work_id"]] = fetch_index.row_to_record(r, page_no)

    say(f"解析出 **{len(parsed)}** 篇（去重后）")
    if old_count is not None:
        delta = len(parsed) - old_count
        say(f"（重建前 {old_count} 篇，{'不变' if delta == 0 else f'{delta:+d}'}）")
    say("")

    # ---- 3. 入库 ----
    con = fetch_index.open_db()
    now = datetime.now().isoformat(timespec="seconds")
    for wid, rec in parsed.items():
        cols = list(rec) + ["first_seen", "last_seen"]
        con.execute(
            f"INSERT INTO works_index ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [rec[c] for c in rec] + [now, now],
        )
    # 侧边栏 canonical 标签（只需第一页，全库口径一致）
    first = pages[0].read_text(encoding="utf-8", errors="ignore")
    facets = index_parser.parse_facets(first)
    if facets:
        con.execute("DELETE FROM facets")
        con.executemany(
            "INSERT INTO facets (kind, tag_id, name, count, captured_at) VALUES (?,?,?,?,?)",
            [(f["kind"], f["tag_id"], f["name"], f["count"], now) for f in facets],
        )
        say(f"侧边栏 canonical 标签：**{len(facets)}** 条")
        say("")

    # ---- 3a. 把「本地有文件、但已不在索引页里」的作品补回去（见 1b 的说明）----
    vanished = []
    for wid, row in previous.items():
        if wid in parsed:
            continue
        if not (config.WORKS_DIR / str(wid) / f"{wid}.html").exists():
            continue                      # 本地也没有文件，那就真的没这篇，不凭空造
        row = dict(row)
        row["missing_since"] = row.get("missing_since") or now
        row["last_seen"] = row.get("last_seen") or now
        row["first_seen"] = row.get("first_seen") or now
        cols = [c for c in row if row[c] is not None or c in ("missing_since",)]
        con.execute(
            f"INSERT OR REPLACE INTO works_index ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [row[c] for c in cols])
        vanished.append((wid, row.get("title")))
    if vanished:
        say(f"## ⚠️ 已不在 AO3 索引页上、但本地有文件的作品（{len(vanished)} 篇）")
        say("")
        say("**记录已原样保留**，并标上 `missing_since`。本地文件一个都没动。")
        say("可能是自删、被下架、或本次索引没抓全 —— **需要人工确认，脚本绝不自行判定。**")
        say("")
        for wid, title in vanished:
            say(f"- `{wid}` {title or '（无标题）'}")
        say("")

    # ---- 3b. 从已保存的作品原始件重建 chapters 与 work_files ----
    # 这两张表和 works_index 一样，**都是派生物**：navigate.html 与 {id}.html 都在本地，
    # 所以解析器改了、字段加了，都不必重新联网。（20260805 补上，此前只重建了 works_index。）
    con.executescript(fetch_works.SCHEMA)
    done_map = {r[0]: r[1] for r in con.execute(
        "SELECT work_id, chapters_done FROM works_index")}
    n_ch = n_file = n_draft = 0
    for wdir in sorted(config.WORKS_DIR.glob("*")):
        if not wdir.is_dir():
            continue
        wid = int(wdir.name)

        navp = wdir / "navigate.html"
        if navp.exists():
            rows = fetch_works._NAV_ROW.findall(
                navp.read_text(encoding="utf-8", errors="ignore"))
            # ⚠️ 作为作者登录时，/navigate 会连**草稿章**一起列出来（DESIGN-NOTES.md N-11）。
            #    已公开的章数以索引页 "13/23" 里的 13 为准，之后的标 is_draft=1。
            published_n = done_map.get(wid) or len(rows)
            for n, (cid, title, date) in enumerate(rows, 1):
                draft = 1 if n > published_n else 0
                n_draft += draft
                con.execute(
                    "INSERT OR REPLACE INTO chapters "
                    "(work_id, idx, chapter_id, title, published_at, source, is_draft) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (wid, n, int(cid), re.sub(r"<[^>]+>", "", title).strip(),
                     date, "navigate", draft))
            n_ch += len(rows)

        dlp = wdir / f"{wid}.html"
        if dlp.exists():
            t = dlp.read_text(encoding="utf-8", errors="ignore")
            mw = fetch_works._STAT_WORDS.search(t)
            mc = fetch_works._STAT_CHAPS.search(t)
            con.execute(
                "INSERT OR REPLACE INTO work_files "
                "(work_id, path, sha256, chars, words_ao3, chapters_text, fetched_at, file_source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (wid, str(dlp.relative_to(config.MAIN_DIR)),
                 hashlib.sha256(t.encode("utf-8")).hexdigest(), len(t),
                 int(mw.group(1).replace(",", "")) if mw else None,
                 mc.group(1) if mc else None,
                 datetime.fromtimestamp(dlp.stat().st_mtime).isoformat(timespec="seconds"),
                 "ao3_official_html"))
            n_file += 1
    if n_ch or n_file:
        say(f"从本地原始件重建：**{n_file}** 个下载件、**{n_ch}** 条章节"
            f"（其中 **{n_draft}** 条是未公开的草稿章）")
        say("")

    con.execute(
        "INSERT INTO runs (started_at, finished_at, pages_fetched, pages_reused, "
        "works_seen, works_new, works_changed, works_missing, note) "
        "VALUES (?,?,0,?,?,?,0,0,'rebuild from raw, no network')",
        (now, datetime.now().isoformat(timespec="seconds"), len(pages),
         len(parsed), len(parsed)),
    )
    con.commit()

    # ---- 4. 体检 ----
    say("## 重建后体检")
    say("")
    checks = [
        ("作品数", "SELECT COUNT(*) FROM works_index"),
        ("总字数", "SELECT SUM(words) FROM works_index"),
        ("未完结", "SELECT COUNT(*) FROM works_index WHERE is_wip=1"),
        ("有系列归属", "SELECT COUNT(*) FROM works_index WHERE series_json!='[]'"),
        ("有合集归属", "SELECT COUNT(*) FROM works_index WHERE collections_count IS NOT NULL"),
        ("标题为空（应为 0）", "SELECT COUNT(*) FROM works_index WHERE title IS NULL OR title=''"),
        ("字数为空（应为 0）", "SELECT COUNT(*) FROM works_index WHERE words IS NULL"),
        ("updated_at 为空（应为 0）", "SELECT COUNT(*) FROM works_index WHERE updated_at_unix IS NULL"),
        ("下载件记录", "SELECT COUNT(*) FROM work_files"),
        ("章节总数", "SELECT COUNT(*) FROM chapters"),
        ("其中未公开草稿章", "SELECT COUNT(*) FROM chapters WHERE is_draft=1"),
        ("已从 AO3 消失但本地保留", "SELECT COUNT(*) FROM works_index WHERE missing_since IS NOT NULL"),
    ]
    say("| 项 | 值 |")
    say("|---|---|")
    for label, q in checks:
        say(f"| {label} | {con.execute(q).fetchone()[0]:,} |")
    say("")
    con.close()

    # 20260805 起不再写 reports/ 文件：内容与运行记录完全重复。
    print("\n（不再单独写报告文件 —— 完整输出已存进运行记录。）")
    print("联网请求数：0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
