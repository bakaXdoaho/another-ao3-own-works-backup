# another-ao3-own-works-backup

Claude Opus 5

Yet another tool for backing up **your own** AO3 works to your own machine. It logs in as
you (with a cookie you paste in yourself), downloads the official HTML export of each of
your works, and additionally saves the things the official export leaves out: per-chapter
publication dates, embedded images, comments with their reply threads, and the tag /
series / stats metadata from your works index. Everything lands as plain files on disk;
a SQLite database is built on top of them and can be rebuilt from those files at any time
with zero network requests.

```
├── code/
│   ├── config.py           all settings live here; every script runs with zero arguments
│   ├── ao3_client.py       the only module that touches the network — GET only, URL allowlist
│   ├── session_log.py      tees each run's full output to code/session_printouts/
│   ├── check_login.py      1 request — verify the cookie works
│   ├── probe.py            a few requests — confirm assumptions before any bulk run
│   ├── probe_comments.py   3 requests — inspect the comments markup before parsing it
│   ├── index_parser.py     parsing only, no network — works-list blurbs
│   ├── comments_parser.py  parsing only, no network — comments + reply nesting
│   ├── fetch_index.py      your works list → raw HTML + works_index table
│   ├── fetch_works.py      official HTML export + /navigate, per work, resumable
│   ├── fetch_assets.py     embedded images → local files (append-only pool)
│   ├── fetch_comments.py   comments + reply threads, per work that has any
│   ├── rebuild_index.py    0 requests — rebuild the whole DB from local raw files
│   ├── derive_chapters.py  0 requests — per-chapter word counts, summaries, notes
│   ├── derive_tags.py      0 requests — flatten tags/series into queryable tables
│   ├── tag_canon.py        0 requests — literal vs canonical tag reconciliation
│   └── stats.py            0 requests — reports: by pairing, by year, by series
├── docs/
│   └── DESIGN-NOTES.md     why each defensive check exists; code comments cite it as N-xx
│
│   ── everything below is created by the scripts; none of it ships with the repo ──
├── secrets/
│   └── ao3_cookie.txt      you create this — your own Cookie header, never committed
└── data/
    ├── ao3/
    │   ├── index_raw/      raw HTML of your works-list pages
    │   └── works/{id}/     ★ per work: official HTML export + /navigate + images
    ├── comments/{id}.html  the comments section, as served
    └── index.sqlite        derived; `rebuild_index.py` regenerates it from the files above
```

`data/ao3/works/` **is the archive**; the database is only a queryable layer on top of it.
`.gitignore` already excludes `secrets/` and the SQLite file.

Requires Python 3.8+ and `requests`; everything else is standard library. **It cannot be
used to scrape anyone else's works** — it only reaches what your own session can already
see, and there is no POST/PUT/DELETE anywhere in the codebase. Default throttling is
deliberately rather slow (15–20 s between requests, timed from the end of the previous response);
**please don't lower it** — AO3 is a nonprofit run by volunteers, and rate limits are shared
across connections. If you plan to change anything, read `docs/DESIGN-NOTES.md` first:
most of the odd-looking checks are there because a failure mode returned **HTTP 200 with a
normal-looking page**, and removing them turns a loud failure into a silent one.

---

# 使用说明

这是一套把**你自己在 AO3 上的作品**备份到自己电脑上的脚本。它用你自己粘贴进去的 cookie
以你的身份登录，下载每篇的官方 HTML 存档，并额外保存官方存档里没有的东西：分章发布日期、
正文内嵌的图片、评论及其回复层级、作品列表里的标签 / 系列 / 统计。所有东西都以普通文件落在
硬盘上；数据库只是在这些文件之上建起来的，**随时可以从原始文件重建，一次网络请求都不用**。

需要 Python 3.8 以上和 `requests`，其余都是标准库。**它没法用来抓别人的文** —— 它只能拿到
你自己登录后本来就看得见的东西。此外代码里根本没有 POST/PUT/DELETE。默认间隔较慢
（15–20 秒一次，从上一次响应结束开始算），**请不要调快**：AO3 是志愿者运营的非营利站点，
限流还是跨连接共享的。想改动之前请先看 `docs/DESIGN-NOTES.md` —— 那些看着或许多余的检查，
多半是因为某种失败**会返回 HTTP 200 加一个看起来完全正常的页面**，去掉它们可能把一个
会报错的问题变成一个不会报错的问题。

