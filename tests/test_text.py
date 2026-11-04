"""Characterize readable, recoverable ADF summaries."""

import pytest

from jira_mcp.text import find_markup_warnings, markdown_to_adf, plain_text_to_adf, render_text, with_warnings


def _task(state, *content):
    """Build an ADF task item with inline content."""
    return {"type": "taskItem", "attrs": {"localId": "id", "state": state}, "content": list(content)}


# ADF nests a task list as a sibling after its parent task item.
TASK_LIST_DOC = {
    "type": "doc",
    "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Action items"}]},
        {
            "type": "taskList",
            "content": [
                _task(
                    "TODO",
                    {"type": "text", "text": "Run "},
                    {"type": "text", "text": "uv sync", "marks": [{"type": "code"}]},
                ),
                _task("DONE", {"type": "text", "text": "Ship"}, {"type": "hardBreak"}, {"type": "text", "text": "it"}),
                {"type": "taskList", "content": [_task("TODO", {"type": "text", "text": "Nested"})]},
            ],
        },
    ],
}
TASK_LIST_TEXT = "Action items\n- [ ] Run `uv sync`\n- [x] Ship\n      it\n  - [ ] Nested"


@pytest.mark.parametrize(
    ("text", "content"),
    [
        ("", [{"type": "paragraph"}]),
        ("hello", [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]),
        (
            "café\r\n\r\n🙂\rfinal\n",
            [
                {"type": "paragraph", "content": [{"type": "text", "text": "café"}]},
                {"type": "paragraph"},
                {"type": "paragraph", "content": [{"type": "text", "text": "🙂"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "final"}]},
                {"type": "paragraph"},
            ],
        ),
        (
            "**not markdown**",
            [{"type": "paragraph", "content": [{"type": "text", "text": "**not markdown**"}]}],
        ),
    ],
)
def test_plain_text_to_adf(text, content):
    """Preserve normalized line breaks and blank lines without empty text nodes."""
    assert plain_text_to_adf(text) == {"type": "doc", "version": 1, "content": content}


def _para(*content):
    """Build an ADF paragraph, leaving out empty content."""
    return {"type": "paragraph", "content": list(content)} if content else {"type": "paragraph"}


def _text(text, *, code=False, strong=False):
    """Build an ADF text node, optionally with a code or strong mark."""
    if code:
        return {"type": "text", "text": text, "marks": [{"type": "code"}]}
    if strong:
        return {"type": "text", "text": text, "marks": [{"type": "strong"}]}
    return {"type": "text", "text": text}


def _bullets(*items):
    """Build an ADF bullet list with one paragraph per item."""
    return {"type": "bulletList", "content": [{"type": "listItem", "content": [item]} for item in items]}


@pytest.mark.parametrize(
    ("markdown", "content"),
    [
        (
            "Fixed in `main`.\r\nSee DEMO-1 https://example.org/x\n\n \n- run `uv sync`\n- \nDone",
            [
                _para(
                    _text("Fixed in "),
                    _text("main", code=True),
                    _text("."),
                    {"type": "hardBreak"},
                    _text("See DEMO-1 https://example.org/x"),
                ),
                _bullets(_para(_text("run "), _text("uv sync", code=True)), _para()),
                _para(_text("Done")),
            ],
        ),
        ("\n\nan `unclosed backtick\n\n", [_para(_text("an `unclosed backtick"))]),
        ("Done\n", [_para(_text("Done"))]),
        ("- a\n", [_bullets(_para(_text("a")))]),
        ("a\n\n    b", [_para(_text("a")), _para(_text("    b"))]),
        ("``  - not a list", [_para(_text("``  - not a list"))]),
    ],
)
def test_markdown_to_adf(markdown, content):
    """Convert paragraphs, bullet lists, and inline code, leaving other text unchanged."""
    assert markdown_to_adf(markdown) == {"type": "doc", "version": 1, "content": content}


def test_markdown_to_adf_round_trips_through_rendering():
    """Render converted code marks and bullets back as Markdown-style text."""
    markdown = "Closing.\n\n- fixed `parse()`\n- added tests"

    rendered = render_text(markdown_to_adf(markdown), limit=100, include_raw=False)["text"]

    assert rendered == "Closing.\n- fixed `parse()`\n- added tests"


