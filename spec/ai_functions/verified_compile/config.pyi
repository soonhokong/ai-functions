"""Configuration for ``@ai_verified_compile``."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from strands.models import Model

class VerifiedCompileKwargs(TypedDict, total=False):
    """Keyword overrides accepted by :func:`ai_verified_compile`."""

    model: Model | str | None
    max_attempts: int
    cache_dir: str | Path
    compile_on: Literal["first_call", "import_time"]
    lean_toolchain: str
    mathlib_revision: str | None
    setup_timeout_seconds: float
    command_timeout_seconds: float

@dataclass(frozen=True)
class VerifiedCompileConfig:
    """Configuration for one Lean-verified compiler template."""

    model: Model | str | None = None
    max_attempts: int = 5
    cache_dir: str | Path = ...
    compile_on: Literal["first_call", "import_time"] = "first_call"
    lean_toolchain: str = "leanprover/lean4:v4.33.1"
    mathlib_revision: str | None = "v4.33.1"
    setup_timeout_seconds: float = 900
    command_timeout_seconds: float = 180

    def __post_init__(self) -> None:
        """Reject values that would make the pipeline ill-defined."""
        ...
