"""Real Lean-to-native integration test, enabled by the dedicated CI matrix."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from ai_functions.testing import ScriptedModel, Turn
from ai_functions.verified_compile import LeanSpec, VerifiedCompileConfig, ai_verified_compile

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AI_FUNCTIONS_RUN_LEAN_TESTS") != "1",
        reason="set AI_FUNCTIONS_RUN_LEAN_TESTS=1 to run the native Lean toolchain test",
    ),
]


def _response(implementation: str, proof: str = "rfl") -> str:
    return f"""\
-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
{implementation}
-- END AI_FUNCTIONS_IMPLEMENTATION
-- BEGIN AI_FUNCTIONS_PROOF
{proof}
-- END AI_FUNCTIONS_PROOF
"""


def _compile_scripted(
    func: Callable[..., Any],
    *,
    lean_spec: LeanSpec,
    base_config: VerifiedCompileConfig,
    implementation: str,
):
    model = ScriptedModel([Turn(text=_response(implementation))])
    compiled = ai_verified_compile(
        func,
        lean_spec=lean_spec,
        config=dataclasses.replace(base_config, model=model),
    )
    return compiled, model


def test_array_int_compiles_and_runs_on_host_platform(tmp_path) -> None:  # noqa: ANN001
    def sum_values(values: list[int]) -> int:
        """Add every integer in values."""
        raise AssertionError

    lean_spec = LeanSpec(proposition="result = values.foldl (fun total value => total + value) 0")
    base_config = VerifiedCompileConfig(
        cache_dir=tmp_path / "cache",
        lean_toolchain=os.environ.get("AI_FUNCTIONS_LEAN_TOOLCHAIN", "leanprover/lean4:v4.33.1"),
        mathlib_revision=None,
        max_attempts=1,
    )
    operation = "values.foldl (fun total value => total + value) 0"
    bad_response = _response(
        "def aiFunctionsImplementation (values : Array Int) : Int := 0",
    )
    good_response = _response(
        f"def aiFunctionsImplementation (values : Array Int) : Int := {operation}",
    )
    model = ScriptedModel([Turn(text=bad_response), Turn(text=good_response)])
    config = dataclasses.replace(
        base_config,
        model=model,
        max_attempts=2,
    )
    compiled = ai_verified_compile(sum_values, lean_spec=lean_spec, config=config)

    assert compiled([3, -2, 10, 4]) == 15
    assert compiled([]) == 0
    assert compiled([2**63 - 1]) == 2**63 - 1
    assert compiled([-(2**63)]) == -(2**63)
    with pytest.raises(OverflowError, match="does not fit signed 64-bit"):
        compiled([2**63 - 1, 1])
    with pytest.raises(OverflowError, match="signed 64-bit FFI"):
        compiled([2**63])
    with pytest.raises(TypeError, match=r"expected list\[int\]"):
        compiled([True])

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(compiled, ([value, 1] for value in range(16)))) == list(range(1, 17))

    assert model.remaining_turns == 0
    assert compiled.implementation_source_path.exists()
    assert compiled.implementation_object_path.exists()
    assert compiled.lean_source_path.exists()
    assert compiled.proof_object_path.exists()
    assert compiled.c_source_path.exists()
    assert compiled.ffi_shim_path.exists()
    assert compiled.shared_library_path.exists()
    assert compiled.metadata_path.exists()
    assert f"import {compiled.implementation_source_path.stem}" in compiled.lean_source
    assert "@[export" not in compiled.lean_source
    assert "@[export" in compiled.implementation_source_path.read_text()

    cached_model = ScriptedModel([])
    cached = ai_verified_compile(
        sum_values,
        lean_spec=lean_spec,
        config=dataclasses.replace(base_config, model=cached_model),
    )
    assert cached([20, 22]) == 42
    assert cached_model.remaining_turns == 0


def test_scalar_abis_compile_and_run_in_one_process(tmp_path) -> None:  # noqa: ANN001
    base_config = VerifiedCompileConfig(
        cache_dir=tmp_path / "cache",
        lean_toolchain=os.environ.get("AI_FUNCTIONS_LEAN_TOOLCHAIN", "leanprover/lean4:v4.33.1"),
        mathlib_revision=None,
        max_attempts=1,
    )

    def answer() -> int:
        """Return the answer."""
        raise AssertionError

    answer_compiled, answer_model = _compile_scripted(
        answer,
        lean_spec=LeanSpec(proposition="result = 42"),
        base_config=base_config,
        implementation="def aiFunctionsImplementation : Int := 42",
    )

    def is_positive(value: int, enabled: bool) -> bool:
        """Return whether value is positive when enabled."""
        raise AssertionError

    positive_compiled, positive_model = _compile_scripted(
        is_positive,
        lean_spec=LeanSpec(proposition="result = (enabled && decide (value > 0))"),
        base_config=base_config,
        implementation="""\
def aiFunctionsImplementation (value : Int) (enabled : Bool) : Bool :=
  enabled && decide (value > 0)""",
    )

    def double_value(value: float) -> float:
        """Return value plus itself."""
        raise AssertionError

    double_compiled, double_model = _compile_scripted(
        double_value,
        lean_spec=LeanSpec(proposition="result = value + value"),
        base_config=base_config,
        implementation="def aiFunctionsImplementation (value : Float) : Float := value + value",
    )

    assert answer_compiled() == 42
    assert positive_compiled(enabled=True, value=4) is True
    assert positive_compiled(4, False) is False
    assert positive_compiled(-1, True) is False
    assert double_compiled(1.25) == 2.5
    with pytest.raises(TypeError, match="expected int, got bool"):
        positive_compiled(True, True)
    with pytest.raises(TypeError, match="expected bool, got int"):
        positive_compiled(1, 1)
    with pytest.raises(TypeError, match="expected float, got bool"):
        double_compiled(True)
    assert answer_compiled() == 42
    assert answer_model.remaining_turns == 0
    assert positive_model.remaining_turns == 0
    assert double_model.remaining_turns == 0
