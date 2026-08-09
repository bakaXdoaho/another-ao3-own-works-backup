# fetch_works.py —— 阶段 C：抓正文（官方下载件）与分章日期
#
# 每篇两次请求：
#   1. /downloads/{id}/{id}.html   官方下载件 → data/ao3/works/{id}/{id}.html
#   2. /works/{id}/navigate        分章发布日期 → data/ao3/works/{id}/navigate.html
#
# （slug 与 ?updated_at= 实测均被忽略，见DESIGN-NOTES.md N-02⑥，所以 URL 可以直接构造，
#   不必先去 work 页抓真实链接 —— 全库省下与作品数相同的请求次数。）
#
# 联网请求数：默认只跑 config.FIRST_RUN_WORK_LIMIT 篇（初始 20）（在config.py调整），即最多 40 次。
#             确认干净后把那个数字调大即可。
# 会写什么：data/ao3/works/{id}/ 下的原始件；index.sqlite 的 work_files / chapters / fetch_state
# 不会写什么：**永不删除任何已落盘的文件**；不改 AO3
#
# 断在半路是正常的，不是失败：状态存在 fetch_state 表里，重跑会自动跳过已完成的。
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'fetch_works'

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import hashlib
    import re
    import sqlite3
    import time
    from datetime import datetime
    import config
    import fetch_index
    from ao3_client import (
        AO3Client, save_raw, confirm, SessionLost, FetchFailed,
        is_forced_logout, looks_like_rate_limit,
    )
