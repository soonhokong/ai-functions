"""Compile a native function from a reviewed Python contract.

The scripted model keeps this demo reproducible and credential-free. In normal
use, omit ``model=...`` and let the configured model generate the Lean
implementation and proof. The user-authored specification remains the Python
function ``all_nonnegative_contract``.
"""

import os
from pathlib import Path

from ai_functions import ai_verified_compile
from ai_functions.testing import ScriptedModel, Turn
from ai_functions.verified_compile import VerifiedCompileConfig


def all_nonnegative_contract(result: bool, values: list[int]) -> bool:
    """The result agrees with Python's universal predicate."""
    return result == all(value >= 0 for value in values)


model = ScriptedModel(
    [
        Turn(
            text="""\
-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
def aiFunctionsImplementation (values : Array Int) : Bool :=
  values.all (fun value => decide (value >= 0))
-- END AI_FUNCTIONS_IMPLEMENTATION
-- BEGIN AI_FUNCTIONS_PROOF
rfl
-- END AI_FUNCTIONS_PROOF
""",
        ),
    ],
)

demo_cache = os.environ.get("AI_FUNCTIONS_DEMO_CACHE")
config = VerifiedCompileConfig(cache_dir=Path(demo_cache)) if demo_cache else VerifiedCompileConfig()


@ai_verified_compile(
    post_condition=all_nonnegative_contract,
    model=model,
    max_attempts=1,
    config=config,
)
def all_nonnegative(values: list[int]) -> bool:
    """Return whether every value is nonnegative."""


if __name__ == "__main__":
    print("Reviewed Python contract:")
    print("  return result == all(value >= 0 for value in values)")
    print("Deterministically generated Lean proposition:")
    print(f"  {all_nonnegative.lean_spec.proposition}")
    print("Native calls:")
    print(f"  all_nonnegative([0, 4, 10]) = {all_nonnegative([0, 4, 10])}")
    print(f"  all_nonnegative([0, -1, 10]) = {all_nonnegative([0, -1, 10])}")
    print(f"Kernel-checked proof: {all_nonnegative.lean_source_path}")
    print(f"Native library: {all_nonnegative.shared_library_path}")
