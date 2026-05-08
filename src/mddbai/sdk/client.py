from __future__ import annotations

"""Python client — synchronous wrapper.

Embedded mode (calling ``Database`` directly) is the only mode shipped
first, to avoid an HTTP dependency. An HTTP client built on ``httpx``
will follow.

Stage W: removed the record model. Restructured around the drawer/section API.
"""

from pathlib import Path
from typing import Any

from mddbai.core.config import MddbConfig
from mddbai.engine import Database


class Client:
    """User-friendly adapter that wraps ``Database``."""

    def __init__(self, data_dir: Path | str, *, config: MddbConfig | None = None) -> None:
        self._db = Database(Path(data_dir), config=config)

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    @property
    def db(self) -> Database:
        return self._db

    def close(self) -> None:
        self._db.close()

    # ---- drawer / section API ----------------------------------------

    def put_section(self, table: str, drawer: str, section: str, body: str) -> None:
        self._db.put_section(table, drawer, section, body)

    def take_section(
        self, table: str, drawer: str, section: str, *, body_only: bool = False
    ) -> str | None:
        return self._db.take_section(table, drawer, section, body_only=body_only)

    def take_drawer(self, table: str, drawer: str) -> str | None:
        return self._db.take_drawer(table, drawer)

    def list_sections(self, table: str, drawer: str) -> list[str]:
        return self._db.list_sections(table, drawer)

    def list_drawers(self, table: str) -> list[str]:
        return self._db.list_drawers(table)

    def delete_section(self, table: str, drawer: str, section: str) -> bool:
        return self._db.delete_section(table, drawer, section)

    def flush(self) -> None:
        self._db.flush()

    def tables(self) -> list[str]:
        return [str(table) for table in self._db.tables()]


__all__ = ["Client"]
