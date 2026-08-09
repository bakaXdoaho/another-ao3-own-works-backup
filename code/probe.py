# probe.py —— Step 3：用 5 次请求关掉几个「不验就得瞎猜」的问题
#
# 本次要验证的（都是拿一两个请求就能验掉的）：
#   Q1  下载 URL 里的 {slug} 到底管不管用？（能不能随便构造文件名）
#   Q2  ?updated_at= 参数是缓存键还是被忽略？
#   Q3  作品索引页的 blurb 里到底有哪些字段？特别是：有没有 gift？summary 会不会被截断？
#   Q4  /navigate 的分章日期格式是否如预期
#   Q5（免费）  脚本抓下来的下载件，与浏览器手动下的那份是否逐字节一致
#
# 联网请求数：5（3 次 downloads + 1 次 navigate + 1 次索引页）
# 会写什么：data/probe_raw/ 下的原始响应；data/reports/ 下一份 markdown 报告
# 不会写什么：不碰 data/ao3/、不碰数据库、不改 AO3 上任何东西
#
# 怎么跑：PyCharm 打开本文件 → 右键 → Run 'probe'

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import hashlib
    import re
    from datetime import datetime
    import config
    from ao3_client import (
        AO3Client, save_raw, looks_like_rate_limit, confirm,
        assert_logged_in, SessionLost, is_forced_logout,
    )
except Exception as _import_error:      # 导入阶段就崩的话，也要留一份日志
    session_log.crash_dump("probe", _import_error)
    raise


# 注意：session_log.start() 放在 main() 里，不放模块顶层——
# 否则「只是 import 一下做测试」也会凭空生出一份运行记录。

# 样本文：某篇五章的作品，5 章，本地已有浏览器手动下载的副本可对照
SAMPLE_WORK_ID = 10000001
SAMPLE_SLUG = "Rhododendron_molle.html"
BOGUS_SLUG = "zzz_this_slug_is_wrong.html"
BOGUS_UPDATED_AT = "1234567890"

REFERENCE_COPY = (
    config.MAIN_DIR.parent / "more_notes" / "example_ao3_html" / "Rhododendron_molle.html"
)

_report: list[str] = []


def say(line: str = "") -> None:
    """既打印到屏幕，也收进报告。"""
    print(line)
    _report.append(line)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def looks_like_a_work(html: str, work_id: int) -> tuple[bool, str]:
    """fail closed：只接受确实像一篇作品的下载件。"""
    if len(html) < 2000:
        return False, f"太短（{len(html)} 字符）"
    if str(work_id) not in html:
        return False, "正文里找不到 work id"
    if "</html>" not in html.lower():
        return False, "没有 </html> 结尾，可能被截断"
    if "Posted originally on the" not in html:
        return False, "找不到官方下载件的 preface 特征句"
    return True, "OK"


# ====================================================================== Q1/Q2
def probe_downloads(client: AO3Client) -> dict[str, str]:
    say("\n## Q1 / Q2 · 下载 URL 的 slug 与 ?updated_at= 参数")
    say("")
    say("做法：对同一篇作品用三种 URL 各取一次，比对内容哈希。")
    say("三份完全相同 → 说明 slug 与 updated_at 都被忽略，可以自由构造下载 URL。")
    say("")

    cases = [
        ("正确 slug，无参数", f"/downloads/{SAMPLE_WORK_ID}/{SAMPLE_SLUG}", None),
        ("错误 slug，无参数", f"/downloads/{SAMPLE_WORK_ID}/{BOGUS_SLUG}", None),
        ("正确 slug + 假的 updated_at",
         f"/downloads/{SAMPLE_WORK_ID}/{SAMPLE_SLUG}", {"updated_at": BOGUS_UPDATED_AT}),
    ]

    results: dict[str, str] = {}
    bodies: dict[str, str] = {}

    for label, path, params in cases:
        say(f"### {label}")
        try:
            html = client.get(path, params=params)
        except Exception as e:
            say(f"  ✗ 请求失败：{e}")
            say("")
            continue

        raw = save_raw(html, f"probe_dl_{label}")

        if is_forced_logout(html):
            say(f"  ✗ **AO3 返回 Lost Cookie / Forced Logout 页**（{len(html):,} 字符）")
            say(f"    已存档：`{raw.name}`")
            say("    → cookie 给得不全。请按操作手册 Step 1.1 重取**整条 Cookie 请求头**。")
            say("")
            continue

        if looks_like_rate_limit(html):
            say(f"  ⚠ 疑似限流页（{len(html)} 字符）。已存档：`{raw.name}`")
            say("    这不是故障。等几分钟重跑本脚本即可。")
            say("")
            continue

        ok, why = looks_like_a_work(html, SAMPLE_WORK_ID)
        h = sha(html)
        results[label] = h if ok else f"INVALID({why})"
        if ok:
            bodies[label] = html

        say(f"  长度 {len(html):,} 字符｜哈希 `{h}`｜校验 {'✓ ' + why if ok else '✗ ' + why}")
        say(f"  已存档：`{raw.name}`")
        say("")

    # 结论
    say("### 结论")
    valid = {k: v for k, v in results.items() if not v.startswith("INVALID")}
    if len(valid) < 2:
        say("  ⚠ 有效响应不足 2 份，无法比对。请看上面的失败原因。")
    elif len(set(valid.values())) == 1:
        say("  ✓ **三份内容完全一致** → slug 被忽略、`updated_at` 也被忽略。")
        say("    → 下载 URL 可以直接构造，不需要先去 work 页抓真实链接。**省掉每篇一次请求。**")
    else:
        say("  ⚠ **内容不一致**，逐项列出：")
        for k, v in results.items():
            say(f"    · {k}：`{v}`")
        say("    → 说明至少有一个参数是有意义的。抓取器必须用真实链接，不能自己拼。")
    say("")

    return bodies


