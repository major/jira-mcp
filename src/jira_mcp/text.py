"""Convert plain text or simple Markdown to ADF and render Jira content as readable text."""

import re
from itertools import groupby
from typing import Any

# Split on one or more blank lines (lines with only whitespace count as blank).
BLANK_LINES = re.compile(r"\n(?:[^\S\n]*\n)+")
# The capture group makes re.split put each code span at an odd index.
INLINE_CODE = re.compile(r"`([^`]+)`")
BULLET = "- "
HEADING = re.compile(r"^(#{1,6}) (.*)$")
ORDERED = re.compile(r"^([0-9]{1,9})\. (.*)$")
LINK = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+|mailto:[^\s()]+)\)", re.IGNORECASE)


def plain_text_to_adf(text: str) -> dict[str, Any]:
    """Convert plain text to minimal ADF paragraphs, preserving normalized line breaks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[dict[str, Any]] = []
    for line in normalized.split("\n"):
        paragraph: dict[str, Any] = {"type": "paragraph"}
        # ADF text nodes must not be empty, so blank lines stay paragraphs without content.
        if line:
            paragraph["content"] = [{"type": "text", "text": line}]
        paragraphs.append(paragraph)
    return {"type": "doc", "version": 1, "content": paragraphs}


def markdown_to_adf(text: str) -> dict[str, Any]:
    """Convert a small Markdown subset to ADF and keep everything else as plain text.

    Support headings, flat bullet and ordered lists, inline code, and explicit safe links.
    Other single line breaks become hard breaks; bare URLs and issue keys stay text.
    """
    # Stripping first keeps a leading or trailing newline from adding a hard break or empty paragraph.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks: list[dict[str, Any]] = []
    for chunk in BLANK_LINES.split(normalized):
        for kind, group in groupby(chunk.split("\n"), key=_block_kind):
            lines = list(group)
            if kind == "bullet":
                items = [{"type": "listItem", "content": [_paragraph([line[len(BULLET) :]])]} for line in lines]
                blocks.append({"type": "bulletList", "content": items})
            elif kind == "ordered":
                matches = [ORDERED.match(line) for line in lines]
                items = [{"type": "listItem", "content": [_paragraph([match.group(2)])]} for match in matches if match]
                blocks.append({"type": "orderedList", "attrs": {"order": int(matches[0].group(1))}, "content": items})
            elif kind == "heading":
                for line in lines:
                    match = HEADING.match(line)
                    if match is None:
                        continue
                    paragraph = _paragraph([match.group(2)])
                    blocks.append(
                        {
                            "type": "heading",
                            "attrs": {"level": len(match.group(1))},
                            **{key: value for key, value in paragraph.items() if key == "content"},
                        }
                    )
            else:
                blocks.append(_paragraph(lines))
    return {"type": "doc", "version": 1, "content": blocks}


def _block_kind(line: str) -> str:
    """Classify a single supported block without interpreting nested Markdown."""
    if line.startswith(BULLET):
        return "bullet"
    if ORDERED.match(line):
        return "ordered"
    if HEADING.match(line):
        return "heading"
    return "paragraph"


def _paragraph(lines: list[str]) -> dict[str, Any]:
    """Join lines with hard breaks in one paragraph, leaving out empty text nodes."""
    content: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if index:
            content.append({"type": "hardBreak"})
        for position, part in enumerate(INLINE_CODE.split(line)):
            if part:
                if position % 2:
                    content.append({"type": "text", "text": part, "marks": [{"type": "code"}]})
                else:
                    start = 0
                    for match in LINK.finditer(part):
                        if match.start() > start:
                            content.append({"type": "text", "text": part[start : match.start()]})
                        content.append(
                            {
                                "type": "text",
                                "text": match.group(1),
                                "marks": [{"type": "link", "attrs": {"href": match.group(2)}}],
                            }
                        )
                        start = match.end()
                    if start < len(part):
                        content.append({"type": "text", "text": part[start:]})
    paragraph: dict[str, Any] = {"type": "paragraph"}
    if content:
        paragraph["content"] = content
    return paragraph


def render_text(body: Any, *, limit: int, include_raw: bool, offset: int = 0) -> dict[str, Any]:
    """Render up to limit characters starting at offset while reporting the full character count.

    An offset past the end returns empty text that is not truncated.
    """
    if body is None:
        text = ""
    elif isinstance(body, str):
        text = body
    else:
        text = _render_node(body)

    length = len(text)
    limit = max(limit, 0)
    end = offset + limit
    result: dict[str, Any] = {"text": text[offset:end], "characters": length, "truncated": length > end}
    if include_raw:
        result["adf"] = body
    return result


def _content(node: dict[str, Any]) -> list[Any]:
    """Return a node's children when they have the expected shape."""
    content = node.get("content")
    return content if isinstance(content, list) else []


