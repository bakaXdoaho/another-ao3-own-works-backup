# tag_canon.py —— 标签同义归并（canonical）：机器能证明的自动定案，证不了的列给人看
#
# 背景（DESIGN-NOTES.md N-12）：
#   AO3 侧边栏按 **canonical** 标签计数（`implied X` 已归并进 `X`），
#   而 blurb 里存的是**字面**标签。所以「库罗德/尤里斯有多少篇」
#   —— AO3 说 172，按字面数是 161。
#
# 用户方针（已确认）：
#   * 字面标签**永不改动**（`implied X` 本身有意义，原始数据不动）
#   * 但**默认统计口径跟随 AO3 的 canonical**：作者本人也把 `implied X` 视作 X 的一部分
#   → 所以：两套口径都留，统计时默认用 canonical，需要时可下钻到字面
#
# 怎么定案（关键：**不靠模式猜**）：
#   ⚠️ 「去掉 implied 前缀」这条规则在 relationship 上成立，在 freeform 上会算错 ——
#      `Implied Sexual Content` 是 AO3 **自己的 canonical 标签**（tag_id=120701，19 篇），
#      不是 `Sexual Content` 的同义词。按模式硬归并就会把统计做坏。
#   所以只认**算术证据**：
#      canonical 计数 − 字面计数 = 差额；若某个变体的篇数恰好等于差额 → 证据成立，自动定案
#      否则一律 candidate，写进报告等人拍板
#
# 联网请求数：**0**（只读 data/index.sqlite 与已存的原始页）
# 会写什么：data/index.sqlite 的 tag_synonyms 表；data/reports/…_tagcanon.md
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'tag_canon'

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import collections
    import json
    import re
    import sqlite3
    from datetime import datetime
    import config
