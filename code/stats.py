# stats.py —— L4 报表：按 CP / 按年 / 按系列 / 按语言（**完全不联网，只读**）
#
# 联网请求数：**0**
# 会写什么：**什么都不写。** 这个脚本只查询和打印，连数据库都是只读打开的。
#           完整输出照例存进 code/session_printouts/。
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'stats'
#         （要先跑过一次 derive_tags.py）
#
# ────────────────────────────────────────────────────────────────
# 口径说明（每张表上都会再印一遍，因为这些最容易看岔）
#
# ① **一篇多 CP 会被重复计入**，所以「按 CP」那张表各行相加**大于**全库总字数。
#    这是对的：问「这对 CP 写了多少字」时，多 CP 的篇目本来就该算进去。
#
# ② **canonical 口径**：`implied X` 并入 `X`（作者本人也这么算）。
#    字面口径同时列出，两者都对，只是问题不同。
#
# ③ **`/` 恋爱向与 `&` 友情向分开**，不合并（DESIGN-NOTES.md N-06②）。
#
# ④ **逐年统计用「分章」而非「整篇」**：一篇连载横跨几年是常事，
#    整篇算在完结那年会把早年的产量算没。分章日期与分章字数都在 `chapters` 表里。
#    ⚠️ 但标签是**作品级**的：把某篇所有章都算作它当前的 CP。
#      作者中途改标签的情况无从还原 —— 这是这张表已知的近似之处，不是 bug。
#
# ⑤ **草稿章不计入**（`is_draft=1`，10 章）：它们还没公开，也没有本地字数。

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import sqlite3
    import config
except Exception as _import_error:
    session_log.crash_dump("stats", _import_error)
    raise


def bar(n: int, top: int, width: int = 22) -> str:
    return "█" * max(1, round(n / top * width)) if top and n else ""


def table(rows, headers, aligns=None) -> None:
    if not rows:
        print("  （没有数据）")
        return
    cols = list(zip(*([headers] + [[str(c) for c in r] for r in rows])))
    w = [max(len(str(x)) for x in col) for col in cols]
    aligns = aligns or ["<"] * len(headers)
    def line(cells):
        return "  " + "  ".join(f"{str(c):{a}{wi}}" for c, a, wi in zip(cells, aligns, w))
    print(line(headers))
    print("  " + "  ".join("-" * wi for wi in w))
    for r in rows:
        print(line(r))


