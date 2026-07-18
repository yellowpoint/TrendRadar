# coding=utf-8
"""
Turso 同步模块（HTTP API 实现）

在主存储流程保存数据后，将当天数据同步到 Turso (libSQL) 统一库，
便于其他后端服务跨日查询（无需下载多个按日分库的 .db 文件）。

设计原则：
- 与主存储解耦：Turso 写入失败只打印日志，不影响爬虫主流程
- 幂等 upsert：基于 (url, platform_id, crawl_date) / (guid, feed_id, crawl_date) 去重
- 复用现有 NewsData / RSSData 数据模型，不依赖具体存储后端
- 支持环境变量覆盖配置（用于 GitHub Actions Secrets 注入）
- 纯 HTTP API 实现：使用 requests 调用 Turso v2 pipeline 接口
  无需 Rust/MSVC 编译，零额外依赖（项目已有 requests）
"""

import base64
import os
from typing import Optional, Dict, List, Any, Tuple

import requests

from trendradar.storage.base import NewsData, RSSData


# Turso 统一库的 schema 语句列表
# 注意：Turso HTTP API 的 execute 不支持多条 SQL 拼接，必须逐条执行
TURSO_SCHEMA_STATEMENTS = [
    # 平台表（跨日共享，按 id upsert）
    """
    CREATE TABLE IF NOT EXISTS platforms (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # 新闻条目表（跨日累积，通过 url + platform_id + crawl_date 唯一去重）
    """
    CREATE TABLE IF NOT EXISTS news_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        platform_id TEXT NOT NULL,
        rank INTEGER NOT NULL,
        url TEXT DEFAULT '',
        mobile_url TEXT DEFAULT '',
        first_crawl_time TEXT NOT NULL,
        last_crawl_time TEXT NOT NULL,
        crawl_count INTEGER DEFAULT 1,
        crawl_date TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (platform_id) REFERENCES platforms(id)
    )
    """,
    # 排名历史表
    """
    CREATE TABLE IF NOT EXISTS rank_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        news_item_id INTEGER NOT NULL,
        rank INTEGER NOT NULL,
        crawl_time TEXT NOT NULL,
        crawl_date TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (news_item_id) REFERENCES news_items(id)
    )
    """,
    # 抓取批次记录表（按 crawl_date + crawl_time 唯一）
    """
    CREATE TABLE IF NOT EXISTS crawl_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        crawl_date TEXT NOT NULL,
        crawl_time TEXT NOT NULL,
        total_items INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(crawl_date, crawl_time)
    )
    """,
    # RSS 源表（跨日共享）
    """
    CREATE TABLE IF NOT EXISTS rss_feeds (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # RSS 条目表（跨日累积，通过 url + feed_id + crawl_date 去重）
    """
    CREATE TABLE IF NOT EXISTS rss_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        feed_id TEXT NOT NULL,
        url TEXT NOT NULL,
        guid TEXT DEFAULT '',
        published_at TEXT,
        summary TEXT,
        author TEXT,
        first_crawl_time TEXT NOT NULL,
        last_crawl_time TEXT NOT NULL,
        crawl_count INTEGER DEFAULT 1,
        crawl_date TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (feed_id) REFERENCES rss_feeds(id)
    )
    """,
    # 索引
    "CREATE INDEX IF NOT EXISTS idx_news_platform ON news_items(platform_id)",
    "CREATE INDEX IF NOT EXISTS idx_news_crawl_date ON news_items(crawl_date)",
    "CREATE INDEX IF NOT EXISTS idx_news_last_crawl ON news_items(last_crawl_time)",
    "CREATE INDEX IF NOT EXISTS idx_news_title ON news_items(title)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url_platform_date ON news_items(url, platform_id, crawl_date) WHERE url != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_title_platform_date ON news_items(title, platform_id, crawl_date)",
    "CREATE INDEX IF NOT EXISTS idx_rank_history_news ON rank_history(news_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_rss_feed ON rss_items(feed_id)",
    "CREATE INDEX IF NOT EXISTS idx_rss_crawl_date ON rss_items(crawl_date)",
    "CREATE INDEX IF NOT EXISTS idx_rss_published ON rss_items(published_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rss_title ON rss_items(title)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_url_feed_date ON rss_items(url, feed_id, crawl_date)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_guid_feed_date ON rss_items(guid, feed_id, crawl_date) WHERE guid != ''",
]


