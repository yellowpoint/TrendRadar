# 源级筛选使用指南

在订阅 RSS / 热榜源之前，先用 AI 判断该源内容是否与你的兴趣主题相关，避免抓一堆无关内容浪费 token 和推送噪音。

---

## 一、快速开始

### 1. 检查单个 RSS 源

```bash
python -m trendradar --check-rss <URL> --source-name <名称> --sample-size 20
```

示例：

```bash
python -m trendradar --check-rss https://www.qbitai.com/feed --source-name 量子位 --sample-size 20
```

### 2. 检查单个热榜平台

```bash
python -m trendradar --check-source <platform_id> --sample-size 20
```

`platform_id` 取自 [config/config.yaml](../config/config.yaml) 中 `platforms.sources[].id`，如 `zhihu` / `ithome` / `hackernews`。

示例：

```bash
python -m trendradar --check-source solidot --sample-size 20
```

### 3. 批量检查所有已配置源

```bash
python -m trendradar --check-all-sources --sample-size 20
```

会自动遍历 `config.yaml` 中所有 `rss.feeds` 和 `platforms.sources`，输出汇总表格 + 建议订阅源的详细信息。

---

## 二、参数说明

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--check-rss URL` | 预检单个 RSS 源 | — |
| `--check-source ID` | 预检单个热榜平台 | — |
| `--check-all-sources` | 批量预检所有已配置源 | — |
| `--source-name NAME` | 配合 `--check-rss`：自定义源显示名 | 等于 URL |
| `--sample-size N` | 抓取样本条目数 | 20 |

### 样本量建议

| 场景 | 推荐 sample-size | 说明 |
|---|---|---|
| 快速预筛 | 10 | 1~2 个源快速看一眼 |
| **标准评估（推荐）** | **20** | 稳定度与 token 成本平衡 |
| 严格评估 | 30~50 | 高质量源判断，token 消耗大 |

⚠️ **不要低于 10**：样本太少会因偶然抽样导致分数剧烈波动。例如 `sample_size=3` 时 The Verge 得 0.10，但 `sample_size=20` 时仍是 0.10 —— 这是因为 The Verge 主线是消费电子，但其他源（如 Engadget）小样本时 0.15、大样本时 0.15，相对稳定。综合媒体（知乎/微博）小样本容易偏高，容易误判。

---

## 三、判定规则

### 相关度分数（0.0~1.0）

| 分数段 | 含义 | 建议 |
|---|---|---|
| 0.9~1.0 | 高度相关 | 强烈建议订阅 |
| 0.6~0.9 | 较相关 | 建议订阅 |
| 0.5~0.6 | 中等相关 | 谨慎订阅（看匹配样本质量） |
| 0.3~0.5 | 偶尔相关 | 不建议订阅 |
| 0.0~0.3 | 基本无关 | 不建议订阅 |

### 阈值

- **`relevant = true` 的判定阈值**：`score ≥ 0.5`
- 这个阈值写在 [trendradar/ai/source_filter.py](../trendradar/ai/source_filter.py) 的提示词中，与文章级 `ai_filter.min_score` **独立**

### 与文章级筛选的关系

| 维度 | 源级筛选（本功能） | 文章级筛选（ai_filter） |
|---|---|---|
| 时机 | 订阅前预检 | 主流程抓取后逐条分类 |
| 粒度 | 整个源 | 单条文章 |
| 用途 | 决定是否加入 config.yaml | 决定是否推送 |
| 触发 | 手动 CLI | 自动 |
| 阈值 | 0.5（固定） | `ai_filter.min_score`（默认 0.7） |

源级筛选是"第一道门"，文章级筛选是"第二道门"。

---

## 四、输出解读

### 单源输出

```
✅ 量子位 → 建议订阅
   类型: rss
   URL/ID: https://www.qbitai.com/feed
   样本数: 20, 命中样本: 5
   相关度: 0.72
   命中方向: AI 眼镜与 AR 可穿戴, 人形机器人与具身智能
   理由: 样本中约5条直接匹配AI硬件兴趣点...
   匹配样本（前 5 条）:
     • (0.95) 千问AI眼镜将升级为智能体眼镜... [AI 眼镜与 AR 可穿戴]
     • (0.90) "小而美"李未可，正用"记忆"重写AI眼镜游戏规则 [AI 眼镜与 AR 可穿戴]
