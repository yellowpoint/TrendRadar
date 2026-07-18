# coding=utf-8
"""
Turso 同步模块

在主存储流程保存数据后，将当天数据同步到 Turso (libSQL) 统一库，
便于其他后端服务跨日查询（无需下载多个按日分库的 .db 文件）。

设计原则：
- 与主存储解耦：Turso 写入失败只打印日志，不影响爬虫主流程
- 幂等 upsert：基于 (url, platform_id, crawl_date) / (guid, feed_id, crawl_date) 去重
- 复用现有 NewsData / RSSData 数据模型，不依赖具体存储后端
- 支持环境变量覆盖配置（用于 GitHub Actions Secrets 注入）
"""

import os
from typing import Optional, Dict, List, Any

from trendradar.storage.base import NewsData, RSSData


# Turso 统一库的 schema（与本地 schema.sql/rss_schema.sql 类似，但增加 crawl_date 字段）
TURSO_SCHEMA_SQL = """
-- 平台表（跨日共享，按 id upsert）
CREATE TABLE IF NOT EXISTS platforms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 新闻条目表（跨日累积，通过 url + platform_id + crawl_date 唯一去重）
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
    crawl_date TEXT NOT NULL,                -- 数据所属日期 YYYY-MM-DD（跨日查询关键字段）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (platform_id) REFERENCES platforms(id)
);

-- 排名历史表
CREATE TABLE IF NOT EXISTS rank_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_item_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    crawl_time TEXT NOT NULL,
    crawl_date TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (news_item_id) REFERENCES news_items(id)
);

-- 抓取批次记录表（按 crawl_date + crawl_time 唯一）
CREATE TABLE IF NOT EXISTS crawl_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_date TEXT NOT NULL,
    crawl_time TEXT NOT NULL,
    total_items INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(crawl_date, crawl_time)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_news_platform ON news_items(platform_id);
CREATE INDEX IF NOT EXISTS idx_news_crawl_date ON news_items(crawl_date);
CREATE INDEX IF NOT EXISTS idx_news_last_crawl ON news_items(last_crawl_time);
CREATE INDEX IF NOT EXISTS idx_news_title ON news_items(title);
CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url_platform_date
    ON news_items(url, platform_id, crawl_date) WHERE url != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_news_title_platform_date
    ON news_items(title, platform_id, crawl_date);
CREATE INDEX IF NOT EXISTS idx_rank_history_news ON rank_history(news_item_id);

-- RSS 源表（跨日共享）
CREATE TABLE IF NOT EXISTS rss_feeds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- RSS 条目表（跨日累积，通过 url + feed_id + crawl_date 去重）
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
);

CREATE INDEX IF NOT EXISTS idx_rss_feed ON rss_items(feed_id);
CREATE INDEX IF NOT EXISTS idx_rss_crawl_date ON rss_items(crawl_date);
CREATE INDEX IF NOT EXISTS idx_rss_published ON rss_items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_rss_title ON rss_items(title);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_url_feed_date
    ON rss_items(url, feed_id, crawl_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_guid_feed_date
    ON rss_items(guid, feed_id, crawl_date) WHERE guid != '';
"""


