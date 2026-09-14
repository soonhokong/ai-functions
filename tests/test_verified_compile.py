"""Unit tests for the Lean-verified compiler surface and proof-search loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_functions import LeanSpec, ai_verified_compile
from ai_functions.testing import ScriptedModel, Turn
from ai_functions.verified_compile import (
    AIVerifiedFunction,
    LeanCompilationError,
    VerifiedCompileConfig,
    VerifiedCompileConfigurationError,
)
from ai_functions.verified_compile import _decorator as decorator_module
from ai_functions.verified_compile._decorator import _cache_key, _compile_artifact, _CompiledArtifact
from ai_functions.verified_compile._ffi import _array_int64, _int64, generate_c_shim
from ai_functions.verified_compile._generation import build_generation_prompt, generate_source
from ai_functions.verified_compile._source import (
    LeanCandidate,
    LeanVerificationError,
    RenderedLeanSource,
    parse_candidate,
    render_sources,
)
from ai_functions.verified_compile._types import inspect_function


def _post_condition(result: int, x: int) -> bool:
    return result == x + 1


def increment(x: int) -> int:
    """Return one more than x."""


def test_signature_mapping_supports_scalar_and_array_inputs() -> None:
    def compute(values: list[int], count: int, enabled: bool, scale: float) -> int:
        raise AssertionError

    shape = inspect_function(compute)
    assert [parameter.lean_type for parameter in shape.parameters] == ["Array Int", "Int", "Bool", "Float"]
    assert shape.return_lean_type == "Int"
    assert shape.lean_name == "compute"


def test_list_return_is_rejected() -> None:
    def unsupported(values: list[int]) -> list[int]:
        return values

    with pytest.raises(VerifiedCompileConfigurationError, match=r"list\[int\] return"):
        inspect_function(unsupported)


def test_colliding_python_names_are_rejected() -> None:
    def collision(foo_bar: int, foo__bar: int) -> int:
        return foo_bar + foo__bar

    with pytest.raises(VerifiedCompileConfigurationError, match="both map to Lean name"):
        inspect_function(collision)


def test_non_function_callables_are_rejected() -> None:
    class CallableObject:
        def __call__(self, x: int) -> int:
            return x

    with pytest.raises(VerifiedCompileConfigurationError, match="Python functions"):
        inspect_function(CallableObject())


def test_async_and_generator_functions_are_rejected() -> None:
    async def asynchronous(x: int) -> int:
        return x

    def generator(x: int):
        yield x

    with pytest.raises(VerifiedCompileConfigurationError, match="synchronous, non-generator"):
        inspect_function(asynchronous)
    with pytest.raises(VerifiedCompileConfigurationError, match="synchronous, non-generator"):
        inspect_function(generator)


def test_decorator_requires_exactly_one_specification() -> None:
    with pytest.raises(VerifiedCompileConfigurationError, match="exactly one"):
        ai_verified_compile()
    with pytest.raises(VerifiedCompileConfigurationError, match="exactly one"):
        ai_verified_compile(post_condition=_post_condition, lean_spec=LeanSpec(proposition="result = x + 1"))


def test_cache_key_includes_post_condition_closure_values(tmp_path: Path) -> None:
    def make_condition(limit: int):
        def condition(result: int, x: int) -> bool:
            return result <= x + limit

        return condition

    shape = inspect_function(increment)
    config = VerifiedCompileConfig(cache_dir=tmp_path)
    first = _cache_key(
        increment,
        shape=shape,
        post_conditions=(make_condition(1),),
        lean_spec=None,
        config=config,
    )
    second = _cache_key(
        increment,
        shape=shape,
        post_conditions=(make_condition(2),),
        lean_spec=None,
        config=config,
    )
    assert first != second


def test_cache_key_handles_recursive_post_condition_state(tmp_path: Path) -> None:
    state: list[object] = []
    state.append(state)

    def condition(result: int, x: int) -> bool:
        return bool(state) and result == x

    key = _cache_key(
        increment,
        shape=inspect_function(increment),
        post_conditions=(condition,),
        lean_spec=None,
        config=VerifiedCompileConfig(cache_dir=tmp_path),
    )
    assert len(key) == 64


def test_lean_spec_must_name_result() -> None:
    with pytest.raises(VerifiedCompileConfigurationError, match="local `result`"):

        @ai_verified_compile(lean_spec=LeanSpec(proposition="x = x"))
        def identity(x: int) -> int:
            """Return x."""


def test_lean_spec_result_in_comment_does_not_count() -> None:
    with pytest.raises(VerifiedCompileConfigurationError, match="local `result`"):

        @ai_verified_compile(lean_spec=LeanSpec(proposition="True -- result"))
        def identity(x: int) -> int:
            """Return x."""


def test_lean_spec_uses_mapped_parameter_names() -> None:
    with pytest.raises(VerifiedCompileConfigurationError, match="'maxValue'"):

        @ai_verified_compile(lean_spec=LeanSpec(proposition="result = max_value"))
        def identity(max_value: int) -> int:
            """Return max_value."""


def test_lean_spec_accepts_quoted_lean_keyword_parameter() -> None:
    @ai_verified_compile(lean_spec=LeanSpec(proposition="result = «match»"))
    def identity(match: int) -> int:
        """Return match."""

    assert isinstance(identity, AIVerifiedFunction)


def test_additional_lean_keyword_parameter_is_quoted() -> None:
    @ai_verified_compile(lean_spec=LeanSpec(proposition="result = «variable»"))
    def identity(variable: int) -> int:
        """Return variable."""

    assert inspect_function(identity._func).parameters[0].lean_name == "«variable»"


def test_parse_generated_candidate_sections() -> None:
    response = """\
