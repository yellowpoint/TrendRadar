# TrendRadar Turso 数据库接入文档

本文档面向需要在其他应用中读取 TrendRadar 爬取数据(热榜 + RSS)的开发者。

TrendRadar 通过 **Turso(libSQL)远程数据库**对外提供统一查询接口,数据按日累积,支持跨日 SQL 查询,无需下载多个按日分库的 `.db` 文件。

---

## 目录

- [1. 架构概览](#1-架构概览)
- [2. 连接信息](#2-连接信息)
- [3. HTTP API 调用规范](#3-http-api-调用规范)
- [4. 数据库 Schema](#4-数据库-schema)
- [5. 数据更新规律](#5-数据更新规律)
- [6. 典型查询示例](#6-典型查询示例)
- [7. 去重与幂等性](#7-去重与幂等性)
- [8. 常见问题](#8-常见问题)

---

## 1. 架构概览

```
┌──────────────────────────┐         ┌──────────────────────┐
│  TrendRadar 爬虫          │         │  你的另一个应用       │
│  (GitHub Actions 每小时)  │         │  (后端服务/BI/AI)    │
│                          │         │                      │
│  1. 爬取数据              │         │  HTTP 查询           │
│  2. 写本地 SQLite(按日)  │         │  ↓                   │
│  3. 双写 Turso(统一库)  │ ──────► │  Turso(libSQL)      │
└──────────────────────────┘  写入   └──────────────────────┘
                                        ↑
                                        │ 读取(SQL)
                                        │
                              ┌──────────────────────┐
                              │  Turso 云端数据库     │
                              │  - news_items         │
                              │  - rss_items          │
                              │  - rank_history       │
                              │  - ...                │
                              └──────────────────────┘
```

**关键设计**:
- 写入端:TrendRadar 爬虫独占,只写不读
- 读取端:你的应用只读不写
- 数据载体:Turso 托管的 libSQL,通过 HTTP API 访问
- 协议:标准 HTTP/HTTPS,无 SDK 依赖

---

## 2. 连接信息

| 项目 | 值 |
|---|---|
| **数据库 URL** | `https://<your-db-id>.turso.io` 或 `libsql://<your-db-id>.turso.io` |
| **认证方式** | `Authorization: Bearer <auth_token>` |
| **API 端点** | `POST {url}/v2/pipeline` |
| **请求格式** | JSON |
| **响应格式** | JSON |
| **超时建议** | 60 秒 |

> **获取凭证**:
> - 在 [Turso Dashboard](https://turso.tech/app) 选择数据库,复制 URL
> - 在数据库详情页 → Tokens → 创建新 token
> - 凭证敏感,**不要 commit 到代码库**,建议用环境变量 `TURSO_URL` / `TURSO_AUTH_TOKEN`

---

## 3. HTTP API 调用规范

### 3.1 请求头

```http
Authorization: Bearer <auth_token>
Content-Type: application/json
```

### 3.2 请求体格式

```json
{
  "requests": [
    {
      "type": "execute",
      "stmt": {
        "sql": "SELECT * FROM news_items WHERE crawl_date = ?",
        "args": [
          {"type": "text", "value": "2026-07-18"}
        ]
      }
    },
    {"type": "close"}
  ]
}
```

### 3.3 参数类型映射

Python / JavaScript 值需要序列化为 Turso API 的对象格式:

| Python 类型 | Turso API 格式 |
|---|---|
| `None` | `{"type": "null"}` |
| `bool` | `{"type": "integer", "value": "1" or "0"}` |
| `int` | `{"type": "integer", "value": "123"}` |
| `float` | `{"type": "float", "value": "1.5"}` |
| `str` | `{"type": "text", "value": "..."}` |
| `bytes` | `{"type": "blob", "base64": "..."}` |

### 3.4 响应格式

```json
{
  "results": [
    {
      "type": "ok",
      "response": {
        "type": "execute",
        "result": {
          "cols": [
            {"name": "title", "type": "text"},
            {"name": "rank", "type": "integer"}
          ],
          "rows": [
            [
              {"type": "text", "value": "新闻标题"},
              {"type": "integer", "value": "1"}
            ],
            [
              {"type": "text", "value": "另一条新闻"},
              {"type": "integer", "value": "2"}
            ]
          ],
          "affected_row_count": 0
        }
      }
    }
  ]
}
```

### 3.5 错误响应

单条 SQL 执行错误的响应:

```json
{
  "results": [
    {
      "type": "error",
      "error": {
        "message": "no such table: non_existent_table"
      }
    }
  ]
}
```

HTTP 状态码非 200 时,response body 含错误信息。

### 3.6 最小调用示例(Python)

```python
import requests
import os

url = os.environ["TURSO_URL"].replace("libsql://", "https://")
token = os.environ["TURSO_AUTH_TOKEN"]

resp = requests.post(
    f"{url}/v2/pipeline",
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
    json={
        "requests": [
            {"type": "execute", "stmt": {"sql": "SELECT COUNT(*) FROM news_items"}},
            {"type": "close"},
        ]
    },
    timeout=60,
)

data = resp.json()
result = data["results"][0]["response"]["result"]
count = int(result["rows"][0][0]["value"])
print(f"新闻总条数: {count}")
```

### 3.7 最小调用示例(cURL)

```bash
curl -X POST https://<your-db-id>.turso.io/v2/pipeline \
  -H "Authorization: Bearer $TURSO_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "requests": [
      {"type": "execute", "stmt": {"sql": "SELECT name FROM sqlite_master WHERE type=\"table\""}},
      {"type": "close"}
    ]
  }'
```

---

## 4. 数据库 Schema

### 4.1 `platforms` 平台表(跨日共享)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 平台 ID,如 `ithome`、`zhihu` |
| `name` | TEXT | 显示名称,如「IT之家」「知乎」 |
| `is_active` | INTEGER | 1=启用 |
| `updated_at` | TIMESTAMP | 更新时间 |

**当前 8 个平台**:

| id | name |
|---|---|
| `ithome` | IT之家 |
| `sspai` | 少数派 |
| `solidot` | Solidot |
| `hackernews` | Hacker News |
| `producthunt` | ProductHunt |
| `zhihu` | 知乎 |
| `weibo` | 微博 |
| `bilibili-hot-search` | bilibili 热搜 |

---

### 4.2 `news_items` 热榜条目表(跨日累积)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `title` | TEXT | 新闻标题 |
| `platform_id` | TEXT FK | 所属平台,关联 `platforms.id` |
| `rank` | INTEGER | 当前排名 |
| `url` | TEXT | 链接(可能为空) |
| `mobile_url` | TEXT | 移动端链接(可能为空) |
| `first_crawl_time` | TEXT | 首次抓取时间(HH:MM 格式) |
| `last_crawl_time` | TEXT | 最后抓取时间(HH:MM) |
| `crawl_count` | INTEGER | 当日被抓取到的次数 |
| `crawl_date` | TEXT | **数据所属日期 YYYY-MM-DD**(跨日查询关键字段) |
| `created_at` | TIMESTAMP | 入库时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

**唯一索引**:
- `idx_news_url_platform_date`:`(url, platform_id, crawl_date) WHERE url != ''`
- `idx_news_title_platform_date`:`(title, platform_id, crawl_date)`

**普通索引**:
- `idx_news_platform`:`(platform_id)`
- `idx_news_crawl_date`:`(crawl_date)`
- `idx_news_last_crawl`:`(last_crawl_time)`
- `idx_news_title`:`(title)`

---

### 4.3 `rank_history` 排名历史表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `news_item_id` | INTEGER FK | 关联 `news_items.id` |
| `rank` | INTEGER | 该次抓取的排名 |
| `crawl_time` | TEXT | 抓取时间(HH:MM) |
| `crawl_date` | TEXT | 抓取日期 YYYY-MM-DD |
| `created_at` | TIMESTAMP | 入库时间 |

**追加写入**:同一条新闻每次被抓取都会追加一行,可用于绘制排名变化曲线。

**索引**:`idx_rank_history_news`(`news_item_id`)

---

### 4.4 `crawl_records` 抓取批次表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `crawl_date` | TEXT | YYYY-MM-DD |
| `crawl_time` | TEXT | HH:MM |
| `total_items` | INTEGER | 该批次总条目数 |
| `created_at` | TIMESTAMP | 入库时间 |

**唯一索引**:`(crawl_date, crawl_time)`

---

### 4.5 `rss_feeds` RSS 源表(跨日共享)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 源 ID,如 `hacker-news`、`ifanr` |
| `name` | TEXT | 显示名称 |
| `is_active` | INTEGER | 1=启用 |
| `created_at` / `updated_at` | TIMESTAMP | 时间戳 |

当前约 22 个 RSS 源(科技媒体为主),如:爱范儿、量子位、机器之心、36氪、The Verge、Engadget、TechCrunch 等。

---

### 4.6 `rss_items` RSS 条目表(跨日累积)

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `title` | TEXT | 文章标题 |
| `feed_id` | TEXT FK | 关联 `rss_feeds.id` |
| `url` | TEXT | 文章链接 |
| `guid` | TEXT | RSS guid / Atom id(可能为空) |
| `published_at` | TEXT | 发布时间(ISO 格式) |
| `summary` | TEXT | 摘要 |
| `author` | TEXT | 作者 |
| `first_crawl_time` / `last_crawl_time` | TEXT | HH:MM |
| `crawl_count` | INTEGER | 当日抓取次数 |
| `crawl_date` | TEXT | YYYY-MM-DD |
| `created_at` / `updated_at` | TIMESTAMP | 时间戳 |

**唯一索引**:
- `idx_rss_url_feed_date`:`(url, feed_id, crawl_date)`
- `idx_rss_guid_feed_date`:`(guid, feed_id, crawl_date) WHERE guid != ''`

---

## 5. 数据更新规律

| 维度 | 行为 |
|---|---|
| **抓取频率** | GitHub Actions 每小时执行一次 |
| **同日同条新闻** | `news_items` 行数不增长,`crawl_count` +1,`rank_history` 追加一行 |
| **跨日同条新闻** | `news_items` **新增一行**(`crawl_date` 不同视为两条) |
| **每条记录大小** | < 1KB(纯文本字段) |
| **单日数据量(全量模式)** | `news_items` ~200 行,`rss_items` ~400 行,`rank_history` ~4800 行 |
| **单日数据量(命中模式)** | `news_items` ~10-50 行,`rss_items` ~20-100 行(仅命中关键词/AI 筛选的) |
| **月累积数据量** | 全量 < 50MB,命中模式 < 5MB |

### 5.1 同步模式(sync_mode)

TrendRadar 支持两种 Turso 同步模式,通过 `config.yaml` 的 `storage.turso.sync_mode` 配置:

| 模式 | 值 | 行为 | 适用场景 |
|---|---|---|---|
| **全量同步**(默认) | `"all"` | 采集阶段把所有抓取到的数据同步到 Turso | 需要完整数据留存、跨日趋势分析 |
| **只存命中** | `"matched_only"` | 采集阶段暂存,分析阶段后只把命中关键词/AI 筛选的数据同步到 Turso | 只关心筛选结果、节省存储 |

**matched_only 模式的工作流程**:

1. 采集阶段:爬取数据 → 本地 SQLite 全量保存 → Turso 暂存到内存(不发起 HTTP 请求)
2. 分析阶段:关键词匹配 / AI 筛选 → 生成 `stats`(命中结果)
3. Flush 阶段:从 `stats` 提取命中的 `url` / `title` 集合 → 从暂存中筛选命中的条目 → 批量同步到 Turso → 清空暂存

**matched_only 模式的特点**:
- 本地 SQLite 仍全量保存(不影响 HTML 报告生成)
- 如果分析失败(如 AI 筛选异常),暂存数据会被丢弃(不会同步到 Turso)
- Turso 中只有命中筛选的数据,存储量大幅减少
- `rank_history` 只记录命中条目的排名历史

**配置示例**:

```yaml
storage:
  turso:
    enabled: true
    sync_mode: "matched_only"   # 只存命中的
    sync_news: true
    sync_rss: true
```

---

## 6. 典型查询示例

### 6.1 查今天某平台的完整热榜

```sql
SELECT title, rank, url, mobile_url, crawl_count, last_crawl_time
FROM news_items
WHERE crawl_date = date('now', '+8 hours')  -- 北京时区
  AND platform_id = 'zhihu'
ORDER BY rank;
```

### 6.2 查最近 7 天所有平台的热榜(分页)

```sql
SELECT crawl_date, platform_id, title, rank, url
FROM news_items
WHERE crawl_date >= date('now', '+8 hours', '-7 days')
ORDER BY crawl_date DESC, platform_id, rank
LIMIT 100 OFFSET 0;
```

### 6.3 某条新闻的排名变化曲线

```sql
-- 先找到 news_item_id
SELECT id FROM news_items
WHERE title = ?
  AND platform_id = ?
  AND crawl_date = ?;

-- 再查排名历史
SELECT crawl_time, rank
FROM rank_history
WHERE news_item_id = ?
  AND crawl_date = ?
ORDER BY crawl_time;
```

### 6.4 跨日热度持续榜(连续上榜 N 天的新闻)

```sql
SELECT title, platform_id,
       COUNT(DISTINCT crawl_date) AS days,
       MIN(crawl_date) AS first_seen,
       MAX(crawl_date) AS last_seen
FROM news_items
WHERE crawl_date >= date('now', '+8 hours', '-7 days')
GROUP BY title, platform_id
HAVING days >= 3
ORDER BY days DESC, last_seen DESC;
```

### 6.5 各平台最近一次抓取的元数据

```sql
SELECT crawl_date, crawl_time, total_items
FROM crawl_records
ORDER BY crawl_date DESC, crawl_time DESC
LIMIT 20;
```

### 6.6 查最近 RSS 文章(按发布时间倒序)

```sql
SELECT i.title, i.url, i.published_at, i.summary, i.author,
       f.name AS feed_name
FROM rss_items i
JOIN rss_feeds f ON f.id = i.feed_id
WHERE i.crawl_date >= date('now', '+8 hours', '-3 days')
ORDER BY i.published_at DESC NULLS LAST
LIMIT 50;
```

### 6.7 获取所有平台列表

```sql
SELECT id, name FROM platforms WHERE is_active = 1 ORDER BY name;
```

### 6.8 按平台统计当天的总抓取次数

```sql
SELECT platform_id, COUNT(*) AS item_count, MAX(crawl_count) AS max_crawl_count
FROM news_items
WHERE crawl_date = date('now', '+8 hours')
GROUP BY platform_id
ORDER BY item_count DESC;
```

### 6.9 标题搜索(模糊查询)

```sql
SELECT crawl_date, platform_id, title, rank, url
FROM news_items
WHERE title LIKE '%AI%'
  AND crawl_date >= date('now', '+8 hours', '-7 days')
ORDER BY crawl_date DESC, rank
LIMIT 50;
```

### 6.10 获取所有表的列表(用于初始化校验)

```sql
SELECT name, type FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY type, name;
```

---

## 7. 去重与幂等性

### 7.1 去重规则

| 情况 | 判定 |
|---|---|
| 同平台同日,url 相同 | ✅ 同一条(命中 url 索引) |
| 同平台同日,url 都为空但 title 相同 | ✅ 同一条(命中 title 索引) |
| 同平台同日,title 相同但 url 不同 | ✅ 被判定为同一条(命中 title 索引) |
| 同平台同日,url 相同但 title 变了 | ⚠️ 可能冲突(取决于 ON CONFLICT 子句) |
| 不同日期 url 完全相同 | ❌ 视为两条(`crawl_date` 不同) |

### 7.2 幂等性保证

爬虫重复运行不会导致数据重复:

- `news_items` / `rss_items`:使用 `INSERT ... ON CONFLICT DO UPDATE`,重复执行只更新 `crawl_count`、`last_crawl_time`,不新增行
- `rank_history`:每次抓取追加一行(设计意图,用于排名趋势分析)
- `platforms` / `rss_feeds`:`INSERT OR IGNORE`,跨日共享同一行
- `crawl_records`:按 `(crawl_date, crawl_time)` 去重,重复执行只更新 `total_items`

### 7.3 已知限制

- **同平台同日 title 相同但 url 不同**会被合并为一条(实际场景罕见)
- **url 相同但 title 被编辑修改**的情况可能无法正确更新 title

如需更精确的去重逻辑,需要修改 [trendradar/storage/turso_sync.py](../trendradar/storage/turso_sync.py) 的 upsert SQL。

---

## 8. 常见问题

### Q1: Turso URL 是 `libsql://` 开头,能用吗?

可以。HTTP API 只认 `https://`,代码里需要做一次转换:

```python
url = os.environ["TURSO_URL"]
if url.startswith("libsql://"):
    url = "https://" + url[len("libsql://"):]
```

### Q2: 时区怎么处理?

Turso 服务器使用 UTC 时间。`date('now')` 返回 UTC 日期。若要按北京时间查询:

```sql
-- 北京时间今天
WHERE crawl_date = date('now', '+8 hours')

-- 北京时间最近 7 天
WHERE crawl_date >= date('now', '+8 hours', '-7 days')
```

TrendRadar 爬虫写入的 `crawl_date` 是按配置的时区(默认 `Asia/Shanghai`)生成的,所以直接按 `crawl_date` 字面值查询也是对的。

### Q3: 如何批量执行多个 SQL?

把多个 `execute` 请求放在同一个 `requests` 数组里,一次 HTTP 调用完成:

```json
{
  "requests": [
    {"type": "execute", "stmt": {"sql": "BEGIN"}},
    {"type": "execute", "stmt": {"sql": "SELECT ...", "args": [...]}},
    {"type": "execute", "stmt": {"sql": "SELECT ...", "args": [...]}},
    {"type": "execute", "stmt": {"sql": "COMMIT"}},
    {"type": "close"}
  ]
}
```

事务失败时追加 `ROLLBACK`:

```json
{"type": "execute", "stmt": {"sql": "ROLLBACK"}}
```

### Q4: 数据量增长会超 Turso 免费额度吗?

不会。Turso 免费版 9GB 总空间,按每月 50MB 增长估算,可用 180 个月。如需控制存储,可在 TrendRadar 配置 `storage.remote.retention_days` 自动清理历史数据(但目前 Turso 同步未实现自动清理,需手动 DELETE)。

### Q5: 我能直接写入 Turso 吗?

**不建议**。Turso 库的写入端由 TrendRadar 爬虫独占,外部写入会破坏 upsert 逻辑。如果只是读取,完全没问题。

### Q6: 如何获取某个时间点的榜单快照?

```sql
-- 获取 12:25 抓取的快照
SELECT title, platform_id, rank
FROM news_items
WHERE crawl_date = '2026-07-18'
  AND first_crawl_time <= '12-25'
  AND last_crawl_time >= '12-25'
ORDER BY platform_id, rank;
```

或用 `rank_history` 表:

```sql
SELECT rh.crawl_time, n.platform_id, n.title, rh.rank
FROM rank_history rh
JOIN news_items n ON n.id = rh.news_item_id
WHERE rh.crawl_date = '2026-07-18'
  AND rh.crawl_time = '12-25'
ORDER BY n.platform_id, rh.rank;
```

### Q7: 如何在本地测试连接?

```bash
# 设置环境变量
export TURSO_URL=https://<your-db-id>.turso.io
export TURSO_AUTH_TOKEN=<your-token>

# 用 curl 测试
curl -X POST $TURSO_URL/v2/pipeline \
  -H "Authorization: Bearer $TURSO_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"requests":[{"type":"execute","stmt":{"sql":"SELECT COUNT(*) FROM news_items"}},{"type":"close"}]}'
```

### Q8: 响应里 `rows` 是数组的数组,如何转成字典?

```python
def rows_to_dicts(response):
    result = response["response"]["result"]
    cols = [c["name"] for c in result["cols"]]
    rows = []
    for row in result["rows"]:
        row_dict = {}
        for col_name, cell in zip(cols, row):
            row_dict[col_name] = cell.get("value") if cell["type"] != "null" else None
            # 数字字段需要类型转换
            if cell["type"] == "integer" and row_dict[col_name] is not None:
                row_dict[col_name] = int(row_dict[col_name])
            elif cell["type"] == "float" and row_dict[col_name] is not None:
                row_dict[col_name] = float(row_dict[col_name])
        rows.append(row_dict)
    return rows
```

---

## 附录:参考链接

- [Turso 官方文档](https://docs.turso.tech/)
- [libSQL HTTP API 规范](https://docs.turso.tech/sdk/http/reference)
- [TrendRadar 项目仓库](https://github.com/sansan0/TrendRadar)
- [Turso Dashboard](https://turso.tech/app)

---

**文档版本**:1.0
**最后更新**:2026-07-18
**对应 TrendRadar 版本**:v6.10.0+