# ====================================================================== Q5
def compare_with_reference(bodies: dict[str, str]) -> None:
    say("\n## Q5 · 与浏览器手动下载的那份对照（本地比对，不联网）")
    say("")
    key = "正确 slug，无参数"
    if key not in bodies:
        say("  （跳过：上面没有取到有效的下载件）")
        say("")
        return
    if not REFERENCE_COPY.exists():
        say(f"  （跳过：找不到参照文件 {REFERENCE_COPY}）")
        say("")
        return

    ref = REFERENCE_COPY.read_text(encoding="utf-8", errors="ignore")
    got = bodies[key]

    say(f"  参照文件：`{REFERENCE_COPY.name}`（{len(ref):,} 字符）")
    say(f"  本次抓取：{len(got):,} 字符")

    if sha(ref) == sha(got):
        say("  ✓ **逐字节一致** —— 脚本抓的与手动下的是同一个东西。")
    else:
        say("  · 不完全一致（正常，作品可能在两次下载之间被改过，或含时间戳）。")
        say(f"    长度差：{len(got) - len(ref):+,} 字符")
        for field in ("Words:", "Chapters:", "Published:", "Completed:"):
            a = re.search(field + r"\s*([^\n<]*)", ref)
            b = re.search(field + r"\s*([^\n<]*)", got)
            sa_ = a.group(1).strip() if a else "—"
            sb_ = b.group(1).strip() if b else "—"
            mark = "=" if sa_ == sb_ else "≠"
            say(f"    {field:<12} 参照 `{sa_}` {mark} 本次 `{sb_}`")
    say("")


# ====================================================================== Q4
def probe_navigate(client: AO3Client) -> None:
    say("\n## Q4 · /navigate 的分章日期")
    say("")
    try:
        html = client.get(f"/works/{SAMPLE_WORK_ID}/navigate")
    except Exception as e:
        say(f"  ✗ 请求失败：{e}")
        say("")
        return

    raw = save_raw(html, "probe_navigate")
    say(f"  长度 {len(html):,} 字符｜已存档：`{raw.name}`")

    if looks_like_rate_limit(html):
        say("  ⚠ 疑似限流页。等几分钟重跑。")
        say("")
        return

    # 期望形如： <a href="/works/ID/chapters/CID">1. 标题</a> (2022-12-28)
    # 实测结构（20260804）：
    #   <li><a href="/works/10000001/chapters/20000001">1. Rhododendron</a>
    #       <span class="datetime">(2022-12-28)</span></li>
    # 第一版正则漏了 <span class="datetime"> 这层包裹，才会解析出 0 章。
    rows = re.findall(
        r'<a href="/works/\d+/chapters/(\d+)">(.*?)</a>\s*'
        r'<span class="datetime">\((\d{4}-\d{2}-\d{2})\)</span>',
        html, re.S,
    )
    say(f"  用预期正则解析出 **{len(rows)} 章**")
    if rows:
        say("")
        say("  | # | chapter_id | 标题 | 发布日期 |")
        say("  |---|---|---|---|")
        for i, (cid, title, date) in enumerate(rows, 1):
            clean = re.sub(r"<[^>]+>", "", title).strip()
            say(f"  | {i} | {cid} | {clean[:40]} | {date} |")
        dates = [d for _, _, d in rows]
        say("")
        say(f"  日期单调递增？{'是' if dates == sorted(dates) else '**否** —— 与DESIGN-NOTES.md N-10 预期一致，排序一律按章号'}")
    else:
        say("  ⚠ 预期正则没匹配到东西 —— 页面结构与假设不同。")
        say(f"    留着 `{raw.name}`，它是排查的依据。")
    say("")


