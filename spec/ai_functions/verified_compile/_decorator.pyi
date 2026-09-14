"""Public decorator and lazy compiled-function wrapper."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Unpack, overload

from ._source import LeanSpec
from .config import VerifiedCompileConfig, VerifiedCompileKwargs

class AIVerifiedFunction[**P, T]:
    """A lazily generated, proved, and natively compiled Python callable."""

    def __init__(
        self,
        func: Callable[P, T],
        *,
        post_conditions: tuple[Callable[..., Any], ...],
        lean_spec: LeanSpec | None,
        config: VerifiedCompileConfig,
    ) -> None: ...

    @property
    def config(self) -> VerifiedCompileConfig:
        """The immutable compiler configuration."""
        ...

    @property
    def artifact_dir(self) -> Path:
        """Directory containing the checked Lean, generated C, and library."""
        ...

    @property
    def implementation_source_path(self) -> Path:
        """Path to the Init-only Lean implementation that is compiled."""
        ...

    @property
    def lean_source_path(self) -> Path:
        """Path to the Lean theorem and axiom audit reviewers should inspect."""
        ...

    @property
    def lean_source(self) -> str:
        """The complete checked Lean proof source."""
        ...

    @property
    def implementation_object_path(self) -> Path:
        """Path to the exact compiled ``.olean`` imported by the proof."""
        ...

    @property
    def proof_object_path(self) -> Path:
        """Path to the kernel-checked proof module object."""
        ...

    @property
    def c_source_path(self) -> Path:
        """Path to C generated from the verified Lean implementation."""
        ...

    @property
    def ffi_shim_path(self) -> Path:
        """Path to the portable C facade used by Python."""
        ...

    @property
    def shared_library_path(self) -> Path:
        """Path to the loaded native shared library."""
        ...

    @property
    def metadata_path(self) -> Path:
        """Path to compiler, platform, and artifact metadata."""
        ...

    def compile(self) -> AIVerifiedFunction[P, T]:
        """Generate, kernel-check, compile, and load this function once."""
        ...

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> T:
        """Call the verified native implementation."""
        ...

@overload
def ai_verified_compile[**P, T](
    func: Callable[P, T],
    /,
    *,
    post_condition: Callable[..., Any] | Sequence[Callable[..., Any]] | None = None,
    lean_spec: LeanSpec | None = None,
    config: VerifiedCompileConfig | None = None,
    **config_overrides: Unpack[VerifiedCompileKwargs],
) -> AIVerifiedFunction[P, T]: ...
@overload
def ai_verified_compile[**P, T](
    func: None = None,
    /,
    *,
    post_condition: Callable[..., Any] | Sequence[Callable[..., Any]] | None = None,
    lean_spec: LeanSpec | None = None,
    config: VerifiedCompileConfig | None = None,
    **config_overrides: Unpack[VerifiedCompileKwargs],
) -> Callable[[Callable[P, T]], AIVerifiedFunction[P, T]]: ...