```lean
-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
def aiFunctionsImplementation (x : Int) : Int := x
-- END AI_FUNCTIONS_IMPLEMENTATION
-- BEGIN AI_FUNCTIONS_SPECIFICATION
def expected (x : Int) : Int := x
-- END AI_FUNCTIONS_SPECIFICATION
-- BEGIN AI_FUNCTIONS_POSTCONDITION
result = expected x
-- END AI_FUNCTIONS_POSTCONDITION
-- BEGIN AI_FUNCTIONS_PROOF
by
  rfl
-- END AI_FUNCTIONS_PROOF
```
"""
    candidate = parse_candidate(response, model_generates_spec=True)
    assert candidate.proposition == "result = expected x"
    assert candidate.proof == "rfl"


def test_generated_specification_section_may_be_empty() -> None:
    response = """\
-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
def aiFunctionsImplementation (x : Int) : Int := x
-- END AI_FUNCTIONS_IMPLEMENTATION
-- BEGIN AI_FUNCTIONS_SPECIFICATION
-- END AI_FUNCTIONS_SPECIFICATION
-- BEGIN AI_FUNCTIONS_POSTCONDITION
result = x
-- END AI_FUNCTIONS_POSTCONDITION
-- BEGIN AI_FUNCTIONS_PROOF
rfl
-- END AI_FUNCTIONS_PROOF
"""
    candidate = parse_candidate(response, model_generates_spec=True)
    assert candidate.spec_prelude == ""


def test_trust_policy_ignores_comments_and_strings() -> None:
    response = """\
-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
-- An unsafe axiom or module initializer would be rejected here.
def aiFunctionsImplementation (x : Int) : Int := x
-- END AI_FUNCTIONS_IMPLEMENTATION
-- BEGIN AI_FUNCTIONS_PROOF
-- Do not use sorry.
rfl
-- END AI_FUNCTIONS_PROOF
"""
    candidate = parse_candidate(response, model_generates_spec=False)
    assert candidate.implementation.endswith("def aiFunctionsImplementation (x : Int) : Int := x")
    assert candidate.proof.endswith("rfl")


@pytest.mark.parametrize(
    "bad_source, expected",
    [
        ("axiom wish : False", "axiom"),
        ("opaque hidden : Int", "opaque"),
        ("namespace Escape", "scope/import"),
        ("exact native_decide", "native_decide"),
        ('attribute [extern "lean_int_neg"] f', "extern"),
        ('initialize IO.println "side effect"', "module initializer"),
        ("#eval 1 + 1", "command-time"),
        ('def secret := include_str "/tmp/secret"', "file include"),
        ("@[export attacker_symbol] def helper : Int := 1", "export"),
        ("def _root_.helper : Int := 1", "root-namespace"),
    ],
)
def test_parse_rejects_trust_widening_source(bad_source: str, expected: str) -> None:
    response = f"""\
