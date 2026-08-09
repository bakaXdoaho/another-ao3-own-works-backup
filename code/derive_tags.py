# derive_tags.py —— L4 第一步：把标签/系列从 JSON 摊平成表（**完全不联网**）
#
# 现在标签是塞在 works_index 的 5 个 JSON 列里的：
#   fandoms_json / warnings_json / relationships_json / characters_json / freeforms_json
# 这样存没错（原样保真），但没法查 ——「某对 CP 写了多少字」
# 得把 280 行 JSON 全解出来才能回答。本脚本把它摊成两张表，之后就只是写 SQL。
#
# 联网请求数：**0**
# 会写什么：index.sqlite 的 tags / work_tags / series / work_series
# 不会写什么：不碰 works_index，不碰任何原始件，不删任何东西
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'derive_tags'
#
# ────────────────────────────────────────────────────────────────
# 三条必须守住的口径（都是之前踩出来的，不是凭空定的）
#
# ① **字面标签永不改动。**（DESIGN-NOTES.md N-12）
#    `implied X` 与 `X` 在 AO3 上是两个不同的字面标签，wrangler 会把前者
#    归并到后者。作者本人也把 `implied X` 算作 X —— 但那是**统计口径**，
#    不是「字面标签错了」。所以两个都存：
#      name_literal   = 页面上原样的字符串，**永远不改**
#      name_canonical = 归并后的字符串，统计默认走这个
#    归并规则来自 `tag_synonyms` 表（已有 3 条，且每条都有算术验证）。
#
# ② **`/` 与 `&` 语义不同，不能混。**（DESIGN-NOTES.md N-06②）
#    `/` = 恋爱向，`&` = 友情/gen 向。`配置表` 三条全是 `&` 且 Category: Gen。
#    若按 CP 统计时把两者合并，等于把「写了他俩的友情」算成「写了他俩的恋爱」。
#    → 存 `is_romantic`：`/` → 1，`&` → 0。
#
# ③ **一篇多 CP 会被重复计入，这是对的。**（DESIGN-NOTES.md N-28）
#    问「这对 CP 写了多少字」时，多 CP 的篇目本来就该算进去。
#    所以各行相加会大于全库总字数 —— 报表上必须写明，否则看的人会以为算错了。

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import json
    import re
    import sqlite3
    from datetime import datetime
    import config
    import fetch_index
except Exception as _import_error:
    session_log.crash_dump("derive_tags", _import_error)
    raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
    kind           TEXT NOT NULL,   -- fandom/warning/relationship/character/freeform
    name_literal   TEXT NOT NULL,   -- 页面上原样的字符串，**永不改动**
    name_canonical TEXT NOT NULL,   -- 归并后；没有归并规则时 = name_literal
    is_romantic    INTEGER,         -- relationship 专用：/ =1，& =0，其它 NULL
    n_members      INTEGER,         -- relationship 专用：几个人（三角 = 3）
    members_json   TEXT,            -- relationship 专用：拆出来的成员
    ao3_tag_id     INTEGER,         -- 侧边栏 facets 里能对上的话就记下
    n_works        INTEGER,         -- 字面口径的篇数
    derived_at     TEXT,
    PRIMARY KEY (kind, name_literal)
);

CREATE TABLE IF NOT EXISTS work_tags (
    work_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    name_literal   TEXT NOT NULL,
    name_canonical TEXT NOT NULL,
    position       INTEGER,         -- 在该篇标签列表里的次序（AO3 的排序有意义）
    PRIMARY KEY (work_id, kind, name_literal)
);
CREATE INDEX IF NOT EXISTS idx_wt_canon ON work_tags(kind, name_canonical);
CREATE INDEX IF NOT EXISTS idx_wt_work  ON work_tags(work_id);

CREATE TABLE IF NOT EXISTS series (
    series_id  INTEGER PRIMARY KEY,
    name       TEXT,
    n_works    INTEGER,
    derived_at TEXT
);