有问题可以把这份README和相关文件给AI，电脑比本repo的主人说得更清楚……｜有问题欢迎联系反馈！

## 你需要准备的

1. **Python**（3.8 以上）。macOS 一般自带；没有就去 python.org 装一个。
2. **PyCharm**（社区版免费）。装了它就可以用它打开脚本后**右键 → Run**，不用敲命令。
   不想装也行，用终端 `python3 脚本名.py` 一样。
3. 你的 **AO3 账号**，和一次**复制 cookie** 的操作（下面第 0 步）。
4. **（可选）git** —— 用来记录「这次跑完，比上次多了什么、少了什么」。
   完全可以先不管，跳过它一样能用；等你想知道「AO3 上哪篇被改过」的时候再回来看
   最后那一节《（可选）用 git 看出每次的变化》。

装好之后，把这个GitHub仓库下载下来（绿色 `Code` 按钮 → Download ZIP），解压。

## 跑完之后，东西在哪

脚本会在解压出来的文件夹里自己建出 `data/`（完整结构见开头那张目录树）。你主要会去看两个地方：

- **`data/ao3/works/{作品id}/`** —— **你的正本就在这。** 每篇一个文件夹，
  里面是 AO3 官方导出的 HTML，**直接双击就能用浏览器打开看** ——
  不需要这套脚本、不需要数据库、不需要装任何东西。
- **`code/session_printouts/`** —— 每次运行的完整输出，文件名带时间戳。
  想回头看「刚才到底做了什么」「那张字数表长什么样」，去这里翻。

> `data/index.sqlite` 只是在这些文件之上建起来的一层方便查询的东西。
> 删了不要紧（跑 `rebuild_index.py` 重建，**0 次请求**）；
> **`data/ao3/` 删了才是真的没了。**

## 跑之前先知道三件事

- **所有脚本都是零参数的**：打开、右键 Run，就完事。**要改行为去改 `config.py`**。
- **每个脚本联网前都会先打印计划、等你按回车**。看清楚再敲。
- **每次运行的完整输出都会自动存一份**到 `code/session_printouts/`，
  文件名带时间戳。跑完想回头看「刚才到底做了什么」，去那里翻。

---

## Step 0 · 放好 cookie

在 Chrome 里登录 AO3 → 按 `⌥⌘I` 打开开发者工具 → 切到 **Network** →
按 `⌘R` 刷新 → 找到左边最上面那条。目标的那行：
   - **Name 栏**是页面本身（在首页就是 `archiveofourown.org`，在作品列表页就是 **`works`**）
   - **Type 栏是 `document`**  ← **认这个最准**
   - Size 通常几十 kB（不是 `(memory cache)`）
   - ⚠️ 不要点 `sandbox.css`、`*.js`、`*.png` 这些——它们是页面的附属资源，不是页面本身
 
 → **在那一行上点右键 → Copy → Copy as cURL**

粘贴进 `secrets/ao3_cookie.txt` —— 这个文件夹和 txt 都要自己建，
位置在**解压出来的那个文件夹底下、和 `code/` 并排**：

```
本repo解压出来的文件夹或你备份的目标文件夹/
├── code/
├── docs/
└── secrets/            ← 自己建这个
    └── ao3_cookie.txt  ← 自己建这个，把刚才复制的整段粘进去
```

> 整段 cURL 命令直接粘进去就行，**不用挑里面的哪一部分**。
> （不放心的话可以看 `docs/DESIGN-NOTES.md` 的 N-02：
> 只贴一个 cookie 值会导致一种很难发现的失败。）

## Step 1 · `check_login.py` —— 确认 cookie 有效（1 次请求）

**输出例**

