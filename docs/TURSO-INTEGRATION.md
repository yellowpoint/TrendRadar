# TrendRadar Turso 数据库接入文档

本文档面向需要在其他应用中读取 TrendRadar 筛选结果(热榜 + RSS 命中条目)的开发者。

TrendRadar 通过 **Turso(libSQL)远程数据库**对外提供统一查询接口。简化设计:**只存储最终筛选出来的命中结果**(关键词匹配 / AI 筛选),热榜和 RSS 统一存到单表 `filtered_items`,通过 `source_type` 字段区分来源,不再分表。

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
┌─────────────────────────────────────┐         ┌──────────────────────┐
│  TrendRadar 爬虫                     │         │  你的另一个应用       │
│  (GitHub Actions 每小时)             │         │  (后端服务/BI/AI)    │
│                                     │         │                      │
│  1. 爬取数据(热榜 + RSS)             │         │  HTTP 查询           │
│  2. 写本地 SQLite(按日全量保存)     │         │  ↓                   │
│  3. 关键词匹配 / AI 筛选             │         │  Turso(libSQL)      │
│  4. 把命中结果同步到 Turso(单表)    │ ──────► │                      │
└─────────────────────────────────────┘  写入   └──────────────────────┘
                                                       ↑
                                                       │ 读取(SQL)
                                                       │
                                             ┌──────────────────────┐
                                             │  Turso 云端数据库     │
                                             │  - filtered_items    │
                                             │    (热榜 + RSS 统一) │
                                             └──────────────────────┘
```

**关键设计**:
- **只存筛选结果**:不再双写采集阶段的原始数据,只在分析流水线完成后同步命中条目
- **单表统一存储**:热榜和 RSS 共用 `filtered_items` 表,通过 `source_type` 字段区分
- **写入端**:TrendRadar 爬虫独占,只写不读
- **读取端**:你的应用只读不写
- **数据载体**:Turso 托管的 libSQL,通过 HTTP API 访问
- **协议**:标准 HTTP/HTTPS,无 SDK 依赖

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
        "sql": "SELECT * FROM filtered_items WHERE crawl_date = ?",
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
            {"type": "execute", "stmt": {"sql": "SELECT COUNT(*) FROM filtered_items"}},
            {"type": "close"},
        ]
    },
    timeout=60,
)

data = resp.json()
result = data["results"][0]["response"]["result"]
count = int(result["rows"][0][0]["value"])
print(f"命中条目总数: {count}")
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

Turso 库只有 **一张表** `filtered_items`,同时存放热榜和 RSS 的命中条目。

### 4.1 `filtered_items` 命中条目统一表

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `title` | TEXT NOT NULL | 标题(热榜新闻标题 / RSS 文章标题) |
| `url` | TEXT | 链接(可能为空,热榜偶尔为空) |
| `mobile_url` | TEXT | 移动端链接(仅热榜有,RSS 为空) |
| `source_id` | TEXT NOT NULL | 来源 ID(热榜:`ithome`/`zhihu` 等平台 ID;RSS:`hacker-news` 等 feed ID) |
| `source_name` | TEXT | 来源显示名称(如「IT之家」「Hacker News」) |
| `source_type` | TEXT NOT NULL | 来源类型:`hotlist` 或 `rss` |
| `rank` | INTEGER | 当前排名(热榜的排名,RSS 默认 0 或基于发布时间) |
| `relevance_score` | REAL | AI 筛选相关度评分(0.0~1.0,关键词匹配模式为 0) |
| `first_crawl_time` | TEXT | 首次抓取时间(热榜:HH:MM 格式;RSS:ISO 格式发布时间) |
| `last_crawl_time` | TEXT | 最后抓取时间(同上) |
| `crawl_date` | TEXT NOT NULL | **数据所属日期 YYYY-MM-DD**(跨日查询关键字段) |
| `crawl_time` | TEXT | 本次抓取的 HH:MM 时间 |
| `created_at` | TIMESTAMP | 入库时间 |
| `updated_at` | TIMESTAMP | 更新时间 |

**唯一索引**:
- `idx_filtered_unique`:`(source_id, crawl_date, title)` — 同源同日同标题视为同一行

**普通索引**:
- `idx_filtered_crawl_date`:`(crawl_date)`
- `idx_filtered_source`:`(source_id, source_type)`
- `idx_filtered_last_crawl`:`(last_crawl_time)`
- `idx_filtered_title_text`:`(title)`
- `idx_filtered_url`:`(url) WHERE url != ''`

### 4.2 来源标识对照

| `source_type` | `source_id` 示例 | `source_name` 示例 |
|---|---|---|
| `hotlist` | `ithome` / `zhihu` / `weibo` / `bilibili-hot-search` | IT之家 / 知乎 / 微博 / bilibili 热搜 |
| `rss` | `hacker-news` / `ifanr` / `techcrunch` | Hacker News / 爱范儿 / TechCrunch |

> **注意**:RSS 在关键词匹配模式下可能缺失 `source_id`,会用 `source_name` 作为 fallback。极端情况下 `source_id` 可能是 `rss`。

---

## 5. 数据更新规律

| 维度 | 行为 |
|---|---|
| **抓取频率** | GitHub Actions 每小时执行一次 |
| **同日同条命中** | `filtered_items` 行数不增长,`rank` / `last_crawl_time` / `relevance_score` 更新 |
| **跨日同条命中** | `filtered_items` **新增一行**(`crawl_date` 不同视为两条) |
| **每条记录大小** | < 1KB(纯文本字段) |
| **单日数据量** | 10-100 行(仅命中关键词/AI 筛选的条目) |
| **月累积数据量** | < 5MB |

### 5.1 同步时机

TrendRadar 的 Turso 同步**只在分析流水线完成后**触发一次:

1. 采集阶段:爬取数据 → 本地 SQLite 全量保存(Turso 不写入)
2. 分析阶段:关键词匹配 / AI 筛选 → 生成 `stats` 和 `rss_items`(命中结果)
3. 同步阶段:从 `stats` / `rss_items` 提取命中条目 → 批量 upsert 到 Turso `filtered_items`

**特点**:
- 本地 SQLite 仍全量保存(不影响 HTML 报告生成)
- Turso 中只有命中筛选的数据,存储量大幅减少
- 如果分析失败(如 AI 筛选异常),不会同步到 Turso
- 不再区分热榜/RSS 表,统一存到 `filtered_items`,通过 `source_type` 区分

**配置示例**:

```yaml
storage:
  turso:
    enabled: true
    url: "libsql://your-db.turso.io"      # 或用环境变量 TURSO_URL
    auth_token: "xxx"                      # 或用环境变量 TURSO_AUTH_TOKEN
