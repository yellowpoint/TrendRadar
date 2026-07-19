# coding=utf-8
"""
Turso 同步模块（HTTP API 实现）

简化设计：只存储最终筛选出来的命中结果（热榜 + RSS 统一存储到单表 filtered_items）。
- 不分热榜/RSS 表，通过 source_type 字段区分来源
- 不再在采集阶段暂存或双写，仅在分析流水线完成后调用 sync_filtered_items
- 幂等 upsert：基于 (source_id, crawl_date, title) 去重，重复抓取更新最新状态
- 与主存储解耦：Turso 写入失败只打印日志，不影响爬虫主流程
- 纯 HTTP API 实现：使用 requests 调用 Turso v2 pipeline 接口
  无需 Rust/MSVC 编译，零额外依赖（项目已有 requests）
"""

import base64
import os
from typing import Optional, Dict, List, Any, Tuple

import requests


# Turso 统一库的 schema 语句列表
# 注意：Turso HTTP API 的 execute 不支持多条 SQL 拼接，必须逐条执行
TURSO_SCHEMA_STATEMENTS = [
    # 命中结果统一表（热榜 + RSS 共用）
    """
    CREATE TABLE IF NOT EXISTS filtered_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        url TEXT DEFAULT '',
        mobile_url TEXT DEFAULT '',
        source_id TEXT NOT NULL,
        source_name TEXT DEFAULT '',
        source_type TEXT NOT NULL DEFAULT 'hotlist',
        rank INTEGER DEFAULT 0,
        relevance_score REAL DEFAULT 0,
        first_crawl_time TEXT,
        last_crawl_time TEXT,
        crawl_date TEXT NOT NULL,
        crawl_time TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # 同源同日同标题视为同一行（跨日累积时同标题会因 crawl_date 不同而各占一行）
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_filtered_unique ON filtered_items(source_id, crawl_date, title)",
    "CREATE INDEX IF NOT EXISTS idx_filtered_crawl_date ON filtered_items(crawl_date)",
    "CREATE INDEX IF NOT EXISTS idx_filtered_source ON filtered_items(source_id, source_type)",
    "CREATE INDEX IF NOT EXISTS idx_filtered_last_crawl ON filtered_items(last_crawl_time)",
    "CREATE INDEX IF NOT EXISTS idx_filtered_title_text ON filtered_items(title)",
    "CREATE INDEX IF NOT EXISTS idx_filtered_url ON filtered_items(url) WHERE url != ''",
    # 给第三方使用：补 published_at 字段（文章真实发布时间，热榜存榜单时间，RSS 存发布时间）
    # ALTER TABLE 不支持 IF NOT EXISTS，单独在 _init_schema 中容错执行
]


class TursoSyncService:
    """
    Turso 同步服务（HTTP API 实现）

    用法：
        service = TursoSyncService(url="libsql://xxx.turso.io", auth_token="xxx")
        service.sync_filtered_items(items)  # items 为命中数据字典列表
        service.cleanup()

    同步策略：
    - 只接收筛选后的命中数据（热榜 + RSS 统一存储到 filtered_items 表）
    - 基于 (source_id, crawl_date, title) 唯一索引 upsert
    - 重复抓取更新 rank / relevance_score / last_crawl_time 等字段
    - 批量提交：单次抓取的所有 SQL 在一个事务中执行，一次 HTTP 请求完成
    """

    def __init__(self, url: str, auth_token: str):
        """
        初始化 Turso 同步服务

        Args:
            url: libSQL 连接 URL（libsql:// 或 https:// 均可）
            auth_token: Turso auth token
        """
        self.url = self._normalize_url(url)
        self.auth_token = auth_token
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

        # 单独执行 ALTER TABLE ADD COLUMN，列已存在时容错跳过
        # libSQL 不支持 ALTER TABLE ADD COLUMN IF NOT EXISTS
        try:
            self._post({
                "requests": [
                    {"type": "execute", "stmt": {"sql": "ALTER TABLE filtered_items ADD COLUMN published_at TEXT"}},
                    {"type": "close"},
                ]
            })
        except Exception as e:
            err_str = str(e)
            # 列已存在属于预期情况，不视为错误
            if "duplicate column" in err_str.lower() or "already exists" in err_str.lower():
                pass
            else:
                print(f"[Turso 同步] 添加 published_at 列失败: {e}")

    @staticmethod
    def _convert_arg(value: Any) -> Dict[str, Any]:
        """Python 值转 Turso HTTP API 参数格式

        注意：float 类型的 value 必须是 JSON 数字而非字符串。
        Turso 服务端（Rust serde）反序列化 float 类型时期望 f64，
        传字符串会报 "expected f64" 错误，与官方文档"value 是 String"描述不符。
        integer 类型用字符串 value 是 OK 的（服务端会转换）。
        """
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "integer", "value": "1" if value else "0"}
        if isinstance(value, int):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, float):
            # float 用数字 value，不能用字符串（Turso 服务端会报 expected f64）
            return {"type": "float", "value": value}
        if isinstance(value, bytes):
            return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
        # 字符串形如数字时按数字处理（AI 返回 relevance_score 常为 "0.95" 字符串）
        if isinstance(value, str):
            # 排除空字符串和纯空白
            stripped = value.strip()
            if not stripped:
                return {"type": "text", "value": value}
            try:
                # 整数形式 → integer 字符串 value（与原生 int 保持一致）
                if "." not in stripped and "e" not in stripped.lower():
                    return {"type": "integer", "value": str(int(stripped))}
                # 浮点形式 → float 数字 value（不能用字符串）
                return {"type": "float", "value": float(stripped)}
            except (ValueError, TypeError):
                return {"type": "text", "value": value}
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

    def sync_filtered_items(self, items: List[Dict[str, Any]]) -> bool:
        """
        同步筛选后的命中条目到 Turso（热榜 + RSS 统一入口）

        Args:
            items: 命中数据字典列表，每个字典应包含以下字段（缺失用默认值）：
                - title (str, 必填)
                - url (str, 可空)
                - mobile_url (str, 可空)
                - source_id (str, 必填)
                - source_name (str, 可空)
                - source_type (str, 'hotlist' 或 'rss', 默认 'hotlist')
                - rank (int, 默认 0)
                - relevance_score (float, 默认 0)
                - first_time (str, 可空)
                - last_time (str, 可空)
                - crawl_date (str, 必填, YYYY-MM-DD)
                - crawl_time (str, 可空, HH:MM)
                - published_at (str, 可空, 文章真实发布时间 ISO 格式)

        Returns:
            True 表示成功（或无数据），False 表示失败
        """
        if not self._enabled:
            return False
        if not items:
            return True

        try:
            statements: List[Tuple[str, List[Any]]] = []
            for item in items:
                title = item.get("title", "")
                if not title:
                    continue
                source_id = item.get("source_id", "")
                if not source_id:
                    continue
                crawl_date = item.get("crawl_date", "")
                if not crawl_date:
                    continue

                statements.append((
                    """
                    INSERT INTO filtered_items
                        (title, url, mobile_url, source_id, source_name, source_type,
                         rank, relevance_score, first_crawl_time, last_crawl_time,
                         crawl_date, crawl_time, published_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, crawl_date, title) DO UPDATE SET
                        url = COALESCE(NULLIF(excluded.url, ''), filtered_items.url),
                        mobile_url = COALESCE(NULLIF(excluded.mobile_url, ''), filtered_items.mobile_url),
                        source_name = COALESCE(NULLIF(excluded.source_name, ''), filtered_items.source_name),
                        rank = excluded.rank,
                        relevance_score = MAX(excluded.relevance_score, filtered_items.relevance_score),
                        first_crawl_time = COALESCE(NULLIF(filtered_items.first_crawl_time, ''), excluded.first_crawl_time),
                        last_crawl_time = COALESCE(NULLIF(excluded.last_crawl_time, ''), filtered_items.last_crawl_time),
                        crawl_time = COALESCE(NULLIF(excluded.crawl_time, ''), filtered_items.crawl_time),
                        published_at = COALESCE(NULLIF(excluded.published_at, ''), filtered_items.published_at),
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    [
                        title,
                        item.get("url", "") or "",
                        item.get("mobile_url", "") or "",
                        source_id,
                        item.get("source_name", "") or "",
                        item.get("source_type", "hotlist") or "hotlist",
                        item.get("rank", 0) or 0,
                        item.get("relevance_score", 0) or 0,
                        item.get("first_time", "") or "",
                        item.get("last_time", "") or "",
                        crawl_date,
                        item.get("crawl_time", "") or "",
                        item.get("published_at", "") or item.get("first_time", "") or "",
                    ],
                ))

            if not statements:
                return True

            self._execute_batch(statements)
            print(f"[Turso 同步] 已同步 {len(statements)} 条命中条目到 Turso")
            return True
        except Exception as e:
            print(f"[Turso 同步] 同步命中数据失败: {e}")
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
        turso_config: 配置字典，包含 enabled / url / auth_token

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

    return TursoSyncService(url=url, auth_token=auth_token)
