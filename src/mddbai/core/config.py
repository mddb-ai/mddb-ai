from __future__ import annotations

"""``MddbConfig`` loading.

Read precedence: function arguments > environment variables (``MDDB_*``) >
``mddbai.toml`` > defaults. Every field is validated with pydantic.
"""

import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigError

if sys.version_info >= (3, 11):
    import tomllib  # noqa: PLC0415
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

_ENV_PREFIX = "MDDB_"


class MddbConfig(BaseModel):
    """Runtime configuration. Injected explicitly instead of being a global."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path = Field(default=Path(".mddbai"))
    """Single memory palace root for one project.

    Fixed location (decided 2026-05-07): the project root's ``.mddbai/``
    directory. Inside live *AI-authored semantic drawer folders* and system
    artifacts (``_palace.md`` — palace root identity, ``_wal/``, ``_brain/``,
    cluster-level ``_meta.md``). *Role-classified folders* (palaces / benches
    / dogfood / scratch) are forbidden — they violate D7 (Loci).

    When users / scripts write their own scenarios, they too land under the
    same ``.mddbai/`` as ``<semantic-drawer>/``. A book story scenario goes
    to ``.mddbai/books/``; a recall verification scenario to
    ``.mddbai/recall-tests/``; and so on.

    Aligned with .claude/rules/install-layout.md.
    """

    decay_half_life_days: float = Field(default=30.0, gt=0)
    """Half-life of record strength in days."""

    summary_levels: int = Field(default=4, ge=1, le=8)
    """Gist hierarchy depth (record / shard / table / global / ...)."""

    shard_fanout: int = Field(default=16, gt=0)
    """Shard fanout (must be a power of two)."""

    lock_timeout_s: float = Field(default=30.0, gt=0)
    """Default file-lock timeout."""

    branch_overflow_threshold: int = Field(default=64, ge=2)
    """Threshold for child directories per folder. Exceeding it writes ``_attention.md`` (H.3)."""

    large_body_warn_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    """Stage N.6.1 — doctor warns when a record body exceeds this size (Gap 14).

    A 100 MB record accepted silently risks disk overhead, WAL trim cost, and
    scan OOMs. If the user *intentionally* writes a large body, raise the
    threshold via configuration.
    """

    require_palace_init: bool = Field(default=False)
    """Strict mode for palace identity (root `_palace.md` + per-table INDEX).

    When True, read/write attempts without an INDEX.md raise
    ``PalaceNotInitializedError``. Drawer-model semantic classification is
    explicit at the call site, so strict mode does not affect it — only the
    record model is enforced.
    """

    autosave_idle_seconds: float = Field(default=30.0, ge=0.0)
    """Stage CC (2026-05-04) — autosave idle timeout for the drawer dirty buffer.

    Word/Unreal-style autosave. After ``put_section`` / ``delete_section``,
    if no further activity occurs for this duration a background thread
    calls ``flush()``. If the AI never explicitly calls ``flush()`` and the
    process is killed, every change older than (last activity + N seconds)
    is already on disk.

    0 = disabled (legacy behaviour — only the with-block + sleep cycle
    safety net). Default 30 s = the standard Notepad / Word autosave cycle.

    D2 aligned: explicit ``flush()`` calls still work; this is purely an
    *additional safety net*.
    """

    drawer_split_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    """Stage Z.7 — doctor *warns only* when a drawer .md exceeds this size (D2 aligned).

    Rationale (revised 2026-05-07):
    - Aligned with the 50 MiB hard cap from U.1/U.2. ``split_recommended``
      becomes True when the threshold (50 MB) is fully reached; the warn
      signal fires at 80 % = 40 MB.
    - Never split automatically — D2 aligned. Only ``db.split_drawer()`` /
      the CLI invoked explicitly by the AI may split.
    """

    drawer_split_warn_pct: float = Field(default=0.8, ge=0.0, le=1.0)
    """Stage II (2026-05-04) — pre-warning ratio when the threshold is approached.

    Returns *real-time write-time* signals to the AI through the
    ``put_section`` return value plus a logger.warning. Default 0.8 = signal
    when drawer_split_bytes reaches 80 %.

    Resolves the R3 shadow — supplies a write-time signal channel rather
    than relying purely on after-the-fact doctor checks. D2 aligned —
    signal only, no automatic split.
    """

    flat_dump_threshold: int = Field(default=100, ge=1)
    """Stage X (2026-05-06) — auto-clamp threshold for list_drawers depth=0.

    When a cold AI calls ``mddbai list-drawers`` and the flat-dump drawer
    count exceeds this value, force depth=1 + a logger warn. Directly
    addresses the bottleneck identity.md §2 calls out ("how do you find
    things across tens of thousands?").

    Rationale: 100 drawers ≈ 4-5 KB flat dump = the safe ceiling for AI
    context. Anything more should be replaced by folder summaries
    (per-folder counts).
    """

    brain_auto: bool = Field(default=True)
    """K.6 (2026-05-05) — whether the sleep cycle's 6 brain-automation tasks are enabled.

    Subjects (added to default_tasks only when ``brain_auto=True``):
    - LexiconExtractTask — auto phrase extraction from utterances
    - LexiconReinforceTask — auto edges among phrases of the previous utterance
    - LexiconPromoteTask — auto promote episodic -> semantic
    - LexiconRetentionTask — apply utterance retention automatically
    - LinkDecayTask — auto-decay link weights
    - LinkPruneTask — auto-prune weak links

    Default True (corrected 2026-05-05): rectifies the principle D2
    interpretation error in the prior decision ("downgrade brain_auto=False
    to opt-in"). D2 = "the DB does not make *semantic* decisions" — auto
    *extraction* is a *space* operation (writing a graph), not a semantic
    decision. Semantic decisions (which graph cluster label / promotion
    criteria / retention policy) remain the AI's responsibility. Disabling
    auto extraction stalls the user's scenario ("just leave it on as
    project memory and recall fuzzy cues") at the substring-grep level. It
    must be on for real fuzzy recall to work.

    When to set False: regression comparisons that worry about semantic
    automation, diagnostic mode, manual-control scenarios. Also possible
    via env var `MDDB_BRAIN_AUTO=0`.
    """

    navigation_strict: bool = Field(default=False)
    """Navigation-first acceptance mode.

    When true, ``mddbai doctor`` treats missing navigation scaffolding as errors:
    root palace, table palace index, folder summaries, drawer-level cues, and
    section-level cues. Writes stay permissive; this flag is an acceptance gate
    for harnesses and large ingests that must prove AI-readable navigation.
    Set with ``MDDB_NAVIGATION_STRICT=1`` or an explicit config object.
    """

    @field_validator("shard_fanout")
    @classmethod
    def _check_pow2(cls, v: int) -> int:
        if v & (v - 1) != 0:
            raise ValueError("shard_fanout must be a power of two")
        return v

    @field_validator("data_dir")
    @classmethod
    def _coerce_path(cls, v: Path | str) -> Path:
        return Path(v).expanduser()


def _coerce_env_value(name: str, raw: str, hint: type) -> Any:
    if hint is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if hint is int:
        return int(raw)
    if hint is float:
        return float(raw)
    if hint is Path:
        return Path(raw)
    return raw


def _load_env() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field_name, field_info in MddbConfig.model_fields.items():
        env_key = f"{_ENV_PREFIX}{field_name.upper()}"
        if env_key in os.environ:
            hint = field_info.annotation
            try:
                out[field_name] = _coerce_env_value(field_name, os.environ[env_key], hint)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"invalid env value for {env_key}", value=os.environ[env_key]
                ) from exc
    return out


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"failed to read {path}") from exc
    section = data.get("mddbai", data)
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: [mddbai] section must be a table")
    return section


def load_config(
    *,
    overrides: dict[str, Any] | None = None,
    config_path: Path | None = None,
) -> MddbConfig:
    """Merge configuration by precedence and validate the result.

    Args:
        overrides: function arguments (highest precedence). Partial dict allowed.
        config_path: ``mddbai.toml`` path. ``None`` falls back to ``./mddbai.toml``.

    Returns:
        A validated ``MddbConfig`` instance.

    Raises:
        ConfigError: on a corrupt TOML file or validation failure.
    """

    merged: dict[str, Any] = {}
    toml_path = config_path if config_path is not None else Path("mddbai.toml")
    merged.update(_load_toml(toml_path))
    merged.update(_load_env())
    if overrides:
        merged.update(overrides)
    try:
        return MddbConfig(**merged)
    except ValidationError as exc:
        raise ConfigError("MddbConfig validation failed", errors=exc.errors()) from exc


__all__ = ["MddbConfig", "load_config"]
