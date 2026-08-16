# fetch_index.py —— 阶段 A：抓作品索引页，建/更新本地清单
#
# 这是整个系统里最要紧的一步：清单驱动一切（增量判断、会话守卫、后续抓正文的名单）。
#
# 联网请求数：作品列表有几页就几次（6 小时内抓过的页会直接复用，不重复请求）
# 会写什么：
#   · data/ao3/index_raw/page-NN.html   ← 原始页，**覆写同路径**，历史交给 git
#   · data/index.sqlite                 ← 派生清单，可随时从原始页重建
#   · data/reports/YYYYMMDD-HHMM_index.md
# 不会写什么：不碰 data/ao3/works/，不删任何东西
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'fetch_index'

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import json
    import sqlite3
    import time
    from datetime import datetime, timedelta
    import config
    import index_parser
    from ao3_client import (
        AO3Client, confirm, assert_logged_in, SessionLost,
        is_forced_logout, looks_like_rate_limit, FetchFailed,
    )
except Exception as _import_error:
    session_log.crash_dump("fetch_index", _import_error)
    raise


_report: list[str] = []


def say(line: str = "") -> None:
    print(line)
    _report.append(line)


# ====================================================================== 数据库
SCHEMA = """
CREATE TABLE IF NOT EXISTS works_index (
    work_id           INTEGER PRIMARY KEY,
    title             TEXT,
    updated_at_unix   INTEGER,
    date_text         TEXT,
    rating            TEXT,
    category          TEXT,
    required_warning  TEXT,
    is_wip            INTEGER,
    language          TEXT,
    chapters_text     TEXT,
    chapters_done     INTEGER,
    chapters_total    INTEGER,
    words             INTEGER,
    comments          INTEGER,
    kudos             INTEGER,
    hits              INTEGER,
    bookmarks         INTEGER,
    fandoms_json      TEXT,
    warnings_json     TEXT,
    relationships_json TEXT,
    characters_json   TEXT,
    freeforms_json    TEXT,
    series_json       TEXT,
    collections_count INTEGER,
    summary_html      TEXT,
    summary_text      TEXT,
    blurb_hash        TEXT,
    page              INTEGER,
    first_seen        TEXT,
    last_seen         TEXT,
    missing_since     TEXT        -- 本次没见到就记一笔；**永不自动删除**
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT, finished_at TEXT,
    pages_fetched INTEGER, pages_reused INTEGER,
    works_seen  INTEGER, works_new INTEGER, works_changed INTEGER, works_missing INTEGER,
    note        TEXT
);
-- AO3 筛选侧边栏抓下来的 canonical 标签（每类只有前 10，见 index_parser.parse_facets）
CREATE TABLE IF NOT EXISTS facets (
    kind        TEXT,      -- relationship_ids / character_ids / freeform_ids / …
    tag_id      INTEGER,   -- AO3 内部 tag id，比名字稳定
    name        TEXT,      -- canonical 标签名
    count       INTEGER,   -- canonical 计数（已归并 implied 等同义标签）
    captured_at TEXT,
    PRIMARY KEY (kind, tag_id)
);
-- kudos / hits / comments / bookmarks 的时间点观测。
-- **只在数值变化时追加一行**，不是每次跑都全量快照 —— 与评论的存储模型同理（DESIGN-NOTES.md N-27）。
-- 数据来自索引页 blurb，而索引页本来每次都要抓，所以**零额外请求**。
CREATE TABLE IF NOT EXISTS stats_observations (
    work_id     INTEGER,
    metric      TEXT,      -- kudos / hits / comments / bookmarks
    value       INTEGER,
    observed_at TEXT,
    PRIMARY KEY (work_id, metric, observed_at)
);
CREATE TABLE IF NOT EXISTS changes (
    run_id  INTEGER, work_id INTEGER, field TEXT, old_value TEXT, new_value TEXT
);
"""

