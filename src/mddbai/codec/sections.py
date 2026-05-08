from __future__ import annotations

"""ATX heading-based section split + markdown table parser/renderer.

``#`` inside a code fence (```````) is not treated as a heading.
"""

from dataclasses import dataclass

from mddbai.core.errors import CodecError


@dataclass(frozen=True, slots=True)
class Section:
    level: int
    title: str
    content: str


def split_sections(body: str) -> list[Section]:
    """Convert body into a list of sections based on ATX headings."""

    lines = body.split("\n")
    in_fence = False
    sections: list[Section] = []
    current_level = 0
    current_title = ""
    current_lines: list[str] = []
    has_started = False

    def _flush() -> None:
        if not has_started and not current_lines:
            return
        content = "\n".join(current_lines).strip("\n")
        sections.append(Section(level=current_level, title=current_title, content=content))

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            current_lines.append(line)
            continue

        if not in_fence and stripped.startswith("#"):
            count = 0
            for ch in stripped:
                if ch == "#":
                    count += 1
                else:
                    break
            if 1 <= count <= 6 and (len(stripped) == count or stripped[count] == " "):
                if has_started or current_lines:
                    _flush()
                current_level = count
                current_title = stripped[count:].strip()
                current_lines = []
                has_started = True
                continue
        current_lines.append(line)

    _flush()
    return sections


# ---- markdown table -------------------------------------------------------


def parse_table(text: str) -> list[dict[str, str]]:
    """Convert a pipe table into a list of dicts.

    Format: first line is header, second line is the ``---|---`` separator, followed by data rows.
    """

    raw_lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    if len(raw_lines) < 2:
        raise CodecError("table requires header and separator rows")

    def _split(line: str) -> list[str]:
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    header = _split(raw_lines[0])
    sep = _split(raw_lines[1])
    if len(sep) != len(header):
        raise CodecError("table separator column count mismatch")
    for cell in sep:
        body = cell.strip().replace(":", "")
        if not body or any(c != "-" for c in body):
            raise CodecError(f"invalid table separator cell: {cell!r}")

    rows: list[dict[str, str]] = []
    for ln in raw_lines[2:]:
        cells = _split(ln)
        if len(cells) != len(header):
            raise CodecError(
                f"table row cell count mismatch: expected {len(header)}, got {len(cells)}"
            )
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def render_table(rows: list[dict[str, str]], columns: list[str]) -> str:
    """Return a markdown table string in the order of ``columns``."""

    if not columns:
        raise CodecError("at least one column is required")
    out: list[str] = []
    out.append("| " + " | ".join(columns) + " |")
    out.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        cells = [str(row.get(col, "")) for col in columns]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


__all__ = ["Section", "parse_table", "render_table", "split_sections"]