def test_markdown_extended_subset():
    """Convert headings, ordered lists and safe links while preserving code and unsafe links."""
    text = "## Heading\n3. [site](https://example.com)\n4. `[x](https://x)`\n[bad](javascript:alert)"
    result = markdown_to_adf(text)["content"]
    assert result == [
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("Heading")]},
        {
            "type": "orderedList",
            "attrs": {"order": 3},
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        _para(
                            {
                                "type": "text",
                                "text": "site",
                                "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}],
                            }
                        )
                    ],
                },
                {"type": "listItem", "content": [_para(_text("[x](https://x)", code=True))]},
            ],
        },
        _para(_text("[bad](javascript:alert)")),
    ]


@pytest.mark.parametrize(
    ("markdown", "content"),
    [
        ("**bold**", [{"type": "text", "text": "bold", "marks": [{"type": "strong"}]}]),
        ("__bold__", [{"type": "text", "text": "bold", "marks": [{"type": "strong"}]}]),
        ("*italic*", [{"type": "text", "text": "italic", "marks": [{"type": "em"}]}]),
        ("_italic_", [{"type": "text", "text": "italic", "marks": [{"type": "em"}]}]),
        (
            "**Pipeline:** [#123](https://example.com/p/123) passed",
            [
                {"type": "text", "text": "Pipeline:", "marks": [{"type": "strong"}]},
                _text(" "),
                {
                    "type": "text",
                    "text": "#123",
                    "marks": [{"type": "link", "attrs": {"href": "https://example.com/p/123"}}],
                },
                _text(" passed"),
            ],
        ),
        (
            "**[label](https://example.com/a_b)**",
            [
                {
                    "type": "text",
                    "text": "label",
                    "marks": [{"type": "strong"}, {"type": "link", "attrs": {"href": "https://example.com/a_b"}}],
                }
            ],
        ),
        (
            "[**label**](https://example.com)",
            [
                {
                    "type": "text",
                    "text": "label",
                    "marks": [{"type": "strong"}, {"type": "link", "attrs": {"href": "https://example.com"}}],
                }
            ],
        ),
        ("`**not bold** _or italic_`", [_text("**not bold** _or italic_", code=True)]),
        (
            "**bold** `__init__`",
            [{"type": "text", "text": "bold", "marks": [{"type": "strong"}]}, _text(" "), _text("__init__", code=True)],
        ),
        ("snake_case_identifier foo__bar__baz", [_text("snake_case_identifier foo__bar__baz")]),
        ("a * b * c", [_text("a * b * c")]),
        ("unmatched **marker", [_text("unmatched **marker")]),
        ("x * spaced* and *end *", [_text("x * spaced* and *end *")]),
        ("***unsupported***", [_text("***unsupported***")]),
        (
            "**a`code`**",
            [{"type": "text", "text": "a", "marks": [{"type": "strong"}]}, _text("code", code=True)],
        ),
        ("**`code`**", [_text("code", code=True)]),
        (
            "**outer _unfinished end**",
            [{"type": "text", "text": "outer _unfinished end", "marks": [{"type": "strong"}]}],
        ),
        (
            "**outer _cross** end_",
            [{"type": "text", "text": "outer _cross", "marks": [{"type": "strong"}]}, _text(" end_")],
        ),
        (
            "**outer _inner_ end**",
            [
                {"type": "text", "text": "outer ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": "inner", "marks": [{"type": "strong"}, {"type": "em"}]},
                {"type": "text", "text": " end", "marks": [{"type": "strong"}]},
            ],
        ),
    ],
)
def test_markdown_emphasis(markdown, content):
    """Convert paired emphasis without changing code, identifiers, or unmatched markers."""
    # Act
    result = markdown_to_adf(markdown)

    # Assert
    assert result == {"type": "doc", "version": 1, "content": [_para(*content)]}