# 存进 works_index 的列 ← index_parser 的键
LIST_FIELDS = {
    "fandoms_json": "fandoms", "warnings_json": "warnings",
    "relationships_json": "relationships", "characters_json": "characters",
    "freeforms_json": "freeforms", "series_json": "series",
}
SCALAR_FIELDS = [
    "title", "updated_at_unix", "date_text", "rating", "category",
    "required_warning", "is_wip", "language", "chapters_text",
    "chapters_done", "chapters_total", "words", "comments", "kudos",
    "hits", "bookmarks", "collections_count", "summary_html", "summary_text",
    "blurb_hash",
]
# 变了就值得报告的字段（hits/kudos 天天变，不算「改动」，单独看）
# ⚠️ 移出两个：
#   · `updated_at_unix` —— 是 blurb 的**缓存戳**不是作品更新时间。实测一次重抓
#     40 篇「有改动」且**全部只改了这一个字段**，新值只有两个（66 篇共用一个值）。
#     它留在库里当缓存键用（fetch_works 拿它破缓存），但**不该再当变更信号**。
# `date_text` **保留在名单里**（作者 要求）：作者会刻意改发布日，
#   那是真信号，不能不报。但读它时要知道 blurb 是**缓存片段、按 UTC 渲染**：
#   本地午夜后几小时发的作品会比实际日期早一天（227 篇单章里 18 篇如此，
#   **全是早、没有晚**）。那 18 篇的偏移是**长期固定**的，不会每轮抖动；
#   只有 blurb 缓存换了渲染上下文时才会翻一次。
#   → **看到 ±1 天变化时，去对 `chapters.published_at`**：那边跟着动才是真改了发布日。
#   见DESIGN-NOTES.md N-02③b。
WATCHED = [
    "title", "date_text", "rating", "category",
    "required_warning", "is_wip", "language", "chapters_text", "words",
    "fandoms_json", "warnings_json", "relationships_json", "characters_json",
    "freeforms_json", "series_json", "collections_count", "summary_text",
]