```

---

## 6. 典型查询示例

### 6.1 查今天的所有命中条目

```sql
SELECT title, source_id, source_name, source_type, rank, url, relevance_score
FROM filtered_items
WHERE crawl_date = date('now', '+8 hours')  -- 北京时区
ORDER BY source_type, source_id, rank;
```

### 6.2 查最近 7 天的命中条目(分页)

```sql
SELECT crawl_date, source_type, source_id, source_name, title, rank, url
FROM filtered_items
WHERE crawl_date >= date('now', '+8 hours', '-7 days')
ORDER BY crawl_date DESC, source_type, source_id, rank
LIMIT 100 OFFSET 0;
```

### 6.3 只看热榜命中

```sql
SELECT crawl_date, source_id, source_name, title, rank, url, mobile_url
FROM filtered_items
WHERE source_type = 'hotlist'
  AND crawl_date = date('now', '+8 hours')
ORDER BY source_id, rank;
```

### 6.4 只看 RSS 命中

```sql
SELECT crawl_date, source_id, source_name, title, url, first_crawl_time AS published_at
FROM filtered_items
WHERE source_type = 'rss'
  AND crawl_date >= date('now', '+8 hours', '-3 days')
ORDER BY first_crawl_time DESC NULLS LAST
LIMIT 50;
```

### 6.5 跨日热度持续榜(连续上榜 N 天的命中条目)

```sql
SELECT title, source_id, source_name,
       COUNT(DISTINCT crawl_date) AS days,
       MIN(crawl_date) AS first_seen,
       MAX(crawl_date) AS last_seen,
       MAX(rank) AS best_rank