# ====================================================================== Q3
# 20260804 修正：第一版这里用的是宽松关键词，结果被导航栏里的 "Gifts"、"Collections"
# 之类的链接刷出一堆假阳性（gift 报 13 处、Collections 报 13 处，实际 blurb 里
# 一个 gift 都没有）。现在全部换成实测确认过的**结构性**特征。
BLURB_CHECKS = {
    "blurb 本体":              r'class="own work blurb group work-(\d+)',
    "updated_at 注释":         r"<!-- updated_at=(\d+) -->",
    "gift（赠文）行":           r'<li class="gift">',
    "Series「Part N of」":     r"Part <strong>\d+</strong> of",
    "Collections 行":          r'<dd class="collections">',
    "Comments 计数":           r'<dd class="comments">',
    "Kudos 计数":              r'<dd class="kudos">',
    "Hits 计数":               r'<dd class="hits">',
    "Bookmarks 计数":          r'<dd class="bookmarks">',
    "Words / Chapters":        r'<dd class="words">',
    "发布日期 <p class=datetime>": r'<p class="datetime">',
    "summary 区块":            r'<blockquote class="userstuff summary">',
    "标签组 (tags commas)":    r'class="tags commas"',
    "「(…)」省略号截断标记":    r"…\s*</p>|\.\.\.\s*</p>",
}


def probe_index(client: AO3Client) -> None:
    say("\n## Q3 · 作品索引页 blurb 含哪些字段")
    say("")
    try:
        html = client.get(f"/users/{config.AO3_USERNAME}/works", params={"page": 1})
    except Exception as e:
        say(f"  ✗ 请求失败：{e}")
        say("")
        return

    raw = save_raw(html, "probe_index_p1")
    say(f"  长度 {len(html):,} 字符｜已存档：`{raw.name}`")

    if looks_like_rate_limit(html):
        say("  ⚠ 疑似限流页。等几分钟重跑。")
        say("")
        return

    # 会话守卫：索引页是每次运行都要抓的页面，正是DESIGN-NOTES.md N-22 守卫的落点
    try:
        assert_logged_in(html, "作品索引页")
        say("  ✓ 会话守卫通过（登录态有效）")
    except SessionLost as e:
        say(f"  ✗ 会话守卫未通过：{e}")
        say("")
        return

    # 总篇数与分页
    m = re.search(r"(\d+)\s+Works? by", html) or re.search(r"<h2[^>]*>\s*([\d,]+)\s+Works", html)
    if m:
        say(f"  · 页面自报作品总数：**{m.group(1)}**")
    else:
        say("  · 没能自动读出作品总数（不影响，人工看一眼页面顶部即可）")

    pages = re.findall(r'href="[^"]*[?&]page=(\d+)', html)
    if pages:
        say(f"  · 分页：最大页码 **{max(int(p) for p in pages)}**")

    blurbs = re.findall(r'class="[^"]*work blurb group', html)
    say(f"  · 本页 blurb 数：**{len(blurbs)}**")
    say("")

    say("  blurb 字段探测：")
    say("")
    say("  | 字段 | 命中 |")
    say("  |---|---|")
    for name, pat in BLURB_CHECKS.items():
        hits = len(re.findall(pat, html))
        say(f"  | {name} | {'✓ ' + str(hits) + ' 处' if hits else '· 未见'} |")
    say("")

    # summary 截断：取本页最长的 summary，与已知的完整 summary 比长度
    sums = re.findall(r'class="userstuff summary">(.*?)</blockquote>', html, re.S)
    if sums:
        lens = sorted(len(re.sub(r"<[^>]+>", "", s).strip()) for s in sums)
        say(f"  · 本页 summary 共 {len(sums)} 条，字符数 最短 {lens[0]} / 中位 {lens[len(lens)//2]} / 最长 {lens[-1]}")
        say("    → 若最长值卡在一个整齐的数字附近（如 250），就是被截断了；")
        say("      据此判定 blurb 里的 summary 有没有被截断。")
    say("")


# ====================================================================== main
def main() -> int:
    session_log.start("probe")
    config.ensure_dirs()

    print("[1/2] 检查 cookie …")
    try:
        cookies = config.load_cookies()
    except config.CookieMissing as e:
        print("\n✗ " + str(e))
        return 1
    print(config.describe_cookies(cookies))

    confirm(
        plan_lines=[
            f"下载件 ×3（样本文 {SAMPLE_WORK_ID}：正确 slug / 错误 slug / 带假 updated_at）",
            f"/works/{SAMPLE_WORK_ID}/navigate ×1",
            f"/users/{config.AO3_USERNAME}/works?page=1 ×1",
            "所有响应原样存进 data/probe_raw/，并写一份报告到 data/reports/",
            "不写入 data/ao3/，不碰数据库",
        ],
        request_count=5,
    )

    say(f"# Probe 报告 · {datetime.now():%Y-%m-%d %H:%M}")
    say("")
    say(f"样本作品：`{SAMPLE_WORK_ID}`（某篇五章的作品）")

    client = AO3Client(cookies=cookies, delay=config.DOWNLOAD_DELAY_SEC)

    bodies = probe_downloads(client)
    compare_with_reference(bodies)
    probe_navigate(client)
    probe_index(client)

    say("---")
    say(f"共发出 {client.request_count} 次请求。未修改 AO3 上任何内容。")

    # 20260805 起不再写 reports/ 文件：报告内容与运行记录完全重复
    # （say() 同时往屏幕和报告里写），留两份没有意义。
    # 完整过程见 code/session_printouts/ 里的运行记录。
    print("\n（不再单独写报告文件 —— 上面的完整输出已存进运行记录。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
