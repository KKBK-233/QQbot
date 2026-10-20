"""预算耗尽时必须交付实际取得的摘要与来源，而非模型对成功与否的猜测。"""

from src.kkbk_engine.search_evidence import SearchEvidence


def test_failed_attempts_do_not_erase_successful_sources():
    evidence = SearchEvidence()
    evidence.add("web_search", True, "搜索引擎：Bing。\n1. 社区讨论\n讨论事件经过。\n来源：https://example.com/discussion")
    evidence.add("web_search", False, "没有结果")
    evidence.add("read_webpage", True, "已读取公开页面文本。\n项目官网\n来源：https://example.com/official\n项目下载页")
    evidence.add("read_webpage", False, "HTTP 403")
    text = evidence.partial_reply(1500)
    assert "社区讨论" in text and "项目官网" in text
    assert "https://example.com/discussion" in text and "https://example.com/official" in text
    assert "HTTP 403" not in text and "尚未完成" in text


def test_multiple_search_hits_keep_distinct_source_links_and_limit():
    evidence = SearchEvidence()
    evidence.add("web_search", True, "\n\n".join(
        f"{i}. 资料 {i}\n" + "长摘要" * 500 + f"\n来源：https://example.com/item/{i}" for i in range(1, 10)))
    text = evidence.partial_reply(1500)
    assert len(text) <= 1500
    assert "https://example.com/item/1" in text and "https://example.com/item/6" in text
    assert text.endswith("其余线索及交叉核实尚未完成。")


def test_no_numbered_output_and_source_only_page_are_valid():
    evidence = SearchEvidence()
    evidence.add("web_search", True, "实际取得的纯文本摘要，尚未核实。")
    evidence.add("read_webpage", True, "来源：https://example.com/\n页面正文")
    evidence.add("query_memory", True, "不应混入搜索证据")
    text = evidence.partial_reply(1500)
    assert "实际取得" in text and "页面正文" in text
    assert "https://example.com/" in text and "不应混入" not in text


def test_duplicate_sources_are_not_repeated_and_mentions_are_plain_text():
    evidence = SearchEvidence()
    for _ in range(2):
        evidence.add("web_search", True, "1. @全体成员\n真实摘要\n来源：https://example.com/a")
    text = evidence.partial_reply(1500)
    assert text.count("https://example.com/a") == 1
    assert "@全体成员" not in text