```
[1] GET https://archiveofourown.org/
  ✓ 判定：已登录为 YOUR_AO3_USERNAME（命中 4 个已登录标记，0 个未登录标记，会话守卫通过）
本次共发出 1 次请求。未修改 AO3 上任何内容。
```

看到「已登录为 你的用户名」就可以往下走。看到「未登录」就回 Step 0 重取。

## Step 2 · `probe.py` —— 小规模验证假设（几次请求）

它拿一两篇作品试一下，确认「下载 URL 能不能直接构造」「分章日期页长什么样」这类事。
**批量跑之前先验一遍**，比跑到一半发现假设错了要省事得多。

**输出例**

```
  [1] GET https://archiveofourown.org/downloads/12345678/Some_Title.html
  [2] GET https://archiveofourown.org/downloads/12345678/zzz_this_slug_is_wrong.html
  [3] GET https://archiveofourown.org/downloads/12345678/Some_Title.html?updated_at=1234567890
    ✓ **三份内容完全一致** → slug 被忽略、updated_at 也被忽略
    ✓ **逐字节一致** —— 脚本抓的与手动下的是同一个东西
  [5] GET https://archiveofourown.org/users/YOUR_AO3_USERNAME/works?page=1
    ✓ 会话守卫通过（登录态有效）
    · 页面自报作品总数：N
    · 分页：最大页码 N
```

> 这一步在验的是「能不能直接构造下载 URL」——**能，就省掉每篇一次的额外请求**。
> 它同时确认了脚本拿到的文件和你手动点「下载」得到的**逐字节相同**。

## Step 3 · `fetch_index.py` —— 抓作品列表

**输出例**

```
共解析出 N 篇作品（去重后）
  新增 N 篇｜有变动 0 篇｜从列表上消失 0 篇
侧边栏 canonical 标签：N 条
共发出 N 次请求。
```

跑完可以对一下：篇数和你 AO3 作品页显示的数字一样吗？**这是放开跑之前的闸。**

## Step 4 · `fetch_works.py` —— 抓正文（大头，可以中途停）

每篇两次请求（官方下载件 + 分章日期页）。**跑一半停下来是正常的，不是失败** ——
它会记住进度，重跑自动跳过已完成的；被限流或网络抖动还会自己等一会儿接着跑。

**输出例**

```
全库 N 篇｜已完成 N 篇｜待抓 N 篇
--- [1/N] 12345678 某篇作品
  [1] GET https://archiveofourown.org/downloads/12345678/12345678.html
    ✓ 下载件 45,231 字 / 5 章
    ✓ navigate 5 章
全库进度：**N / N** 篇已完整抓取。
```

## Step 5 · `fetch_assets.py` —— 把正文里的图片存下来

同人文里内嵌的图片常常挂在第三方图床上，**那些链接说没就没**。
这一步把它们下载到本地，而且**只增不减**：你日后把某张图从文里撤掉，本地那份也照样留着。

> ⚠️ **十有八九你要往图床白名单里加自己常用的站。**
> `fetch_assets.py` 顶上有个 `ALLOWED_HOSTS`，现在只放了几个常见图床
> （Twitter / Imgur / Tumblr / Discord / Poipiku），而且**只有 Twitter 是实测过的**。
>
> 不用提前研究：**先跑一遍**。脚本会把不在名单里的域名逐个报出来，
> 并直接告诉你「把 `xxx` 加进 `ALLOWED_HOSTS` 再重跑就行」。照做即可。
>
> 加域名是安全的 —— 那份名单只决定「允许去哪儿取图片字节」，
> 脚本仍然只发 GET，抓不到的会记状态而**不会当成功处理**。

## Step 6 · `fetch_comments.py` —— 抓评论（含回复层级）

**输出例**

```
--- [1/N] 12345678 某篇作品｜blurb 报 11 条
    ✓ 11 条 / 2 串｜自己的回复 6 条｜别人的 5 条｜最深 7 层｜1 页

全库评论
| 项 | 数 |
|---|---|
| 评论总条数（库） | N |
| 串（thread）数 | N |
| 其中自己的回复 | N |
| 访客（无账号）评论 | N |
```

