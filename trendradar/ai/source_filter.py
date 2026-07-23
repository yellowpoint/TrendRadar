# coding=utf-8
"""
AI 源级筛选模块

在订阅 RSS / 热榜源之前，先抓取该源的最新若干条样本，
交由 AI 结合用户兴趣描述判断是否值得订阅。

调用方式见 trendradar/commands/source_check.py。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from trendradar.ai.client import AIClient
from trendradar.ai.prompt_loader import load_prompt_template


@dataclass
class SourceCheckResult:
    """单次源级筛选结果"""
    source_type: str            # "rss" | "platform"
    source_id: str              # feed_id 或 platform_id
    source_name: str            # 显示名
    source_url: str             # RSS URL 或热榜 API URL

    sample_count: int = 0       # 实际抓到的样本数
    matched_count: int = 0      # AI 判定匹配样本数

    # 字段覆盖率统计（仅 RSS 源有效，用于判断源质量）
    has_cover_count: int = 0    # 样本中有封面图的条数
    has_summary_count: int = 0  # 样本中有简介的条数

    relevant: bool = False      # 是否值得订阅（score ≥ 0.5）
    score: float = 0.0          # 整体相关度 0~1
    reason: str = ""            # AI 给出的判断理由
    matched_tags: List[str] = field(default_factory=list)
    sample_matches: List[Dict] = field(default_factory=list)

    success: bool = False
    error: str = ""


class SourceFilter:
    """AI 源级筛选器"""

    def __init__(
        self,
        ai_config: Dict[str, Any],
        filter_config: Dict[str, Any],
        interests_file: Optional[str] = None,
        debug: bool = False,
    ):
        self.client = AIClient(ai_config)
        self.filter_config = filter_config or {}
        self.interests_file = interests_file or self.filter_config.get("INTERESTS_FILE")
        self.debug = debug

        # 加载源级筛选提示词模板
        prompt_file = self.filter_config.get(
            "SOURCE_CHECK_PROMPT_FILE", "source_check_prompt.txt"
        )
        self.system_prompt, self.user_prompt = load_prompt_template(
            prompt_file, config_subdir="ai_filter", label="源级筛选"
        )

    # === 兴趣描述加载 ===

    def load_interests_content(self) -> Optional[str]:
        """加载兴趣描述文件（逻辑与 AIFilter.load_interests_content 一致）

        解析规则：
        - interests_file 为 None：使用默认 config/ai_interests.txt
        - interests_file 有值：仅查 config/custom/ai/{filename}
        """
        config_dir = Path(__file__).parent.parent.parent / "config"
        configured_file = self.interests_file

        if configured_file:
            interests_path = config_dir / "custom" / "ai" / configured_file
            if not interests_path.exists():
                print(f"[源级筛选] 自定义兴趣描述文件不存在: {configured_file}")
                print(f"[源级筛选]   已查找: {interests_path}")
                return None
        else:
            interests_path = config_dir / "ai_interests.txt"
            if not interests_path.exists():
                print(f"[源级筛选] 默认兴趣描述文件不存在: {interests_path}")
                return None

        content = interests_path.read_text(encoding="utf-8").strip()
        if not content:
            print("[源级筛选] 兴趣描述文件为空")
            return None
        return content

    # === RSS 源检查 ===

    def check_rss(
        self,
        url: str,
        name: str = "",
        sample_size: int = 20,
        timeout: int = 15,
    ) -> SourceCheckResult:
        """检查单个 RSS URL 是否值得订阅"""
        result = SourceCheckResult(
            source_type="rss",
            source_id=name or url,
            source_name=name or url,
            source_url=url,
        )

        # 1. 抓取样本
        samples = self._fetch_rss_samples(url, sample_size, timeout, result)
        if not samples:
            return result

        # 2. AI 判断
        return self._ai_check(samples, result)

    def _fetch_rss_samples(
        self,
        url: str,
        sample_size: int,
        timeout: int,
        result: SourceCheckResult,
    ) -> List[Dict]:
        """抓取 RSS 源的样本条目"""
        try:
            from trendradar.crawler.rss import RSSParser
        except ImportError as e:
            result.error = f"缺少 RSS 依赖: {e}"
            print(f"[源级筛选] {result.error}")
            return []

        parser = RSSParser()
        try:
            parsed_items = parser.parse_url(url, timeout=timeout)
        except Exception as e:
            result.error = f"RSS 抓取失败: {type(e).__name__}: {e}"
            print(f"[源级筛选] {result.error}")
            return []

        if not parsed_items:
            result.error = "RSS 解析为空"
            print(f"[源级筛选] {result.error}: {url}")
            return []

        # 取前 N 条
        parsed_items = parsed_items[:sample_size] if sample_size > 0 else parsed_items

        samples: List[Dict] = []
        has_cover = 0
        has_summary = 0
        for idx, item in enumerate(parsed_items, 1):
            summary = (item.summary or "")[:200]
            cover_url = item.cover_url or ""
            if cover_url:
                has_cover += 1
            if summary.strip():
                has_summary += 1
            samples.append({
                "index": idx,
                "title": item.title or "",
                "summary": summary,
                "cover_url": cover_url,
                "url": item.url or "",
                "published_at": item.published_at or "",
            })

        result.sample_count = len(samples)
        result.has_cover_count = has_cover
        result.has_summary_count = has_summary
        print(
            f"[源级筛选] {result.source_name}: 抓取到 {len(samples)} 条样本"
            f"（封面图 {has_cover}/{len(samples)}，简介 {has_summary}/{len(samples)}）"
        )
        return samples

    # === 热榜平台检查 ===

    def check_platform(
        self,
        platform_id: str,
        platform_name: str = "",
        api_url: str = "",
        sample_size: int = 20,
    ) -> SourceCheckResult:
        """检查单个热榜平台是否值得订阅"""
        result = SourceCheckResult(
            source_type="platform",
            source_id=platform_id,
            source_name=platform_name or platform_id,
            source_url=api_url or f"newsnow:{platform_id}",
        )

        # 1. 抓取样本
        samples = self._fetch_platform_samples(
            platform_id, sample_size, api_url, result
        )
        if not samples:
            return result

        # 2. AI 判断
        return self._ai_check(samples, result)

    def _fetch_platform_samples(
        self,
        platform_id: str,
        sample_size: int,
        api_url: str,
        result: SourceCheckResult,
    ) -> List[Dict]:
        """从 newsnow API 抓取热榜样本"""
        from trendradar.crawler.fetcher import DataFetcher

        fetcher = DataFetcher(api_url=api_url or None)
        response_text, _, _ = fetcher.fetch_data(platform_id)
        if not response_text:
            result.error = "热榜 API 返回空"
            print(f"[源级筛选] {result.error}: {platform_id}")
            return []

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as e:
            result.error = f"热榜响应 JSON 解析失败: {e}"
            print(f"[源级筛选] {result.error}")
            return []

        items = data.get("items", [])
        if not items:
            result.error = "热榜响应 items 为空"
            print(f"[源级筛选] {result.error}: {platform_id}")
            return []

        items = items[:sample_size] if sample_size > 0 else items

        samples: List[Dict] = []
        for idx, item in enumerate(items, 1):
            title = item.get("title")
            if not title or not str(title).strip():
                continue
            samples.append({
                "index": idx,
                "title": str(title).strip(),
                "summary": "",
                "url": item.get("url", "") or item.get("mobileUrl", "") or "",
                "published_at": "",
            })

        result.sample_count = len(samples)
        print(f"[源级筛选] {result.source_name}: 抓取到 {len(samples)} 条样本")
        return samples

    # === AI 判断 ===

    def _ai_check(
        self,
        samples: List[Dict],
        result: SourceCheckResult,
    ) -> SourceCheckResult:
        """调用 AI 判断样本是否与兴趣相关"""
        if not self.user_prompt:
            result.error = "源级筛选提示词模板为空"
            print(f"[源级筛选] {result.error}")
            return result

        # 加载兴趣描述
        interests_content = self.load_interests_content()
        if not interests_content:
            result.error = "兴趣描述文件为空或不存在"
            print(f"[源级筛选] {result.error}")
            return result

        # 构建样本列表文本
        sample_lines = []
        for s in samples:
            line = f"{s['index']}. {s['title']}"
            if s.get("summary"):
                line += f"  | 摘要: {s['summary']}"
            sample_lines.append(line)
        sample_list_text = "\n".join(sample_lines)

        # 填充模板
        user_prompt = self.user_prompt
        user_prompt = user_prompt.replace("{interests_content}", interests_content)
        user_prompt = user_prompt.replace("{source_type}", result.source_type)
        user_prompt = user_prompt.replace("{source_name}", result.source_name)
        user_prompt = user_prompt.replace("{source_url}", result.source_url)
        user_prompt = user_prompt.replace("{sample_count}", str(result.sample_count))
        user_prompt = user_prompt.replace("{sample_list}", sample_list_text)

        messages: List[Dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        if self.debug:
            print(f"\n[源级筛选][DEBUG] === Prompt ({result.source_name}) ===")
            for m in messages:
                content = m["content"]
                if len(content) > 2000:
                    content = content[:2000] + f"\n... (省略 {len(content) - 2000} 字符)"
                print(f"[{m['role']}]\n{content}")
            print(f"[源级筛选][DEBUG] === Prompt 结束 ===")

        try:
            response = self.client.chat(messages)
        except Exception as e:
            result.error = f"AI 调用失败: {type(e).__name__}: {e}"
            print(f"[源级筛选] {result.error}")
            return result

        if not response or not response.strip():
            result.error = "AI 响应为空"
            print(f"[源级筛选] {result.error}")
            return result

        if self.debug:
            print(f"\n[源级筛选][DEBUG] === AI 响应 ===")
            print(response)
            print(f"[源级筛选][DEBUG] === 响应结束 ===")

        # 解析响应
        self._parse_check_response(response, result)
        return result

    def _parse_check_response(self, response: str, result: SourceCheckResult) -> None:
        """解析 AI 返回的 JSON"""
        json_str = self._extract_json(response)
        if not json_str:
            result.error = "无法从响应中提取 JSON"
            print(f"[源级筛选] {result.error}")
            if self.debug:
                print(f"[源级筛选][DEBUG] 原始响应前 500 字符: {(response or '')[:500]}")
            return

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            result.error = f"JSON 解析失败: {e}"
            print(f"[源级筛选] {result.error}")
            if self.debug:
                print(f"[源级筛选][DEBUG] 提取的 JSON 文本前 500 字符: {json_str[:500]}")
            return

        # 校验并填充字段
        try:
            relevant = bool(data.get("relevant", False))
        except (TypeError, ValueError):
            relevant = False

        try:
            score = float(data.get("score", 0.0))
            score = max(0.0, min(1.0, score))
        except (TypeError, ValueError):
            score = 0.0

        reason = str(data.get("reason", "")).strip()
        matched_tags_raw = data.get("matched_tags", [])
        if not isinstance(matched_tags_raw, list):
            matched_tags_raw = []
        matched_tags = [str(t).strip() for t in matched_tags_raw if t]

        sample_matches_raw = data.get("sample_matches", [])
        if not isinstance(sample_matches_raw, list):
            sample_matches_raw = []
        sample_matches: List[Dict] = []
        for m in sample_matches_raw:
            if not isinstance(m, dict):
                continue
            title = str(m.get("title", "")).strip()
            if not title:
                continue
            try:
                m_score = float(m.get("score", 0.0))
                m_score = max(0.0, min(1.0, m_score))
            except (TypeError, ValueError):
                m_score = 0.0
            m_tags_raw = m.get("tags", [])
            if not isinstance(m_tags_raw, list):
                m_tags_raw = []
            m_tags = [str(t).strip() for t in m_tags_raw if t]
            sample_matches.append({
                "title": title,
                "tags": m_tags,
                "score": m_score,
            })

        result.relevant = relevant
        result.score = score
        result.reason = reason
        result.matched_tags = matched_tags
        result.sample_matches = sample_matches
        result.matched_count = len(sample_matches)
        result.success = True

    def _extract_json(self, response: str) -> Optional[str]:
        """从 AI 响应中提取 JSON 字符串（逻辑与 AIFilter._extract_json 一致）"""
        if not response or not response.strip():
            return None

        json_str = response.strip()

        if "```json" in json_str:
            parts = json_str.split("```json", 1)
            if len(parts) > 1:
                code_block = parts[1]
                end_idx = code_block.find("```")
                json_str = code_block[:end_idx] if end_idx != -1 else code_block
        elif "```" in json_str:
            parts = json_str.split("```", 2)
            if len(parts) >= 2:
                json_str = parts[1]

        json_str = json_str.strip()
        return json_str if json_str else None