```

- **状态图标**：✅ = relevant=true / ❌ = relevant=false
- **命中方向**：AI 从样本中归纳的兴趣标签（最多 5 个）
- **匹配样本**：AI 列出的高匹配样本（最多 10 条，按 score 降序）

### 批量输出

```
状态   类型     名称                       分数     命中/样本      建议
--------------------------------------------------------------------------------
✅   rss    智东西                      0.72    5/20       订阅
❌   rss    量子位                      0.20    1/10       跳过
...
--------------------------------------------------------------------------------
汇总: 共 17 个源, 成功检查 17 个, 建议订阅 4 个, 不建议 13 个
```

末尾会打印所有"建议订阅"源的详细信息，便于直接复制到 config.yaml。

---

## 五、判定依据

AI 判断时读取的内容：

1. **用户兴趣描述**：[config/ai_interests.txt](../config/ai_interests.txt)（或 `ai_filter.interests_file` 指定的自定义文件）
2. **源元信息**：类型（rss/platform）、名称、URL
3. **样本条目**：抓取最新 N 条，包含 title / summary（前 200 字）/ url / published_at

提示词模板：[config/ai_filter/source_check_prompt.txt](../config/ai_filter/source_check_prompt.txt)

### 自定义兴趣描述

如果有多套兴趣主题，可以创建独立文件：

```
config/custom/ai/finance.txt     # 金融主题
config/custom/ai/robotics.txt   # 机器人主题
```

然后在 CLI 调用前，临时修改 `config.yaml` 的 `ai_filter.interests_file: "robotics.txt"`，或直接编辑 [config/ai_interests.txt](../config/ai_interests.txt)。

---

## 六、应用筛选结果到 config.yaml

### 启用源（score ≥ 0.5）

把源加到 `rss.feeds` 列表：

```yaml
rss:
  feeds:
    - id: "zhidx"                    # 用作数据库主键，建议用英文短标识
      name: "智东西"                 # 显示名
      url: "https://www.zhidx.com/rss"
```

### 禁用源（score < 0.5）

**推荐做法**：注释保留，附带分数备注，方便日后回溯

```yaml
rss:
  feeds:
    # 源级筛选结果（sample_size=20）：智东西 0.72 ✅ / 量子位 0.20 ❌
    - id: "zhidx"
      name: "智东西"
      url: "https://www.zhidx.com/rss"

    # - id: "qbitai"
    #   name: "量子位"
    #   url: "https://www.qbitai.com/feed"