@pytest.mark.parametrize(
    ("markdown", "content"),
    [
        ("h3. Title", [_para(_text("h3. Title"))]),
        ("use {{code}} here", [_para(_text("use {{code}} here"))]),
        ("* item", [_bullets(_para(_text("item")))]),
        ("- a\n* b", [_bullets(_para(_text("a")), _para(_text("b")))]),
        ("* text*", [_bullets(_para(_text("text*")))]),
        ("**bold** x", [_para(_text("bold", strong=True), _text(" x"))]),
        ("*em* line", [_para({"type": "text", "text": "em", "marks": [{"type": "em"}]}, _text(" line"))]),
        ("*a", [_para(_text("*a"))]),
    ],
)
def test_markdown_wiki_markup_and_bullet_markers(markdown, content):
    """Keep wiki markup literal and treat dash or star plus a space as a bullet."""
    # Act
    result = markdown_to_adf(markdown)

    # Assert
    assert result == {"type": "doc", "version": 1, "content": content}


@pytest.mark.parametrize(
    ("markdown", "content"),
    [
        (
            "- [x](https://example.com)",
            [
                _bullets(
                    _para(
                        {
                            "type": "text",
                            "text": "x",
                            "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}],
                        }
                    )
                )
            ],
        ),
    ],
)
def test_markdown_block_syntax_stays_literal(markdown, content):
    """Characterize a Markdown link in a bullet staying a bullet, not a task."""
    # Act
    result = markdown_to_adf(markdown)

    # Assert
    assert result == {"type": "doc", "version": 1, "content": content}


def _code(code=None, language=None):
    """Build an ADF code block, leaving out empty content and absent language."""
    block = {"type": "codeBlock"}
    if language:
        block["attrs"] = {"language": language}
    if code:
        block["content"] = [{"type": "text", "text": code}]
    return block


@pytest.mark.parametrize(
    ("markdown", "content"),
    [
        ("```\ncode\n```", [_code("code")]),
        ("```python extra\nx = 1\n\n  y = 2\n```", [_code("x = 1\n\n  y = 2", "python")]),
        ("intro\n```sh\nls\n```\noutro", [_para(_text("intro")), _code("ls", "sh"), _para(_text("outro"))]),
        ("```\n```", [_code()]),
        ("```\nopen\n\n- not a list", [_code("open\n\n- not a list")]),
        ("```\n- a\n# b\n**c**\n```  ", [_code("- a\n# b\n**c**")]),
        ("```x```", [_para(_text("``"), _text("x", code=True), _text("``"))]),
        ("```\na\n```\n\n```\nb\n```", [_code("a"), _code("b")]),
        ("```\n```python\n```", [_code("```python")]),
    ],
)
def test_markdown_fenced_code_blocks(markdown, content):
    """Keep fenced lines verbatim, including blank lines, and close only on a bare fence."""
    # Act
    result = markdown_to_adf(markdown)

    # Assert
    assert result == {"type": "doc", "version": 1, "content": content}


RULE = {"type": "rule"}


def _quote(*content):
    """Build an ADF blockquote around one paragraph of inline content."""
    return {"type": "blockquote", "content": [_para(*content)]}


@pytest.mark.parametrize(
    ("markdown", "content"),
    [
        ("---", [RULE]),
        ("text\n---\nmore", [_para(_text("text")), RULE, _para(_text("more"))]),
        ("***\n___\n----  ", [RULE, RULE, RULE]),
        ("- - -", [_bullets(_para(_text("- -")))]),
        ("--\n\n  ---", [_para(_text("--")), _para(_text("  ---"))]),
        ("> a\n> b", [_quote(_text("a"), {"type": "hardBreak"}, _text("b"))]),
        ("> a\n>\n> b", [_quote(_text("a"), {"type": "hardBreak"}, {"type": "hardBreak"}, _text("b"))]),
        ("> **x** `y`", [_quote(_text("x", strong=True), _text(" "), _text("y", code=True))]),
        ("> a\n\n> b", [_quote(_text("a")), _quote(_text("b"))]),
        ("> - not a list", [_quote(_text("- not a list"))]),
        (">= 2.0", [_para(_text(">= 2.0"))]),
    ],
)
def test_markdown_rules_and_blockquotes(markdown, content):
    """Convert rule lines and quoted lines while leaving lookalikes as text."""
    # Act
    result = markdown_to_adf(markdown)

    # Assert
    assert result == {"type": "doc", "version": 1, "content": content}