-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
def aiFunctionsImplementation (x : Int) : Int := x
-- END AI_FUNCTIONS_IMPLEMENTATION
-- BEGIN AI_FUNCTIONS_PROOF
{bad_source}
-- END AI_FUNCTIONS_PROOF
"""
    with pytest.raises(LeanVerificationError, match=expected):
        parse_candidate(response, model_generates_spec=False)


def test_rendered_user_spec_is_fixed_and_audited() -> None:
    shape = inspect_function(increment)
    export_name = "aivc_stub_123"
    source = render_sources(
        LeanCandidate(
            implementation="def aiFunctionsImplementation (x : Int) : Int := x + 1",
            proof="rfl",
        ),
        shape=shape,
        build_id="abcdef0123456789",
        export_name=export_name,
        lean_spec=LeanSpec(proposition="result = x + 1"),
        use_mathlib=False,
    )
    assert "let result : Int := increment x" in source.verification
    assert "result = x + 1 := by" in source.verification
    assert "import AIVerifiedabcdef0123456789" in source.verification
    assert source.implementation.count(f"@[export {export_name}]") == 1
    assert source.implementation.count("def aiFunctionsImplementation") == 1
    assert "def increment (x : Int) : Int :=\n  aiFunctionsImplementation x" in source.implementation
    assert "@[export" not in source.verification
    assert "#auditAxioms AIVerifiedabcdef0123456789" in source.verification
    assert "import Mathlib" not in source.verification


def test_internal_implementation_name_never_collides_with_wrapper() -> None:
    def ai_functions_implementation(x: int) -> int:
        raise AssertionError

    shape = inspect_function(ai_functions_implementation)
    source = render_sources(
        LeanCandidate(
            implementation="def aiFunctionsGeneratedImplementation (x : Int) : Int := x",
            proof="rfl",
        ),
        shape=shape,
        build_id="abcdef0123456789",
        export_name="aivc_collision",
        lean_spec=LeanSpec(proposition="result = x"),
        use_mathlib=False,
    )
    assert "def aiFunctionsImplementation (x : Int) : Int :=" in source.implementation
    assert "aiFunctionsGeneratedImplementation x" in source.implementation


def test_implementation_marker_inside_comment_is_not_accepted() -> None:
    shape = inspect_function(increment)
    with pytest.raises(LeanVerificationError, match="must define"):
        render_sources(
            LeanCandidate(
                implementation="""\
/- def aiFunctionsImplementation (x : Int) : Int := x + 1 -/
def increment (x : Int) : Int := x + 1
""",
                proof="rfl",
            ),
            shape=shape,
            build_id="abcdef0123456789",
            export_name="aivc_stub_123",
            lean_spec=LeanSpec(proposition="result = x + 1"),
            use_mathlib=False,
        )


def test_generated_post_condition_must_reference_result() -> None:
    shape = inspect_function(increment)
    with pytest.raises(LeanVerificationError, match="local `result`"):
        render_sources(
            LeanCandidate(
                implementation="def aiFunctionsImplementation (x : Int) : Int := x",
                proof="trivial",
                proposition="True",
            ),
            shape=shape,
            build_id="abcdef0123456789",
            export_name="aivc_stub_123",
            lean_spec=None,
            use_mathlib=False,
        )


def test_c_shim_checks_full_signed_64_bit_range() -> None:
    shim = generate_c_shim(
        inspect_function(increment),
        export_name="aivc_increment",
        module_name="AIVerifiedTest",
    )
    assert "lean_int64_of_int(result)" in shim
    assert "lean_int_dec_eq(result, roundtrip)" in shim
    assert "lean_is_scalar(result)" not in shim


def test_c_shim_handles_zero_argument_exports_as_globals() -> None:
    def answer() -> int:
        return 42

    shim = generate_c_shim(
        inspect_function(answer),
        export_name="aivc_answer",
        module_name="AIVerifiedTest",
    )
    assert "extern lean_object* aivc_answer;" in shim
    assert "extern lean_object* aivc_answer(void);" not in shim
    assert "lean_object* result = aivc_answer;" in shim
    assert "lean_inc(result);" in shim


def test_integer_ffi_rejects_bool_values() -> None:
    with pytest.raises(TypeError, match="expected int, got bool"):
        _int64(True)
    with pytest.raises(TypeError, match=r"expected list\[int\]"):
        _array_int64([1, True])


def test_python_post_condition_prompt_requests_formalization() -> None:
    prompt = build_generation_prompt(
        increment,
        shape=inspect_function(increment),
        post_conditions=(_post_condition,),
        lean_spec=None,
    )
    assert "AI_FUNCTIONS_SPECIFICATION" in prompt
    assert "AI_FUNCTIONS_POSTCONDITION" in prompt
    assert "return result == x + 1" in prompt
    assert "def aiFunctionsImplementation (x : Int) : Int := ..." in prompt
    assert "@[export" not in prompt


def test_generation_retries_with_lean_feedback() -> None:
    model = ScriptedModel([Turn(text="first attempt"), Turn(text="second attempt")])
    config = VerifiedCompileConfig(model=model, max_attempts=2)
    accepted = RenderedLeanSource(
        module_name="AIVerifiedTest",
        proof_module_name="AIVerifiedTestProof",
        namespace="AIVerifiedTest",
        theorem_name="AIVerifiedTest.stub_spec",
        export_name="aivc_stub_test",
        implementation="implementation",
        verification="verification",
        candidate=LeanCandidate(implementation="implementation", proof="proof"),
    )
    seen: list[str] = []

    def validate(response: str) -> RenderedLeanSource:
        response = response.strip()
        seen.append(response)
        if response == "first attempt":
            raise LeanVerificationError("Lean rejected the first attempt")
        return accepted

    result = generate_source(
        increment,
        shape=inspect_function(increment),
        post_conditions=(),
        lean_spec=LeanSpec(proposition="result = x + 1"),
        config=config,
        validate=validate,
    )
    assert result is accepted
    assert seen == ["first attempt", "second attempt"]
    assert model.remaining_turns == 0


def test_native_compilation_failure_is_retried_and_cached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    key = "a" * 64
    response = """\
