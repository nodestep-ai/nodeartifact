import html
import json
import re

import pytest

from nodeartifact.server.display import DataTree, DataView, MessagePreview
from nodeartifact.server.models import JsonValue
from nodeartifact.server.ui import template_environment

STATE = {
    "question": "Which ten products sold the most units?",
    "attempts": 2,
    "ratio": 0.5,
    "approved": True,
    "error": None,
    "tags": [],
    "result": {
        "columns": ["product", "units"],
        "rows": [["Salted caramel bar", 258]],
        "store": {"name": "Vienna Neubau", "city": {"name": "Vienna"}},
    },
}
MESSAGES = [
    {"id": "m1", "type": "system", "content": "Answer with SQL."},
    {"id": "m2", "type": "human", "content": "Which store sold the most?\nIn 2025."},
    {
        "id": "m3",
        "type": "ai",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "name": "run_sql",
                "arguments": {"sql": "SELECT name FROM stores WHERE city = 'Wien'"},
            }
        ],
    },
    {
        "id": "m4",
        "type": "tool",
        "name": "run_sql",
        "tool_call_id": "call_1",
        "content": '{"rows": [["Vienna Neubau", 180]], "row_count": 1}',
    },
    {"id": "m5", "role": "ai", "content": "Vienna Neubau, with 180 orders."},
]
GENAI_MESSAGES = [
    {"role": "system", "parts": [{"type": "text", "content": "Answer with SQL."}]},
    {
        "role": "user",
        "parts": [{"type": "text", "content": "Which store sold the most?"}],
    },
    {
        "role": "assistant",
        "parts": [
            {
                "type": "tool_call",
                "id": "call_1",
                "name": "run_sql",
                "arguments": {"sql": "SELECT name FROM stores"},
            }
        ],
    },
    {
        "role": "tool",
        "parts": [
            {
                "type": "tool_call_response",
                "id": "call_1",
                "response": '{"rows": [["Vienna Neubau", 180]], "row_count": 1}',
            }
        ],
    },
    {
        "role": "assistant",
        "parts": [{"type": "text", "content": "Vienna Neubau, with 180 orders."}],
        "finish_reason": "stop",
    },
]
TOOLBAR = (
    '<div class="nodestep-data-toolbar">'
    '<input class="nodestep-input nodestep-data-search" type="search"'
    ' placeholder="Search keys and values" aria-label="Search the state">'
    '<span class="nodestep-data-matches" aria-live="polite"></span>'
    '<button class="nodestep-button nodestep-button-quiet nodestep-button-small"'
    ' type="button" data-nodestep-data-action="expand">Expand all</button>'
    '<button class="nodestep-button nodestep-button-quiet nodestep-button-small"'
    ' type="button" data-nodestep-data-action="collapse">Collapse all</button>'
    '<button class="nodestep-button nodestep-button-quiet nodestep-button-small"'
    ' type="button" data-nodestep-data-action="copy">Copy JSON</button>'
    "</div>"
)


def view(value) -> DataView:
    data = DataView.from_text(json.dumps(value, ensure_ascii=False, indent=2))
    assert data is not None
    return data


def render(source: str, **context) -> str:
    template = template_environment().from_string(
        '{% from "data.html" import data_viewer, data_block, data_cell %}' + source
    )
    return template.render(**context)


def viewer(value, label="the state") -> str:
    return render("{{ data_viewer(data, label) }}", data=view(value), label=label)


def tree(markup: str) -> str:
    return markup.split('<div class="nodestep-data-tree">')[1].split(
        '</div><pre class="nodestep-data-source">'
    )[0]


def message_rows(markup: str) -> list[str]:
    return markup.split('<li class="nodestep-data-message">')[1:]


def head(row: str) -> str:
    return row.split("</summary>")[0]


def body(row: str) -> str:
    return row.split('<div class="nodestep-data-message-body">')[1]


def preview_lines(row: str) -> list[str]:
    return re.findall(
        r'<span class="nodestep-data-preview-line[^"]*">(.*?)</span>', head(row)
    )


def entry(markup: str, key: str) -> str:
    start = markup.index(
        f'<li class="nodestep-data-entry"><span class="nodestep-data-key">{key}</span>'
    )
    return markup[start : markup.index("</li>", start) + len("</li>")]