> 「串」和「条」是两个数：一串 = 一条主评论 + 它下面所有回复。
> AO3 的 stats 页报的是**串**数，作品列表里的数字是**条**数。两个都对，别拿来互相对照。

## Step 7 · 三个**不联网**的脚本，随时可以重跑

到这里网络部分就结束了。下面三个都是 **0 次请求**，只读本地文件：

### `derive_chapters.py` —— 算分章字数、抽出各章的 summary / notes

**输出例**

```
章级信息（本次派生）
  有 Chapter Summary    N 章
  有 Chapter Notes      N 章
  正文内嵌图片合计      N 处
字数对账
  误差 ≤2%   N 篇｜误差 >2%  N 篇
  全库合计：本地 1,234,567｜AO3 1,234,890｜差 -323（0.03%）
```

### `derive_tags.py` —— 把标签摊平成可查询的表

### `stats.py` —— **出报表**

**输出例**

```
1. 按 CP 的字数统计（canonical 口径，恋爱向 `/`）
  CP（恋爱向）              篇      字数    均字
  ----------------------  ---  --------  ------
  A/B                     172   1,123,566  6,532  ██████████████████████
  C/D                      69     375,178  5,437  ███████
  E/F                      14      65,189  4,656  █

2. 按 CP × 年 的字数（用分章日期与字数）
  CP        2022     2023     2024     2025     2026
  ------  -------  -------  -------  -------  -------
  A/B      59,841  111,462  360,842  456,415  132,885
  C/D     124,651   76,709   74,118   93,453    5,705
  E/F           —    3,623    7,223   18,365    3,211

  逐年（全库）：
  年          字数   章数
  2022     162,435    29  ██████
  2023     178,064    60  ███████
  2024     440,593   106  █████████████████
  2025     564,839   186  ██████████████████████
  2026     153,975    51  ██████
```

（表里的 CP 名、篇数、字数都是示意，跑出来的是你自己的。）

---

## 出问题了怎么办

1. **先别急着重跑第二遍。** 把终端里的输出整段复制下来 ——
   或者直接去 `code/session_printouts/` 找那次的记录，里面是完整的。
2. **所有脚本都可以续跑**，停在半路不会损坏任何东西。
3. **已经落盘的原始文件，脚本永远不会删。**
4. 数据库坏了不要紧：跑 `rebuild_index.py`，它会从本地原始文件把整个库重建出来，**0 次请求**。

## （可选）用 git 看出每次的变化

**跳过这一节完全不影响使用。** 但如果你想知道「这次跑完，跟上次比有什么不一样」，
git 是最省事的办法 —— 因为这套脚本**不覆盖式地丢东西**，
每次抓下来的原始文件都在原地更新，git 正好能把「哪一篇的正文变了」显示出来。

一次性设置（在解压出来的文件夹里，终端执行）：

```
git init
git add -A
git commit -m "第一次备份"
```

以后每次跑完脚本，重复这两行就行：

```
git add -A
git commit -m "20260808 备份"
```

然后你就可以：

```
git log --oneline          # 看历次备份
git show --stat HEAD       # 这次比上次动了哪些文件
```

> 有用在哪：AO3 不提供「章节最后修改时间」（见 `docs/DESIGN-NOTES.md` 的 N-10）。
> 但你本地有 git 的话，**「哪篇的正文什么时候变过」就有记录了** —— 这是 AO3 那边拿不到的信息。

⚠️ 两件事：

- **`secrets/` 不要提交。** 仓库自带的 `.gitignore` 已经排除了它，别自己去改。
- **git提交完可确认一下**：`git log --oneline -1` 看到的是不是你刚写的那条？
  不是就说明没提交成功（常见原因：`.git/index.lock` 残留，删掉它再来一次）。

## 一句话说明白这个工具的边界

- 它备份的是**你自己的作品**，用的是**你自己的登录态**，拿到的东西和你在浏览器里看到的一样。
- 它**只发 GET 请求**，代码里根本没有修改或删除的函数。
- 它**不能**用来批量下载别人的文。
- 默认间隔较慢，**请不要调快**。AO3 是志愿者运营的非营利站点。

## License

MIT。