FROM filtered_items
WHERE source_type = 'hotlist'
  AND crawl_date >= date('now', '+8 hours', '-7 days')
GROUP BY title, source_id, source_name
HAVING days >= 3
ORDER BY days DESC, last_seen DESC;
```

### 6.6 按来源统计当天的命中数

```sql
SELECT source_type, source_id, source_name, COUNT(*) AS hit_count
FROM filtered_items
WHERE crawl_date = date('now', '+8 hours')
GROUP BY source_type, source_id, source_name
ORDER BY source_type, hit_count DESC;
```

### 6.7 标题搜索(模糊查询)

```sql
SELECT crawl_date, source_type, source_id, title, rank, url
FROM filtered_items
WHERE title LIKE '%AI%'
  AND crawl_date >= date('now', '+8 hours', '-7 days')
ORDER BY crawl_date DESC, source_type, rank
LIMIT 50;
```

### 6.8 高相关度命中(AI 筛选模式)

```sql
SELECT crawl_date, source_type, source_name, title, url, relevance_score
FROM filtered_items
WHERE relevance_score >= 0.8
  AND crawl_date >= date('now', '+8 hours', '-3 days')
ORDER BY relevance_score DESC, crawl_date DESC
LIMIT 30;
```

### 6.9 获取所有表的列表(用于初始化校验)

```sql
SELECT name, type FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY type, name;
```

---

## 7. 去重与幂等性

### 7.1 去重规则

| 情况 | 判定 |
|---|---|
| 同源同日同标题 | ✅ 同一条(命中 `idx_filtered_unique` 索引) |
| 同源同日 url 相同但 title 不同 | ❌ 视为两条(以 title 为去重键) |
| 同源同日 title 相同但 url 不同 | ✅ 同一条(url 会被新值覆盖) |
| 不同日期同源同标题 | ❌ 视为两条(`crawl_date` 不同) |

### 7.2 幂等性保证

爬虫重复运行不会导致数据重复:

- `filtered_items`:使用 `INSERT ... ON CONFLICT(source_id, crawl_date, title) DO UPDATE`,重复执行只更新 `rank`、`last_crawl_time`、`relevance_score` 等字段,不新增行
- 同一日内多次抓取:排名取最新值,相关度评分取最大值,首末时间用 COALESCE 保留非空值

### 7.3 已知限制

- **同源同日 title 相同但 url 不同**会被合并为一条(url 字段更新为最新的非空值)
- **url 相同但 title 被编辑修改**的情况会视为两条不同的记录

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

不会。Turso 免费版 9GB 总空间。简化设计后只存命中结果,每月增长 < 5MB,可用 1800+ 个月。如需控制存储,可定期手动 DELETE 历史数据(目前 Turso 同步未实现自动清理)。

### Q5: 我能直接写入 Turso 吗?

**不建议**。Turso 库的写入端由 TrendRadar 爬虫独占,外部写入会破坏 upsert 逻辑。如果只是读取,完全没问题。

### Q6: 为什么我看到的是单表 `filtered_items`,而不是多张表?

从 v6.x 起,Turso 同步逻辑已简化:
- **之前**:6 张表(`platforms` / `news_items` / `rank_history` / `crawl_records` / `rss_feeds` / `rss_items`),双写采集阶段全量数据
- **现在**:1 张表 `filtered_items`,只存最终筛选出来的命中结果,热榜和 RSS 统一存储

如果你的应用依赖旧的 6 表结构,需要适配新的单表 schema。旧表如果存在于历史库中不会被自动删除,但 TrendRadar 不再写入它们。

### Q7: 如何在本地测试连接?

```bash
# 设置环境变量
export TURSO_URL=https://<your-db-id>.turso.io
export TURSO_AUTH_TOKEN=<your-token>

# 用 curl 测试
curl -X POST $TURSO_URL/v2/pipeline \
  -H "Authorization: Bearer $TURSO_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"requests":[{"type":"execute","stmt":{"sql":"SELECT COUNT(*) FROM filtered_items"}},{"type":"close"}]}'
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

**文档版本**:2.0
**最后更新**:2026-07-19
**对应 TrendRadar 版本**:简化版 Turso 同步(单表 `filtered_items`)