def _task(position, number, state, *content):
    """Build an ADF task item with a position-based id and inline content."""
    item = {"type": "taskItem", "attrs": {"localId": f"task-{position}-{number}", "state": state}}
    if content:
        item["content"] = list(content)
    return item


def _task_list(position, *items):
    """Build an ADF task list with a position-based id."""
    return {"type": "taskList", "attrs": {"localId": f"tasklist-{position}"}, "content": list(items)}


@pytest.mark.parametrize(
    ("markdown", "content"),
    [
        (
            "- [ ] todo\n- [x] done **b**\n* [X] star",
            [
                _task_list(
                    0,
                    _task(0, 1, "TODO", _text("todo")),
                    _task(0, 2, "DONE", _text("done "), _text("b", strong=True)),
                    _task(0, 3, "DONE", _text("star")),
                )
            ],
        ),
        ("- [ ]", [_task_list(0, _task(0, 1, "TODO"))]),
        ("- [ ]text", [_bullets(_para(_text("[ ]text")))]),
        ("- [ ] a\n- b", [_task_list(0, _task(0, 1, "TODO", _text("a"))), _bullets(_para(_text("b")))]),
        (
            "intro\n\n- [ ] a\n\n- [x] b",
            [
                _para(_text("intro")),
                _task_list(1, _task(1, 1, "TODO", _text("a"))),
                _task_list(2, _task(2, 1, "DONE", _text("b"))),
            ],
        ),
    ],
)
def test_markdown_task_lists(markdown, content):
    """Convert checkbox bullets to task lists with stable ids, leaving lookalikes as bullets."""
    # Act
    result = markdown_to_adf(markdown)

    # Assert
    assert result == {"type": "doc", "version": 1, "content": content}


def test_markdown_task_list_round_trips_through_rendering():
    """Render a converted task list as the checklist text that read tools show."""
    # Act
    rendered = render_text(markdown_to_adf("- [ ] todo\n- [x] done"), limit=100, include_raw=False)

    # Assert
    assert rendered["text"] == "- [ ] todo\n- [x] done"


@pytest.mark.parametrize("level", range(1, 7))
def test_markdown_heading_levels(level):
    """Support each ADF heading level."""
    assert markdown_to_adf(f"{'#' * level} Title")["content"][0]["attrs"] == {"level": level}