CREATE TABLE IF NOT EXISTS work_series (
    work_id   INTEGER NOT NULL,
    series_id INTEGER NOT NULL,
    part      INTEGER,
    PRIMARY KEY (work_id, series_id)
);
"""

JSON_COLS = [
    ("fandoms_json", "fandom"),
    ("warnings_json", "warning"),
    ("relationships_json", "relationship"),
    ("characters_json", "character"),
    ("freeforms_json", "freeform"),
]

# facets 表里的 kind 名字和这里的对法
FACET_KIND = {
    "fandom": "fandom_ids", "warning": "archive_warning_ids",
    "relationship": "relationship_ids", "character": "character_ids",
    "freeform": "freeform_ids",
}


def split_relationship(name: str) -> tuple[int | None, list[str]]:
    """把 CP 标签拆成成员，并判断是恋爱向还是友情向。

    AO3 的写法：
      `Yuris Leclair | Yuri Leclerc/Claude von Riegan`   → `/` 恋爱向，2 人
      `Dimitri Alexandre Blaiddyd & Claude von Riegan`   → `&` 友情向，2 人
      `Ashe/Yuri/Claude`                                  → 三角，3 人

    ⚠️ 注意 `|`：那是 **AO3 自己的「原名 | 译名」写法**（Yuris Leclair | Yuri Leclerc
       是同一个人的两种叫法），**不是**作者笔记里那个分隔符。别拿它拆人。
    """
    if "/" in name:
        return 1, [p.strip() for p in name.split("/") if p.strip()]
    if "&" in name:
        return 0, [p.strip() for p in name.split("&") if p.strip()]
    return None, [name.strip()]          # 单人 tag（如 Minor or Background Relationship(s)）


def load_synonyms(con) -> dict[tuple[str, str], str]:
    """读归并规则：(kind, 字面) → canonical。只认 status='confirmed' 的。

    ⚠️ 20260805 的一个真 bug，值得留着当例子：
       这个函数原先写成 `except sqlite3.OperationalError: pass; return {}`，
       而查询里的列名又写错了（写成 name_literal/name_canonical，
       实际是 literal/canonical）。于是 ——
       **「读不到归并规则」被无声地变成了「没有归并规则」**，
       脚本照常跑完、照常打印「归并规则 0 条」，一切看着正常，
       只有统计口径悄悄错了（canonical 172 会退回字面 161）。

       所以现在分两种情况区别对待：
         · 表**不存在** → 合法，安静地当作没有规则（首次运行就是这样）
         · 表存在但**读不出来** → **抛出去**，绝不假装没有规则
    """
    have = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tag_synonyms'"
    ).fetchone()[0]
    if not have:
        print("（还没有 tag_synonyms 表 —— 本次不做任何归并，统计走字面口径）")
        return {}

    out = {}
    for r in con.execute(
            "SELECT kind, literal, canonical, status FROM tag_synonyms"):
        if (r["status"] or "").lower() == "confirmed":
            out[(r["kind"], r["literal"])] = r["canonical"]

    total = con.execute("SELECT COUNT(*) FROM tag_synonyms").fetchone()[0]
    if total and not out:
        print(f"⚠ tag_synonyms 里有 {total} 条，但没有一条 status='confirmed' —— "
              f"本次不做归并。若这不是你要的，检查那张表的 status 列。")
    return out


def main() -> int:
    session_log.start("derive_tags")
    config.ensure_dirs()

    if not config.INDEX_DB.exists():
        print("✗ 还没有 data/index.sqlite。先跑 fetch_index.py。")
        return 1

    con = fetch_index.open_db()
    con.executescript(SCHEMA)
    now = datetime.now().isoformat(timespec="seconds")

    syn = load_synonyms(con)
    print(f"归并规则（tag_synonyms 里 confirmed 的）：{len(syn)} 条")
    for (k, lit), canon in syn.items():
        print(f"  {k}｜{lit}  →  {canon}")
    print()

    # AO3 侧边栏给的 canonical tag_id（只有前 10 名，有就用，没有就算了）
    facet_id: dict[tuple[str, str], int] = {}
    for r in con.execute("SELECT kind, tag_id, name FROM facets"):
        for our, theirs in FACET_KIND.items():
            if r["kind"] == theirs:
                facet_id[(our, r["name"])] = r["tag_id"]

    # ---- 重建。**先清空再写**：这两张表是纯派生物，源是 works_index。----
    # ⚠️ DESIGN-NOTES.md N-24 的教训：「清空重建」在**重建源比目标窄**时等于隐性删除。
    #    这里安全，因为源就是 works_index 本身（它已经保留了已删作品的行），
    #    目标不可能比源宽。**但换任何别的源之前，都要重新想一遍这句话。**
    con.execute("DELETE FROM tags")
    con.execute("DELETE FROM work_tags")
    con.execute("DELETE FROM series")
    con.execute("DELETE FROM work_series")

    rows = con.execute("SELECT * FROM works_index").fetchall()
    print(f"读入 {len(rows)} 篇作品的标签 JSON …\n")

    tag_seen: dict[tuple[str, str], int] = {}      # (kind, literal) → 篇数
    ser_seen: dict[int, tuple[str, int]] = {}      # series_id → (name, 篇数)
    n_wt = n_ws = 0
    bad_json = 0

    for w in rows:
        wid = w["work_id"]
        for col, kind in JSON_COLS:
            try:
                items = json.loads(w[col] or "[]")
            except Exception:
                bad_json += 1
                print(f"  ⚠ {wid} 的 {col} 解不出 JSON，跳过（**没有猜，也没有写入**）")
                continue
            for pos, lit in enumerate(items, 1):
                if not isinstance(lit, str) or not lit.strip():
                    continue
                lit = lit.strip()
                canon = syn.get((kind, lit), lit)
                con.execute(
                    "INSERT OR REPLACE INTO work_tags "
                    "(work_id, kind, name_literal, name_canonical, position) "
                    "VALUES (?,?,?,?,?)", (wid, kind, lit, canon, pos))
                tag_seen[(kind, lit)] = tag_seen.get((kind, lit), 0) + 1
                n_wt += 1

        # 系列
        try:
            for s in json.loads(w["series_json"] or "[]"):
                sid = s.get("series_id")
                if sid is None:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO work_series (work_id, series_id, part) "
                    "VALUES (?,?,?)", (wid, sid, s.get("part")))
                name, n = ser_seen.get(sid, (s.get("name"), 0))
                ser_seen[sid] = (name or s.get("name"), n + 1)
                n_ws += 1
        except Exception:
            bad_json += 1
            print(f"  ⚠ {wid} 的 series_json 解不出，跳过")

    # ---- tags 表 ----
    for (kind, lit), n in tag_seen.items():
        canon = syn.get((kind, lit), lit)
        rom = memb = None
        mj = None
        if kind == "relationship":
            rom, members = split_relationship(lit)
            memb = len(members)
            mj = json.dumps(members, ensure_ascii=False)
        con.execute(
            "INSERT OR REPLACE INTO tags (kind, name_literal, name_canonical, is_romantic,"
            " n_members, members_json, ao3_tag_id, n_works, derived_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, lit, canon, rom, memb, mj, facet_id.get((kind, canon)), n, now))

    for sid, (name, n) in ser_seen.items():
        con.execute(
            "INSERT OR REPLACE INTO series (series_id, name, n_works, derived_at) "
            "VALUES (?,?,?,?)", (sid, name, n, now))

    con.commit()

    # ---- 体检 ----
    print("=" * 62)
    print("摊平结果")
    print("| 类别 | 不同标签数 | 挂载次数 |")
    print("|---|---|---|")
    for _, kind in JSON_COLS:
        nt = con.execute("SELECT COUNT(*) FROM tags WHERE kind=?", (kind,)).fetchone()[0]
        nl = con.execute("SELECT COUNT(*) FROM work_tags WHERE kind=?", (kind,)).fetchone()[0]
        print(f"| {kind} | {nt} | {nl} |")
    print(f"| series | {len(ser_seen)} | {n_ws} |")

    # 归并前后的差，正是DESIGN-NOTES.md N-12 讲的那件事
    print("\n字面 vs canonical（只有 relationship 目前有归并规则）")
    diff = con.execute("""
        SELECT name_canonical,
               COUNT(DISTINCT name_literal) n_variants,
               COUNT(DISTINCT work_id) n_works
          FROM work_tags WHERE kind='relationship'
         GROUP BY name_canonical HAVING n_variants > 1
         ORDER BY n_works DESC""").fetchall()
    if diff:
        for r in diff:
            lits = con.execute(
                "SELECT DISTINCT name_literal FROM work_tags "
                "WHERE kind='relationship' AND name_canonical=?",
                (r["name_canonical"],)).fetchall()
            print(f"  {r['name_canonical'][:50]}")
            print(f"    canonical 口径 {r['n_works']} 篇 ← 由 {r['n_variants']} 个字面标签合并：")
            for l in lits:
                n = con.execute(
                    "SELECT COUNT(*) FROM work_tags WHERE kind='relationship' "
                    "AND name_literal=?", (l["name_literal"],)).fetchone()[0]
                print(f"      {n:>3} 篇  {l['name_literal'][:56]}")
    else:
        print("  （没有一个 canonical 对应多个字面标签）")

    print("\n恋爱向 / 友情向（DESIGN-NOTES.md N-06② 要求必须分开）")
    for rom, label in ((1, "恋爱向 `/`"), (0, "友情向 `&`"), (None, "单人 tag")):
        q = ("SELECT COUNT(*) FROM tags WHERE kind='relationship' AND is_romantic IS NULL"
             if rom is None else
             "SELECT COUNT(*) FROM tags WHERE kind='relationship' AND is_romantic=?")
        n = con.execute(q, () if rom is None else (rom,)).fetchone()[0]
        print(f"  {label:<12} {n} 个不同标签")

    n3 = con.execute(
        "SELECT COUNT(*) FROM tags WHERE kind='relationship' AND n_members>=3").fetchone()[0]
    print(f"  三人及以上的 CP 标签：{n3} 个")

    # ---- 自动找出「还没有归并规则的 implied 标签」----
    # 为什么每次都查：AO3 的 wrangler 会把 `implied X` 归到 `X`，而归并规则
    # 是一条条人工确认加进 tag_synonyms 的（DESIGN-NOTES.md N-12）。新写的篇随时可能带来新的
    # implied 变体，**漏掉一个，canonical 统计就少算一篇**，而且悄无声息。
    # 这里只**报告**，绝不自动归并 —— 「看起来该并」和「确实是同一个」是两回事。
    pend = con.execute("""
        SELECT kind, name_literal, n_works FROM tags
         WHERE LOWER(name_literal) LIKE 'implied %'
           AND name_literal = name_canonical
         ORDER BY kind, n_works DESC""").fetchall()
    if pend:
        print(f"\n{'=' * 62}")
        print(f"还没有归并规则的 `implied` 标签：{len(pend)} 个")
        print("（**脚本不会自动归并**。下面只是候选，需要人确认后写进 tag_synonyms）")
        strong = []
        for r in pend:
            base = r["name_literal"][len("implied "):].strip()
            hit = con.execute(
                "SELECT n_works FROM tags WHERE kind=? AND name_literal=?",
                (r["kind"], base)).fetchone()
            mark = ""
            if hit:
                mark = f"  ← 去掉 implied 后的标签**库里有**（{hit[0]} 篇），归并候选较强"
                strong.append((r["kind"], r["name_literal"], base, r["n_works"], hit[0]))
            print(f"  {r['kind']:<12} {r['n_works']:>3} 篇  {r['name_literal'][:50]}{mark}")
        if strong:
            print(f"\n  其中 {len(strong)} 个「去掉 implied 后库里确实有同名标签」——")
            print("  这与已确认的那 3 条是同一个模式，**建议优先人工确认这几个**：")
            for k, lit, base, n1, n2 in strong:
                print(f"    {k}｜{lit[:44]}（{n1} 篇）→ {base[:44]}（{n2} 篇）")
        print("  其余的去掉 implied 后库里没有同名标签，归并与否**不影响任何统计数字**"
              "（没有兄弟可合并），优先级低。")

    if bad_json:
        print(f"\n⚠ 有 {bad_json} 处 JSON 解不出，已跳过（没有猜测填充）")

    print(f"\n共写入 work_tags {n_wt} 行、work_series {n_ws} 行。联网请求数：0")
    print("下一步跑 stats.py 出报表（按 CP / 按年 / 按系列 / 按语言）。")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
