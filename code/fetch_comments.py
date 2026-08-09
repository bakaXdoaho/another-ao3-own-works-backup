# fetch_comments.py —— 阶段 F：把评论抓下来存好
#
# 联网请求数：**每篇有评论的作品各 1 次**（没有评论的一次都不发）
#             有分页的话会多几次，脚本会照实报。
# 会写什么：data/comments/{work_id}.html（评论区原文）
#           index.sqlite 的 comments / comment_threads / comment_fetch_state
# 不会写什么：不碰 data/ao3/ 里的作品件，不删任何东西，不改 AO3
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'fetch_comments'
#
# ────────────────────────────────────────────────────────────────
# 一、为什么用 `/works/{id}?show_comments=true&view_full_work=true`
#
#   20260805 探测了三种形式（见 probe_comments 那次的运行记录）：
#     A 这一种 …… 11 条评论，与 blurb 报的 11 完全对上   ✓
#     B `/works/{id}/comments` …… 标题对，但**一条评论都没有**  ✗
#     C 单章 + show_comments …… 只出那一章的；本作评论全在第 2 章，
#       所以拿第 1 章去试自然是空的——这不算 C 不能用，
#       但要按章抓的话，25 章的某篇二十多章的长文就得发 25 次请求，不划算。
#   → 选 A。有评论的作品数 = 请求数，是三种里最省的。
#
# 二、为什么只存「评论区那一段」，不存整页
#
#   A 形式会把**全文正文也一并返回**（view_full_work）。某篇二十多章的长文15 万字，
#   整页存下来就是把已经存好的作品件再抄一遍——白占地方，还容易让人
#   分不清哪份才是正本。所以这里只存 `comments_placeholder` 往后的那一段，
#   同时把整页的长度与 sha256 记进库，来源仍然可追。
#   （正本仍然是 data/ao3/works/{id}/{id}.html，本文件不碰它。）
#
# 三、thread ≠ comment
#
#   AO3 stats 页报「Comment Threads 113」，索引页 blurb 报的是**评论条数**（全库 346）。
#   一串 = 主评论 + 全部回复，**作者自己的回复也算在里面**。
#   两个数都对，只是口径不同。所以本脚本两样都存：
#   `comments` 存每一条（带 parent_id / depth / thread_root_id），
#   `comment_threads` 存每一串的汇总。日后问「收到过多少评论」才答得对。

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import hashlib
    import re
    import sqlite3
    import time
    from datetime import datetime
    from urllib.parse import urlparse, parse_qs
    import config
    import comments_parser
    import fetch_index
    from ao3_client import (
        AO3Client, confirm, assert_logged_in, SessionLost, FetchFailed,
        is_forced_logout, looks_like_rate_limit, UrlNotAllowed,
    )
except Exception as _import_error:
    session_log.crash_dump("fetch_comments", _import_error)
    raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    comment_id     INTEGER PRIMARY KEY,   -- AO3 自己的 id
    work_id        INTEGER NOT NULL,
    chapter_id     INTEGER,               -- 可空：不是每条都挂在某一章上
    chapter_label  TEXT,
    parent_id      INTEGER,               -- NULL = 主评论（一串之首）
    thread_root_id INTEGER NOT NULL,      -- 所属串的根
    depth          INTEGER NOT NULL,      -- 0 = 主评论
    seq            INTEGER,               -- 在页面上的出现次序
    user_id        INTEGER,               -- AO3 数字 user id
    username       TEXT,
    pseud          TEXT,
    display_name   TEXT,
    is_guest       INTEGER NOT NULL DEFAULT 0,
    is_mine        INTEGER NOT NULL DEFAULT 0,
    posted_at      TEXT,                  -- ISO；时区另存，见下
    posted_tz      TEXT,                  -- AO3 按查看者时区渲染，不存说不清
    body           TEXT,
    body_html      TEXT,
    body_chars     INTEGER,
    fetched_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_work ON comments(work_id);
CREATE INDEX IF NOT EXISTS idx_comments_chapter ON comments(chapter_id);
CREATE INDEX IF NOT EXISTS idx_comments_thread ON comments(thread_root_id);

CREATE TABLE IF NOT EXISTS comment_threads (
    thread_root_id INTEGER PRIMARY KEY,
    work_id        INTEGER NOT NULL,
    chapter_id     INTEGER,
    n_comments     INTEGER,
    n_mine         INTEGER,
    n_others       INTEGER,
    max_depth      INTEGER,
    started_at     TEXT,
    last_at        TEXT,
    starter_user   TEXT,
    starter_uid    INTEGER
);