def main() -> int:
    session_log.start("stats")

    if not config.INDEX_DB.exists():
        print("✗ 还没有 data/index.sqlite。先跑 fetch_index.py。")
        return 1

    # 只读打开：这个脚本没有任何理由写数据库
    con = sqlite3.connect(f"file:{config.INDEX_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    if not con.execute("SELECT COUNT(*) FROM sqlite_master "
                       "WHERE type='table' AND name='work_tags'").fetchone()[0]:
        print("✗ 还没有 work_tags 表。先跑 derive_tags.py。")
        return 1

    nw = con.execute("SELECT COUNT(*) FROM works_index").fetchone()[0]
    tw = con.execute("SELECT SUM(words) FROM works_index").fetchone()[0] or 0
    print(f"全库 {nw} 篇｜AO3 口径总字数 {tw:,}\n")

    # ================= 1. 按 CP =================
    print("=" * 66)
    print("1. 按 CP 的字数统计（canonical 口径，恋爱向 `/`）")
    print("=" * 66)
    rows = con.execute("""
        SELECT wt.name_canonical nm, COUNT(DISTINCT wt.work_id) n,
               SUM(w.words) words
          FROM work_tags wt
          JOIN works_index w ON w.work_id = wt.work_id
          JOIN tags t ON t.kind='relationship' AND t.name_literal = wt.name_literal
         WHERE wt.kind='relationship' AND t.is_romantic = 1
         GROUP BY wt.name_canonical
         ORDER BY words DESC""").fetchall()
    top = rows[0]["words"] if rows else 0
    table([(r["nm"][:46], f"{r['n']:,}", f"{r['words']:,}",
            f"{round(r['words']/r['n']):,}", bar(r["words"], top)) for r in rows[:15]],
          ["CP（恋爱向）", "篇", "字数", "均字", ""],
          ["<", ">", ">", ">", "<"])
    print(f"\n  共 {len(rows)} 个恋爱向 CP。**各行相加 > 全库总字数**：")
    s = sum(r["words"] for r in rows)
    print(f"  相加 {s:,} vs 全库 {tw:,} —— 多出来的是多 CP 篇目的重复计入，**这是对的**。")

    print("\n" + "-" * 66)
    print("   按 CP（友情向 `&`，与上表**分开**，不能相加）")
    print("-" * 66)
    rows2 = con.execute("""
        SELECT wt.name_canonical nm, COUNT(DISTINCT wt.work_id) n, SUM(w.words) words
          FROM work_tags wt
          JOIN works_index w ON w.work_id = wt.work_id
          JOIN tags t ON t.kind='relationship' AND t.name_literal = wt.name_literal
         WHERE wt.kind='relationship' AND t.is_romantic = 0
         GROUP BY wt.name_canonical ORDER BY words DESC""").fetchall()
    top2 = rows2[0]["words"] if rows2 else 0
    table([(r["nm"][:46], f"{r['n']:,}", f"{r['words']:,}", bar(r["words"], top2))
           for r in rows2[:10]], ["CP（友情向）", "篇", "字数", ""], ["<", ">", ">", "<"])
    print(f"\n  共 {len(rows2)} 个友情向标签。")

    # 三人及以上
    rows3 = con.execute("""
        SELECT t.name_literal nm, t.n_members m, t.n_works n, SUM(w.words) words
          FROM tags t JOIN work_tags wt
            ON wt.kind='relationship' AND wt.name_literal=t.name_literal
          JOIN works_index w ON w.work_id=wt.work_id
         WHERE t.kind='relationship' AND t.n_members>=3
         GROUP BY t.name_literal ORDER BY words DESC""").fetchall()
    if rows3:
        print("\n" + "-" * 66)
        print("   三人及以上（三角等）")
        print("-" * 66)
        table([(r["nm"][:50], r["m"], r["n"], f"{r['words']:,}") for r in rows3],
              ["标签", "人数", "篇", "字数"], ["<", ">", ">", ">"])

    # ================= 2. 按 CP × 年 =================
    print("\n" + "=" * 66)
    print("2. 按 CP × 年 的字数（用**分章**日期与字数，见口径④）")
    print("=" * 66)
    yrs = [r[0] for r in con.execute("""
        SELECT DISTINCT substr(published_at,1,4) y FROM chapters
         WHERE published_at IS NOT NULL AND COALESCE(is_draft,0)=0
         ORDER BY y""").fetchall() if r[0]]
    main_cps = [r["nm"] for r in rows[:6]]
    grid = []
    for cp in main_cps:
        line = [cp[:30]]
        for y in yrs:
            v = con.execute("""
                SELECT COALESCE(SUM(ch.words_local),0)
                  FROM chapters ch
                  JOIN work_tags wt ON wt.work_id=ch.work_id AND wt.kind='relationship'
                 WHERE wt.name_canonical=? AND substr(ch.published_at,1,4)=?
                   AND COALESCE(ch.is_draft,0)=0""", (cp, y)).fetchone()[0]
            line.append(f"{v:,}" if v else "—")
        grid.append(tuple(line))
    table(grid, ["CP"] + yrs, ["<"] + [">"] * len(yrs))

    tot_ch = con.execute("""SELECT COALESCE(SUM(words_local),0) FROM chapters
                            WHERE COALESCE(is_draft,0)=0""").fetchone()[0]
    print(f"\n  全库分章字数合计 {tot_ch:,}（本地口径，与 AO3 的 {tw:,} 有小差，见DESIGN-NOTES.md N-09）")
    print("  逐年（全库，不分 CP）：")
    ylines = []
    for y in yrs:
        v = con.execute("""SELECT COALESCE(SUM(words_local),0), COUNT(*) FROM chapters
                           WHERE substr(published_at,1,4)=? AND COALESCE(is_draft,0)=0""",
                        (y,)).fetchone()
        ylines.append((y, f"{v[0]:,}", v[1]))
    topy = max(int(l[1].replace(",", "")) for l in ylines) if ylines else 0
    table([(y, w, c, bar(int(w.replace(",", "")), topy)) for y, w, c in ylines],
          ["年", "字数", "章数", ""], ["<", ">", ">", "<"])

    # ================= 3. 按系列 =================
    print("\n" + "=" * 66)
    print("3. 按系列")
    print("=" * 66)
    rows4 = con.execute("""
        SELECT s.name nm, s.n_works n, SUM(w.words) words
          FROM series s JOIN work_series ws ON ws.series_id=s.series_id
          JOIN works_index w ON w.work_id=ws.work_id
         GROUP BY s.series_id ORDER BY words DESC""").fetchall()
    table([(r["nm"][:44], r["n"], f"{r['words']:,}") for r in rows4],
          ["系列", "篇", "字数"], ["<", ">", ">"])
    solo = con.execute("""SELECT COUNT(*) FROM works_index
                          WHERE work_id NOT IN (SELECT work_id FROM work_series)""").fetchone()[0]
    print(f"\n  不属于任何系列的：{solo} 篇")

    # ================= 4. 按语言 / 分级 / 完结 =================
    print("\n" + "=" * 66)
    print("4. 按语言 / 分级 / 完结状态")
    print("=" * 66)
    for col, label in (("language", "语言"), ("rating", "分级"), ("category", "Category")):
        r = con.execute(f"""SELECT COALESCE({col},'（空）') k, COUNT(*) n, SUM(words) w
                            FROM works_index GROUP BY k ORDER BY n DESC""").fetchall()
        print(f"\n  ── {label} ──")
        table([(x["k"][:40], x["n"], f"{x['w']:,}") for x in r],
              [label, "篇", "字数"], ["<", ">", ">"])
    r = con.execute("""SELECT CASE is_wip WHEN 1 THEN '未完结 WIP' ELSE '已完结' END k,
                       COUNT(*) n, SUM(words) w FROM works_index GROUP BY is_wip""").fetchall()
    print("\n  ── 完结状态 ──")
    table([(x["k"], x["n"], f"{x['w']:,}") for x in r], ["状态", "篇", "字数"], ["<", ">", ">"])

    # ================= 5. freeform 前几名 =================
    print("\n" + "=" * 66)
    print("5. Additional Tags（freeform）前 20")
    print("=" * 66)
    r = con.execute("""SELECT name_canonical nm, COUNT(DISTINCT work_id) n
                       FROM work_tags WHERE kind='freeform'
                       GROUP BY nm ORDER BY n DESC LIMIT 20""").fetchall()
    table([(x["nm"][:44], x["n"]) for x in r], ["标签", "篇"], ["<", ">"])
    nf = con.execute("SELECT COUNT(*) FROM tags WHERE kind='freeform'").fetchone()[0]
    print(f"\n  共 {nf} 个不同的 freeform 标签")

    print("\n" + "=" * 66)
    print("口径提醒：①多 CP 重复计入 ②canonical 已并入 implied ③恋爱/友情不合并")
    print("          ④逐年用分章 ⑤草稿章不计。联网请求数：0，未写入任何数据。")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
