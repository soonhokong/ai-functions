"""Tests for deterministic Python-contract translation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_functions import ai_verified_compile
from ai_functions.verified_compile import (
    PythonContractError,
    PythonContractViolation,
    VerifiedCompileConfig,
)
from ai_functions.verified_compile import _decorator as decorator_module
from ai_functions.verified_compile._contracts import translate_post_conditions
from ai_functions.verified_compile._decorator import _CompiledArtifact
from ai_functions.verified_compile._types import inspect_function


def increment_contract(result: int, value: int) -> bool:
    return result == value + 1


def sign_contract(result: bool, value: int) -> bool:
    return result == (value >= 0)


def all_nonnegative_contract(result: bool, values: list[int]) -> bool:
    return result == all(value >= 0 for value in values)


def filtered_all_contract(result: bool, values: list[int]) -> bool:
    return result == all(value >= 0 for value in values if value != 99)


def membership_and_length_contract(result: bool, needle: int, values: list[int]) -> bool:
    return result == (needle in values and len(values) >= 1)


def unsupported_division(result: int, value: int) -> bool:
    return result == value // 2


def unsupported_index(result: int, values: list[int]) -> bool:
    return result == values[0]


def ignores_result(result: int, value: int) -> bool:
    return value >= 0


def wrong_input_name(result: int, missing: int) -> bool:
    return result == missing


def float_contract(result: float, value: float) -> bool:
    return result == value + value


def _stub_increment(value: int) -> int:
    raise AssertionError


def _stub_sign(value: int) -> bool:
    raise AssertionError


def _stub_all(values: list[int]) -> bool:
    raise AssertionError


def _stub_first(values: list[int]) -> int:
    raise AssertionError


def _stub_membership(needle: int, values: list[int]) -> bool:
    raise AssertionError


def test_integer_contract_translates_to_readable_lean() -> None:
    spec = translate_post_conditions((increment_contract,), shape=inspect_function(_stub_increment))
    assert spec.proposition == "(result = (value + 1))"


def test_boolean_result_can_equal_a_comparison() -> None:
    spec = translate_post_conditions((sign_contract,), shape=inspect_function(_stub_sign))
    assert spec.proposition == "(result = decide (value >= 0))"


def test_all_generator_translates_to_array_all() -> None:
    spec = translate_post_conditions((all_nonnegative_contract,), shape=inspect_function(_stub_all))
    assert spec.proposition == "(result = (values).all (fun value => decide (value >= 0)))"


def test_filtered_all_generator_skips_values_failing_guard() -> None:
    spec = translate_post_conditions((filtered_all_contract,), shape=inspect_function(_stub_all))
    assert ".all (fun value => (!(decide (value ≠ 99)) || (decide (value >= 0))))" in spec.proposition


def test_membership_len_and_boolean_connectives_translate() -> None:
    spec = translate_post_conditions(
        (membership_and_length_contract,),
        shape=inspect_function(_stub_membership),
    )
    assert "(values).contains (needle)" in spec.proposition
    assert "Int.ofNat (values).size >= 1" in spec.proposition
    assert " && " in spec.proposition


def test_multiple_contracts_are_conjoined() -> None:
    def exact(result: int, value: int) -> bool:
        return result == value + 1

    def greater(result: int, value: int) -> bool:
        return result > value

    spec = translate_post_conditions((exact, greater), shape=inspect_function(_stub_increment))
    assert " ∧\n    " in spec.proposition
    assert "(result = (value + 1))" in spec.proposition
    assert "(result > value)" in spec.proposition


def test_immutable_captured_integer_is_snapshotted() -> None:
    offset = 7

    def captured(result: int, value: int) -> bool:
        return result == value + offset

    spec = translate_post_conditions((captured,), shape=inspect_function(_stub_increment))
    assert spec.proposition == "(result = (value + 7))"


@pytest.mark.parametrize(
    ("contract", "stub", "message"),
    [
        (unsupported_division, _stub_increment, "only integer"),
        (unsupported_index, _stub_first, "Subscript"),
        (ignores_result, _stub_increment, "must depend"),
        (wrong_input_name, _stub_increment, "is not an input"),
        (float_contract, lambda value: value, "outside the verified contract subset"),
    ],
)
def test_unsupported_contracts_fail_closed(contract, stub, message: str) -> None:  # noqa: ANN001
    if contract is float_contract:

        def float_stub(value: float) -> float:
            raise AssertionError

        stub = float_stub
    with pytest.raises(PythonContractError, match=message):
        translate_post_conditions((contract,), shape=inspect_function(stub))


def test_decorator_fixes_python_contract_before_model_invocation() -> None:
    @ai_verified_compile(post_condition=increment_contract)
    def increment(value: int) -> int:
        """Return one more than value."""

    assert increment.lean_spec.proposition == "(result = (value + 1))"


def test_original_python_contract_is_rechecked_after_ffi(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact = _CompiledArtifact(
        directory=tmp_path,
        implementation_path=tmp_path / "implementation.lean",
        verification_path=tmp_path / "proof.lean",
        implementation_object_path=tmp_path / "implementation.olean",
        proof_object_path=tmp_path / "proof.olean",
        c_source_path=tmp_path / "implementation.c",
        ffi_shim_path=tmp_path / "ffi_shim.c",
        library_path=tmp_path / "compiled.dylib",
        metadata_path=tmp_path / "metadata.json",
        export_name="aivc_increment",
    )
    monkeypatch.setattr(decorator_module, "_compile_artifact", lambda *args, **kwargs: artifact)
    monkeypatch.setattr(decorator_module, "load_compiled_function", lambda *args, **kwargs: lambda value: value)

    @ai_verified_compile(
        post_condition=increment_contract,
        config=VerifiedCompileConfig(cache_dir=tmp_path),
    )
    def increment(value: int) -> int:
        """Return one more than value."""

    with pytest.raises(PythonContractViolation, match="increment_contract"):
        increment(10)
