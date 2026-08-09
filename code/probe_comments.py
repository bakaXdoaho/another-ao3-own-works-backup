# probe_comments.py —— 阶段 F 的前置探测：评论页到底长什么样
#
# 为什么要先探测：
#   **写这段时还没见过 AO3 的评论页长什么样。** 按本项目一路踩出来的规矩（见 DESIGN-NOTES.md 第九节：不报错的错误最难对付），
#   没实际请求过、没看过返回内容，就不该写解析器 —— 那样写出来的东西
#   要么静默出错，要么给出自信的错误答案。
#
# 而且已经有一条线索说明那个假设可能就是错的：
#   索引页 blurb 里，AO3 自己给评论的链接是
#       /works/10000006?show_comments=true&view_full_work=true#comments
#   而当初往白名单里塞的是
#       /works/{id}/comments
#   两者不是一回事。到底哪个能用、返回什么，只能实测。
#
# 本次还要特别看清一件事：
#   AO3 stats 页报的是「Comment **Threads**」（评论**串**数），索引页 blurb 报的是**评论条数**。
#   一串 = 主评论 + 全部回复（作者自己的回复也算进去）。两个数都对，只是口径不同。
#   → 所以探测时必须看清楚：页面上**能不能分辨主评论与回复、谁回复谁**。
#     分辨得出来，comments 表才建得对；分辨不出来，就得另想办法。
#
# 联网请求数：3（都挑了小作品，避免拉一个 15 万字的整本下来）
# 会写什么：data/probe_raw/ 下的原始响应
# 不会写什么：不碰数据库、不碰 data/ao3/、不改 AO3
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'probe_comments'

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import re
    import sqlite3
    import config
    from ao3_client import (
        AO3Client, save_raw, confirm, assert_logged_in, SessionLost,
        is_forced_logout, looks_like_rate_limit, UrlNotAllowed,
    )
except Exception as _import_error:
    session_log.crash_dump("probe_comments", _import_error)
    raise


# 挑《某篇四章的短作》：4 章、11 条评论、篇幅不大。
# 特意**不用**某篇二十多章的长文（25 章 / 15 万字 / 109 条）—— 那会拉下来一个巨大的页面，
# 对 AO3 不客气，对探测也没额外好处。
SAMPLE_WORK = 10000003


def describe(html: str, expected_comments: int = 0) -> None:
    """只陈述看到了什么，不下结论。"""
    print(f"    长度 {len(html):,} 字符")
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    print(f"    <title> = {title.group(1).strip() if title else '（无）'}")

    if is_forced_logout(html):
        print("    ⚠ 这是 Lost Cookie / Forced Logout 页 —— cookie 不全")
        return
    if looks_like_rate_limit(html):
        print("    ⚠ 疑似限流页 —— 等几分钟再试")
        return

    # 一组**候选**结构特征。命中与否都只是观察，不据此断言。
    probes = {
        "评论区容器 id=comments":      r'id="comments"',
        "单条评论 li.comment":          r'<li[^>]*class="[^"]*comment[^"]*"',
        "comment_id 形式 id=comment_": r'id="comment_(\d+)"',
        "评论作者 a[rel=author]":       r'<a rel="author"',
        "评论时间 span.posted":         r'class="[^"]*posted[^"]*"',
        "评论正文 blockquote.userstuff": r'<blockquote class="userstuff"',
        "章节归属 chapter":             r'(?i)on chapter|chapters/(\d+)',
        "分页 next":                    r'(?i)>\s*next\s*(?:→|&#8594;|»)?\s*<',
        "「Comments」小标题":            r'(?i)<h\d[^>]*>\s*Comments',
        # ---- thread / 回复结构----
        "thread 容器 class=thread":     r'class="[^"]*\bthread\b[^"]*"',
        "回复嵌套 ul.thread":            r'<ul[^>]*class="[^"]*thread[^"]*"',
        "「Reply」链接":                 r'(?i)>\s*Reply\s*<',
        "回复层级 role=group":           r'role="group"',
        "自己的回复（用户名出现）":        rf'>\s*{re.escape(config.AO3_USERNAME)}\s*<',
    }
    print("    结构特征：")
    for name, pat in probes.items():
        n = len(re.findall(pat, html))
        print(f"      {'✓' if n else '·'} {name:<30} {n if n else ''}")

    ids = sorted(set(re.findall(r'id="comment_(\d+)"', html)))
    if ids:
        print(f"    找到 {len(ids)} 个 comment_id，前几个：{ids[:6]}")
    ch = sorted(set(re.findall(r'/works/\d+/chapters/(\d+)', html)))
    if ch:
        print(f"    页面里出现的 chapter_id：{len(ch)} 个 {ch[:6]}")

    # ---- thread vs comment：本次探测最要紧的一件事 ----
    # AO3 stats 页报的是 **Comment Threads**（串数），索引页 blurb 报的是
    # **评论条数**。一串 = 主评论 + 全部回复（含作者自己的回复）。
    # 两个数都对，只是口径不同（与DESIGN-NOTES.md N-12 的 canonical/字面标签同类）。
    # 所以这里要看清楚：**页面上能不能分辨「哪条是主评论、哪条是回复、谁回复谁」**。
    # 分辨得出来，comments 表才建得对；分辨不出来，就得另想办法。
    print("\n    ── thread / 回复结构（本次探测的重点）──")
    print(f"      本作品 blurb 报的评论条数：{expected_comments}")
    print(f"      页面里的 comment_id 个数：{len(ids)}")
    if ids and expected_comments:
        if len(ids) == expected_comments:
            print("      → 数量相等：页面列出的应该就是**全部评论条目**（含回复）")
        elif len(ids) < expected_comments:
            print("      → 页面比 blurb 少：可能有分页，或回复默认折叠")
        else:
            print("      → 页面比 blurb 多：可能把别的东西也算成 comment_id 了")

    # 缩进/嵌套深度是判断「回复」的关键线索之一
    depths = re.findall(r'<li[^>]*id="comment_\d+"[^>]*class="([^"]*)"', html)
    if depths:
        import collections
        print(f"      评论 <li> 的 class 取值分布：")
        for c, n in collections.Counter(depths).most_common(8):
            print(f"        {n:>3} × {c}")
        print("      （若不同层级的 class 不同，就能据此判断主评论 vs 回复）")


