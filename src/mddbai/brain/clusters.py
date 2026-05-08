from __future__ import annotations

"""Stage L.10 — Cluster disk layout (no automatic placement).

`_clusters/<id>/records/<key>.md` tree. Used only when the AI explicitly
embeds ``_cluster: <id>`` in `record.meta` or passes ``--cluster <id>`` on
the CLI. MDDB does not decide *which cluster is appropriate* — meaning is
the AI's job.

Principles:

- D1 search — AI enters the semantic region directly via
  `Glob "_clusters/<id>/records/"`
- D2 decision — DB does *mechanical work* only (look at
  record.meta._cluster, scatter into folders, embed indexes). No semantic
  classification, automatic moves, or automatic centroid updates.
- D3 environment — zero invasion of user environment files (CLAUDE.md etc.)
- L.8 OFPA — shard index works the same inside a cluster (rel_path is
  simply written as `_clusters/<id>/records/<key>.md`)
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mddbai.codec.frontmatter import parse as fm_parse, render as fm_render
from mddbai.core.errors import StorageError
from mddbai.core.types import TableName
from mddbai.storage.atomic import atomic_write_text

if TYPE_CHECKING:
    pass


CLUSTERS_DIR = "_clusters"
CLUSTER_RECORDS_DIR = "records"
CLUSTER_META_NAME = "_meta.md"
CLUSTER_MANIFEST_NAME = "_manifest.md"


@dataclass
class Cluster:
    """Metadata for a single cluster."""

    id: str
    label: str = ""
    created_ns: int = 0
    last_accessed_ns: int = 0
    member_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_md(self) -> str:
        meta: dict[str, Any] = {
            "_kind": "cluster_meta",
            "id": self.id,
            "label": self.label,
            "created_ns": self.created_ns,
            "last_accessed_ns": self.last_accessed_ns,
            "member_count": self.member_count,
        }
        for k, v in self.extra.items():
            if k.startswith("_"):
                continue
            meta[k] = v
        body_lines = [
            f"# Cluster {self.id}",
            "",
            f"label: {self.label}" if self.label else "",
            f"members: {self.member_count}",
            f"created: {_iso(self.created_ns)}",
            f"last_accessed: {_iso(self.last_accessed_ns)}",
        ]
        return fm_render(meta, "\n".join(line for line in body_lines if line) + "\n")

    @classmethod
    def from_md(cls, text: str) -> Cluster:
        meta, _ = fm_parse(text)
        if meta.get("_kind") != "cluster_meta":
            raise StorageError("not a cluster meta document")
        extra: dict[str, Any] = {}
        for k, v in meta.items():
            if k in {"_kind", "id", "label", "created_ns", "last_accessed_ns", "member_count"}:
                continue
            extra[str(k)] = v
        return cls(
            id=str(meta["id"]),
            label=str(meta.get("label", "")),
            created_ns=int(meta.get("created_ns", 0)),
            last_accessed_ns=int(meta.get("last_accessed_ns", 0)),
            member_count=int(meta.get("member_count", 0)),
            extra=extra,
        )


def _iso(ns: int) -> str:
    if ns <= 0:
        return ""
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()


def cluster_root(data_dir: Path, table: TableName | str, cluster_id: str) -> Path:
    """Absolute path of ``<table>/_clusters/<id>/``."""

    return Path(data_dir) / str(table) / CLUSTERS_DIR / cluster_id


def cluster_records_dir(data_dir: Path, table: TableName | str, cluster_id: str) -> Path:
    """``<table>/_clusters/<id>/records/`` — parent of the record .md files."""

    return cluster_root(data_dir, table, cluster_id) / CLUSTER_RECORDS_DIR


def cluster_meta_path(data_dir: Path, table: TableName | str, cluster_id: str) -> Path:
    return cluster_root(data_dir, table, cluster_id) / CLUSTER_META_NAME


def manifest_path(data_dir: Path, table: TableName | str) -> Path:
    """``<table>/_clusters/_manifest.md`` — only the list of cluster ids."""

    return Path(data_dir) / str(table) / CLUSTERS_DIR / CLUSTER_MANIFEST_NAME


def load_cluster(
    data_dir: Path, table: TableName | str, cluster_id: str
) -> Cluster | None:
    """Parse a single cluster meta. Returns None if missing or broken."""

    path = cluster_meta_path(data_dir, table, cluster_id)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return Cluster.from_md(text)
    except (OSError, ValueError, StorageError):
        return None


def upsert_cluster(
    data_dir: Path, table: TableName | str, cluster: Cluster
) -> Path:
    """Atomically write cluster meta. Creates the folder automatically."""

    path = cluster_meta_path(data_dir, table, cluster.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, cluster.to_md(), fsync=False)
    return path


def delete_cluster(
    data_dir: Path, table: TableName | str, cluster_id: str
) -> bool:
    """Delete the cluster meta + records folder entirely. Dangerous — caller is responsible."""

    root = cluster_root(data_dir, table, cluster_id)
    if not root.exists():
        return False
    _rmtree(root)
    return True


def _rmtree(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            _rmtree(child)
        try:
            path.rmdir()
        except OSError:
            pass
    else:
        try:
            path.unlink()
        except OSError:
            pass


def iter_clusters(
    data_dir: Path, table: TableName | str
) -> Iterable[Cluster]:
    """Yield cluster metas from the ``_clusters/`` tree."""

    root = Path(data_dir) / str(table) / CLUSTERS_DIR
    if not root.exists():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        cid = child.name
        if cid.startswith("_"):
            continue  # _manifest.md etc. are files, not folders, so naturally skipped
        cluster = load_cluster(data_dir, table, cid)
        if cluster is None:
            # If the meta file is missing, build an instance with minimal info
            cluster = Cluster(id=cid)
        yield cluster


def write_manifest(data_dir: Path, table: TableName | str) -> Path:
    """``_clusters/_manifest.md`` — record only cluster id + member_count.

    Larger data such as centroid and label live in each cluster's
    ``_meta.md``. The manifest is an *index of clusters* — kept light.
    """

    clusters = list(iter_clusters(data_dir, table))
    meta: dict[str, Any] = {
        "_kind": "cluster_manifest",
        "table": str(table),
        "cluster_count": len(clusters),
    }
    body_lines = [
        f"# Cluster manifest: {table}",
        "",
        f"clusters: {len(clusters)}",
        "",
        "| id | label | members |",
        "| --- | --- | --- |",
    ]
    for c in clusters:
        body_lines.append(f"| `{c.id}` | {c.label} | {c.member_count} |")
    path = manifest_path(data_dir, table)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, fm_render(meta, "\n".join(body_lines) + "\n"), fsync=False)
    return path


def count_cluster_members(
    data_dir: Path, table: TableName | str, cluster_id: str
) -> int:
    """How many .md files exist in the cluster's records/."""

    rec_dir = cluster_records_dir(data_dir, table, cluster_id)
    if not rec_dir.exists():
        return 0
    return sum(1 for cand in rec_dir.iterdir() if cand.is_file() and cand.suffix == ".md")


def cluster_record_path(
    data_dir: Path,
    table: TableName | str,
    key: str,
    cluster_id: str,
) -> Path:
    """Deterministic absolute path of a single record's .md inside its cluster."""

    return cluster_records_dir(data_dir, table, cluster_id) / f"{key}.md"


__all__ = [
    "CLUSTERS_DIR",
    "CLUSTER_MANIFEST_NAME",
    "CLUSTER_META_NAME",
    "CLUSTER_RECORDS_DIR",
    "Cluster",
    "cluster_meta_path",
    "cluster_record_path",
    "cluster_records_dir",
    "cluster_root",
    "count_cluster_members",
    "delete_cluster",
    "iter_clusters",
    "load_cluster",
    "manifest_path",
    "upsert_cluster",
    "write_manifest",
]
