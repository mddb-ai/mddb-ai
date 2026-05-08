from __future__ import annotations

"""Frontmatter parser / serializer.

Format: if the file begins with ``---\\n``, everything up to the next ``\\n---\\n`` is YAML
frontmatter, and what follows is the body. On serialize, key order follows the input dict order.
"""

from typing import Any

import yaml

from mddbai.core.errors import FrontmatterParseError

_DELIM = "---"


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split text into frontmatter and body.

    Returns ``({}, text)`` if there is no frontmatter block.
    """

    if not text.startswith(_DELIM + "\n") and not text.startswith(_DELIM + "\r\n"):
        return {}, text

    # Search from the line after the delimiter until the closing delimiter
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != _DELIM:
        return {}, text

    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == _DELIM:
            end_idx = i
            break

    if end_idx is None:
        raise FrontmatterParseError("frontmatter delimiter not closed")

    yaml_block = "\n".join(line.rstrip("\r") for line in lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])

    try:
        loaded = yaml.safe_load(yaml_block) if yaml_block.strip() else {}
    except yaml.YAMLError as exc:
        raise FrontmatterParseError(f"invalid YAML in frontmatter: {exc}") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise FrontmatterParseError("frontmatter must decode to a mapping")
    return loaded, body


def render(meta: dict[str, Any], body: str) -> str:
    """Serialize frontmatter + body. If ``meta`` is empty, frontmatter is omitted."""

    if not meta:
        return body
    yaml_text = yaml.safe_dump(
        meta,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    if yaml_text.endswith("\n"):
        yaml_text = yaml_text[:-1]
    return f"{_DELIM}\n{yaml_text}\n{_DELIM}\n{body}"


__all__ = ["parse", "render"]