except Exception as _import_error:
    session_log.crash_dump("fetch_works", _import_error)
    raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_state (
    work_id         INTEGER PRIMARY KEY,
    download_status TEXT,      -- ok / quarantined / failed / (NULL=没做过)
    download_at     TEXT,
    navigate_status TEXT,
    navigate_at     TEXT,
    fetched_updated_at INTEGER, -- 抓的时候索引里的 updated_at，用来判断要不要重抓
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT
);
CREATE TABLE IF NOT EXISTS work_files (
    work_id     INTEGER PRIMARY KEY,
    path        TEXT,
    sha256      TEXT,
    chars       INTEGER,
    words_ao3   INTEGER,     -- 下载件 Stats 里的字数
    chapters_text TEXT,      -- 下载件 Stats 里的 "5/5"
    fetched_at  TEXT,
    file_source TEXT         -- ao3_official_html / ao3_deletion_email / fff
);
CREATE TABLE IF NOT EXISTS chapters (
    work_id      INTEGER,
    idx          INTEGER,
    chapter_id   INTEGER,
    title        TEXT,
    published_at TEXT,       -- 发布日期，**不是**最后修改日期（AO3 不提供后者）
    source       TEXT,       -- navigate
    is_draft     INTEGER,    -- 1 = 只在 navigate 里、下载件里没有 → **未公开的草稿章**
    PRIMARY KEY (work_id, idx)
);
"""

# navigate 页的分章行（结构见DESIGN-NOTES.md N-02⑤）
_NAV_ROW = re.compile(
    r'<a href="/works/\d+/chapters/(\d+)">(.*?)</a>\s*'
    r'<span class="datetime">\((\d{4}-\d{2}-\d{2})\)</span>',
    re.S,
)
_STAT_WORDS = re.compile(r"Words:\s*([\d,]+)")
_STAT_CHAPS = re.compile(r"Chapters:\s*(\d+/[\d?]+)")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_download(html: str, work_id: int, idx_words: int | None,
                   idx_chaps: str | None) -> tuple[bool, str]:
    """fail closed：只接受确实像一篇作品、且与索引对得上的下载件。"""
    if is_forced_logout(html):
        return False, "Lost Cookie / Forced Logout 页"
    if looks_like_rate_limit(html):
        return False, "疑似限流页"
    if len(html) < 2000:
        return False, f"太短（{len(html)} 字符）"
    if str(work_id) not in html:
        return False, "正文里找不到 work id"
    if "</html>" not in html.lower():
        return False, "没有 </html> 结尾，可能被截断"
    if "Posted originally on the" not in html:
        return False, "找不到官方下载件的 preface 特征句"

    # ---- 与索引对账 ----
    mw = _STAT_WORDS.search(html)
    got_words = int(mw.group(1).replace(",", "")) if mw else None
    if got_words is None:
        return False, "下载件里读不到 Words"

    if idx_words:
        # 容差：AO3 自己在不同页面上的数字就能差 1（DESIGN-NOTES.md N-02），所以不能用相等
        tol = max(5, int(idx_words * 0.001))
        if abs(got_words - idx_words) > tol:
            return False, (f"字数与索引差太多（下载件 {got_words:,} vs 索引 {idx_words:,}，"
                           f"容差 ±{tol}）—— 可能命中了旧缓存")

    mc = _STAT_CHAPS.search(html)
    got_chaps = mc.group(1) if mc else None
    if idx_chaps and got_chaps:
        def done(s: str) -> int:
            try:
                return int(s.split("/")[0])
            except Exception:
                return -1
        # 下载件章数**少于**索引 → 很可能是旧缓存，隔离；
        # 多于索引 → 是索引旧了（期间发了新章），正常接受。
        if done(got_chaps) < done(idx_chaps):
            return False, (f"下载件章数少于索引（{got_chaps} < {idx_chaps}）—— 疑似旧缓存")

    return True, "OK"


def pick_todo(con) -> list[dict]:
    """挑出还需要处理的作品。

    排序优先级（20260804 修正）：
      ① **半成品**（下载件与 navigate 只成了一个）—— 补完只要 1 次请求，
         而且这种状态最容易被忘掉。原先它们被排到队尾，260 篇里排 189–192，
         等于永远轮不到。
      ② 从没抓过的
      ③ 抓过、但 AO3 上 updated_at 变新了，需要刷新
    """
    return [dict(r) for r in con.execute("""
        SELECT w.work_id, w.title, w.words, w.chapters_text, w.chapters_done,
               w.updated_at_unix,
               s.download_status, s.navigate_status, s.fetched_updated_at
        FROM works_index w LEFT JOIN fetch_state s USING (work_id)
        WHERE s.download_status IS NULL OR s.download_status != 'ok'
           OR s.navigate_status IS NULL OR s.navigate_status != 'ok'
           OR s.fetched_updated_at IS NULL OR w.updated_at_unix > s.fetched_updated_at
        ORDER BY
          CASE
            -- ① 半成品最优先：下载件与 navigate 只成了一个，补完只要 1 次请求，
            --    而且这种状态最容易被忘掉。（20260804 修：原先把它们排到了队尾）
            WHEN COALESCE(s.download_status,'') = 'ok'
             AND COALESCE(s.navigate_status,'') <> 'ok' THEN 0
            WHEN COALESCE(s.download_status,'') <> 'ok'
             AND COALESCE(s.navigate_status,'') = 'ok' THEN 0
            -- ② 从没抓过的
            WHEN s.work_id IS NULL OR s.download_status IS NULL THEN 1
            -- ③ 抓过但 AO3 上有更新，需要刷新
            ELSE 2
          END,
          w.updated_at_unix DESC
    """)]


def main() -> int:
    session_log.start("fetch_works")
    config.ensure_dirs()

    if not config.INDEX_DB.exists():
        print("✗ 还没有 data/index.sqlite。先跑 fetch_index.py。")
        return 1

    print("[1/4] 检查 cookie …")
    try:
        cookies = config.load_cookies()
    except config.CookieMissing as e:
        print("\n✗ " + str(e))
        return 1
    print(config.describe_cookies(cookies))

    con = fetch_index.open_db()
    con.executescript(SCHEMA)

    todo = pick_todo(con)
    total_pending = len(todo)
    limit = config.FIRST_RUN_WORK_LIMIT
    batch = todo[:limit] if limit else todo

    done_ok = con.execute(
        "SELECT COUNT(*) FROM fetch_state WHERE download_status='ok' AND navigate_status='ok'"
    ).fetchone()[0]
    all_works = con.execute("SELECT COUNT(*) FROM works_index").fetchone()[0]

    print(f"\n全库 {all_works} 篇｜已完成 {done_ok} 篇｜待抓 {total_pending} 篇")
    if not batch:
        print("\n✓ 没有需要抓的。全部完成。")
        return 0
    print(f"本次只处理前 {len(batch)} 篇（上限在 config.FIRST_RUN_WORK_LIMIT，现为 {limit}）")

    est = len(batch) * (config.DOWNLOAD_DELAY_SEC + config.REQUEST_DELAY_SEC)
    confirm(
        plan_lines=[
            f"抓 {len(batch)} 篇作品，每篇 2 次请求（下载件 + navigate）",
            f"下载件存到 data/ao3/works/{{id}}/{{id}}.html（覆写同路径，历史交给 git）",
            "校验不过的**不入库**，隔离到 data/probe_raw/ 留给人看",
            f"预计约 {est / 60:.0f} 分钟；中途停下是正常的，重跑会跳过已完成的",
            "永不删除任何已落盘文件，不改 AO3",
        ],
        request_count=len(batch) * 2,
    )

    client = AO3Client(cookies=cookies, delay=config.DOWNLOAD_DELAY_SEC)
    now = lambda: datetime.now().isoformat(timespec="seconds")

    report: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        report.append(line)

    say(f"# 正文抓取报告 · {datetime.now():%Y-%m-%d %H:%M}")
    say("")

    ok_dl = ok_nav = quarantined = failed = 0
    stopped = None
    rounds_used = 0

  # ==================== 自动续跑：外层轮次循环 ====================
    while True:
     for i, w in enumerate(batch, 1):
        wid = w["work_id"]
        wdir = config.WORKS_DIR / str(wid)
        wdir.mkdir(parents=True, exist_ok=True)
        print(f"\n--- [{i}/{len(batch)}] {wid} {(w['title'] or '')[:36]}")

        con.execute(
            "INSERT OR IGNORE INTO fetch_state (work_id, attempts) VALUES (?, 0)", (wid,))
        con.execute(
            "UPDATE fetch_state SET attempts=COALESCE(attempts,0)+1 WHERE work_id=?", (wid,))

        # ---------- 1. 下载件 ----------
        if w["download_status"] != "ok" or (w["fetched_updated_at"] or 0) < (w["updated_at_unix"] or 0):
            try:
                html = client.get(f"/downloads/{wid}/{wid}.html")
            except SessionLost as e:
                stopped = f"登录态失效：{e}"
                break
            except FetchFailed as e:
                stopped = str(e)
                break

            good, why = check_download(html, wid, w["words"], w["chapters_text"])
            if good:
                tmp = wdir / f"{wid}.html.tmp"
                tmp.write_text(html, encoding="utf-8")
                tmp.replace(wdir / f"{wid}.html")
                mw = _STAT_WORDS.search(html)
                mc = _STAT_CHAPS.search(html)
                con.execute(
                    "INSERT OR REPLACE INTO work_files "
                    "(work_id, path, sha256, chars, words_ao3, chapters_text, fetched_at, file_source) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (wid, str((wdir / f"{wid}.html").relative_to(config.MAIN_DIR)),
                     sha256(html), len(html),
                     int(mw.group(1).replace(",", "")) if mw else None,
                     mc.group(1) if mc else None, now(), "ao3_official_html"),
                )
                con.execute(
                    "UPDATE fetch_state SET download_status='ok', download_at=?, "
                    "fetched_updated_at=?, last_error=NULL WHERE work_id=?",
                    (now(), w["updated_at_unix"], wid))
                ok_dl += 1
                print(f"    ✓ 下载件 {len(html):,} 字符")
            else:
                q = save_raw(html, f"QUARANTINE_work{wid}")
                con.execute(
                    "UPDATE fetch_state SET download_status='quarantined', download_at=?, "
                    "last_error=? WHERE work_id=?", (now(), why, wid))
                quarantined += 1
                say(f"- ⚠ `{wid}` {(w['title'] or '')[:30]}：**未入库** —— {why}")
                say(f"  隔离件：`{q.name}`")
                print(f"    ⚠ 校验未过：{why}（已隔离，未入库）")
        else:
            print("    · 下载件已是最新，跳过")

        # ---------- 2. navigate ----------
        if w["navigate_status"] != "ok" or (w["fetched_updated_at"] or 0) < (w["updated_at_unix"] or 0):
            try:
                nav = client.get(f"/works/{wid}/navigate")
            except SessionLost as e:
                stopped = f"登录态失效：{e}"
                break
            except FetchFailed as e:
                stopped = str(e)
                break

            rows = _NAV_ROW.findall(nav)
            if is_forced_logout(nav) or looks_like_rate_limit(nav) or not rows:
                q = save_raw(nav, f"QUARANTINE_nav{wid}")
                con.execute(
                    "UPDATE fetch_state SET navigate_status='quarantined', navigate_at=?, "
                    "last_error=? WHERE work_id=?", (now(), "navigate 解析出 0 章", wid))
                say(f"- ⚠ `{wid}` navigate 解析出 0 章，已隔离：`{q.name}`")
                print("    ⚠ navigate 解析出 0 章（已隔离）")
            else:
                tmp = wdir / "navigate.html.tmp"
                tmp.write_text(nav, encoding="utf-8")
                tmp.replace(wdir / "navigate.html")
                # 已公开的章数：以索引页的 "13/23" 里的 13 为准。
                # ⚠️ 20260804 实测：**作为作者登录时，/navigate 会连草稿章一起列出来**。
                #    某篇索引显示 13/23，navigate 却列了 23 章 —— 多出的 10 章是尚未公开的草稿。
                #    这是白捡的额外信息（自己的草稿章标题与日期），但**绝不能当作公开数据**：
                #    往公开网站导出时必须过滤 is_draft=1（见DESIGN-NOTES.md N-11 的草稿章发现）。
                published_n = w["chapters_done"] if w["chapters_done"] is not None else len(rows)
                con.execute("DELETE FROM chapters WHERE work_id=?", (wid,))
                for n, (cid, title, date) in enumerate(rows, 1):
                    con.execute(
                        "INSERT OR REPLACE INTO chapters "
                        "(work_id, idx, chapter_id, title, published_at, source, is_draft) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (wid, n, int(cid), re.sub(r"<[^>]+>", "", title).strip(),
                         date, "navigate", 1 if n > published_n else 0),
                    )
                con.execute(
                    "UPDATE fetch_state SET navigate_status='ok', navigate_at=? WHERE work_id=?",
                    (now(), wid))
                ok_nav += 1
                print(f"    ✓ navigate {len(rows)} 章")
        else:
            print("    · navigate 已是最新，跳过")

        con.commit()

     con.commit()

     # ---------- 这一轮结束：要不要自动续 ----------
     if stopped is None:
         break                      # 正常跑完，收工
     if isinstance(stopped, str) and "登录态失效" in stopped:
         break                      # 登录问题等多久都没用，必须人处理
     if rounds_used >= config.RESUME_MAX_ROUNDS:
         say(f"已自动续跑 {rounds_used} 轮，达到上限 "
             f"（config.RESUME_MAX_ROUNDS = {config.RESUME_MAX_ROUNDS}），本次到此为止。")
         break

     rounds_used += 1
     wait = config.RESUME_WAIT_MIN * 60
     print(f"\n{'=' * 60}")
     print(f"本轮停下：{stopped}")
     print(f"等 {config.RESUME_WAIT_MIN} 分钟后自动续跑"
           f"（第 {rounds_used}/{config.RESUME_MAX_ROUNDS} 轮）。")
     print("期间请不要自己刷 AO3 —— 限流是跨连接共享的。")
     print("不想等就直接关掉窗口：已完成的都已入库，下次重跑会接着来。")
     print("=" * 60, flush=True)
     say(f"- 自动续跑第 {rounds_used} 轮（上一轮停因：{stopped}）")
     time.sleep(wait)

     # 重新读取待抓清单（状态已变），继续
     stopped = None
     todo = pick_todo(con)
     batch = todo[:limit] if limit else todo
     if not batch:
         break
     client._last_request_at = 0.0   # 等了这么久，不必再叠加节流

    con.commit()

    # ---------- 报告 ----------
    say("")
    if stopped:
        say(f"**中途停下：** {stopped}")
        say("")
        say("**这不算失败。** 状态已存进 `fetch_state`，直接重跑本脚本会跳过已完成的。")
        say("")
    say("| 项 | 数 |")
    say("|---|---|")
    say(f"| 本次处理 | {i if batch else 0} / {len(batch)} |")
    say(f"| 下载件入库 | {ok_dl} |")
    say(f"| navigate 入库 | {ok_nav} |")
    say(f"| **校验未过被隔离** | **{quarantined}** |")
    say("")

    tot_done = con.execute(
        "SELECT COUNT(*) FROM fetch_state WHERE download_status='ok' AND navigate_status='ok'"
    ).fetchone()[0]
    say(f"全库进度：**{tot_done} / {all_works}** 篇已完整抓取。")
    say("")
    if tot_done < all_works:
        say(f"还剩 {all_works - tot_done} 篇。再跑一次本脚本即可继续"
            f"（每次 {config.FIRST_RUN_WORK_LIMIT} 篇；确认干净后可把 "
            f"`config.FIRST_RUN_WORK_LIMIT` 调大）。")
    say("")
    say("---")
    say(f"共发出 {client.request_count} 次请求。未修改 AO3 上任何内容，未删除任何本地文件。")

    # 只有「出了要人处理的事」才写报告文件。
    # 20260805：例行抓取的报告与运行记录内容几乎完全重复（23 个报告里 10 个是这种），
    # 属于冗余。现在改成：一切正常就不写文件，运行记录里已经全都有了。
    # 20260805 起不再写 reports/ 文件：内容与运行记录完全重复。
    print("\n（不再单独写报告文件 —— 完整过程见上方运行记录。）")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