def main() -> int:
    session_log.start("probe_comments")
    config.ensure_dirs()

    try:
        cookies = config.load_cookies()
    except config.CookieMissing as e:
        print("\n✗ " + str(e))
        return 1
    print(config.describe_cookies(cookies))

    con = sqlite3.connect(config.INDEX_DB)
    row = con.execute(
        "SELECT title, comments, chapters_done FROM works_index WHERE work_id=?",
        (SAMPLE_WORK,)).fetchone()
    con.close()
    print(f"\n样本作品 {SAMPLE_WORK}：{row[0]}｜{row[1]} 条评论｜{row[2]} 章")

    # 三种候选 URL 形式，都试一遍，看哪个真能拿到评论
    cases = [
        ("A. AO3 自己在 blurb 里用的形式",
         f"/works/{SAMPLE_WORK}", {"show_comments": "true", "view_full_work": "true"}),
        ("B. 当初塞进白名单的猜测",
         f"/works/{SAMPLE_WORK}/comments", None),
        ("C. 单章形式（评论是按章挂的，所以也试试）",
         None, None),          # 运行时再填 chapter_id
    ]

    confirm(
        plan_lines=[
            f"对样本作品 {SAMPLE_WORK}（{row[1]} 条评论、{row[2]} 章）试三种 URL 形式",
            "每种都把原始响应存进 data/probe_raw/",
            "只**陈述**看到的结构特征，不下结论、不写解析器",
            "不碰数据库，不改 AO3",
        ],
        request_count=3,
    )

    client = AO3Client(cookies=cookies, delay=config.REQUEST_DELAY_SEC)

    # 取第 1 章的 chapter_id 填进 C
    con = sqlite3.connect(config.INDEX_DB)
    cid = con.execute(
        "SELECT chapter_id FROM chapters WHERE work_id=? AND idx=1", (SAMPLE_WORK,)
    ).fetchone()[0]
    con.close()
    cases[2] = ("C. 单章形式（评论是按章挂的，所以也试试）",
                f"/works/{SAMPLE_WORK}/chapters/{cid}",
                {"show_comments": "true"})

    for label, path, params in cases:
        print(f"\n--- {label}")
        print(f"    路径：{path}  参数：{params or '无'}")
        try:
            html = client.get(path, params=params)
        except UrlNotAllowed as e:
            print(f"    ✗ 被 URL 白名单拒绝：{e}")
            print("      （这本身就是一条发现：说明白名单需要调整）")
            continue
        except Exception as e:
            print(f"    ✗ 请求失败：{e}")
            continue
        raw = save_raw(html, f"probe_comments_{label[0]}")
        print(f"    已存档：{raw.name}")
        describe(html, row[1])

    print("\n" + "=" * 62)
    print("探测结束。**没有写入任何数据，也没有写解析器。**")
    print("把上面的输出（以及 data/probe_raw/ 里那三个文件）留好，")
    print("解析器要照实际结构写 —— 而不是照假设写。")
    print(f"\n共发出 {client.request_count} 次请求。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