class TursoSyncService:
    """
    Turso 同步服务

    用法：
        service = TursoSyncService(url="libsql://xxx.turso.io", auth_token="xxx")
        service.sync_news_data(news_data)   # 同步热榜
        service.sync_rss_data(rss_data)     # 同步 RSS
        service.cleanup()

    同步策略：
    - 平台/RSS 源信息：INSERT OR IGNORE（首次写入后不再更新）
    - 新闻/RSS 条目：基于唯一索引 ON CONFLICT DO UPDATE（upsert）
    - 排名历史：每次抓取追加新行
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
            url: libSQL 连接 URL（如 libsql://xxx.turso.io）
            auth_token: Turso auth token
            sync_news: 是否同步热榜数据
            sync_rss: 是否同步 RSS 数据
        """
        self.url = url
        self.auth_token = auth_token
        self.sync_news_enabled = sync_news
        self.sync_rss_enabled = sync_rss
        self._conn = None
        self._initialized = False
        self._enabled = True

        if not url or not auth_token:
            print("[Turso 同步] 未配置 url 或 auth_token，同步功能禁用")
            self._enabled = False
            return

        try:
            import libsql_experimental as libsql
        except ImportError:
            print("[Turso 同步] libsql-experimental 未安装，同步功能禁用")
            print("[Turso 同步] 请运行: uv add libsql-experimental  或  pip install libsql-experimental")
            self._enabled = False
            return

        try:
            self._libsql = libsql
            self._conn = libsql.connect(url, auth_token=auth_token)
            self._init_schema()
            self._initialized = True
            print(f"[Turso 同步] 初始化完成: {url}")
        except Exception as e:
            print(f"[Turso 同步] 连接初始化失败: {e}")
            self._enabled = False

    def _init_schema(self) -> None:
        """初始化数据库 schema（幂等）"""
        if not self._conn:
            return
        try:
            self._conn.executescript(TURSO_SCHEMA_SQL)
            self._conn.commit()
        except Exception as e:
            print(f"[Turso 同步] schema 初始化失败: {e}")
            raise

    def sync_news_data(self, data: NewsData) -> bool:
        """
        同步热榜数据到 Turso

        Args:
            data: 新闻数据

        Returns:
            是否同步成功
        """
        if not self._enabled or not self.sync_news_enabled:
            return False

        if not data or not data.items:
            return True

        crawl_date = data.date
        crawl_time = data.crawl_time

        try:
            # 1. upsert 平台信息
            platform_ids = list(data.items.keys())
            for source_id in platform_ids:
                source_name = data.id_to_name.get(source_id, source_id)
                self._conn.execute(
                    "INSERT OR IGNORE INTO platforms (id, name, is_active) VALUES (?, ?, 1)",
                    (source_id, source_name),
                )

            # 2. upsert 新闻条目 + 排名历史
            total_items = 0
            for source_id, news_list in data.items.items():
                for item in news_list:
                    # upsert 新闻条目（基于 url+platform_id+date 或 title+platform_id+date）
                    self._conn.execute(
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
                        (
                            item.title,
                            source_id,
                            item.rank,
                            item.url,
                            item.mobile_url,
                            item.first_time or crawl_time,
                            item.last_time or crawl_time,
                            item.count if item.count > 0 else 1,
                            crawl_date,
                        ),
                    )

                    # 获取 news_item id（无论新增还是已存在）
                    row = self._conn.execute(
                        "SELECT id FROM news_items WHERE title = ? AND platform_id = ? AND crawl_date = ?",
                        (item.title, source_id, crawl_date),
                    ).fetchone()
                    if row:
                        news_item_id = row[0]
                        # 追加本次抓取的排名历史
                        self._conn.execute(
                            """
                            INSERT INTO rank_history (news_item_id, rank, crawl_time, crawl_date)
                            VALUES (?, ?, ?, ?)
                            """,
                            (news_item_id, item.rank, crawl_time, crawl_date),
                        )

                    total_items += 1

            # 3. 记录本次抓取批次
            self._conn.execute(
                """
                INSERT INTO crawl_records (crawl_date, crawl_time, total_items)
                VALUES (?, ?, ?)
                ON CONFLICT(crawl_date, crawl_time) DO UPDATE SET
                    total_items = excluded.total_items
                """,
                (crawl_date, crawl_time, total_items),
            )

            self._conn.commit()
            print(f"[Turso 同步] 已同步 {total_items} 条新闻到 Turso (date={crawl_date}, time={crawl_time})")
            return True
        except Exception as e:
            print(f"[Turso 同步] 同步新闻数据失败: {e}")
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False

    def sync_rss_data(self, data: RSSData) -> bool:
        """
        同步 RSS 数据到 Turso

        Args:
            data: RSS 数据

        Returns:
            是否同步成功
        """
        if not self._enabled or not self.sync_rss_enabled:
            return False

        if not data or not data.items:
            return True

        crawl_date = data.date
        crawl_time = data.crawl_time

        try:
            # 1. upsert RSS 源信息
            for feed_id, rss_list in data.items.items():
                feed_name = data.id_to_name.get(feed_id, feed_id)
                self._conn.execute(
                    "INSERT OR IGNORE INTO rss_feeds (id, name, is_active) VALUES (?, ?, 1)",
                    (feed_id, feed_name),
                )

            # 2. upsert RSS 条目
            total_items = 0
            for feed_id, rss_list in data.items.items():
                for item in rss_list:
                    self._conn.execute(
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
                        (
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
                        ),
                    )
                    total_items += 1

            self._conn.commit()
            print(f"[Turso 同步] 已同步 {total_items} 条 RSS 到 Turso (date={crawl_date}, time={crawl_time})")
            return True
        except Exception as e:
            print(f"[Turso 同步] 同步 RSS 数据失败: {e}")
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False

    def cleanup(self) -> None:
        """清理资源"""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

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
