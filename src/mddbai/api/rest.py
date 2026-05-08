from __future__ import annotations

"""FastAPI-based REST API.

.. note::
    Since H.7 (2026-04-30) the HTTP API is for **debugging and external
    tool integration**. It is the fallback when an AI cannot reach the
    file system directly. AI clients that can read files directly
    (Claude Code, Codex CLI, Gemini CLI) are fine using only ``Glob`` /
    ``Grep`` / ``Read`` / the ``mddbai`` CLI.

    Stage W: removed the record model. Restructured around the drawer/section API.

Routes::
    PUT    /v1/{table}/{drawer}/{section}
    GET    /v1/{table}/{drawer}/{section}
    DELETE /v1/{table}/{drawer}/{section}
    GET    /v1/{table}/{drawer}            (full drawer body)
    GET    /v1/{table}                    (drawer list)
    GET    /healthz
    GET    /v1/_stats
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mddbai.core.config import MddbConfig
from mddbai.core.types import TableName
from mddbai.engine import Database


class SectionIn(BaseModel):
    body: str = ""


class SectionOut(BaseModel):
    table: str
    drawer: str
    section: str
    body: str


def create_app(data_dir: Path, *, config: MddbConfig | None = None) -> FastAPI:
    """FastAPI app sharing a single ``Database`` instance."""

    cfg = config or MddbConfig(data_dir=Path(data_dir))
    db = Database(Path(data_dir), config=cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            db.close()

    app = FastAPI(title="MDDB", version="0.0.1", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/_stats")
    def stats() -> dict[str, Any]:
        tables = db.tables()
        return {
            "tables": sorted(tables),
            "table_count": len(tables),
        }

    @app.get("/v1/{table}")
    def list_drawers(table: str) -> dict[str, Any]:
        drawers = db.list_drawers(table)
        return {"table": table, "drawers": drawers}

    @app.get("/v1/{table}/{drawer}")
    def get_drawer(table: str, drawer: str) -> dict[str, Any]:
        text = db.take_drawer(table, drawer)
        if text is None:
            raise HTTPException(status_code=404, detail="drawer not found")
        sections = db.list_sections(table, drawer)
        return {"table": table, "drawer": drawer, "sections": sections, "text": text}

    @app.get("/v1/{table}/{drawer}/{section}", response_model=SectionOut)
    def get_section(table: str, drawer: str, section: str) -> SectionOut:
        text = db.take_section(table, drawer, section, body_only=True)
        if text is None:
            raise HTTPException(status_code=404, detail="section not found")
        return SectionOut(table=table, drawer=drawer, section=section, body=text)

    @app.put("/v1/{table}/{drawer}/{section}", response_model=SectionOut)
    def put_section(table: str, drawer: str, section: str, payload: SectionIn) -> SectionOut:
        db.put_section(table, drawer, section, payload.body)
        db.flush(TableName(table))
        return SectionOut(table=table, drawer=drawer, section=section, body=payload.body)

    @app.delete("/v1/{table}/{drawer}/{section}")
    def delete_section(table: str, drawer: str, section: str) -> dict[str, str]:
        ok = db.delete_section(table, drawer, section)
        if not ok:
            raise HTTPException(status_code=404, detail="section not found")
        db.flush(TableName(table))
        return {"status": "deleted"}

    return app


__all__ = ["create_app"]
