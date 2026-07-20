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
        dedup_key TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # 去重索引：dedup_key 由调用方计算
    #   - URL 非空（RSS / 带链接热榜）: dedup_key = url  → 跨日同文 upsert 到同一行
    #   - URL 为空（无链接热榜）:        dedup_key = "crawl_date|title" → 同日同文 upsert 到同一行
    # 这样 RSS 源站修改标题后再次抓取不会产生重复行
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_filtered_unique ON filtered_items(source_id, dedup_key)",
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
        for col_sql in (
            "ALTER TABLE filtered_items ADD COLUMN published_at TEXT",
            "ALTER TABLE filtered_items ADD COLUMN dedup_key TEXT NOT NULL DEFAULT ''",
        ):
            try:
                self._post({
                    "requests": [
                        {"type": "execute", "stmt": {"sql": col_sql}},
                        {"type": "close"},
                    ]
                })
            except Exception as e:
                err_str = str(e)
                # 列已存在属于预期情况，不视为错误
                if "duplicate column" in err_str.lower() or "already exists" in err_str.lower():
                    pass
                else:
                    print(f"[Turso 同步] 添加列失败 ({col_sql}): {e}")

        # 从旧 schema (source_id, crawl_date, title) 迁移到新 schema (source_id, dedup_key)
        # 步骤：回填 dedup_key → 删除重复行 → 重建唯一索引
        self._migrate_dedup_key()

    def _migrate_dedup_key(self) -> None:
        """从旧去重键 (source_id, crawl_date, title) 迁移到新去重键 (source_id, dedup_key)

        幂等：dedup_key 已回填/索引已重建时直接跳过。
        """
        try:
            # 1. 回填 dedup_key：URL 非空用 URL，否则用 "crawl_date|title"
            self._post({
                "requests": [
                    {"type": "execute", "stmt": {"sql":
                        "UPDATE filtered_items SET dedup_key = url "
                        "WHERE (dedup_key IS NULL OR dedup_key = '') AND url != ''"
                    }},
                    {"type": "execute", "stmt": {"sql":
                        "UPDATE filtered_items SET dedup_key = crawl_date || '|' || title "
                        "WHERE (dedup_key IS NULL OR dedup_key = '') AND (url = '' OR url IS NULL)"
                    }},
                    {"type": "close"},
                ]
            })
        except Exception as e:
            print(f"[Turso 同步] 回填 dedup_key 失败: {e}")
            # 不 raise，后续索引重建可能仍能成功

        # 2. 删除重复行：同一 (source_id, dedup_key) 保留 relevance_score 最高（平局取最新 id）
        #    只在旧索引仍存在时执行（迁移标志）
        try:
            self._post({
                "requests": [
                    {"type": "execute", "stmt": {"sql":
                        "DELETE FROM filtered_items WHERE id NOT IN ("
                        "  SELECT id FROM ("
                        "    SELECT id, ROW_NUMBER() OVER ("
                        "      PARTITION BY source_id, dedup_key "
                        "      ORDER BY relevance_score DESC, id DESC"
                        "    ) AS rn FROM filtered_items"
                        "    WHERE dedup_key != ''"
                        "  ) WHERE rn = 1"
                        ")"
                    }},
                    {"type": "close"},
                ]
            })
        except Exception as e:
            print(f"[Turso 同步] 删除重复行失败: {e}")

        # 3. 重建唯一索引：先删旧的 (source_id, crawl_date, title)，再确保新的存在
        try:
            self._post({
                "requests": [
                    {"type": "execute", "stmt": {"sql":
                        "DROP INDEX IF EXISTS idx_filtered_unique"
                    }},
                    {"type": "execute", "stmt": {"sql":
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_filtered_unique "
                        "ON filtered_items(source_id, dedup_key)"
                    }},
                    {"type": "close"},
                ]
            })
        except Exception as e:
            # 仍有重复时索引创建会失败，提示用户手动清理
            print(f"[Turso 同步] 重建唯一索引失败（可能仍有重复行）: {e}")
            print("[Turso 同步] 请运行清理脚本: python -m trendradar.storage.turso_sync --cleanup")

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

                url = item.get("url", "") or ""
                # 去重键：URL 非空用 URL（跨日同文 upsert 到同一行），否则用 "crawl_date|title"
                dedup_key = url if url else f"{crawl_date}|{title}"

                statements.append((
                    """
                    INSERT INTO filtered_items
                        (title, url, mobile_url, source_id, source_name, source_type,
                         rank, relevance_score, first_crawl_time, last_crawl_time,
                         crawl_date, crawl_time, published_at, dedup_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, dedup_key) DO UPDATE SET
                        title = excluded.title,
                        url = COALESCE(NULLIF(excluded.url, ''), filtered_items.url),
                        mobile_url = COALESCE(NULLIF(excluded.mobile_url, ''), filtered_items.mobile_url),
                        source_name = COALESCE(NULLIF(excluded.source_name, ''), filtered_items.source_name),
                        rank = excluded.rank,
                        relevance_score = MAX(excluded.relevance_score, filtered_items.relevance_score),
                        first_crawl_time = COALESCE(NULLIF(filtered_items.first_crawl_time, ''), excluded.first_crawl_time),
                        last_crawl_time = COALESCE(NULLIF(excluded.last_crawl_time, ''), filtered_items.last_crawl_time),
                        crawl_date = excluded.crawl_date,
                        crawl_time = COALESCE(NULLIF(excluded.crawl_time, ''), filtered_items.crawl_time),
                        published_at = COALESCE(NULLIF(excluded.published_at, ''), filtered_items.published_at),
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    [
                        title,
                        url,
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
                        dedup_key,
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

    # ============================================================
    # 查询/清理辅助方法（供 CLI 和第三方使用）
    # ============================================================

    def _execute_read(self, sql: str) -> List[Dict[str, Any]]:
        """执行只读查询，返回行列表（每行为 {列名: 值}）"""
        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql}},
                {"type": "close"},
            ]
        }
        data = self._post(payload)
        results = data.get("results", [])
        if not results:
            return []
        first = results[0]
        if first.get("type") != "ok":
            return []
        result_obj = first.get("response", {}).get("result", {})
        rows_resp = result_obj.get("rows", [])
        cols_resp = result_obj.get("cols", [])
        col_names = [c.get("name", f"col{i}") for i, c in enumerate(cols_resp)]

        out: List[Dict[str, Any]] = []
        for row in rows_resp:
            # row 是 cell 列表，如 [{"type":"integer","value":"30"}, ...]
            record: Dict[str, Any] = {}
            for i, val in enumerate(row):
                v = val.get("value") if isinstance(val, dict) else val
                record[col_names[i] if i < len(col_names) else f"col{i}"] = v
            out.append(record)
        return out

    def get_stats(self) -> Dict[str, Any]:
        """获取 filtered_items 表的统计信息（总行数、重复行数、去重键空值数）"""
        try:
            total_row = self._execute_read("SELECT COUNT(*) AS cnt FROM filtered_items")
            total = total_row[0].get("cnt", 0) if total_row else 0

            # 重复行数：同一 (source_id, dedup_key) 出现 >1 次的额外行数
            dup_row = self._execute_read(
                "SELECT COALESCE(SUM(c - 1), 0) AS cnt FROM ("
                "  SELECT source_id, dedup_key, COUNT(*) AS c "
                "  FROM filtered_items "
                "  WHERE dedup_key != '' "
                "  GROUP BY source_id, dedup_key HAVING COUNT(*) > 1"
                ")"
            )
            duplicates = dup_row[0].get("cnt", 0) if dup_row else 0

            empty_key_row = self._execute_read(
                "SELECT COUNT(*) AS cnt FROM filtered_items WHERE dedup_key IS NULL OR dedup_key = ''"
            )
            empty_keys = empty_key_row[0].get("cnt", 0) if empty_key_row else 0

            return {
                "total_rows": total,
                "duplicate_rows": duplicates,
                "empty_dedup_keys": empty_keys,
            }
        except Exception as e:
            print(f"[Turso 同步] 获取统计信息失败: {e}")
            return {"total_rows": -1, "duplicate_rows": -1, "empty_dedup_keys": -1, "error": str(e)}

    def show_duplicates(self, limit: int = 50) -> List[Dict[str, Any]]:
        """展示当前重复行（同一 source_id+dedup_key 下的所有行），用于排查"""
        try:
            return self._execute_read(
                "SELECT id, source_id, source_name, title, url, crawl_date, "
                "relevance_score, dedup_key, created_at "
                "FROM filtered_items "
                "WHERE (source_id, dedup_key) IN ("
                "  SELECT source_id, dedup_key FROM filtered_items "
                "  WHERE dedup_key != '' "
                "  GROUP BY source_id, dedup_key HAVING COUNT(*) > 1"
                ") "
                f"ORDER BY source_id, dedup_key, id LIMIT {int(limit)}"
            )
        except Exception as e:
            print(f"[Turso 同步] 查询重复行失败: {e}")
            return []

    def force_cleanup_duplicates(self) -> Dict[str, int]:
        """强制清理重复行（保留 relevance_score 最高、平局取最新 id 的一行）

        Returns:
            {"deleted": 删除行数, "remaining": 剩余行数}
        """
        before = self.get_stats()
        try:
            self._post({
                "requests": [
                    {"type": "execute", "stmt": {"sql":
                        "DELETE FROM filtered_items WHERE id NOT IN ("
                        "  SELECT id FROM ("
                        "    SELECT id, ROW_NUMBER() OVER ("
                        "      PARTITION BY source_id, dedup_key "
                        "      ORDER BY relevance_score DESC, id DESC"
                        "    ) AS rn FROM filtered_items"
                        "    WHERE dedup_key != ''"
                        "  ) WHERE rn = 1"
                        ") AND dedup_key != ''"
                    }},
                    {"type": "close"},
                ]
            })
        except Exception as e:
            print(f"[Turso 同步] 强制清理失败: {e}")
            return {"deleted": 0, "remaining": before.get("total_rows", 0), "error": str(e)}

        after = self.get_stats()
        deleted = before.get("total_rows", 0) - after.get("total_rows", 0)
        return {"deleted": deleted, "remaining": after.get("total_rows", 0)}


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


def _create_service_from_env() -> Optional[TursoSyncService]:
    """从环境变量直接创建 Turso 服务（用于 CLI）"""
    url = os.environ.get("TURSO_URL", "")
    auth_token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url or not auth_token:
        print("[Turso 同步] 未设置 TURSO_URL / TURSO_AUTH_TOKEN 环境变量")
        return None
    return TursoSyncService(url=url, auth_token=auth_token)


def _cli_main() -> int:
    """CLI 入口：python -m trendradar.storage.turso_sync [--status|--duplicates|--cleanup]"""
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "--status"

    service = _create_service_from_env()
    if not service or not service.enabled:
        print("[Turso 同步] 服务未启用，退出")
        return 1

    try:
        if cmd in ("--status", "-s"):
            stats = service.get_stats()
            print("=" * 60)
            print("Turso filtered_items 表统计")
            print("=" * 60)
            print(f"  总行数:        {stats.get('total_rows', -1)}")
            print(f"  重复行数:      {stats.get('duplicate_rows', -1)}")
            print(f"  空 dedup_key:  {stats.get('empty_dedup_keys', -1)}")
            if "error" in stats:
                print(f"  错误: {stats['error']}")
            print("=" * 60)

        elif cmd in ("--duplicates", "-d"):
            limit = 50
            if len(sys.argv) > 2:
                try:
                    limit = int(sys.argv[2])
                except ValueError:
                    pass
            rows = service.show_duplicates(limit=limit)
            if not rows:
                print("✅ 没有发现重复行")
            else:
                print(f"发现 {len(rows)} 行重复（limit={limit}）：")
                print("-" * 100)
                for r in rows:
                    print(f"  id={r.get('id')} | {r.get('source_name', '')} | "
                          f"crawl_date={r.get('crawl_date', '')} | "
                          f"score={r.get('relevance_score', '')}")
                    print(f"    标题: {r.get('title', '')}")
                    print(f"    URL:  {r.get('url', '')}")
                    print(f"    key:  {r.get('dedup_key', '')}")
                    print()

        elif cmd in ("--cleanup", "-c"):
            print("清理前统计：")
            before = service.get_stats()
            print(f"  总行数: {before.get('total_rows', -1)}, 重复行数: {before.get('duplicate_rows', -1)}")
            print()
            print("执行强制清理...")
            result = service.force_cleanup_duplicates()
            print()
            print(f"✅ 清理完成: 删除 {result.get('deleted', 0)} 行，剩余 {result.get('remaining', 0)} 行")
            if "error" in result:
                print(f"⚠️ 错误: {result['error']}")

        else:
            print(f"未知命令: {cmd}")
            print("用法: python -m trendradar.storage.turso_sync [--status|--duplicates|--cleanup]")
            print("  --status, -s           显示统计信息（默认）")
            print("  --duplicates, -d [N]   显示前 N 行重复（默认 50）")
            print("  --cleanup, -c          强制清理重复行")
            return 2

        return 0
    finally:
        service.cleanup()


if __name__ == "__main__":
    raise SystemExit(_cli_main())
