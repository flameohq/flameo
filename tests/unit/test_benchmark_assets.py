"""Keep the load-test payload, template, and SQL assets valid."""

from __future__ import annotations


def test_benchmark_payload_is_valid_and_non_trivial():
    import json

    from benchmarks.common import PAYLOAD

    assert isinstance(PAYLOAD, dict)
    assert PAYLOAD
    assert len(json.dumps(PAYLOAD)) > 1000


def test_benchmark_template_renders_one_hundred_rows():
    from benchmarks.common import TEMPLATE_CONTEXT, render_template

    assert TEMPLATE_CONTEXT["count"] == 100
    html = render_template()
    assert "Modern UI, rendered fast" in html
    assert "A light theme template with two strings and a 100-row loop." in html
    assert "Benchmark row 1" in html
    assert 'data-row="100"' in html
    assert html.count('class="item"') == 100


def test_benchmark_sql_uses_join_and_limits_list_rows():
    from benchmarks.common import SQL_LIST, SQL_SINGLE

    assert "JOIN department D ON D.id = E.department_id" in SQL_SINGLE
    assert "WHERE E.id = 5" in SQL_SINGLE
    assert "JOIN department D ON D.id = E.department_id" in SQL_LIST
    assert "LIMIT 20" in SQL_LIST


def test_benchmark_latency_summary_handles_empty_and_percentiles():
    from benchmarks.common import format_latency_ms, latency_summary

    assert latency_summary([]) == {"avg": None, "p50": None, "p99": None}
    summary = latency_summary([1.0, 2.0, 3.0, 4.0, 5.0])
    assert summary == {"avg": 3.0, "p50": 3.0, "p99": 4.96}
    assert format_latency_ms(1.234) == "1.23ms"
    assert format_latency_ms(None) is None