def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(config.INDEX_DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def row_to_record(r: dict, page: int) -> dict:
    rec = {k: r.get(k) for k in SCALAR_FIELDS}
    for col, key in LIST_FIELDS.items():
        rec[col] = json.dumps(r.get(key) or [], ensure_ascii=False, sort_keys=True)
    rec["work_id"] = r["work_id"]
    rec["page"] = page
    return rec


# ====================================================================== 抓页
def fetch_page(client: AO3Client, page: int) -> tuple[str, bool]:
    """返回 (html, 是否走了网络)。6 小时内抓过就直接复用本地文件。"""
    path = config.INDEX_RAW_DIR / f"page-{page:02d}.html"

    if config.INDEX_RAW_FRESH_HOURS > 0 and path.exists():
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        if age < timedelta(hours=config.INDEX_RAW_FRESH_HOURS):
            print(f"  第 {page:>2} 页：复用 {int(age.total_seconds() // 60)} 分钟前抓的本地文件")
            return path.read_text(encoding="utf-8"), False

    html = client.get(f"/users/{config.AO3_USERNAME}/works", params={"page": page})

    # ---- fail closed：三道检查全过才落盘 ----
    if is_forced_logout(html):
        raise SessionLost(
            f"第 {page} 页返回 Lost Cookie / Forced Logout 页。"
            "cookie 可能过期了，请重做操作手册 Step 1.1。已抓到的页不会丢。"
        )
    if looks_like_rate_limit(html):
        raise FetchFailed(
            f"第 {page} 页疑似限流页。这不是故障 —— 等几分钟重跑本脚本即可，"
            "已抓到的页会被复用，不会重来。"
        )
    assert_logged_in(html, f"索引第 {page} 页")

    n = len(index_parser.parse_page(html))
    if n == 0:
        raise FetchFailed(
            f"第 {page} 页解析出 0 个 blurb —— 页面结构可能变了。"
            f"原始页已存到 {config.PROBE_RAW_DIR}，留着它对照排查。"
        )

    # 先写临时文件，校验通过才移动到正式位置（绝不原地覆盖）
    tmp = path.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(path)
    print(f"  第 {page:>2} 页：{len(html):,} 字符，{n} 篇 → {path.name}")
    return html, True


# ====================================================================== main
def main() -> int:
    session_log.start("fetch_index")
    config.ensure_dirs()

    print("[1/4] 检查 cookie …")
    try:
        cookies = config.load_cookies()
    except config.CookieMissing as e:
        print("\n✗ " + str(e))
        return 1
    print(config.describe_cookies(cookies))

    # 先看第 1 页决定总页数（若本地有新鲜副本则不花请求）
    confirm(
        plan_lines=[
            # 页数取决于你有多少篇（AO3 每页 20 篇），先抓第 1 页才知道，所以这里不写死
            f"抓 /users/{config.AO3_USERNAME}/works 的全部页（每页 20 篇，页数看第 1 页才知道）",
            f"原始页写入 data/ao3/index_raw/（覆写同名文件，历史交给 git）",
            f"解析后写入 data/index.sqlite，并输出变更报告",
            f"{config.INDEX_RAW_FRESH_HOURS} 小时内抓过的页会直接复用，不重复请求",
            "不碰 data/ao3/works/，不删任何东西",
        ],
        request_count=14,
    )

    client = AO3Client(cookies=cookies, delay=config.REQUEST_DELAY_SEC)
    started = datetime.now()
    say(f"# 索引抓取报告 · {started:%Y-%m-%d %H:%M}")
    say("")

    print("\n[2/4] 抓取索引页 …")
    pages_data: dict[int, str] = {}
    fetched = reused = 0
    total_pages = None

    page = 1
    while True:
        try:
            html, went_online = fetch_page(client, page)
        except (SessionLost, FetchFailed) as e:
            say(f"**在第 {page} 页停下：** {e}")
            say("")
            say(f"已成功取得 {len(pages_data)} 页。**这不算失败** —— "
                f"修好后直接重跑本脚本，已抓到的页会被复用。")
            break
        except Exception as e:
            say(f"**第 {page} 页出错：** {e}")
            break

        pages_data[page] = html
        fetched += int(went_online)
        reused += int(not went_online)

        if total_pages is None:
            total_pages = index_parser.max_page(html) or 1
            reported = index_parser.page_total_works(html)
            say(f"页面自报：共 **{reported}** 篇，**{total_pages}** 页")
            say("")

        if page >= total_pages:
            break
        page += 1

    if not pages_data:
        say("一页都没取到，什么也没写。")
        _write_report()
        return 1

    # ---- 解析 ----
    print("\n[3/4] 解析并入库 …")
    parsed: dict[int, dict] = {}
    for pg in sorted(pages_data):
        for r in index_parser.parse_page(pages_data[pg]):
            parsed[r["work_id"]] = row_to_record(r, pg)
    say(f"解析出 **{len(parsed)}** 篇（去重后）")

    # ---- 入库 + 变更检测 ----
    con = open_db()
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute("INSERT INTO runs (started_at) VALUES (?)", (started.isoformat(timespec="seconds"),))
    run_id = cur.lastrowid

    existing = {r["work_id"]: dict(r) for r in con.execute("SELECT * FROM works_index")}
    new_ids, changed = [], {}

    for wid, rec in parsed.items():
        old = existing.get(wid)
        if old is None:
            new_ids.append(wid)
            cols = list(rec) + ["first_seen", "last_seen"]
            con.execute(
                f"INSERT INTO works_index ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [rec[c] for c in rec] + [now, now],
            )
        else:
            diffs = [(f, old.get(f), rec.get(f)) for f in WATCHED
                     if str(old.get(f)) != str(rec.get(f))]
            if diffs:
                changed[wid] = diffs
                for f, o, n in diffs:
                    con.execute(
                        "INSERT INTO changes (run_id, work_id, field, old_value, new_value) VALUES (?,?,?,?,?)",
                        (run_id, wid, f, str(o)[:500], str(n)[:500]),
                    )
            sets = ", ".join(f"{c}=?" for c in rec)
            con.execute(
                f"UPDATE works_index SET {sets}, last_seen=?, missing_since=NULL WHERE work_id=?",
                [rec[c] for c in rec] + [now, wid],
            )

    # ---- 侧边栏 facet（canonical 标签 + tag_id + canonical 计数）----
    facets = index_parser.parse_facets(pages_data[min(pages_data)])
    if facets:
        con.execute("DELETE FROM facets")
        con.executemany(
            "INSERT INTO facets (kind, tag_id, name, count, captured_at) VALUES (?,?,?,?,?)",
            [(f["kind"], f["tag_id"], f["name"], f["count"], now) for f in facets],
        )
        say(f"侧边栏 canonical 标签：**{len(facets)}** 条（每类前 10）")
        say("")

    # ---- 统计量观测：只在变化时记一笔（DESIGN-NOTES.md N-27）----
    STAT_METRICS = ("kudos", "hits", "comments", "bookmarks")
    last = {}
    for wid, metric, val in con.execute(
        "SELECT work_id, metric, value FROM stats_observations o WHERE observed_at = "
        "(SELECT MAX(observed_at) FROM stats_observations WHERE work_id=o.work_id AND metric=o.metric)"
    ):
        last[(wid, metric)] = val
    n_stat = 0
    for wid, rec in parsed.items():
        for m in STAT_METRICS:
            v = rec.get(m)
            if v is None:
                continue
            if last.get((wid, m)) != v:
                con.execute(
                    "INSERT OR REPLACE INTO stats_observations "
                    "(work_id, metric, value, observed_at) VALUES (?,?,?,?)",
                    (wid, m, v, now))
                n_stat += 1
    if n_stat:
        say(f"统计量观测：本次记录 **{n_stat}** 条变化（kudos/hits/comments/bookmarks）")
        say("")

    missing = [w for w in existing if w not in parsed]
    for wid in missing:
        if not existing[wid].get("missing_since"):
            con.execute("UPDATE works_index SET missing_since=? WHERE work_id=?", (now, wid))

    con.execute(
        "UPDATE runs SET finished_at=?, pages_fetched=?, pages_reused=?, works_seen=?, "
        "works_new=?, works_changed=?, works_missing=? WHERE run_id=?",
        (datetime.now().isoformat(timespec="seconds"), fetched, reused, len(parsed),
         len(new_ids), len(changed), len(missing), run_id),
    )
    con.commit()

    # ---- 报告 ----
    print("\n[4/4] 写报告 …")
    say("")
    say(f"| 项 | 数 |")
    say(f"|---|---|")
    say(f"| 联网抓取的页 | {fetched} |")
    say(f"| 复用本地的页 | {reused} |")
    say(f"| 本次见到的作品 | {len(parsed)} |")
    say(f"| **新增** | **{len(new_ids)}** |")
    say(f"| **有改动** | **{len(changed)}** |")
    say(f"| 本次未见到 | {len(missing)} |")
    say("")

    if new_ids:
        say("## 新增作品")
        say("")
        for wid in new_ids[:60]:
            say(f"- `{wid}` {parsed[wid]['title']}")
        if len(new_ids) > 60:
            say(f"- …还有 {len(new_ids) - 60} 篇")
        say("")

    if changed:
        say("## 有改动的作品")
        say("")
        for wid, diffs in list(changed.items())[:40]:
            say(f"### `{wid}` {parsed[wid]['title']}")
            for f, o, n in diffs:
                say(f"- `{f}`：`{str(o)[:80]}` → `{str(n)[:80]}`")
            say("")

    if missing:
        say("## 本次未见到的作品（**已保留，未删除任何文件**）")
        say("")
        say("可能是自删、被下架、或索引页没抓全。**需要人工确认**，脚本绝不自动判定。")
        say("")
        for wid in missing[:40]:
            say(f"- `{wid}` {existing[wid].get('title')}")
        say("")

    say("---")
    say(f"共发出 {client.request_count} 次请求。未修改 AO3 上任何内容。")
    _write_report()
    con.close()
    return 0


def _write_report() -> None:
    # 不再写 reports/ 文件：内容与运行记录完全重复。
    # 完整过程见 code/session_printouts/。
    print(f"\n数据库：{config.INDEX_DB}")
    print("（不再单独写报告文件 —— 完整输出已存进运行记录。）")


if __name__ == "__main__":
    sys.exit(main())