def test_the_viewer_has_the_toolbar_the_tree_and_the_text_to_copy():
    markup = viewer(STATE)

    assert markup.startswith(
        f'<div class="nodestep-data" role="group" aria-label="the state">{TOOLBAR}'
    )
    assert markup.endswith("</pre></div>")
    source = markup.split('<pre class="nodestep-data-source">')[1].split("</pre>")[0]
    assert json.loads(html.unescape(source)) == STATE
    assert html.unescape(source) == json.dumps(STATE, ensure_ascii=False, indent=2)
    assert "\n" not in tree(markup)
    assert " style=" not in markup
    assert "<script" not in markup


def test_values_are_typed_with_the_code_token_classes():
    markup = tree(viewer(STATE))

    assert entry(markup, "question") == (
        '<li class="nodestep-data-entry"><span class="nodestep-data-key">question</span>'
        '<span class="nodestep-data-value nodestep-token-string">'
        "Which ten products sold the most units?</span></li>"
    )
    for key, token, text in [
        ("attempts", "number", "2"),
        ("ratio", "number", "0.5"),
        ("approved", "keyword", "true"),
        ("error", "comment", "null"),
        ("tags", "operator", "[]"),
    ]:
        assert entry(markup, key).endswith(
            f'<span class="nodestep-data-value nodestep-token-{token}">{text}</span></li>'
        ), key


def test_objects_and_arrays_fold_with_their_size_and_open_two_levels_deep():
    markup = tree(viewer(STATE))

    assert markup.startswith('<ul class="nodestep-data-children">')
    assert (
        '<details class="nodestep-data-node" open><summary>'
        '<span class="nodestep-data-key">result</span>'
        '<span class="nodestep-data-size">3 keys</span></summary>'
        '<ul class="nodestep-data-children">'
    ) in markup
    assert (
        '<details class="nodestep-data-node" open><summary>'
        '<span class="nodestep-data-key">rows</span>'
        '<span class="nodestep-data-size">1 item</span></summary>'
        '<ol class="nodestep-data-children">'
    ) in markup
    assert (
        '<li class="nodestep-data-entry"><details class="nodestep-data-node"><summary>'
        '<span class="nodestep-data-index">0</span>'
        '<span class="nodestep-data-size">2 items</span></summary>'
    ) in markup
    assert (
        '<details class="nodestep-data-node"><summary>'
        '<span class="nodestep-data-key">city</span>'
        '<span class="nodestep-data-size">1 key</span></summary>'
    ) in markup
    assert (
        '<li class="nodestep-data-entry"><span class="nodestep-data-index">1</span>'
        '<span class="nodestep-data-value nodestep-token-number">258</span></li>'
    ) in markup


def test_an_array_at_the_top_lists_its_items_by_index():
    markup = tree(viewer([{"a": 1}, {"b": [1, 2]}]))

    assert markup.startswith(
        '<ol class="nodestep-data-children"><li class="nodestep-data-entry">'
        '<details class="nodestep-data-node" open><summary>'
        '<span class="nodestep-data-index">0</span>'
    )


def test_chat_messages_are_rows_with_their_role_text_and_tool_calls():
    markup = tree(viewer({"messages": MESSAGES}))

    assert markup.startswith(
        '<ul class="nodestep-data-children"><li class="nodestep-data-entry">'
        '<details class="nodestep-data-node" open><summary>'
        '<span class="nodestep-data-key">messages</span>'
        '<span class="nodestep-data-size">5 messages</span></summary>'
        '<ol class="nodestep-data-messages">'
    )
    rows = message_rows(markup)
    assert len(rows) == 5
    roles = [
        re.findall(r'nodestep-data-role nodestep-data-role-(\w+)">(\w+)<', row)[0]
        for row in rows
    ]
    assert roles == [
        ("system", "system"),
        ("human", "human"),
        ("ai", "ai"),
        ("tool", "tool"),
        ("ai", "ai"),
    ]
    assert (
        '<p class="nodestep-data-text">Which store sold the most?\nIn 2025.</p>'
        in body(rows[1])
    )
    assert "nodestep-data-text" not in rows[2]
    assert (
        '<ul class="nodestep-data-calls"><li class="nodestep-data-call">'
        '<span class="nodestep-data-call-name">run_sql</span>'
        '<span class="nodestep-data-call-arguments">'
        "{&#34;sql&#34;: &#34;SELECT name FROM stores WHERE city = &#39;Wien&#39;&#34;}"
        "</span></li></ul>"
    ) in body(rows[2])
    assert head(rows[3]).startswith(
        '<details class="nodestep-data-message-row">'
        '<summary class="nodestep-data-message-head">'
        '<span class="nodestep-data-role nodestep-data-role-tool">tool</span>'
        '<span class="nodestep-data-message-name">run_sql</span>'
    )