```

### 热榜平台

热榜全部禁用时，注意**关键陷阱**：

⚠️ **不要用 `platforms.enabled: false` 禁用热榜**！

虽然配置项名叫 `platforms.enabled`，但代码里它实际被映射成 `ENABLE_CRAWLER`（[trendradar/core/loader.py L64](../trendradar/core/loader.py#L64)），是**整个爬虫的总开关**（含 RSS）。设为 false 会让主流程直接退出，连 RSS 也不抓。

正确的禁用方式：

1. `platforms.enabled: true`（保持爬虫总开关开启）
2. `platforms.sources: []`（让热榜源列表为空，自然不抓热榜；不能留空 `sources:`，否则加载会报 `NoneType` 错误）

原 sources 项以注释形式保留即可。

### 独立展示区联动

如果 `display.standalone.platforms` 引用了被禁用的热榜 ID（如 `["zhihu"]`），需同步清空为 `[]`，否则独立展示区会因找不到源而出错。

---

## 七、常见问题

### Q1. AI 调用失败怎么办？

检查：

1. `config.yaml` 的 `ai` 段是否配置了 `api_key` / `base_url` / `model`
2. 或环境变量 `AI_API_KEY` / `AI_MODEL` 是否设置
3. 运行 `python -m trendradar --doctor` 验证 AI 配置

### Q2. 同一个源多次跑分数不一样？

正常。AI 模型有随机性，样本也会随时间变化（新文章覆盖旧文章）。建议：

- 用 `--sample-size 20` 跑 2~3 次
- 关注分数的**区间**而非单次值
- 边界值（0.4~0.6）的源谨慎决策

### Q3. 抓不到样本？

- RSS：URL 是否失效？用浏览器打开 `<URL>` 看是否有 XML 内容
- 热榜：`platform_id` 是否拼写正确？是否在 newsnow 上线？
- 网络：公司代理或防火墙可能拦截

### Q4. 跑 `--check-all-sources` 时间很长？

17 个源 × 20 样本 ≈ 17 次 AI 调用，每次约 5~15 秒，总计 2~5 分钟。如需加速：

- 减小 `--sample-size`（但不要低于 10）
- 临时把不关心的源从 config.yaml 注释掉

### Q5. 可以集成到主流程自动跳过吗？

当前是**独立预检工具**，主流程不会自动跳过被判为不相关的源。如需自动跳过，需要修改 `trendradar/analyzer.py` 中 `_crawl_rss_data` / `_crawl_platforms_data` 的逻辑（本仓库未实现该功能，避免改变现有行为）。

---

## 八、检测记录

### 2026-07-19：首次全量检测

**配置**：

- 兴趣主题：AI 硬件方向（12 个子主题，[config/ai_interests.txt](../config/ai_interests.txt)）
- 样本量：20
- AI 模型：openai/agnes-2.0-flash
- 阈值：0.5

**结果**：

| 状态 | 类型 | 名称 | 分数 | 命中/样本 | 处理 |
|---|---|---|---|---|---|
| ✅ | rss | 雷科技 | 0.82 | 7/20 | 保留 |
| ✅ | rss | New Atlas | 0.75 | 6/20 | 保留 |
| ✅ | rss | 智东西 | 0.72 | 5/20 | 保留 |
| ✅ | rss | 爱范儿 | 0.65 | 6/20 | 保留 |
| ❌ | rss | IEEE Spectrum | 0.25 | 2/20 | 注释禁用 |
| ❌ | rss | TechCrunch | 0.25 | 1/20 | 注释禁用 |
| ❌ | rss | 量子位 | 0.20 | 1/10 | 注释禁用 |
| ❌ | rss | Engadget | 0.15 | 0/20 | 注释禁用 |
| ❌ | rss | The Verge | 0.10 | 0/10 | 注释禁用 |
| ❌ | platform | IT之家 | 0.35 | 2/20 | 注释禁用 |
| ❌ | platform | 少数派 | 0.10 | 0/20 | 注释禁用 |
| ❌ | platform | Solidot | 0.10 | 0/20 | 注释禁用 |
| ❌ | platform | Hacker News | 0.10 | 1/20 | 注释禁用 |
| ❌ | platform | 知乎 | 0.05 | 0/20 | 注释禁用 |
| ❌ | platform | bilibili 热搜 | 0.05 | 0/20 | 注释禁用 |
| ❌ | platform | ProductHunt | 0.00 | 0/20 | 注释禁用 |
| ❌ | platform | 微博 | 0.00 | 0/20 | 注释禁用 |

**汇总**：17 个源 / 建议订阅 4 个 / 不建议 13 个

**应用动作**：

1. [config/config.yaml L60-98](../config/config.yaml)：`platforms.sources:` 改为 `sources: []`（保留 `enabled: true`，否则会误伤 RSS），原 8 个热榜源以注释保留
2. [config/config.yaml L123-164](../config/config.yaml)：5 个低分 RSS 源以注释保留
3. [config/config.yaml L258-264](../config/config.yaml)：`standalone.platforms: ["zhihu"] → []`

**保留的 4 个 RSS 源命中方向**：

- **雷科技 0.82**：人形机器人与具身智能、AI 芯片与算力硬件、四足机器人与机器狗、AI 硬件展会与新品发布
- **New Atlas 0.75**：AI 眼镜与 AR 可穿戴、AI 可穿戴陪伴设备、人形机器人与具身智能、AI 无人机、VR/AR 头显与空间计算
- **智东西 0.72**：AI 眼镜与 AR 可穿戴、人形机器人与具身智能、AI 芯片与算力硬件、四足机器人与机器狗
- **爱范儿 0.65**：AI 眼镜与 AR 可穿戴、人形机器人与具身智能、AI 芯片与算力硬件

### 后续复检建议

- **频率**：兴趣主题大幅调整时复检一次（如从 AI 硬件 → 金融主题）
- **新增源**：每次添加新源前先跑 `--check-rss` 或 `--check-source`
- **被禁源**：3~6 个月后可复检一次，源内容方向可能变化（如 IT之家 0.35 接近阈值，可能未来命中度上升）

---

### 2026-07-19：海外 AI 智能硬件内容源批量检测

**背景**：用户提供了一份《AI 智能硬件海外内容来源》清单（3 个梯队共 17 个源），用源级筛选流程评估是否值得订阅。

**配置**：

- 兴趣主题：AI 硬件方向（与首次检测相同，[config/ai_interests.txt](../config/ai_interests.txt)）
- 样本量：20
- AI 模型：openai/agnes-2.0-flash
- 阈值：0.5

**第一步：RSS feed URL 探测结果**

清单中给出的是网页 URL，需要先找到对应的 RSS feed URL 才能用 `--check-rss` 检测。通过尝试常见 RSS 路径 + feedburner 等方式探测：

| 源 | 网页 URL | RSS feed URL | 状态 |
|---|---|---|---|
| The Verge | https://www.theverge.com/tech | https://www.theverge.com/rss/index.xml | ✅ 已知 |
| Engadget | https://www.engadget.com/ | https://www.engadget.com/rss.xml | ✅ 已知 |
| Wareable | https://www.wareable.com/category/wearable-tech | — | ❌ 无公开 RSS |
| New Atlas | https://newatlas.com/technology/ | https://newatlas.com/technology/index.rss | ✅ 已知 |
| TechCrunch | https://techcrunch.com/category/hardware/ | https://techcrunch.com/feed/ | ✅ 已知 |
| IEEE Spectrum | https://spectrum.ieee.org/robotics | https://spectrum.ieee.org/feeds/feed.rss | ✅ 已知 |
| TechRadar | https://www.techradar.com/ | https://www.techradar.com/rss | ✅ 探测到（50 条） |
| Tom's Guide | https://www.tomsguide.com/ai | https://www.tomsguide.com/feeds/rss | ✅ 探测到（50 条） |
| Yanko Design | https://www.yankodesign.com/category/technology/ | https://www.yankodesign.com/feed/ | ✅ 探测到 |
| Gadget Flow | https://thegadgetflow.com/ | https://thegadgetflow.com/feed/ | ✅ 探测到 |
| Notebookcheck | https://www.notebookcheck.net/ | — | ❌ 无公开 RSS |
| Designboom | https://www.designboom.com/technology/ | https://www.designboom.com/technology/feed/ | ✅ 探测到 |
| Kickstarter | https://www.kickstarter.com/discover | https://feeds.feedburner.com/Kickstarter | ✅ feedburner（10 条） |
| BackerLens | https://backerlens.com/ | — | ❌ 无 RSS |
| Kicktraq | https://www.kicktraq.com/ | — | ❌ 无 RSS（HTML 页面） |
| Crowd Supply | https://www.crowdsupply.com/ | — | ❌ 无 RSS |
| ProductHunt | https://www.producthunt.com/ | https://www.producthunt.com/feed | ✅ 探测到（50 条） |

**RSS 可用性汇总**：17 个源中 13 个有 RSS feed，4 个无 RSS（Wareable / Notebookcheck / BackerLens / Kicktraq / Crowd Supply 实际是 5 个，但 Crowd Supply 没找到）。这些源只能用浏览器人工查看，不能进入主流程自动抓取。

**第二步：源级筛选检测结果**

| 梯队 | 状态 | 名称 | 分数 | 命中/样本 | 建议 | 说明 |
|---|---|---|---|---|---|---|
| T1 | ❌ | The Verge | 0.30 | 1/10 | 跳过 | 仅 1 条勉强相关（GoPro Max 2），主线是消费电子综述 |
| T1 | ❌ | Engadget | 0.15 | 1/20 | 跳过 | 仅 1 条（Apple TV 4K）勉强算硬件 |
| T1 | ❌ | Wareable | — | — | 失败 | 无 RSS |
| T1 | ✅ | New Atlas | 0.75 | 6/20 | **订阅** | 沿用首次检测结果（本次 RSS 临时网络异常）|
| T1 | ✅ | TechCrunch | 0.65 | 2/20 | **订阅** | 命中 Agility Robotics / AI 内存芯片 |
| T1 | ❌ | IEEE Spectrum | 0.25 | 2/20 | 跳过 | 多为软件伦理/学术新闻 |
| T2 | ❌ | TechRadar | 0.30 | 3/20 | 跳过 | 命中脑机接口/智能眼镜/扫地机但密度低 |
| T2 | ❌ | Tom's Guide | 0.15 | 1/20 | 跳过 | 仅 1 条（AI 笔）弱相关 |
| T2 | ❌ | Yanko Design | 0.30 | 1/10 | 跳过 | 仅 Narwal Flow 2 匹配智能家居 |
| T2 | ❌ | Gadget Flow | 0.35 | 4/20 | 跳过 | 综合消费电子，内容宽泛 |
| T2 | ❌ | Notebookcheck | — | — | 失败 | 无 RSS |
| T2 | ❌ | Designboom | 0.30 | 3/10 | 跳过 | 偏设计美学，硬核 AI 硬件少 |
| T3 | ❌ | Kickstarter | 0.00 | 0/10 | 跳过 | feedburner 是项目进度更新，非新品发现 |
| T3 | ❌ | BackerLens | — | — | 失败 | 无 RSS |
| T3 | ❌ | Kicktraq | — | — | 失败 | 无 RSS |
| T3 | ❌ | Crowd Supply | — | — | 失败 | 无 RSS |
| T3 | ❌ | ProductHunt | 0.10 | 0/20 | 跳过 | 主体是 SaaS/AI 软件，硬件产品极少 |

**汇总**：17 个源 / 成功检测 11 个 / 建议订阅 2 个 / 不建议 9 个 / 无 RSS 5 个

**建议订阅源详情**：

- **New Atlas 0.75**（首次检测已加入 config.yaml，沿用结果）
- **TechCrunch 0.65**：人形机器人与具身智能、AI 芯片与算力硬件、AI 硬件展会与新品发布
  - (0.90) Agility Robotics plants its flag in Tesla's backyard
  - (0.60) AI-driven memory crunch jolts India's smartphone market

**与首次检测的对比**：

| 源 | 首次分数 | 本次分数 | 变化 | 说明 |
|---|---|---|---|---|
| The Verge | 0.10 | 0.30 | ↑ | 仍低于阈值 |
| Engadget | 0.15 | 0.15 | — | 一致 |
| New Atlas | 0.75 | 0.75 | — | 沿用首次结果 |
| TechCrunch | 0.25 | 0.65 | ↑↑ | 命中 Agility Robotics 报道 |
| IEEE Spectrum | 0.25 | 0.25 | — | 一致 |

**观察**：TechCrunch 分数波动较大（0.25 → 0.65），说明该源偶有相关内容但不稳定。建议先订阅观察，配合文章级 `ai_filter.min_score=0.7` 做二次过滤。

**应用动作**：

1. **TechCrunch** 加回 config.yaml 的 rss.feeds（首次检测时被注释禁用，本次复检命中度上升至 0.65，超过阈值）
2. 其他源维持现状（首次检测已禁用的仍然禁用，新检测的低分源不加入）

**无 RSS 源的处理建议**：

5 个无 RSS 的源（Wareable / Notebookcheck / BackerLens / Kicktraq / Crowd Supply）只能人工浏览：

- **Wareable**：可穿戴垂直媒体，建议加入浏览器书签每日查看
- **Notebookcheck**：硬件规格详细，适合核实参数时手动搜索
- **BackerLens / Kicktraq**：众筹数据查询工具，按需使用
- **Crowd Supply**：开源硬件众筹，月度查看即可

这些源不进入 TrendRadar 主流程，但可作为人工选题补充。

---

## 九、相关文件

| 文件 | 作用 |
|---|---|
| [trendradar/ai/source_filter.py](../trendradar/ai/source_filter.py) | 核心模块：`SourceFilter` 类、`SourceCheckResult` 数据结构 |
| [trendradar/commands/source_check.py](../trendradar/commands/source_check.py) | CLI 命令实现（表格输出） |
| [config/ai_filter/source_check_prompt.txt](../config/ai_filter/source_check_prompt.txt) | AI 提示词模板 |
| [config/ai_interests.txt](../config/ai_interests.txt) | 兴趣描述（与文章级筛选共用） |
| [config/config.yaml](../config/config.yaml) | RSS / 热榜源配置 |
