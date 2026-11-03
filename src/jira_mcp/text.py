"""Convert plain text or simple Markdown to ADF and render Jira content as readable text."""

import hashlib
import json
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
INLINE_TOKEN = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+|mailto:[^\s()]+)\)|\*+|_+", re.IGNORECASE)


def hash_adf(adf: dict[str, Any]) -> str:
    """Hash the complete canonical UTF-8 ADF JSON document."""
    encoded = json.dumps(adf, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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

    Support headings, flat lists, inline code, emphasis, and explicit safe links.
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
                matches = [match for line in lines if (match := ORDERED.match(line)) is not None]
                items = [{"type": "listItem", "content": [_paragraph([match.group(2)])]} for match in matches]
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
        _inline(line, content)
    paragraph: dict[str, Any] = {"type": "paragraph"}
    if content:
        paragraph["content"] = content
    return paragraph


def _append_text(content: list[dict[str, Any]], text: str, marks: list[dict[str, Any]]) -> None:
    """Append text, joining adjacent nodes with the same marks."""
    if not text:
        return
    if content and content[-1].get("type") == "text" and content[-1].get("marks", []) == marks:
        content[-1]["text"] += text
        return
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    content.append(node)


def _inline_tokens(line: str, *, in_link: bool) -> tuple[list[tuple[str, str, Any]], dict[int, int]]:
    """Scan inline tokens and pair only properly nested delimiter runs."""
    tokens: list[tuple[str, str, Any]] = []
    stack: list[int] = []
    openings: dict[str, list[int]] = {marker: [] for marker in ("*", "**", "_", "__")}
    pairs: dict[int, int] = {}
    parts = INLINE_CODE.split(line)
    offset = 0
    for position, part in enumerate(parts):
        if position % 2:
            tokens.append(("code", part, None))
            offset += len(part) + 2
            continue
        start = 0
        for match in INLINE_TOKEN.finditer(part):
            tokens.append(("text", part[start : match.start()], None))
            value = match.group()
            if match.group(1) is not None and not in_link:
                tokens.append(("link", match.group(1), match.group(2)))
            elif match.group(1) is not None:
                tokens.append(("text", value, None))
            else:
                beginning, end = offset + match.start(), offset + match.end()
                previous = line[beginning - 1] if beginning else ""
                following = line[end] if end < len(line) else ""
                intraword = value[0] == "_" and previous.isalnum() and following.isalnum()
                can_open = bool(following and not following.isspace()) and not intraword
                can_close = bool(previous and not previous.isspace()) and not intraword
                index = len(tokens)
                tokens.append(("delimiter", value, None))
                if value in openings:
                    if can_close and openings[value]:
                        opening = openings[value][-1]
                        # Unmatched inner openers stay literal, rather than blocking the outer pair.
                        while stack[-1] != opening:
                            openings[tokens[stack.pop()][1]].pop()
                        stack.pop()
                        openings[value].pop()
                        pairs[opening] = index
                        pairs[index] = opening
                    elif can_open:
                        stack.append(index)
                        openings[value].append(index)
            start = match.end()
        tokens.append(("text", part[start:], None))
        offset += len(part)
    return tokens, pairs


def _inline(line: str, content: list[dict[str, Any]], link: str | None = None) -> None:
    """Render paired inline tokens, keeping code and link destinations opaque."""
    tokens, pairs = _inline_tokens(line, in_link=link is not None)
    active: dict[str, int] = {"strong": 0, "em": 0}
    for index, (kind, value, extra) in enumerate(tokens):
        if kind == "delimiter" and index in pairs:
            mark = "strong" if value in ("**", "__") else "em"
            active[mark] += 1 if pairs[index] > index else -1
            continue
        marks: list[dict[str, Any]] = [{"type": mark} for mark in ("strong", "em") if active[mark]]
        if link is not None:
            marks.append({"type": "link", "attrs": {"href": link}})
        if kind == "code":
            _append_text(content, value, [{"type": "code"}])
        elif kind == "link":
            label: list[dict[str, Any]] = []
            _inline(value, label, extra)
            for node in label:
                label_marks = node.get("marks", [])
                combined = [
                    {"type": mark}
                    for mark in ("strong", "em")
                    if active[mark] or any(item["type"] == mark for item in label_marks)
                ]
                combined.extend(item for item in label_marks if item["type"] == "link")
                _append_text(content, node["text"], combined)
        else:
            _append_text(content, value, marks)


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