def test_each_message_is_a_folded_row_whose_summary_is_its_head():
    rows = message_rows(tree(viewer({"messages": MESSAGES})))

    for row, role in zip(rows, ["system", "human", "ai", "tool", "ai"], strict=True):
        assert row.startswith(
            '<details class="nodestep-data-message-row">'
            '<summary class="nodestep-data-message-head">'
            f'<span class="nodestep-data-role nodestep-data-role-{role}">{role}</span>'
        )
        assert row.endswith("</div></details></li>") or row.endswith(
            "</div></details></li></ol></details></li></ul>"
        )
        assert '<div class="nodestep-data-message-body">' in row
        assert '<details class="nodestep-data-raw">' in body(row)
        assert "nodestep-data-raw" not in head(row)


def test_a_short_text_shows_in_full_in_its_folded_row():
    rows = message_rows(tree(viewer({"messages": MESSAGES})))

    assert head(rows[1]).endswith(
        '<span class="nodestep-data-preview">'
        '<span class="nodestep-data-preview-line nodestep-data-preview-text'
        ' nodestep-data-preview-short">Which store sold the most? In 2025.</span>'
        "</span>"
    )
    assert preview_lines(rows[4]) == ["Vienna Neubau, with 180 orders."]
    assert "nodestep-data-preview-short" in head(rows[4])


def test_a_long_text_is_one_line_cut_with_an_ellipsis():
    text = "Revenue per store in 2025. " * 20
    rows = message_rows(
        tree(viewer({"messages": [{"type": "ai", "content": text.strip()}]}))
    )

    (line,) = preview_lines(rows[0])
    assert len(line) == DataTree.preview_length
    assert line.endswith("…")
    assert line.startswith("Revenue per store in 2025. Revenue")
    assert "nodestep-data-preview-short" not in head(rows[0])
    assert "nodestep-data-preview-text" in head(rows[0])
    assert f'<p class="nodestep-data-text">{text.strip()}</p>' in body(rows[0])


def test_each_tool_call_is_one_line_with_its_name_and_arguments():
    message = {
        "type": "ai",
        "content": "Two queries.",
        "tool_calls": [
            {"id": "c1", "name": "list_tables", "arguments": {}},
            {
                "id": "c2",
                "name": "run_sql",
                "arguments": {"sql": "SELECT 1", "limit": 10},
            },
            {"id": "c3", "name": "make_chart", "arguments": None},
        ],
    }

    (row,) = message_rows(tree(viewer({"messages": [message]})))

    assert preview_lines(row) == [
        "Two queries.",
        "list_tables()",
        "run_sql(sql: &#34;SELECT 1&#34;, limit: 10)",
        "make_chart()",
    ]
    assert head(row).count('<span class="nodestep-data-preview-line">') == 3


def test_a_long_tool_call_is_cut_to_one_line():
    sql = "SELECT coalesce(s.name, 'Online shop') AS store " * 20
    message = {
        "type": "ai",
        "content": None,
        "tool_calls": [{"id": "c1", "name": "run_sql", "arguments": {"sql": sql}}],
    }

    (row,) = message_rows(tree(viewer({"messages": [message]})))

    (line,) = preview_lines(row)
    assert html.unescape(line).startswith(
        "run_sql(sql: \"SELECT coalesce(s.name, 'Online shop') AS store"
    )
    assert len(html.unescape(line)) == DataTree.preview_length
    assert line.endswith("…")
    assert json.dumps(sql)[:60] in html.unescape(body(row))


