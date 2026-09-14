"""Public specification type used by verified compilation."""

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class LeanSpec:
    """A user-reviewed Lean post-condition."""

    proposition: str
    prelude: str = ""

    def __post_init__(self) -> None: ...

@dataclass(frozen=True)
class LeanCandidate:
    """Model-produced implementation, formalization, and proof body."""

    implementation: str
    proof: str
    spec_prelude: str = ""
    proposition: str = ""

@dataclass(frozen=True)
class RenderedLeanSource:
    """Complete implementation and verification modules."""

    module_name: str
    proof_module_name: str
    namespace: str
    theorem_name: str
    export_name: str
    implementation: str
    verification: str
    candidate: LeanCandidate

def parse_candidate(response: str, *, model_generates_spec: bool) -> LeanCandidate:
    """Parse the model's marker-delimited response."""
    ...

def render_sources(
    candidate: LeanCandidate,
    *,
    shape: Any,
    build_id: str,
    export_name: str,
    lean_spec: LeanSpec | None,
    use_mathlib: bool,
) -> RenderedLeanSource:
    """Assemble fixed modules around a model-produced candidate."""
    ...

def validate_lean_spec_names(spec: LeanSpec, shape: Any) -> None:
    """Catch the common mistake of using Python rather than mapped Lean names."""
    ...