@pytest.mark.parametrize("url", ["http://example.com", "mailto:team@example.com", "HTTPS://example.com"])
def test_markdown_links_with_surrounding_text(url):
    """Keep surrounding text and multiple links while accepting supported schemes."""
    markdown = f"Read [one]({url}) and [two]({url}); [unfinished](https://example.com"

    content = markdown_to_adf(markdown)["content"]

    assert content == [
        _para(
            _text("Read "),
            {"type": "text", "text": "one", "marks": [{"type": "link", "attrs": {"href": url}}]},
            _text(" and "),
            {"type": "text", "text": "two", "marks": [{"type": "link", "attrs": {"href": url}}]},
            _text("; [unfinished](https://example.com"),
        )
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, ""),
        ("plain", "plain"),
        (
            {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "link",
                                "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}],
                            },
                            {"type": "hardBreak"},
                            {"type": "mention", "attrs": {"text": "@Reader"}},
                            {"type": "emoji", "attrs": {"text": "🙂"}},
                            {"type": "inlineCard", "attrs": {"url": "https://example.com/card"}},
                        ],
                    },
                    {"type": "media"},
                    {"type": "unknown"},
                ],
            },
            "link (https://example.com)\n@Reader🙂https://example.com/card\n[media]\n[unknown]",
        ),
        (
            {
                "type": "doc",
                "content": [
                    {
                        "type": "bulletList",
                        "content": [
                            {
                                "type": "listItem",
                                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "One"}]}],
                            },
                            {
                                "type": "listItem",
                                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Two"}]}],
                            },
                        ],
                    }
                ],
            },
            "- One\n- Two",
        ),
        (TASK_LIST_DOC, TASK_LIST_TEXT),
    ],
)
def test_readable_adf(body, expected):
    """Keep block boundaries, links, and visible unsupported-node placeholders."""
    assert render_text(body, limit=1000, include_raw=False) == {
        "text": expected,
        "characters": len(expected),
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {
                "type": "text",
                "text": "same",
                "marks": [
                    None,
                    {"type": "strong"},
                    {"type": "link", "attrs": {"href": "same"}},
                    {"type": "link", "attrs": {}},
                ],
            },
            "same",
        ),
        (
            {"type": "text", "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}]},
            "https://example.com",
        ),
        ({"type": "text", "text": 42}, ""),
        ({"type": "text", "text": "uv sync", "marks": [{"type": "code"}, {"type": "strong"}]}, "`uv sync`"),
        (
            {
                "type": "text",
                "text": "make check",
                "marks": [{"type": "code"}, {"type": "link", "attrs": {"href": "https://example.com"}}],
            },
            "`make check` (https://example.com)",
        ),
        (
            {
                "type": "orderedList",
                "attrs": {"order": "invalid"},
                "content": [
                    {
                        "type": "listItem",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "One"}]}],
                    },
                ],
            },
            "1. One",
        ),
        (
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [{"type": "text", "text": "A"}]},
                            {"type": "tableCell", "content": [{"type": "text", "text": "B"}]},
                        ],
                    }
                ],
            },
            "A\tB",
        ),
        ({"type": "mediaGroup", "content": [{"type": "media"}]}, "[media]"),
        ({"type": "rule"}, "---"),
        ({"type": "doc", "content": [42]}, "[unknown]"),
        ({"type": "doc", "content": "invalid"}, ""),
        ({"type": "mention", "attrs": None}, "[mention]"),
        ({"type": "mention", "attrs": {"id": "account-id"}}, "account-id"),
        ({"type": "emoji", "attrs": {"shortName": ":smile:"}}, ":smile:"),
        ({"type": "emoji"}, "[emoji]"),
        ({"type": "inlineCard"}, "[card]"),
        ({"type": "taskList", "content": [{"type": "taskItem", "attrs": {"state": 1}}, 42]}, "- [ ] \n- [ ] [unknown]"),
        (
            {
                "type": "taskList",
                "content": [
                    {
                        "type": "blockTaskItem",
                        "attrs": {"state": "DONE"},
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "Ship"}]},
                            {"type": "paragraph", "content": [{"type": "text", "text": "it"}]},
                        ],
                    }
                ],
            },
            "- [x] Ship\n      it",
        ),
    ],
)
def test_adf_structure_and_fallbacks(body, expected):
    """Keep common structures and malformed content readable without crashing."""
    result = render_text(body, limit=1000, include_raw=True)

    assert result == {"text": expected, "characters": len(expected), "truncated": False, "adf": body}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("h3. Title", ["wiki heading"]),
        ("use {{code}} here", ["wiki monospace"]),
        ("{code:python}\nx = 1\n{code}", ["wiki block macro"]),
        ("see [docs|https://example.com/d]", ["wiki link"]),
        ("||a||b||", ["wiki table"]),
        ("** nested", ["wiki nested bullet"]),
        ("*** \n___", []),
        ("# one\n# two", ["H1 headings"]),
        ("h3. A\r\n{{x}}", ["wiki heading", "wiki monospace"]),
        ("`h3. Title` and `{{x}}`", []),
        ("`open\nh3. Title\nclose`", ["wiki heading"]),
        ("# one\ntext\n# two", []),
        ("```\nh3. Title\n{{x}}\n```", []),
        ("# one\n```\nx\n```\n# two", []),
        ("### Title\n**bold** [docs](https://example.com)\n1. one", []),
    ],
)
def test_find_markup_warnings(text, expected):
    """Warn once per wiki construct, skipping code spans and valid Markdown."""
    # Act
    warnings = find_markup_warnings(text)

    # Assert
    assert len(warnings) == len(expected)
    assert all(part in warning for part, warning in zip(expected, warnings, strict=True))


def test_with_warnings_adds_key_only_when_needed():
    """Keep the result unchanged unless the Markdown looks like wiki markup."""
    # Arrange
    result = {"id": "1"}

    # Act and Assert
    assert with_warnings(result, "clean **text**") == {"id": "1"}
    assert with_warnings(result, "h3. Title")["warnings"] == find_markup_warnings("h3. Title")