-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
def aiFunctionsImplementation (x : Int) : Int := x + 1
-- END AI_FUNCTIONS_IMPLEMENTATION
-- BEGIN AI_FUNCTIONS_PROOF
rfl
-- END AI_FUNCTIONS_PROOF
"""
    model = ScriptedModel([Turn(text=response), Turn(text=response)])
    compile_calls = 0

    class FakeProject:
        def __init__(self, config: VerifiedCompileConfig) -> None:
            self.config = config

        def compile(
            self,
            source: RenderedLeanSource,
            *,
            shape: object,
            artifact_dir: Path,
        ) -> Path:
            nonlocal compile_calls
            del shape
            compile_calls += 1
            if compile_calls == 1:
                raise LeanCompilationError("simulated linker failure")
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"{source.module_name}.lean").write_text(source.implementation)
            (artifact_dir / f"{source.proof_module_name}.lean").write_text(source.verification)
            (artifact_dir / f"{source.module_name}.olean").write_bytes(b"implementation object")
            (artifact_dir / f"{source.proof_module_name}.olean").write_bytes(b"proof object")
            (artifact_dir / "implementation.c").write_text("/* generated */\n")
            (artifact_dir / "ffi_shim.c").write_text("/* shim */\n")
            library = artifact_dir / f"compiled{decorator_module.shared_library_suffix()}"
            library.write_bytes(b"test")
            return library

    monkeypatch.setattr(decorator_module, "_cache_key", lambda *args, **kwargs: key)
    monkeypatch.setattr(decorator_module, "LeanProject", FakeProject)
    config = VerifiedCompileConfig(model=model, max_attempts=2, cache_dir=tmp_path, mathlib_revision=None)
    shape = inspect_function(increment)

    artifact = _compile_artifact(
        increment,
        shape=shape,
        post_conditions=(),
        lean_spec=LeanSpec(proposition="result = x + 1"),
        config=config,
    )

    assert compile_calls == 2
    assert model.remaining_turns == 0
    assert (artifact.directory / ".complete").exists()

    cached = _compile_artifact(
        increment,
        shape=shape,
        post_conditions=(),
        lean_spec=LeanSpec(proposition="result = x + 1"),
        config=VerifiedCompileConfig(
            model=ScriptedModel([]),
            max_attempts=1,
            cache_dir=tmp_path,
            mathlib_revision=None,
        ),
    )
    assert cached == artifact
    assert compile_calls == 2


def test_lazy_wrapper_binds_keywords_and_compiles_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
        export_name="aivc_add",
    )
    compile_calls = 0
    load_calls = 0

    def fake_compile(*args: object, **kwargs: object) -> _CompiledArtifact:
        nonlocal compile_calls
        del args, kwargs
        compile_calls += 1
        return artifact

    def fake_load(*args: object, **kwargs: object):  # noqa: ANN202 - test double
        nonlocal load_calls
        del args, kwargs
        load_calls += 1
        return lambda x, y: x + y

    monkeypatch.setattr(decorator_module, "_compile_artifact", fake_compile)
    monkeypatch.setattr(decorator_module, "load_compiled_function", fake_load)

    @ai_verified_compile(lean_spec=LeanSpec(proposition="result = x + y"))
    def add(x: int, y: int = 2) -> int:
        raise AssertionError("the Python stub must never execute")

    assert isinstance(add, AIVerifiedFunction)
    assert add(y=4, x=3) == 7
    assert add(5) == 7
    assert compile_calls == 1
    assert load_calls == 1