except Exception as _import_error:
    session_log.crash_dump("tag_canon", _import_error)
    raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_synonyms (
    kind        TEXT,     -- relationship / character / freeform / fandom / warning
    literal     TEXT,     -- 作者写下的字面标签（**原样保留，永不改动**）
    canonical   TEXT,     -- 归并到哪个 canonical 标签
    evidence    TEXT,     -- arithmetic / manual
    status      TEXT,     -- confirmed / candidate / rejected
    note        TEXT,
    decided_at  TEXT,
    PRIMARY KEY (kind, literal)
);
"""

# blurb 列 → 标签种类
KIND_COLS = {
    "relationships_json": "relationship",
    "characters_json": "character",
    "freeforms_json": "freeform",
    "fandoms_json": "fandom",
    "warnings_json": "warning",
}
# facets 表的 kind → 标签种类
FACET_KIND = {
    "relationship_ids": "relationship", "character_ids": "character",
    "freeform_ids": "freeform", "fandom_ids": "fandom",
    "archive_warning_ids": "warning",
}

_PREFIX = re.compile(r"^(implied|Implied|IMPLIED)\s+")
_SUFFIX = re.compile(r"\s*-\s*Relationship$", re.I)


def _base(tag: str) -> str:
    return _SUFFIX.sub("", _PREFIX.sub("", tag)).strip()


def main() -> int:
    session_log.start("tag_canon")
    config.ensure_dirs()

    if not config.INDEX_DB.exists():
        print("✗ 还没有 data/index.sqlite。先跑 fetch_index.py。")
        return 1

    con = sqlite3.connect(config.INDEX_DB)
    con.executescript(SCHEMA)

    has_facets = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='facets'"
    ).fetchone()[0]
    if not has_facets or not con.execute("SELECT COUNT(*) FROM facets").fetchone()[0]:
        print("✗ facets 表是空的 —— 先跑一次 rebuild_index.py 把侧边栏 canonical 标签写进去。")
        return 1

    report: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        report.append(line)

    say(f"# 标签 canonical 归并报告 · {datetime.now():%Y-%m-%d %H:%M}")
    say("")
    say("**不联网。** 只用本地已有的 blurb 字面标签 + 侧边栏 canonical 计数。")
    say("")
    say("口径：**字面标签永不改动**；统计默认走 canonical（作者本人也把 `implied X` 算作 X）。")
    say("")

    # ---- 字面标签计数 ----
    literal: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    cols = list(KIND_COLS)
    for row in con.execute(f"SELECT {','.join(cols)} FROM works_index"):
        for col, val in zip(cols, row):
            for t in json.loads(val or "[]"):
                literal[KIND_COLS[col]][t] += 1

    # ---- 逐个 canonical 标签做算术验证 ----
    now = datetime.now().isoformat(timespec="seconds")
    confirmed, candidates, clean = [], [], 0

    for fkind, tag_id, name, canon_count in con.execute(
        "SELECT kind, tag_id, name, count FROM facets ORDER BY count DESC"
    ):
        kind = FACET_KIND.get(fkind)
        if not kind:
            continue
        counts = literal[kind]
        mine = counts.get(name, 0)
        gap = canon_count - mine
        if gap == 0:
            clean += 1
            continue

        # 候选：去掉 implied 前缀后与 canonical 同名的字面标签
        cands = {t: c for t, c in counts.items()
                 if t != name and _base(t) == name}
        exact = [t for t, c in cands.items() if c == gap]

        if len(exact) == 1:
            confirmed.append((kind, exact[0], name, gap, canon_count, mine))
            con.execute(
                "INSERT OR REPLACE INTO tag_synonyms "
                "(kind, literal, canonical, evidence, status, note, decided_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (kind, exact[0], name, "arithmetic", "confirmed",
                 f"canonical {canon_count} − 字面 {mine} = {gap}，与该变体篇数完全相等", now),
            )
        else:
            candidates.append((kind, name, canon_count, mine, gap, dict(cands)))
            for t, c in cands.items():
                con.execute(
                    "INSERT OR IGNORE INTO tag_synonyms "
                    "(kind, literal, canonical, evidence, status, note, decided_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (kind, t, name, "arithmetic", "candidate",
                     f"差额 {gap}，本变体 {c} 篇，对不上 → 待人工确认", now),
                )
    con.commit()

    # ---- 报告 ----
    say(f"侧边栏 canonical 标签共 {clean + len(confirmed) + len(candidates)} 个，"
        f"其中 **{clean}** 个字面与 canonical 完全一致（无需归并）。")
    say("")

    if confirmed:
        say("## 已自动定案（算术证据成立）")
        say("")
        say("| 种类 | 字面标签 | → canonical | 篇数 | AO3 | 字面 |")
        say("|---|---|---|---|---|---|")
        for kind, lit, canon, gap, cc, mine in confirmed:
            say(f"| {kind} | `{lit[:46]}` | `{canon[:40]}` | {gap} | {cc} | {mine} |")
        say("")

    if candidates:
        say("## 需要人工确认")
        say("")
        say("差额存在但对不上任何单一变体。**没有自动归并**，等你拍板。")
        say("")
        for kind, name, cc, mine, gap, cands in candidates:
            say(f"### {kind}｜`{name}`")
            say(f"- AO3 canonical **{cc}**，本地字面 **{mine}**，差 **{gap}**")
            if cands:
                for t, c in cands.items():
                    say(f"- 候选变体：`{t}`（{c} 篇）")
            else:
                say("- **找不到任何候选变体** —— 可能是别的同义写法，或 AO3 侧的归并规则更复杂")
            say("")

    # ---- 归并后的对照表 ----
    say("## 归并后 vs AO3（relationship）")
    say("")
    syn = {(k, l): c for k, l, c in con.execute(
        "SELECT kind, literal, canonical FROM tag_synonyms WHERE status='confirmed'")}
    merged = collections.Counter()
    for t, c in literal["relationship"].items():
        merged[syn.get(("relationship", t), t)] += c
    say("| 标签 | AO3 | 归并后 | 差 |")
    say("|---|---|---|---|")
    for fkind, name, cc in con.execute(
        "SELECT kind, name, count FROM facets WHERE kind='relationship_ids' ORDER BY count DESC"
    ):
        m = merged.get(name, 0)
        mark = "" if m == cc else f" ← 仍差 {cc - m}"
        say(f"| {name[:44]} | {cc} | {m} | {cc - m:+d}{mark} |")
    say("")

    say("---")
    say("**字面标签一个字都没有改动。** 归并只体现在 `tag_synonyms` 表里，")
    say("统计时按需套用；随时可以关掉这层，回到纯字面口径。")

    # 不再写 reports/ 文件：内容与运行记录完全重复。
    con.close()
    print("\n（不再单独写报告文件 —— 完整输出已存进运行记录。）")
    print("联网请求数：0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