def test_a_tool_result_is_folded_to_its_name_and_a_short_preview():
    rows = message_rows(tree(viewer({"messages": MESSAGES})))

    assert preview_lines(rows[3]) == [
        "rows: [[&#34;Vienna Neubau&#34;, 180]], row_count: 1",
    ]
    assert "nodestep-data-preview-text" not in head(rows[3])
    assert "nodestep-data-preview-short" not in head(rows[3])
    assert '<span class="nodestep-data-key">row_count</span>' in body(rows[3])


def test_a_tool_result_leads_with_what_its_call_was_not_given():
    messages = [
        {
            "type": "ai",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "run_sql",
                    "arguments": {"sql": "SELECT name FROM stores"},
                }
            ],
        },
        {
            "type": "tool",
            "name": "run_sql",
            "tool_call_id": "call_1",
            "content": json.dumps(
                {"sql": "SELECT\n  name\nFROM stores", "columns": ["name"], "count": 1}
            ),
        },
        {
            "type": "tool",
            "name": "run_sql",
            "tool_call_id": "call_9",
            "content": json.dumps({"sql": "SELECT 1", "count": 1}),
        },
    ]

    rows = message_rows(tree(viewer({"messages": messages})))

    assert preview_lines(rows[1]) == [
        "columns: [&#34;name&#34;], count: 1, sql: &#34;SELECT name FROM stores&#34;"
    ]
    assert preview_lines(rows[2]) == ["sql: &#34;SELECT 1&#34;, count: 1"]
    assert body(rows[1]).index("sql") < body(rows[1]).index("columns")


def test_a_genai_tool_result_leads_with_what_its_call_was_not_given():
    messages: list[JsonValue] = [
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "id": "call_1",
                    "name": "run_sql",
                    "arguments": {"sql": "SELECT name FROM stores"},
                }
            ],
        },
        {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": "call_1",
                    "response": {"sql": "SELECT name FROM stores", "count": 1},
                }
            ],
        },
    ]

    previews = DataTree.previews(messages)

    assert [preview.text for preview in previews] == [
        None,
        'count: 1, sql: "SELECT name FROM stores"',
    ]
    assert previews[0].calls == ['run_sql(sql: "SELECT name FROM stores")']


@pytest.mark.parametrize(
    ("count", "lines"),
    [
        (3, ["f0(n: 0)", "f1(n: 1)", "f2(n: 2)"]),
        (4, ["f0(n: 0)", "f1(n: 1)", "2 more calls"]),
        (12, ["f0(n: 0)", "f1(n: 1)", "10 more calls"]),
    ],
)
def test_many_tool_calls_fold_to_two_calls_and_how_many_more(count, lines):
    message = {
        "type": "ai",
        "content": None,
        "tool_calls": [
            {"id": f"c{n}", "name": f"f{n}", "arguments": {"n": n}}
            for n in range(count)
        ],
    }

    (row,) = message_rows(tree(viewer({"messages": [message]})))

    assert preview_lines(row) == lines
    assert body(row).count('<li class="nodestep-data-call">') == count


def test_the_json_of_a_message_marks_what_its_row_already_shows():
    rows = message_rows(tree(viewer({"messages": MESSAGES})))
    shown = '<li class="nodestep-data-entry" data-nodestep-data-echo>'
    plain = '<li class="nodestep-data-entry">'

    def raw(row: str) -> str:
        return row.split('<details class="nodestep-data-raw"><summary>JSON</summary>')[
            1
        ]

    def key(name: str) -> str:
        return f'<span class="nodestep-data-key">{name}</span>'

    tool = raw(rows[3])
    for name in ("name", "content"):
        assert shown + key(name) in tool
    for name in ("id", "type", "tool_call_id"):
        assert plain + key(name) in tool
    call = raw(rows[2])
    assert plain + key("content") in call
    assert plain + key("id") + '<span class="nodestep-data-value' in call
    assert (
        plain + '<details class="nodestep-data-node"><summary>' + key("tool_calls")
    ) in call
    assert shown + key("name") in call
    assert (
        shown + '<details class="nodestep-data-node"><summary>' + key("arguments")
    ) in call
    assert call.count("data-nodestep-data-echo") == 2
    assert shown + key("content") in raw(rows[1])
    assert raw(rows[1]).count("data-nodestep-data-echo") == 1


