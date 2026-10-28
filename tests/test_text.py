"""Characterize readable, recoverable ADF summaries."""

import pytest

from jira_mcp.text import plain_text_to_adf, render_text


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
    ],
)
def test_adf_structure_and_fallbacks(body, expected):
    """Keep common structures and malformed content readable without crashing."""
    result = render_text(body, limit=1000, include_raw=True)

    assert result == {"text": expected, "characters": len(expected), "truncated": False, "adf": body}
