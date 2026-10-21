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
    assert "工具返回失败：没有结果" in text and "HTTP 403" in text
    assert "尚未完成" in text


def test_local_cooldown_not_executed_is_distinct_from_empty_result():
    evidence = SearchEvidence()
    evidence.add(
        "read_webpage", False,
        "宿主本地冷却：同用户近期已使用该工具名额，等待预算不足，"
        "本次调用尚未执行且未向远端发出请求；不能据此判断远端站点限流。",
        arguments={"url": "https://github.com/example/project/issues/1"},
        not_executed=True,
    )
    text = evidence.partial_reply(1500)
    assert "未取得可用来源" in text
    assert "https://github.com/example/project/issues/1" in text
    assert "本地冷却" in text and "工具返回失败" not in text
    assert "暂未取得可用资料" not in text


def test_upstream_failure_reason_is_preserved_instead_of_claiming_no_result():
    evidence = SearchEvidence()
    evidence.add("web_search", False, "HTTP 403", arguments={"query": "GitHub 项目"})
    text = evidence.partial_reply(1500)
    assert "未取得可用来源" in text
    assert "GitHub 项目" in text and "工具返回失败" in text and "HTTP 403" in text
    assert "暂未取得可用资料" not in text


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


def test_many_long_failures_do_not_hide_successful_source():
    evidence = SearchEvidence()
    evidence.add("web_search", True,
                 "1. 已有检索资料\n相关内容摘要\n来源：https://example.com/known-source")
    for n in range(6):
        evidence.add(
            "read_webpage", False, "远端拒绝，尚未核实原因，" * 24,
            arguments={"url": "https://github.com/example/repository" + str(n)},
        )
    text = evidence.partial_reply(1500)
    assert len(text) <= 1500
    assert "相关内容摘要" in text
    assert "https://example.com/known-source" in text
    # 真实取得的来源和摘录必须先于失败说明出现。
    assert text.index("相关内容摘要") < text.index("工具返回失败")
    assert "省略" in text


def test_multiple_sources_keep_links_before_trimmed_failures():
    evidence = SearchEvidence()
    urls = [f"https://example.com/source/{i}" for i in range(4)]
    for index, url in enumerate(urls, 1):
        evidence.add("web_search", True,
                     f"{index}. 资料{index}\n第{index}条摘要\n来源：{url}")
    for n in range(6):
        evidence.add(
            "read_webpage", False, "远端拒绝，尚未核实原因，" * 24,
            arguments={"url": "https://github.com/example/repository" + str(n)},
        )
    text = evidence.partial_reply(900)
    assert len(text) <= 900
    assert all(url in text for url in urls)
    failure_pos = text.index("以下调用未完成")
    assert all(text.index(url) < failure_pos for url in urls)
    assert "工具返回失败" in text and "省略" in text


def test_long_source_url_is_kept_when_budget_allows():
    evidence = SearchEvidence()
    url = "https://example.com/" + "x" * 1200
    evidence.add("web_search", True, f"1. 长链接资料\n核心正文仍在\n来源：{url}")
    text = evidence.partial_reply(1500)
    assert len(text) <= 1500
    assert url in text and "核心正文仍在" in text


def test_tiny_budgets_and_overlong_url_keep_real_excerpt():
    evidence = SearchEvidence()
    url = "https://example.com/" + "x" * 2000
    evidence.add("web_search", True, f"1. 超长链接资料\n仍需核实的正文\n来源：{url}")
    evidence.add("read_webpage", False, "HTTP 500",
                 arguments={"url": "https://example.com/fail"})
    for limit in (0, 1, 5, 10, 19, 50, 200):
        text = evidence.partial_reply(limit)
        assert isinstance(text, str)
        assert len(text) <= limit
    # 链接放不下时也不能只剩下成功标题，真实摘录必须保留在前面。
    assert "超长链接资料" in evidence.partial_reply(10)
    assert "仍需核实的正文" in evidence.partial_reply(50)


def test_no_sources_long_failures_are_bounded_and_marked():
    evidence = SearchEvidence()
    for n in range(6):
        evidence.add(
            "read_webpage", False, "远端拒绝，尚未核实原因，" * 24,
            arguments={"url": "https://github.com/example/repository" + str(n)},
        )
    for limit in (0, 15, 80):
        assert len(evidence.partial_reply(limit)) <= limit
    text = evidence.partial_reply(1500)
    assert len(text) <= 1500
    assert "未取得可用来源" in text
    assert "工具返回失败" in text
    assert "省略" in text
    assert text.endswith("待核实的线索仍未完成。")