def test_the_json_of_a_genai_message_marks_the_parts_its_row_already_shows():
    rows = message_rows(tree(viewer({"messages": GENAI_MESSAGES})))
    shown = '<li class="nodestep-data-entry" data-nodestep-data-echo>'

    def raw(row: str) -> str:
        return row.split('<details class="nodestep-data-raw"><summary>JSON</summary>')[
            1
        ]

    def key(name: str) -> str:
        return f'<span class="nodestep-data-key">{name}</span>'

    assert shown + key("content") in raw(rows[1])
    assert raw(rows[1]).count("data-nodestep-data-echo") == 1
    call = raw(rows[2])
    assert shown + key("name") in call
    assert (
        shown + '<details class="nodestep-data-node"><summary>' + key("arguments")
        in call
    )
    assert call.count("data-nodestep-data-echo") == 2
    assert shown + key("response") in raw(rows[3])
    assert raw(rows[3]).count("data-nodestep-data-echo") == 1
    assert raw(rows[4]).count("data-nodestep-data-echo") == 1


def test_a_value_outside_a_message_is_never_marked_as_shown():
    markup = tree(viewer({"name": "x", "content": "y", "tool_calls": [{"name": "z"}]}))

    assert "data-nodestep-data-echo" not in markup


@pytest.mark.parametrize("depth", [1100, 1500])
def test_a_message_nested_deeper_than_python_recursion_still_renders(depth):
    deep = '{"k":' * depth + "1" + "}" * depth
    message = {"type": "tool", "name": "run_sql", "content": deep}
    call: dict[str, JsonValue] = {
        "type": "ai",
        "content": None,
        "tool_calls": [{"name": "f", "arguments": json.loads(deep)}],
    }

    (row,) = message_rows(tree(viewer({"messages": [message]})))
    (line,) = preview_lines(row)
    (call_line,) = DataTree.preview(call).calls

    assert html.unescape(line).startswith('k: {"k": {"k": ')
    assert line.endswith("…")
    assert call_line.startswith('f(k: {"k": {"k": ')
    assert call_line.endswith("…")


def test_a_short_tool_error_is_still_one_line():
    message = {"type": "tool", "name": "run_sql", "content": "Unknown column."}

    (row,) = message_rows(tree(viewer({"messages": [message]})))

    assert preview_lines(row) == ["Unknown column."]
    assert "nodestep-data-preview-short" not in head(row)


def test_a_message_without_text_or_calls_has_no_preview():
    (row,) = message_rows(tree(viewer({"messages": [{"type": "ai", "content": None}]})))

    assert head(row) == (
        '<details class="nodestep-data-message-row">'
        '<summary class="nodestep-data-message-head">'
        '<span class="nodestep-data-role nodestep-data-role-ai">ai</span>'
    )


@pytest.mark.parametrize(
    ("message", "preview"),
    [
        (
            {"type": "human", "content": "Which store?\n  In 2025."},
            MessagePreview(
                text="Which store? In 2025.", prose=True, short=True, calls=[]
            ),
        ),
        (
            {"role": "system", "content": "x" * 300},
            MessagePreview(text="x" * 239 + "…", prose=True, short=False, calls=[]),
        ),
        (
            {"type": "tool", "name": "run_sql", "content": '{"rows": [[1]]}'},
            MessagePreview(text="rows: [[1]]", prose=False, short=False, calls=[]),
        ),
        (
            {"type": "tool", "content": "[1, 2]"},
            MessagePreview(text="[1, 2]", prose=False, short=False, calls=[]),
        ),
        (
            {
                "type": "ai",
                "content": "",
                "tool_calls": [{"name": "f", "arguments": "x"}],
            },
            MessagePreview(text=None, prose=False, short=False, calls=['f("x")']),
        ),
        (
            {"type": "tool", "content": '{"sql": "SELECT\\n  1", "rows": [["a\\tb"]]}'},
            MessagePreview(
                text='sql: "SELECT 1", rows: [["a b"]]',
                prose=False,
                short=False,
                calls=[],
            ),
        ),
        (
            {
                "type": "ai",
                "content": None,
                "tool_calls": [{"name": "f", "arguments": {"sql": "SELECT\n  1"}}],
            },
            MessagePreview(
                text=None, prose=False, short=False, calls=['f(sql: "SELECT 1")']
            ),
        ),
        (
            {"type": "ai", "content": {"answer": "yes"}},
            MessagePreview(text='answer: "yes"', prose=False, short=False, calls=[]),
        ),
    ],
)
def test_the_preview_of_a_message(message, preview):
    assert DataTree.preview(message) == preview


