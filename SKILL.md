---
name: x-bookmark-sync
description: Sync X (Twitter) bookmarks into Memos as designed cards, incrementally. Use when the user asks to sync/backfill/import X or Twitter bookmarks to memos (书签同步、书签导入、推特收藏入库), to check what new bookmarks appeared (增量检查、有没有新书签), to re-render an already-synced bookmark with a better layout (重排、重新排版、重发某条), or to audit which bookmarks are missing from memos. Orchestrates twitter-cli for data, the memos skill for publishing, and an ID ledger for dedupe — it does not re-implement any of them.
---

# X Bookmark → Memos

把 X 书签变成 memos 里的「卡片」：一条书签一条 memo，增量同步，重复运行是安全的。

本 skill 只做三件事：**编排**、**排版**、**去重**。数据抓取、memos 写入、去重存储都交给既有 skill，不要在本 skill 里重写它们的 API 调用。

| 依赖 | 负责 | 调用方式 |
| --- | --- | --- |
| [twitter-cli](https://github.com/public-clis/twitter-cli) | 书签/推文/长文数据 | `twitter bookmarks -n N --json`、`twitter article <id> -m` |
| [memos skill](https://github.com/rogeecn/memos-skill) | 发布、列表、标签解析 | `python3 <memos>/scripts/memos.py publish --file - --json` |
| [id-ledger-query](https://github.com/rogeecn/cf-id-ledger/tree/main/skills/id-ledger-query) | 已同步 id 台账 | `Ledger` 类（`scripts/sync.py`），`POST/GET /v1/projects/<key>/ids` |

本 skill 不绑定某个 agent runtime：放进任意 skills 目录即可（`~/.agents/skills/`、`~/.claude/skills/`、`~/.pi/agent/skills/`），脚本靠 `MEMOS_SKILL` / `TWITTER_CLI` 或按顺序探测来定位依赖。

## 第一次使用（每个 instance 只需一次）

```bash
# 1. 依赖在位
twitter status                        # twitter-cli 已登录
python3 ~/.agents/skills/memos/scripts/memos.py list -n 1 --json   # memos 已配置

# 2. 环境变量
#    MEMOS_URL / MEMOS_API_KEY           （memos skill 用）
#    ID_LEDGER_URL / ID_LEDGER_TOKEN     （台账用）
#    TWITTER_PROXY                        （可选）

# 3. 把已有历史 memo 回填进台账，否则首次同步会重复发布几百条
python3 scripts/sync.py seed
```

`seed` 扫描 memos 里的历史同步（默认扫 `#Bookmark` 标签 2000 条），按上面第 2 条的位置规则提取 id 写入台账。这一步做一次就够；台账丢了重跑即可恢复。

## 增量同步（常规流程）

```bash
# ① 抓书签 + 过滤已同步 → plan.json（含完整 tweet 对象）
python3 scripts/sync.py plan --count 60

# ② 看一眼要发什么（可选，确认排版）
python3 scripts/sync.py render --plan plan.json --id <tweet_id>

# ③ 发布并写入台账
python3 scripts/sync.py publish --plan plan.json
```

三步可以分开跑，也可以按需只跑其中一步。`plan` 与 `publish` 之间是**唯一需要 agent 介入**的地方。

### 去重契约（为什么重复运行安全）

一个 tweet 视为「已同步」，只要满足任一条件：

1. id 在台账里（`GET /v1/projects/x-bookmark/ids`）；
2. id 出现在任一历史 memo 的**本体位置**：老格式首行的 `[@作者](…/status/<id>)`，或新格式尾部的 `[原推](…/status/<id>)`。

第 2 条只认同这两个位置，不认引用块里的 `/status/` 链接——引用推的 id 属于**另一条**推文，把它算作「已同步」会让之后真的 bookmarked 那条被误判跳过。

两个来源取并集。台账快而权威，memos 扫描慢但是「地面真相」——台账被清空、项目 key 写错、或别的工具发过同一条，靠 memos 扫描仍然不会重复发。

第三道闸在 `publish` 里：开跑前再读一次台账，把已在台账的 item 标成 `skip`（原因 `already in ledger`）。所以拿一份旧的 `plan.json` 重跑也不会重复发布。**唯一会破坏契约的做法是绕过 `sync.py` 直接调 memos 发。**

### agent 在这一步做什么（这是排版的价值所在）

`plan` 给出的每条 item 已经带了机器可算的东西：`title`（自动抽取）、`suggested_tags`（从正文/链接匹配出的历史词表标签）、`has_article`（是否值得多拉一次全文）。读 `plan.json`，然后**只改你认为该改的**：

- `title`：自动标题只做**抽取**（推文自己的首行或第一句），不理解内容，约 8% 的长单段推文抽不出标题、`title` 为空。读完正文，用一句说清「这条到底给了什么」的话替换或补上（≤30 字，中文）。数据型/工具型/观点型各有各的写法，别都用「分享一个…」开头；空着比写假标题好。
- `tags`：从 `suggested_tags` 里挑，或按内容补 0–2 个**已存在于归档**的标签。宁缺勿滥，超过 3 个就没有区分度了。
- 跳过营销味儿/纯广告/重复内容的书签：把 `status` 设为 `"skip"` 并写 `skip_reason`。`publish` 会跳过它们，报告里也会列出原因。

没时间读就原样发布：默认标题和标签是合格的，只是不够好。**不要为了「更丰富」编造推文里没有的事实。**

注意 `plan.json` 是快照：标题在 `plan` 时算好写进文件。改了 `render.py` 的排版逻辑后，先重跑 `plan` 再 `publish`，否则老 plan 里的标题仍是旧算法产物。

## 排版

完整规范在 `references/layout.md`（结构、每个字段为什么放在那里、反模式清单、验收清单）。实现是 `scripts/render.py`。要点：

```
### 标题                                    ← h3：memos 里「卡片标题」那一档，h1/h2 会压过相邻卡片
[@handle](url) · 名字 ✓ · 11-16 19:36 · 👁6.4k 🔖186 ♥113
正文（t.co 已换成真实地址）
> **@引用作者** 名字 · [引用](url)          ← 转推的实体，历史同步只贴链接，价值损失最大
> 引用正文
![](pbs...jpg?name=large)                    ← name=large 才是大图，裸 URL 是缩略图
🎬 [视频 · 1920×1080](video.twimg.com/...)
**🔗 链接**
https://github.com/...                       ← 裸 URL 独占段落 → memos 渲染成预览卡
<details><summary>📄 全文 · 4820 字</summary>
[原推](url) · 存档 2026-11-17
#Twitter #Bookmark #github
```

原则：**扫描区（标题/byline/正文）在上，深挖区（链接/全文/来源）在下。** 列表里 3 秒能判断值不值得点开，不点开也是完整信息。

只改排版时不要重新抓推文：`render --plan plan.json --id <id>` 就能出稿，改完 `publish` 前把该 item 的 `status` 改回 `"new"`（或直接 `--limit 1`），否则它会被当成已发布跳过。

**短链必须是真实地址。** t.co 在 memo 里点开是短链，读者看不出要去哪，memos 的预览卡也只认目标域名。`plan` 会顺着每个 t.co 的 301 走一遍（走 `$TWITTER_PROXY`，实测 280/280 全部命中），结果写进 `plan.json` 的 `links`，`render` 在排版前替换。缓存落在 `~/.cache/x-bookmark-sync/tco.json`，第二次跑同一条链接不发请求。

替换规则（`render.py` 里三个函数，都有测试）：

| 情况 | 处理 |
| --- | --- |
| 短链解析到外部站点 | 原位替换成真实 URL，外面不加 `[文字](url)`，交给 memos 生成预览卡 |
| 短链解析回**这条推文自己**（常见于视频/图片） | 整段删掉：媒体块和引用块已经在展示它，留着就是自己指自己 |
| 短链指向 `x.com/i/…`、登录墙、onboarding | 删掉：那不是内容，是把读者挡在门外 |
| 没有代理、解析失败 | 保留短链（仍可重定向），另有 `urls[]` 1:1 匹配的兜底 |

## 常用命令

```bash
# 只看新书签，不发
python3 scripts/sync.py plan --count 100
python3 scripts/sync.py render --plan plan.json | head -60

# 只看会发出去的正文，不真的发
python3 scripts/sync.py publish --plan plan.json --limit 2 --dry-run

# 发前 5 条试水
python3 scripts/sync.py publish --plan plan.json --limit 5

# 已经发过的旧 memo 想换新排版：原地重渲染，不重新发布
python3 scripts/sync.py repair --dry-run --limit 2   # 先看稿
python3 scripts/sync.py repair                        # 保留原标题/标签/存档日期

# 只想把旧 memo 里的 t.co 换成真实地址，排版一个字不动
python3 scripts/sync.py repair --links-only --limit 50

# 想要机器自动补主题标签（默认关：标签是判断，不是正则的活）
python3 scripts/sync.py publish --plan plan.json --auto-tags

# 拉长文全文折进卡片（慢，每条多一次请求）
python3 scripts/sync.py publish --plan plan.json --articles

# 改台账项目 key（默认 x-bookmark）
python3 scripts/sync.py --ledger-project my-project plan

# 单条渲染（不经过 plan，直接喂 twitter-cli 输出）
twitter bookmarks -n 20 --json | python3 scripts/render.py --id 2122286309855338953
```

所有子命令支持 `--memos <path>`、`--twitter <path>` 覆盖依赖位置；`--help` 有完整参数。

## 失败处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `plan` 的 `new` 为 0，但用户说「有新书签」 | 书签已被别的工具同步过，或 `--count` 太小 | 加大 `--count`；或 `seed` 确认台账里有它 |
| 首次同步想发几百条 | 没跑 `seed`，或台账项目 key 不对 | 跑 `seed`；用 `--ledger-project` 确认 key（区分大小写） |
| `publish` 报 HTTP 401/403 | memos 或台账 token 失效 | 重新配置 `MEMOS_API_KEY` / `ID_LEDGER_TOKEN`，本 skill 不碰凭证存储 |
| 旧 memo 里还是 t.co | 发出去时还没做短链解析 | `repair --links-only`：只换链接，排版不动 |
| `plan` 报 `no proxy` | 没配 `TWITTER_PROXY`，t.co 出不去 | 配代理，或接受短链降级（只做 `urls[]` 1:1 展开） |
| 媒体图不显示 | 读者侧访问不到 `pbs.twimg.com` | 不要改成下载上传（本机同样出网受限）；改为在正文补一句图片描述 |
| 某条 publish 失败 | 单条数据异常 | 循环不会中断，失败的 item 标 `failed` 写进 `sync-report.json`，修好数据后把它改回 `"new"` 重跑 |
| 台账写入失败 | 台账服务抖动 | 默认降级：警告后继续（memo 扫描兜底），memo 已发出但 id 未记账 → 重跑 `seed` 补回；要强制失败加 `--strict-ledger` |

`sync-report.json` 记录了每条的结果（`published` / `failed` / `skipped` + 原因），收尾时读它汇报，不要凭记忆。

## 验证

```bash
python3 scripts/test_render.py     # 排版回归（纯函数，不联网）
```

改了 `render.py` 或 `references/layout.md` 之后跑一遍；断言覆盖标题抽取、t.co 解析替换/自指删除/内部链接删除、链接区排除、媒体高分辨率、引用块每行 `>`、`<details>` 前后空行、标签去重。
