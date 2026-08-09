# check_login.py —— Step 2：最安全的第一次联网
#
# 干什么：取一次 AO3 首页，判断 cookie 有没有生效，并**顺便把「登录标记长什么样」这件事查清楚**
#         （DESIGN-NOTES.md N-22 需要这个标记来做运行期会话守卫）。
#
# 联网请求数：1
# 会写什么：data/probe_raw/ 下一个带时间戳的 html 文件（原始响应，留档）
# 不会写什么：不碰 data/ao3/、不碰数据库、不改 AO3 上任何东西
#
# 怎么跑：PyCharm 里打开本文件 → 右键 → Run 'check_login'
#         （或终端：cd <你放这个项目的目录>/code && python3 check_login.py）

from __future__ import annotations

import sys

import session_log          # 只依赖标准库，必须最先 import

try:
    import re
    import config
    from ao3_client import (
        AO3Client, save_raw, looks_like_rate_limit, confirm,
        assert_logged_in, SessionLost,
    )
except Exception as _import_error:      # 导入阶段就崩的话，也要留一份日志
    session_log.crash_dump("check_login", _import_error)
    raise


# 注意：session_log.start() 放在 main() 里，不放模块顶层——
# 否则「只是 import 一下做测试」也会凭空生出一份运行记录。


# 候选的「已登录」标记。AO3 改版时这些可能变，所以**同时查一组**，
# 并把每一条的命中情况都打印出来，由人确认哪一条最可靠 —— 而不是写死一条然后静默失败。
LOGGED_IN_MARKERS = {
    "greeting 区块 (id=greeting)":      re.compile(r'id="greeting"'),
    "登出链接 (/users/logout)":          re.compile(r'/users/logout'),
    f"用户名链接 (/users/{config.AO3_USERNAME})":
        re.compile(rf'/users/{re.escape(config.AO3_USERNAME)}\b'),
    "「My Dashboard」字样":              re.compile(r'My Dashboard', re.I),
}

LOGGED_OUT_MARKERS = {
    "登录表单 (user_session)":           re.compile(r'user_session'),
    "「Log In」按钮":                     re.compile(r'>\s*Log In\s*<', re.I),
}


def main() -> int:
    session_log.start("check_login")
    config.ensure_dirs()

    # ---- 1. 先读 cookie。读不出来就直接给可操作的报错，不联网 ----
    print("[1/4] 检查 cookie 文件 …")
    try:
        cookies = config.load_cookies()
    except config.CookieMissing as e:
        print("\n✗ " + str(e))
        return 1
    print(config.describe_cookies(cookies))

    # ---- 2. 说明计划，等确认 ----
    confirm(
        plan_lines=[
            f"取一次 {config.AO3_BASE}/ 首页（带登录 cookie）",
            "把原始响应存进 data/probe_raw/",
            "检查一组「已登录 / 未登录」标记，打印命中情况",
        ],
        request_count=1,
    )

    # ---- 3. 发请求 ----
    print("\n[2/4] 请求中 …")
    client = AO3Client(cookies=cookies)
    try:
        html = client.get("/")
    except Exception as e:
        print(f"\n✗ 请求失败：{e}")
        print("  没有写入任何东西。可以稍后直接重跑本脚本。")
        return 1

    # ---- 4. 先落盘，再解析（约定 3）----
    raw_path = save_raw(html, "check_login_home")
    print(f"\n[3/4] 原始响应已存档：")
    print(f"      {raw_path}")
    print(f"      大小：{len(html):,} 字符")

    # ---- 5. 判定 ----
    print("\n[4/4] 检查登录标记 …\n")

    if looks_like_rate_limit(html):
        print("  ⚠ 这个响应看起来像 AO3 的限流页（很短 + 含 retry later 之类字样）。")
        print("    这不是故障，是正常现象。等几分钟再跑一次即可。")
        print(f"    响应已存档，可以打开上面那个文件看看它到底长什么样：")
        print(f"    open '{raw_path}'")
        return 2

    hits_in, hits_out = [], []

    print("  「已登录」候选标记：")
    for name, pat in LOGGED_IN_MARKERS.items():
        ok = bool(pat.search(html))
        print(f"    {'✓' if ok else '·'} {name}")
        if ok:
            hits_in.append(name)

    print("\n  「未登录」候选标记：")
    for name, pat in LOGGED_OUT_MARKERS.items():
        ok = bool(pat.search(html))
        print(f"    {'✓' if ok else '·'} {name}")
        if ok:
            hits_out.append(name)

    # 顺便实跑一次正式的会话守卫（DESIGN-NOTES.md N-22），确认它在真实页面上判定正确
    print("\n  正式会话守卫（DESIGN-NOTES.md N-22，后续所有脚本都用它）：")
    try:
        assert_logged_in(html, where="首页")
        print(f"    ✓ 通过 —— 找到 {'Hi, ' + config.AO3_USERNAME + '!'!r}")
        guard_ok = True
    except SessionLost as e:
        print(f"    ✗ 未通过：{e}")
        guard_ok = False

    print("\n" + "=" * 60)
    if hits_in and not hits_out and guard_ok:
        print(f"✓ 判定：已登录为 {config.AO3_USERNAME}"
              f"（命中 {len(hits_in)} 个已登录标记，0 个未登录标记，会话守卫通过）")
        rc = 0
    elif hits_out and not hits_in:
        print("✗ 判定：未登录 —— cookie 没生效或已过期。")
        print(config._FIX_HINT)
        rc = 1
    elif not hits_in and not hits_out:
        print("? 判定：两组标记都没命中 —— 说明 AO3 页面结构与预期不同。")
        print("  这不代表出错了，只说明这组标记的猜测不对。")
        print(f"  留着这个文件，它是排查的依据：{raw_path}")
        rc = 3
    else:
        print("? 判定：两组标记都有命中，需要人看一眼。")
        print(f"  留着这个文件，它是排查的依据：{raw_path}")
        rc = 3
    print("=" * 60)
    print(f"\n本次共发出 {client.request_count} 次请求。未修改 AO3 上任何内容。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