CREATE TABLE IF NOT EXISTS comment_fetch_state (
    work_id        INTEGER PRIMARY KEY,
    status         TEXT,      -- ok / mismatch / failed
    expected       INTEGER,   -- blurb 报的评论条数（抓的时候）
    parsed         INTEGER,   -- 实际解析出多少条
    threads        INTEGER,
    pages          INTEGER,   -- 翻了几页
    page_bytes     INTEGER,   -- 整页多大（含正文）
    page_sha256    TEXT,      -- 整页的指纹，来源可追
    fetched_at     TEXT,
    note           TEXT
);
"""


def looks_like_comments_page(html: str, work_id: int) -> tuple[bool, str]:
    """**先否决再接受**（与 fetch_works.check_download 同一套路数）。

    评论页长得和正常页很像，光看 HTTP 200 是不够的——
    DESIGN-NOTES.md N-02 那次 Lost Cookie 页就是 200，还带着「Hi, YOUR_AO3_USERNAME!」。
    """
    if is_forced_logout(html):
        return False, "Lost Cookie / 强制登出页"
    if looks_like_rate_limit(html):
        return False, "疑似限流页"
    if len(html) < 2000:
        return False, f"页面只有 {len(html)} 字符，太短"
    if f"/works/{work_id}" not in html:
        return False, f"页面里找不到 /works/{work_id}，可能抓错篇了"
    if comments_parser._PLACEHOLDER not in html:
        return False, "找不到评论区容器 comments_placeholder"
    return True, ""


def check_invariants(rows: list[dict]) -> list[str]:
    """自查。发现不对**只报不改**——数据先老实存着，判断交给人。

    时间单调性是最有用的一条：回复必然晚于被回复的那条。
    若父子时间反了，多半是层级/父子认错了，而这种错**看起来毫不可疑**，
    只能靠这类不变式抓出来。
    """
    bad: list[str] = []
    by_id = {r["comment_id"]: r for r in rows}
    for r in rows:
        p = by_id.get(r["parent_id"]) if r["parent_id"] else None
        if p and r["posted_at"] and p["posted_at"] and r["posted_at"] < p["posted_at"]:
            bad.append(f"comment {r['comment_id']} 早于其父 {p['comment_id']}")
        if r["parent_id"] and r["parent_id"] not in by_id:
            bad.append(f"comment {r['comment_id']} 的父 {r['parent_id']} 不在本页")
        if r["depth"] == 0 and r["parent_id"]:
            bad.append(f"comment {r['comment_id']} 层级为 0 却有父")
        if r["depth"] > 0 and not r["parent_id"]:
            bad.append(f"comment {r['comment_id']} 层级 {r['depth']} 却没有父")
    return bad


def my_user_id(con) -> int | None:
    """自己的 AO3 数字 id。索引页 blurb 的 class 里就带着（user-40000001）。"""
    for p in sorted(config.INDEX_RAW_DIR.glob("page-*.html")):
        m = re.search(r'class="[^"]*\buser-(\d+)\b', p.read_text(encoding="utf-8",
                                                                 errors="ignore"))
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------- 已核实的例外
# 这里登记「数量对不上，但**人已经去 AO3 核实过**、确认本来就是这样」的作品。
#
# 为什么要有这个名单：脚本对「blurb 说有 N 条、页面只有 M 条」一律记 mismatch
# 并在下次重抓 —— 这是对的默认行为，能防止真 bug 被当成正常。
# 但若差异的原因已经查清且无法消除（AO3 自己的计数器过期），
# 再抓一万次也还是那个数，只会每轮白费一个请求、多一行吓人的 ⚠。
#
# **加进来的唯一条件：人真的去 AO3 页面上看过。** 不允许凭猜测加。
ACCEPTED_MISMATCH = {
    # 20260805 作者实测：打开 works/10000002?view_full_work=true，按章看也一样，
    # **页面上一条评论都没有**；抓下来的原文里也确实是空的 <ol class="thread"></ol>。
    # → 索引页 blurb 的 comments=1 是 AO3 自己的过期计数（评论删了但数字没减）。
    #   这解释了库里条数与 blurb 合计之间那 1 条之差。
    10000002: "作者已在 AO3 页面核实：确实没有评论，blurb 计数器过期（20260805）",
}


def pick_todo(con) -> list[dict]:
    """挑要抓的：blurb 说有评论、且（没抓过 or 抓的时候条数和现在对不上）。

    条数变了就重抓 —— 这是最自然的变更检测：有人新评论了，blurb 数字会动。

    例外：ACCEPTED_MISMATCH 里的作品，只要 blurb 数字**没再变过**就不重抓。
    一旦数字变了（说明真有新评论），它照样会回到队列里 —— 例外不是永久豁免。
    """
    rows = con.execute("""
        SELECT w.work_id, w.title, w.comments, s.status, s.expected
          FROM works_index w
          LEFT JOIN comment_fetch_state s ON s.work_id = w.work_id
         WHERE COALESCE(w.comments, 0) > 0
           AND (s.work_id IS NULL
                OR s.status != 'ok'
                OR COALESCE(s.expected, -1) != w.comments)
         ORDER BY w.comments DESC
    """).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if (r["work_id"] in ACCEPTED_MISMATCH
                and r["status"] == "mismatch"
                and r["expected"] == r["comments"]):
            continue                 # 已核实过，且 blurb 数字没再变 → 跳过
        out.append(r)
    return out


def fetch_one(client, wid: int, expected: int) -> tuple[str, list[str], int]:
    """抓一篇的评论区。返回（评论区 HTML 合并、整页 sha 列表、页数）。"""
    segs: list[str] = []
    shas: list[str] = []
    path, params = f"/works/{wid}", {"show_comments": "true", "view_full_work": "true"}
    pages = 0
    seen: set[str] = set()

    while True:
        html = client.get(path, params=params)
        good, why = looks_like_comments_page(html, wid)
        if not good:
            raise FetchFailed(f"评论页校验未过：{why}")
        assert_logged_in(html, f"works/{wid} 评论页")
        shas.append(hashlib.sha256(html.encode("utf-8")).hexdigest())
        seg = comments_parser.comments_segment(html)
        segs.append(seg)
        pages += 1

        # 分页：**用 AO3 自己给的 href**，不自己拼参数（见 comments_parser 的说明）
        nxt = comments_parser.next_page_href(seg)
        if not comments_parser.has_pagination(seg) or not nxt or nxt in seen:
            break
        seen.add(nxt)
        u = urlparse(nxt)
        if not re.fullmatch(rf"/works/{wid}", u.path or ""):
            print(f"    ⚠ 下一页链接指向 {u.path!r}，不是本篇，停止翻页（已抓 {pages} 页）")
            break
        path = u.path
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        print(f"    · 还有下一页，继续：{u.query}")
        if pages >= 20:
            print("    ⚠ 已翻 20 页，保险起见停下")
            break

    return "\n".join(segs), shas, pages


def main() -> int:
    session_log.start("fetch_comments")
    config.ensure_dirs()

    if not config.INDEX_DB.exists():
        print("✗ 还没有 data/index.sqlite。先跑 fetch_index.py。")
        return 1

    print("[1/3] 检查 cookie …")
    try:
        cookies = config.load_cookies()
    except config.CookieMissing as e:
        print("\n✗ " + str(e))
        return 1
    print(config.describe_cookies(cookies))

    con = fetch_index.open_db()
    con.executescript(SCHEMA)
    config.COMMENTS_DIR.mkdir(parents=True, exist_ok=True)

    me = my_user_id(con)
    print(f"\n你的 AO3 数字 id：{me if me else '（没找出来，is_mine 一律记 0）'}")

    with_comments = con.execute(
        "SELECT COUNT(*), SUM(comments) FROM works_index WHERE COALESCE(comments,0)>0"
    ).fetchone()
    todo = pick_todo(con)
    print(f"全库有评论的作品 {with_comments[0]} 篇｜评论合计 {with_comments[1]} 条")
    print(f"本次要抓 {len(todo)} 篇")
    if not todo:
        print("\n✓ 都抓过了，且 blurb 的评论数没变化。无事可做。")
        return 0

    est = len(todo) * config.REQUEST_DELAY_SEC / 60
    confirm(
        plan_lines=[
            f"抓 {len(todo)} 篇的评论区，每篇 1 次请求（有分页会多几次）",
            "只存评论区那一段到 data/comments/{id}.html —— 正文已经存过，不再抄一遍",
            "comments 表存每一条（含 parent_id / depth），comment_threads 存每串汇总",
            "会自查父子时间顺序等不变式，发现问题**只报不改**",
            f"预计约 {est:.0f} 分钟；中途停下会自动等 "
            f"{config.RESUME_WAIT_MIN} 分钟续跑，重跑也会跳过已完成的",
            "不碰作品件，不删任何文件，不改 AO3",
        ],
        request_count=len(todo),
    )

    client = AO3Client(cookies=cookies, delay=config.REQUEST_DELAY_SEC)
    now = lambda: datetime.now().isoformat(timespec="seconds")

    ok = mismatch = failed = 0
    all_bad: list[str] = []
    stopped = None
    rounds_used = 0

    # ==================== 自动续跑：外层轮次循环 ====================
    while True:
     for i, w in enumerate(todo, 1):
        wid, expected = w["work_id"], w["comments"]
        print(f"\n--- [{i}/{len(todo)}] {wid} {(w['title'] or '')[:34]}｜blurb 报 {expected} 条")

        try:
            seg, shas, pages = fetch_one(client, wid, expected)
        except SessionLost as e:
            stopped = f"登录态失效：{e}"
            break
        except (FetchFailed, UrlNotAllowed) as e:
            print(f"    ✗ {e}")
            con.execute(
                "INSERT OR REPLACE INTO comment_fetch_state "
                "(work_id,status,expected,parsed,threads,pages,page_bytes,page_sha256,"
                " fetched_at,note) VALUES (?,'failed',?,0,0,0,NULL,NULL,?,?)",
                (wid, expected, now(), str(e)[:300]))
            con.commit()
            failed += 1
            continue

        rows = comments_parser.parse_comments(seg, my_user_id=me)

        # 原文落盘（只是评论区那一段，理由见文件头第二条）
        tmp = config.COMMENTS_DIR / f"{wid}.html.tmp"
        tmp.write_text(seg, encoding="utf-8")
        tmp.replace(config.COMMENTS_DIR / f"{wid}.html")

        bad = check_invariants(rows)
        all_bad += [f"{wid}: {b}" for b in bad]
        threads = sorted({r["thread_root_id"] for r in rows})
        n_mine = sum(r["is_mine"] for r in rows)
        status = "ok" if len(rows) == expected and not bad else "mismatch"
        ok += status == "ok"
        mismatch += status == "mismatch"

        print(f"    ✓ {len(rows)} 条 / {len(threads)} 串｜自己的回复 {n_mine} 条"
              f"｜别人的 {len(rows) - n_mine} 条"
              f"｜最深 {max((r['depth'] for r in rows), default=0) + 1} 层"
              f"｜{pages} 页")
        if len(rows) != expected:
            print(f"    ⚠ 解析出 {len(rows)} 条，blurb 报 {expected} 条 —— 数量对不上")
        for b in bad:
            print(f"    ⚠ {b}")

        # 入库。同一篇重抓时，先清掉这篇的旧记录再写，避免删掉的评论留成幽灵。
        con.execute("DELETE FROM comments WHERE work_id=?", (wid,))
        con.execute("DELETE FROM comment_threads WHERE work_id=?", (wid,))
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO comments (comment_id,work_id,chapter_id,"
                "chapter_label,parent_id,thread_root_id,depth,seq,user_id,username,pseud,"
                "display_name,is_guest,is_mine,posted_at,posted_tz,body,body_html,"
                "body_chars,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["comment_id"], wid, r["chapter_id"], r["chapter_label"], r["parent_id"],
                 r["thread_root_id"], r["depth"], r["seq"], r["user_id"], r["username"],
                 r["pseud"], r["display_name"], r["is_guest"], r["is_mine"],
                 r["posted_at"], r["posted_tz"], r["body"], r["body_html"],
                 r["body_chars"], now()))
        for root in threads:
            g = [r for r in rows if r["thread_root_id"] == root]
            head = next(r for r in g if r["comment_id"] == root)
            times = sorted(r["posted_at"] for r in g if r["posted_at"])
            con.execute(
                "INSERT OR REPLACE INTO comment_threads (thread_root_id,work_id,chapter_id,"
                "n_comments,n_mine,n_others,max_depth,started_at,last_at,starter_user,"
                "starter_uid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (root, wid, head["chapter_id"], len(g), sum(r["is_mine"] for r in g),
                 sum(1 for r in g if not r["is_mine"]),
                 max(r["depth"] for r in g), times[0] if times else None,
                 times[-1] if times else None, head["display_name"], head["user_id"]))
        con.execute(
            "INSERT OR REPLACE INTO comment_fetch_state "
            "(work_id,status,expected,parsed,threads,pages,page_bytes,page_sha256,"
            " fetched_at,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (wid, status, expected, len(rows), len(threads), pages, len(seg),
             shas[0] if shas else None, now(),
             "; ".join(bad)[:300] if bad else None))
        con.commit()

     con.commit()

     # ---------- 这一轮结束：要不要自动续 ----------
     if stopped is None:
         break
     if "登录态失效" in str(stopped):
         break                      # 等多久都没用，必须人处理
     if rounds_used >= config.RESUME_MAX_ROUNDS:
         print(f"\n已自动续跑 {rounds_used} 轮，达到上限，本次到此为止。")
         break
     rounds_used += 1
     print(f"\n{'=' * 60}")
     print(f"本轮停下：{stopped}")
     print(f"等 {config.RESUME_WAIT_MIN} 分钟后自动续跑"
           f"（第 {rounds_used}/{config.RESUME_MAX_ROUNDS} 轮）。")
     print("期间请不要自己刷 AO3 —— 限流是跨连接共享的。")
     print("不想等就直接关掉窗口：已完成的都已入库，下次重跑会接着来。")
     print("=" * 60, flush=True)
     time.sleep(config.RESUME_WAIT_MIN * 60)
     stopped = None
     todo = pick_todo(con)
     if not todo:
         break
     client._last_request_at = 0.0

    # ---------- 汇总 ----------
    print("\n" + "=" * 62)
    if stopped:
        print(f"**中途停下：** {stopped}")
        print("这不算失败。已抓的都已入库，直接重跑会跳过它们。\n")

    print(f"本次：入库正常 {ok} 篇｜数量/自查对不上 {mismatch} 篇｜请求失败 {failed} 篇")

    tot = con.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    thr = con.execute("SELECT COUNT(*) FROM comment_threads").fetchone()[0]
    mine = con.execute("SELECT COUNT(*) FROM comments WHERE is_mine=1").fetchone()[0]
    guest = con.execute("SELECT COUNT(*) FROM comments WHERE is_guest=1").fetchone()[0]
    blurb = con.execute(
        "SELECT SUM(comments) FROM works_index WHERE COALESCE(comments,0)>0").fetchone()[0]

    # 已核实的例外差额：blurb 多算了多少条
    gap = 0
    for wid, why in ACCEPTED_MISMATCH.items():
        row = con.execute(
            "SELECT expected, parsed FROM comment_fetch_state WHERE work_id=?",
            (wid,)).fetchone()
        if row:
            gap += (row["expected"] or 0) - (row["parsed"] or 0)

    print("\n全库评论")
    print("| 项 | 数 |")
    print("|---|---|")
    print(f"| 评论总条数（库） | {tot:,} |")
    print(f"| 索引页 blurb 合计 | {blurb:,} |")
    if gap:
        print(f"| 其中已核实的 blurb 虚高 | {gap:,} |")
    print(f"| 串（thread）数 | {thr:,} |")
    print(f"| 其中自己的回复 | {mine:,} |")
    print(f"| 别人给的 | {tot - mine:,} |")
    print(f"| 访客（无账号）评论 | {guest:,} |")

    print("\n对照 AO3 stats 页（作者 20260805 提供）：Comment Threads **113**")
    print(f"  本库串数 {thr}"
          f"{'：对上了' if thr == 113 else f'：差 {thr - 113:+d}，还没抓全或口径有别，需查'}")

    if all_bad:
        print(f"\n⚠ 自查发现 {len(all_bad)} 处可疑（**数据已照实存下，没有改动**）：")
        for b in all_bad[:20]:
            print(f"   {b}")
    else:
        print("\n自查全过：父子时间顺序、父节点存在性、层级与父的一致性，均无异常。")

    if ACCEPTED_MISMATCH:
        print(f"\n已核实的例外（不再重抓，但 blurb 数字一变就会自动回到队列）：")
        for wid, why in ACCEPTED_MISMATCH.items():
            t = con.execute("SELECT title FROM works_index WHERE work_id=?",
                            (wid,)).fetchone()
            print(f"  {wid} {(t['title'] if t else '?')[:26]} —— {why}")
        if tot + gap == blurb:
            print(f"  → {tot:,} + {gap} = {blurb:,}：与 blurb 合计**完全对上**。")

    left = len(pick_todo(con))
    if left:
        print(f"\n还剩 {left} 篇没抓。再跑一次本脚本即可继续。")
    else:
        print("\n✓ 全部抓完，没有待处理的。")

    print(f"\n共发出 {client.request_count} 次请求。"
          f"未修改 AO3 上任何内容，未删除任何本地文件。")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