def test_each_message_keeps_its_raw_json_one_click_away():
    rows = message_rows(tree(viewer({"messages": MESSAGES})))

    for row in rows:
        raw = row.split('<details class="nodestep-data-raw"><summary>JSON</summary>')
        assert len(raw) == 2
        assert raw[1].startswith('<ul class="nodestep-data-children">')
        assert '<details class="nodestep-data-node" open>' not in raw[1]
    assert (
        '<span class="nodestep-data-key">tool_call_id</span>'
        '<span class="nodestep-data-value nodestep-token-string">call_1</span>'
    ) in rows[3]


def test_a_tool_result_holding_json_reads_as_a_tree():
    row = message_rows(tree(viewer({"messages": MESSAGES})))[3]
    result = row.split('<details class="nodestep-data-raw">')[0]

    assert "nodestep-data-text" not in result
    assert (
        '<ul class="nodestep-data-children"><li class="nodestep-data-entry">'
        '<details class="nodestep-data-node"><summary>'
        '<span class="nodestep-data-key">rows</span>'
    ) in result
    assert (
        '<span class="nodestep-data-key">row_count</span>'
        '<span class="nodestep-data-value nodestep-token-number">1</span>'
    ) in result


def test_genai_messages_are_rows_with_their_role_text_and_tool_calls():
    rows = message_rows(tree(viewer({"messages": GENAI_MESSAGES})))

    assert len(rows) == 5
    roles = [
        re.findall(r'nodestep-data-role nodestep-data-role-(\w+)">(\w+)<', row)[0]
        for row in rows
    ]
    assert roles == [
        ("system", "system"),
        ("human", "human"),
        ("ai", "ai"),
        ("tool", "tool"),
        ("ai", "ai"),
    ]
    assert '<p class="nodestep-data-text">Which store sold the most?</p>' in rows[1]
    assert preview_lines(rows[1]) == ["Which store sold the most?"]
    assert preview_lines(rows[2]) == ["run_sql(sql: &#34;SELECT name FROM stores&#34;)"]
    assert preview_lines(rows[3]) == [
        "rows: [[&#34;Vienna Neubau&#34;, 180]], row_count: 1",
    ]
    assert "nodestep-data-text" not in rows[2].split("nodestep-data-raw")[0]
    assert (
        '<span class="nodestep-data-call-name">run_sql</span>'
        '<span class="nodestep-data-call-arguments">'
        "{&#34;sql&#34;: &#34;SELECT name FROM stores&#34;}</span>"
    ) in rows[2]
    result = rows[3].split('<details class="nodestep-data-raw">')[0]
    assert (
        '<span class="nodestep-data-key">row_count</span>'
        '<span class="nodestep-data-value nodestep-token-number">1</span>'
    ) in result
    assert (
        '<p class="nodestep-data-text">Vienna Neubau, with 180 orders.</p>' in rows[4]
    )
    assert (
        '<span class="nodestep-data-key">finish_reason</span>'
        '<span class="nodestep-data-value nodestep-token-string">stop</span>'
    ) in rows[4]


def test_the_json_of_a_message_shows_messages_inside_it_as_plain_objects():
    inner = [{"type": "human", "content": "inner"}]

    markup = tree(viewer({"messages": [{"type": "ai", "content": inner}]}))

    assert markup.count('<li class="nodestep-data-message">') == 2
    assert markup.count('<details class="nodestep-data-raw">') == 2