class TursoSyncService:
    """
    Turso 同步服务（HTTP API 实现）

    用法：
        service = TursoSyncService(url="libsql://xxx.turso.io", auth_token="xxx")
        service.sync_news_data(news_data)
        service.sync_rss_data(rss_data)
        service.cleanup()

    同步策略：
    - 平台/RSS 源信息：INSERT OR IGNORE（首次写入后不再更新）
    - 新闻/RSS 条目：基于唯一索引 ON CONFLICT DO UPDATE（upsert）
    - 排名历史：每次抓取追加新行
    - 批量提交：单次抓取的所有 SQL 在一个事务中执行，一次 HTTP 请求完成
    """

    def __init__(
        self,
        url: str,
        auth_token: str,
        sync_news: bool = True,
        sync_rss: bool = True,
    ):
        """
        初始化 Turso 同步服务

        Args:
            url: libSQL 连接 URL（libsql:// 或 https:// 均可）
            auth_token: Turso auth token
            sync_news: 是否同步热榜数据
            sync_rss: 是否同步 RSS 数据
        """
        self.url = self._normalize_url(url)
        self.auth_token = auth_token
        self.sync_news_enabled = sync_news
        self.sync_rss_enabled = sync_rss
        self._session: Optional[requests.Session] = None
        self._enabled = True
        self._initialized = False

        if not url or not auth_token:
            print("[Turso 同步] 未配置 url 或 auth_token，同步功能禁用")
            self._enabled = False
            return

        try:
            self._session = requests.Session()
            self._session.headers.update({
                "Authorization": f"Bearer {auth_token}",
                "Content-Type": "application/json",
            })
            self._init_schema()
            self._initialized = True
            print(f"[Turso 同步] 初始化完成: {self.url}")
        except Exception as e:
            print(f"[Turso 同步] 连接初始化失败: {e}")
            self._enabled = False

    @staticmethod
    def _normalize_url(url: str) -> str:
        """把 libsql:// / ws:// 转 https://"""
        if url.startswith("libsql://"):
            return "https://" + url[len("libsql://"):]
        if url.startswith("ws://"):
            return "http://" + url[len("ws://"):]
        if not url.startswith(("http://", "https://")):
            return "https://" + url
        return url.rstrip("/")

    def _init_schema(self) -> None:
        """初始化数据库 schema（幂等，逐条执行）"""
        try:
            requests_list: List[Dict[str, Any]] = []
            for sql in TURSO_SCHEMA_STATEMENTS:
                requests_list.append({
                    "type": "execute",
                    "stmt": {"sql": sql.strip()},
                })
            requests_list.append({"type": "close"})
            self._post({"requests": requests_list})
        except Exception as e:
            print(f"[Turso 同步] schema 初始化失败: {e}")
            raise

    @staticmethod
    def _convert_arg(value: Any) -> Dict[str, Any]:
        """Python 值转 Turso HTTP API 参数格式"""
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "integer", "value": "1" if value else "0"}
        if isinstance(value, int):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, float):
            return {"type": "float", "value": str(value)}
        if isinstance(value, bytes):
            return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
        return {"type": "text", "value": str(value)}

    def _execute_batch(self, statements: List[Tuple[str, List[Any]]]) -> None:
        """
        在一个事务中批量执行 SQL

        Args:
            statements: [(sql, args), ...] 列表
        """
        requests_list: List[Dict[str, Any]] = [
            {"type": "execute", "stmt": {"sql": "BEGIN"}}
        ]
        for sql, args in statements:
            stmt: Dict[str, Any] = {"sql": sql}
            if args:
                stmt["args"] = [self._convert_arg(a) for a in args]
            requests_list.append({"type": "execute", "stmt": stmt})
        requests_list.append({"type": "execute", "stmt": {"sql": "COMMIT"}})
        requests_list.append({"type": "close"})

        try:
            self._post({"requests": requests_list})
        except Exception:
            # 事务失败时尝试回滚（best effort）
            try:
                self._post({
                    "requests": [
                        {"type": "execute", "stmt": {"sql": "ROLLBACK"}},
                        {"type": "close"},
                    ]
                })
            except Exception:
                pass
            raise

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用 Turso v2 pipeline API"""
        endpoint = f"{self.url}/v2/pipeline"
        resp = self._session.post(endpoint, json=payload, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Turso HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        # 检查每条结果是否有错误
        for result in data.get("results", []):
            if result.get("type") == "error":
                err_msg = result.get("error", {}).get("message", "unknown error")
                raise RuntimeError(f"Turso SQL 错误: {err_msg}")
        return data

    def sync_news_data(self, data: NewsData) -> bool:
        """同步热榜数据到 Turso"""
        if not self._enabled or not self.sync_news_enabled:
            return False
        if not data or not data.items:
            return True

        crawl_date = data.date
        crawl_time = data.crawl_time

        try:
            statements: List[Tuple[str, List[Any]]] = []

            # 1. upsert 平台信息
            for source_id in data.items.keys():
                source_name = data.id_to_name.get(source_id, source_id)
                statements.append((
                    "INSERT OR IGNORE INTO platforms (id, name, is_active) VALUES (?, ?, 1)",
                    [source_id, source_name],
                ))

            # 2. upsert 新闻条目（不追加 rank_history，单独查询 id）
            for source_id, news_list in data.items.items():
                for item in news_list:
                    statements.append((
                        """
                        INSERT INTO news_items
                            (title, platform_id, rank, url, mobile_url,
                             first_crawl_time, last_crawl_time, crawl_count, crawl_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(title, platform_id, crawl_date) DO UPDATE SET
                            rank = excluded.rank,
                            url = COALESCE(NULLIF(excluded.url, ''), news_items.url),
                            mobile_url = COALESCE(NULLIF(excluded.mobile_url, ''), news_items.mobile_url),
                            last_crawl_time = excluded.last_crawl_time,
                            crawl_count = news_items.crawl_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        [
                            item.title,
                            source_id,
                            item.rank,
                            item.url,
                            item.mobile_url,
                            item.first_time or crawl_time,
                            item.last_time or crawl_time,
                            item.count if item.count > 0 else 1,
                            crawl_date,
                        ],
                    ))
                    # 排名历史（无需回查 id，子查询获取，简化批量流程）
                    statements.append((
                        """
                        INSERT INTO rank_history (news_item_id, rank, crawl_time, crawl_date)
                        SELECT id, ?, ?, ? FROM news_items
                        WHERE title = ? AND platform_id = ? AND crawl_date = ?
                        """,
                        [
                            item.rank,
                            crawl_time,
                            crawl_date,
                            item.title,
                            source_id,
                            crawl_date,
                        ],
                    ))

            # 3. 记录本次抓取批次
            total_items = sum(len(v) for v in data.items.values())
            statements.append((
                """
                INSERT INTO crawl_records (crawl_date, crawl_time, total_items)
                VALUES (?, ?, ?)
                ON CONFLICT(crawl_date, crawl_time) DO UPDATE SET
                    total_items = excluded.total_items
                """,
                [crawl_date, crawl_time, total_items],
            ))

            self._execute_batch(statements)
            print(f"[Turso 同步] 已同步 {total_items} 条新闻到 Turso (date={crawl_date}, time={crawl_time})")
            return True
        except Exception as e:
            print(f"[Turso 同步] 同步新闻数据失败: {e}")
            return False

    def sync_rss_data(self, data: RSSData) -> bool:
        """同步 RSS 数据到 Turso"""
        if not self._enabled or not self.sync_rss_enabled:
            return False
        if not data or not data.items:
            return True

        crawl_date = data.date
        crawl_time = data.crawl_time

        try:
            statements: List[Tuple[str, List[Any]]] = []

            # 1. upsert RSS 源信息
            for feed_id in data.items.keys():
                feed_name = data.id_to_name.get(feed_id, feed_id)
                statements.append((
                    "INSERT OR IGNORE INTO rss_feeds (id, name, is_active) VALUES (?, ?, 1)",
                    [feed_id, feed_name],
                ))

            # 2. upsert RSS 条目
            total_items = 0
            for feed_id, rss_list in data.items.items():
                for item in rss_list:
                    statements.append((
                        """
                        INSERT INTO rss_items
                            (title, feed_id, url, guid, published_at, summary, author,
                             first_crawl_time, last_crawl_time, crawl_count, crawl_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(url, feed_id, crawl_date) DO UPDATE SET
                            title = excluded.title,
                            guid = COALESCE(NULLIF(excluded.guid, ''), rss_items.guid),
                            published_at = COALESCE(NULLIF(excluded.published_at, ''), rss_items.published_at),
                            summary = COALESCE(NULLIF(excluded.summary, ''), rss_items.summary),
                            author = COALESCE(NULLIF(excluded.author, ''), rss_items.author),
                            last_crawl_time = excluded.last_crawl_time,
                            crawl_count = rss_items.crawl_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        [
                            item.title,
                            feed_id,
                            item.url,
                            item.guid,
                            item.published_at,
                            item.summary,
                            item.author,
                            item.first_time or crawl_time,
                            item.last_time or crawl_time,
                            item.count if item.count > 0 else 1,
                            crawl_date,
                        ],
                    ))
                    total_items += 1

            self._execute_batch(statements)
            print(f"[Turso 同步] 已同步 {total_items} 条 RSS 到 Turso (date={crawl_date}, time={crawl_time})")
            return True
        except Exception as e:
            print(f"[Turso 同步] 同步 RSS 数据失败: {e}")
            return False

    def cleanup(self) -> None:
        """清理资源"""
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    @property
    def enabled(self) -> bool:
        """是否启用"""
        return self._enabled and self._initialized


def create_turso_service_from_config(
    turso_config: Dict[str, Any],
) -> Optional[TursoSyncService]:
    """
    从配置字典创建 Turso 同步服务

    优先级：配置文件字段 > 环境变量

    Args:
        turso_config: 配置字典，包含 enabled / url / auth_token / sync_news / sync_rss

    Returns:
        TursoSyncService 实例，未启用或配置缺失时返回 None
    """
    if not turso_config:
        return None

    # 环境变量覆盖配置（用于 GitHub Actions）
    enabled = (
        turso_config.get("enabled", False)
        or os.environ.get("TURSO_ENABLED", "").lower() in ("true", "1", "yes")
    )
    if not enabled:
        return None

    url = turso_config.get("url", "") or os.environ.get("TURSO_URL", "")
    auth_token = (
        turso_config.get("auth_token", "")
        or os.environ.get("TURSO_AUTH_TOKEN", "")
    )

    if not url or not auth_token:
        print("[Turso 同步] 已启用但未配置 url 或 auth_token，跳过初始化")
        return None

    return TursoSyncService(
        url=url,
        auth_token=auth_token,
        sync_news=turso_config.get("sync_news", True),
        sync_rss=turso_config.get("sync_rss", True),
    )
