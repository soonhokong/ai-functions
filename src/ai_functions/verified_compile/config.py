"""Configuration for ``@ai_verified_compile``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

import platformdirs

if TYPE_CHECKING:
    from strands.models import Model


def _default_cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir("ai_functions")) / "verified_compile"


class VerifiedCompileKwargs(TypedDict, total=False):
    """Keyword overrides accepted by :func:`ai_verified_compile`."""

    model: Model | str | None
    max_attempts: int
    cache_dir: str | Path
    compile_on: Literal["first_call", "import_time"]
    toolchain_mode: Literal["managed", "system"]
    lean_toolchain: str
    mathlib_revision: str | None
    setup_timeout_seconds: float
    command_timeout_seconds: float


@dataclass(frozen=True)
class VerifiedCompileConfig:
    """Configuration for one Lean-verified compiler template.

    ``max_attempts`` counts total model responses, including the first one.
    Setting ``mathlib_revision`` to ``None`` creates a Lean-core-only project;
    this is useful for small proofs and hermetic build tests.
    """

    model: Model | str | None = None
    max_attempts: int = 5
    cache_dir: str | Path = field(default_factory=_default_cache_dir)
    compile_on: Literal["first_call", "import_time"] = "first_call"
    toolchain_mode: Literal["managed", "system"] = "managed"
    lean_toolchain: str = "leanprover/lean4:v4.33.1"
    mathlib_revision: str | None = None
    setup_timeout_seconds: float = 900
    command_timeout_seconds: float = 180

    def __post_init__(self) -> None:
        """Reject values that would make the pipeline ill-defined."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.compile_on not in ("first_call", "import_time"):
            raise ValueError("compile_on must be 'first_call' or 'import_time'")
        if self.toolchain_mode not in ("managed", "system"):
            raise ValueError("toolchain_mode must be 'managed' or 'system'")
        if not self.lean_toolchain.strip():
            raise ValueError("lean_toolchain must not be empty")
        if self.setup_timeout_seconds <= 0:
            raise ValueError("setup_timeout_seconds must be positive")
        if self.command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
