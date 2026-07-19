# coding=utf-8
"""源级筛选 CLI 命令

预检单个 RSS URL / 热榜平台，或批量检查 config.yaml 中所有已配置源，
判断是否与用户兴趣主题相关。

调用：
  python -m trendradar --check-rss <url> [--source-name NAME] [--sample-size N]
  python -m trendradar --check-source <platform_id> [--sample-size N]
  python -m trendradar --check-all-sources [--sample-size N]
"""

from typing import Dict, List, Optional

from trendradar.ai.source_filter import SourceCheckResult, SourceFilter
from trendradar.core import load_config


def _build_filter(config: Dict, debug: bool) -> SourceFilter:
    ai_config = config.get("AI", {})
    filter_config = config.get("AI_FILTER", {})
    interests_file = filter_config.get("INTERESTS_FILE")
    return SourceFilter(
        ai_config=ai_config,
        filter_config=filter_config,
        interests_file=interests_file,
        debug=debug,
    )


def _validate_ai_config(config: Dict) -> Optional[str]:
    """校验 AI 配置，返回错误信息或 None"""
    from trendradar.ai.client import AIClient
    valid, msg = AIClient(config.get("AI", {})).validate_config()
    if not valid:
        return msg
    return None


def _print_single_result(result: SourceCheckResult) -> None:
    """打印单个源的检查结果"""
    print()
    print("=" * 60)
    if not result.success:
        print(f"❌ 检查失败: {result.source_name}")
        print(f"   类型: {result.source_type}")
        print(f"   URL/ID: {result.source_url}")
        print(f"   错误: {result.error}")
        print("=" * 60)
        return

    icon = "✅" if result.relevant else "❌"
    verdict = "建议订阅" if result.relevant else "不建议订阅"
    print(f"{icon} {result.source_name} → {verdict}")
    print(f"   类型: {result.source_type}")
    print(f"   URL/ID: {result.source_url}")
    print(f"   样本数: {result.sample_count}, 命中样本: {result.matched_count}")
    print(f"   相关度: {result.score:.2f}")
    if result.matched_tags:
        print(f"   命中方向: {', '.join(result.matched_tags)}")
    if result.reason:
        print(f"   理由: {result.reason}")
    if result.sample_matches:
        print(f"   匹配样本（前 5 条）:")
        for m in result.sample_matches[:5]:
            tags_str = f" [{', '.join(m['tags'])}]" if m.get("tags") else ""
            print(f"     • ({m['score']:.2f}) {m['title']}{tags_str}")
    print("=" * 60)


def _print_batch_results(results: List[SourceCheckResult]) -> None:
    """以表格形式打印批量结果"""
    print()
    print("=" * 80)
    print("源级筛选批量预检结果")
    print("=" * 80)

    # 表头
    header = f"{'状态':<4} {'类型':<6} {'名称':<24} {'分数':<6} {'命中/样本':<10} {'建议':<8}"
    print(header)
    print("-" * 80)

    success_count = 0
    relevant_count = 0
    for r in results:
        if not r.success:
            print(f"❌   {r.source_type:<6} {r.source_name[:24]:<24} {'-':<6} {'-':<10} 失败")
            print(f"     错误: {r.error}")
            continue
        success_count += 1
        icon = "✅" if r.relevant else "❌"
        verdict = "订阅" if r.relevant else "跳过"
        if r.relevant:
            relevant_count += 1
        ratio = f"{r.matched_count}/{r.sample_count}"
        print(
            f"{icon}   {r.source_type:<6} {r.source_name[:24]:<24} "
            f"{r.score:.2f}    {ratio:<10} {verdict}"
        )

    print("-" * 80)
    print(
        f"汇总: 共 {len(results)} 个源, "
        f"成功检查 {success_count} 个, "
        f"建议订阅 {relevant_count} 个, "
        f"不建议 {success_count - relevant_count} 个"
    )

    # 打印每个相关源的详细信息
    relevant_results = [r for r in results if r.success and r.relevant]
    if relevant_results:
        print()
        print("=" * 80)
        print("建议订阅的源（详细信息）")
        print("=" * 80)
        for r in relevant_results:
            _print_single_result(r)

    print()
    print("=" * 80)
    print("说明：分数 ≥ 0.5 才建议订阅，0.6~0.9 较相关，0.9+ 高度相关")
    print("=" * 80)


