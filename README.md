# x-bookmark-sync

X（Twitter）书签 → Memos，增量同步，一条书签一张卡片。

本 skill 只做**编排**与**排版**：数据来自 [twitter-cli](https://github.com/public-clis/twitter-cli)，发布走 [memos skill](https://github.com/rogeecn/memos-skill)，去重台账用 [cf-id-ledger](https://github.com/rogeecn/cf-id-ledger) 的 id-ledger-query 接口。

## 安装

```bash
git clone https://github.com/rogeecn/x-bookmark-sync-skill.git ~/.agents/skills/x-bookmark-sync
```

任何加载 `~/.agents/skills` 的 agent runtime 都能用（Claude Code 的 `~/.claude/skills`、pi 的 `~/.pi/agent/skills` 同理，脚本会自动探测 memos skill 的位置）。

## 快速开始

```bash
export MEMOS_URL=https://your.memos ID_LEDGER_URL=https://your.ledger
export MEMOS_API_KEY=... ID_LEDGER_TOKEN=...

python3 scripts/sync.py seed                 # 一次性：把历史 memo 回填进台账
python3 scripts/sync.py plan --count 60      # 抓书签，过滤已同步 → plan.json
python3 scripts/sync.py publish --plan plan.json
```

## 目录

```
SKILL.md                 agent 的操作手册（何时用、怎么编排、失败怎么处理）
references/layout.md     排版规范：每个字段为什么在这里、反模式、验收清单
scripts/render.py        推特 JSON → memo markdown（纯函数、可预览、可单测）
scripts/sync.py          编排：seed / plan / publish / render
scripts/test_render.py   排版回归测试（42 条，不联网）
scripts/test_sync.py     编排层测试（6 条：id 提取、台账降级/重试）
evals/evals.json         触发与行为评测用例
```

## 卡片长什么样

```
### 标题
[@名字](https://x.com/handle) · 11-16 11:36

正文（t.co 已换成真实地址）

> **@引用作者** 名字 · [引用](https://x.com/…/status/…)
>
> 引用正文

![](https://pbs.twimg.com/media/…jpg?name=large)
🎬 [视频 · 2784×1944](https://video.twimg.com/…mp4)

**🔗 链接**
https://github.com/…

<details><summary>📄 全文 · 4820 字</summary>

文章全文

</details>

[原推](https://x.com/…/status/…) · 存档 2026-11-17
#Twitter #Bookmark #github
```

比历史同步多出来的东西：名字当链接文字（handle 只作 href）、发布时间、转发标记、**引用推全文**、视频链接与尺寸、**展开后的真实链接**（跟着 t.co 的 301 走，实测 280/280 命中，渲染成预览卡）、长文全文、可被后续去重扫描的 tweet id。

## 设计取舍

- **去重用「台账 ∪ memo 正文扫描」**：台账快，memo 扫描是地面真相（台账丢了、项目 key 错了、别的工具发过，都不会重复发）。
- **t.co 靠 301 解析，不靠 `urls[]` 顺序**：twitter-cli 常出现 `urls` 数量 ≠ 短链数量（200 条书签里 100 条如此），按序硬替换会把句子指向错误站点。解析结果进缓存 `~/.cache/x-bookmark-sync/tco.json`，指向自身推文的链接、`x.com/i/…` 导航页直接删。
- **标题用 `###` 而非 `#`/`##`**：memos v0.30 里 h3 才是「卡片标题」的字号档，h1/h2 会压过相邻卡片。
- **图片热链，不下载上传**：`?name=large` 拿 1200px 版本，附件会撑爆 instance 存储。
- **台账读失败不阻塞发布**：首次读失败会重试（Cloudflare 会掐 keep-alive 连接）；仍失败则降级到 memo 扫描，memo 发成功后台账写失败只 warn —— 不能让已发出的 memo 被报成失败。
- **自动抽不出标题时就不给标题**（约 8% 的长单段推文），由 agent 在 `plan.json` 里补写；空标题好过假标题。

## 测试

```bash
python3 scripts/test_render.py   # 排版（42 条）
python3 scripts/test_sync.py     # id 提取与台账容错（6 条，含 3 秒重试等待）
```

## License

MIT

## 旧 memo 的原地翻新

`repair` 不重新发布，只改已有 memo：

```bash
python3 scripts/sync.py repair --dry-run --limit 2   # 看稿
python3 scripts/sync.py repair                       # 重渲染（保留原标题、标签、存档日期）
python3 scripts/sync.py repair --links-only          # 只换 t.co，排版一个字不动
python3 scripts/sync.py repair --in-place            # 文本手术：短链 + 头部（名字/时间，去掉指标与 ✓）
```

`repair` 默认只处理「推文还在当前书签列表里」的 memo（排版需要完整 tweet 对象，逐条 `twitter tweet` 又慢又不全）；`--links-only` 不受这个限制，因为换短链只需要 memo 自己的正文。实测 823 条 memo：104 条换成新排版，324 条只换了短链，全库 t.co 归零。