def _attrs(node: dict[str, Any]) -> dict[str, Any]:
    """Return a node's attributes when they have the expected shape."""
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _string(value: Any) -> str:
    """Return a string value or an empty string for malformed data."""
    return value if isinstance(value, str) else ""


def _render_text_node(node: dict[str, Any]) -> str:
    """Wrap code marks in backticks and include link destinations from link marks."""
    text = _string(node.get("text"))
    marks = node.get("marks")
    marks = [mark for mark in marks if isinstance(mark, dict)] if isinstance(marks, list) else []
    rendered = f"`{text}`" if text and any(mark.get("type") == "code" for mark in marks) else text
    for mark in marks:
        if mark.get("type") == "link":
            url = _string(_attrs(mark).get("href"))
            if url and url != text:
                return f"{rendered} ({url})" if text else url
    return rendered


def _render_list(node: dict[str, Any], *, ordered: bool) -> str:
    """Render list items with visible markers and indented continuation lines."""
    start = _attrs(node).get("order", 1)
    if not isinstance(start, int) or isinstance(start, bool):
        start = 1
    lines = []
    for index, item in enumerate(_content(node), start=start):
        marker = f"{index}. " if ordered else "- "
        text = _render_node(item)
        lines.append(marker + text.replace("\n", "\n" + " " * len(marker)))
    return "\n".join(lines)


def _render_task_list(node: dict[str, Any]) -> str:
    """Render task items as checklist lines and indent nested task lists.

    ADF nests a task list as a sibling that follows its parent task item, not as
    a child of that item, so nesting is handled at the list level.
    """
    lines = []
    for child in _content(node):
        if isinstance(child, dict) and child.get("type") == "taskList":
            lines.append("  " + _render_task_list(child).replace("\n", "\n  "))
            continue
        marker = "- [x] " if isinstance(child, dict) and _attrs(child).get("state") == "DONE" else "- [ ] "
        lines.append(marker + _render_node(child).replace("\n", "\n" + " " * len(marker)))
    return "\n".join(lines)


def _render_inline(node: dict[str, Any]) -> str | None:
    """Render recognized inline nodes, or return None for other types."""
    kind = node.get("type")
    attrs = _attrs(node)
    if kind == "text":
        return _render_text_node(node)
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        fallback, placeholder = attrs.get("id"), "[mention]"
    elif kind == "emoji":
        fallback, placeholder = attrs.get("shortName"), "[emoji]"
    elif kind in ("inlineCard", "blockCard", "embedCard"):
        return _string(attrs.get("url")) or "[card]"
    elif kind in ("media", "mediaInline"):
        return "[media]"
    else:
        return None
    return _string(attrs.get("text")) or _string(fallback) or placeholder


def _render_node(node: Any) -> str:
    """Render common ADF nodes and leave placeholders for unsupported nodes."""
    if not isinstance(node, dict):
        return "[unknown]"
    inline = _render_inline(node)
    if inline is not None:
        return inline
    kind = node.get("type")
    children = _content(node)
    if kind in ("mediaSingle", "mediaGroup"):
        return "\n".join(_render_node(child) for child in children) or "[media]"
    if kind in ("bulletList", "orderedList", "taskList"):
        return _render_task_list(node) if kind == "taskList" else _render_list(node, ordered=kind == "orderedList")
    if kind == "tableRow":
        separator = "\t"
    elif kind in ("paragraph", "heading", "codeBlock", "taskItem"):
        separator = ""
    elif kind in (
        "doc",
        "blockquote",
        "panel",
        "listItem",
        "blockTaskItem",
        "table",
        "tableCell",
        "tableHeader",
        "expand",
    ):
        separator = "\n"
    else:
        return "---" if kind == "rule" else "[unknown]"
    return separator.join(_render_node(child) for child in children)