def run_check_rss(
    url: str,
    name: str = "",
    sample_size: int = 20,
    debug: bool = False,
) -> int:
    """检查单个 RSS URL"""
    config = load_config()

    err = _validate_ai_config(config)
    if err:
        print(f"❌ AI 配置无效: {err}")
        print("   请在 config.yaml 的 ai 段或环境变量 AI_API_KEY / AI_MODEL 中配置")
        return 1

    sf = _build_filter(config, debug)
    result = sf.check_rss(url, name=name or url, sample_size=sample_size)
    _print_single_result(result)
    return 0 if result.success else 1


def run_check_platform(
    platform_id: str,
    sample_size: int = 20,
    debug: bool = False,
) -> int:
    """检查单个热榜平台"""
    config = load_config()

    err = _validate_ai_config(config)
    if err:
        print(f"❌ AI 配置无效: {err}")
        print("   请在 config.yaml 的 ai 段或环境变量 AI_API_KEY / AI_MODEL 中配置")
        return 1

    # 在 config 中查找平台显示名
    platform_name = platform_id
    api_url = config.get("PLATFORMS_API_URL", "") or ""
    for p in config.get("PLATFORMS", []):
        if p.get("id") == platform_id:
            platform_name = p.get("name", platform_id)
            break

    sf = _build_filter(config, debug)
    result = sf.check_platform(
        platform_id=platform_id,
        platform_name=platform_name,
        api_url=api_url,
        sample_size=sample_size,
    )
    _print_single_result(result)
    return 0 if result.success else 1


def run_check_all(
    sample_size: int = 20,
    debug: bool = False,
    include_rss: bool = True,
    include_platforms: bool = True,
) -> int:
    """批量检查 config.yaml 中所有已配置的 RSS 源和热榜平台"""
    config = load_config()

    err = _validate_ai_config(config)
    if err:
        print(f"❌ AI 配置无效: {err}")
        print("   请在 config.yaml 的 ai 段或环境变量 AI_API_KEY / AI_MODEL 中配置")
        return 1

    sf = _build_filter(config, debug)

    results: List[SourceCheckResult] = []

    # 检查 RSS
    if include_rss:
        rss_feeds = config.get("RSS", {}).get("FEEDS", [])
        if not rss_feeds:
            print("[源级筛选] 未配置任何 RSS 源")
        else:
            print(f"[源级筛选] 开始检查 {len(rss_feeds)} 个 RSS 源...")
            for feed in rss_feeds:
                if not feed.get("enabled", True):
                    continue
                feed_id = feed.get("id", "")
                feed_name = feed.get("name", feed_id)
                feed_url = feed.get("url", "")
                if not feed_url:
                    continue
                print(f"\n[源级筛选] >>> RSS: {feed_name} ({feed_url})")
                result = sf.check_rss(
                    url=feed_url,
                    name=feed_name or feed_id,
                    sample_size=sample_size,
                )
                # 用 feed_id 作为 source_id 以保持一致性
                result.source_id = feed_id
                results.append(result)

    # 检查热榜平台
    if include_platforms:
        platforms = config.get("PLATFORMS", [])
        if not platforms:
            print("[源级筛选] 未配置任何热榜平台")
        else:
            api_url = config.get("PLATFORMS_API_URL", "") or ""
            print(f"\n[源级筛选] 开始检查 {len(platforms)} 个热榜平台...")
            for p in platforms:
                pid = p.get("id", "")
                pname = p.get("name", pid)
                if not pid:
                    continue
                print(f"\n[源级筛选] >>> 平台: {pname} ({pid})")
                result = sf.check_platform(
                    platform_id=pid,
                    platform_name=pname,
                    api_url=api_url,
                    sample_size=sample_size,
                )
                results.append(result)

    if not results:
        print("[源级筛选] 没有可检查的源（请检查 config.yaml 中 rss.feeds / platforms.sources 配置）")
        return 1

    _print_batch_results(results)
    return 0