def test_messages_nested_in_their_content_are_cut_into_json_text():
    deep: object = "bottom"
    for _ in range(130):
        deep = [{"type": "human", "content": deep}]

    markup = tree(viewer({"messages": deep}))

    rows = markup.count('<li class="nodestep-data-message">')
    assert rows == DataTree.max_depth // DataTree.open_depth
    assert markup.count('<details class="nodestep-data-raw">') == rows
    assert len(markup) < 300_000
    assert "bottom" in markup


def test_a_list_at_the_top_that_holds_messages_is_shown_as_rows():
    markup = tree(viewer(MESSAGES[:2]))

    assert markup.startswith(
        '<ol class="nodestep-data-messages"><li class="nodestep-data-message">'
    )


@pytest.mark.parametrize(
    "value",
    [
        [{"type": "human", "content": "Hi"}, {"type": "robot", "content": "Hi"}],
        [{"type": "human", "text": "Hi"}],
        [{"role": "user", "parts": "Hi"}],
        [{"role": "robot", "parts": [{"type": "text", "content": "Hi"}]}],
        [{"type": "human", "content": "Hi"}, "plain"],
    ],
)
def test_lists_that_are_not_all_chat_messages_stay_a_tree(value):
    markup = tree(viewer({"items": value}))

    assert "nodestep-data-messages" not in markup
    assert '<span class="nodestep-data-size">' in markup


def test_text_from_the_data_is_escaped():
    markup = viewer({"<b>key</b>": "<script>alert(1)</script>"})

    assert "<script>alert(1)" not in markup
    assert "<b>key" not in markup
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in tree(markup)
    assert '<span class="nodestep-data-key">&lt;b&gt;key&lt;/b&gt;</span>' in tree(
        markup
    )


def test_very_deep_nesting_is_cut_into_json_text():
    deep: object = "bottom"
    for _ in range(200):
        deep = {"level": deep}

    markup = tree(viewer(deep))

    assert markup.count('<details class="nodestep-data-node"') == DataTree.max_depth
    assert (
        '<span class="nodestep-data-value nodestep-token-operator">'
        "{&#34;level&#34;: {&#34;level&#34;:"
    ) in markup
    assert "bottom" in markup


@pytest.mark.parametrize(
    "text",
    [
        '"plain"',
        "3",
        "null",
        "[]",
        "{}",
        '["stop"]',
        '["a", 1, null]',
        '{"messages": ["hello", "wor',
        "not json",
    ],
)
def test_only_objects_and_arrays_of_objects_or_arrays_become_a_tree(text):
    assert DataView.from_text(text) is None


@pytest.mark.parametrize("text", ['{"a": 1}', '[{"a": 1}]', "[[1]]"])
def test_objects_and_nested_arrays_become_a_tree(text):
    data = DataView.from_text(text)

    assert data is not None
    assert data.value == json.loads(text)
    assert data.text == text


def test_a_block_falls_back_to_a_code_box_and_a_cell_to_plain_text():
    assert render("{{ data_block(text, 'x') }}", text="not json") == (
        '<div class="nodestep-code nodestep-code-wrap"><pre>not json</pre></div>'
    )
    assert render("{{ data_cell(text, 'x') }}", text='["stop"]') == (
        "<pre>[&#34;stop&#34;]</pre>"
    )
    for name in ("data_block", "data_cell"):
        markup = render(
            f"{{{{ {name}(text, 'gen_ai.input.messages') }}}}", text='{"a": 1}'
        )
        assert markup.startswith(
            '<div class="nodestep-data" role="group"'
            ' aria-label="gen_ai.input.messages">'
        )
        assert 'aria-label="Search gen_ai.input.messages"' in markup


@pytest.mark.parametrize(
    ("value", "size"),
    [
        ({"a": 1}, "1 key"),
        ({"a": 1, "b": 2}, "2 keys"),
        ([1], "1 item"),
        ([1, 2, 3], "3 items"),
        ([{"type": "human", "content": "Hi"}], "1 message"),
        ([{"role": "user", "parts": []}], "1 message"),
        (MESSAGES, "5 messages"),
    ],
)
def test_sizes_name_keys_items_and_messages(value, size):
    assert DataTree.size(value) == size
