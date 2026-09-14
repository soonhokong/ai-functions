# Lean-Verified Compilation

`@ai_verified_compile` uses an AI Function to synthesize Lean code, asks Lean to prove a post-condition, and exposes the compiled result as an ordinary synchronous Python callable.

## Recommended Usage

Use `LeanSpec` when the Lean proposition is the contract you reviewed:

```python
from ai_functions import LeanSpec, VerifiedCompileConfig, ai_verified_compile


@ai_verified_compile(
    lean_spec=LeanSpec(
        proposition="result = values.foldl (fun total value => total + value) 0",
    ),
    config=VerifiedCompileConfig(mathlib_revision=None),
)
def sum_values(values: list[int]) -> int:
    """Return the sum of values."""


assert sum_values([3, -2, 10, 4]) == 15
```

Compilation is lazy by default: the first call generates, verifies, compiles, caches, and loads the native library. Pass `compile_on="import_time"` to compile while applying the decorator.

The proposition can refer to the local `result` binding and the Lean parameter names. Python snake-case names are converted to lower camel case, so `max_value` becomes `maxValue`. `LeanSpec.prelude` can contain reviewed helper definitions used by the proposition.

## Pipeline

For each uncached function, the library:

1. Maps the supported Python signature to Lean and a fixed native ABI.
2. Uses an `@ai_function` to generate a Lean-core implementation and proof body.
3. Rejects generated trust-widening constructs such as `sorry`, axioms, opaque or unsafe declarations, compiler overrides, initializers, and custom elaborators.
4. Compiles the implementation to both `.olean` and C in one Lean invocation.
5. Imports that exact `.olean` into the proof module, checks the theorem, and audits its declarations for non-foundational axioms and generated opaque constants.
6. Links the generated C with a library-generated FFI shim and loads the shared library with `ctypes`.

Any parsing, Lean, code-generation, or linking failure becomes post-condition feedback for the next AI Function attempt. `max_attempts` is the total number of model responses, including the first.

## Trust Model

With a user-reviewed `LeanSpec`, the model is outside the trust boundary. It can propose an implementation and proof, but it cannot make the pipeline accept either unless Lean checks the fixed theorem.

The trust base still includes:

- the reviewed Lean specification and any reviewed `prelude`;
- Lean's kernel, compiler, runtime, and core primitives;
- the deterministic FFI shim and Python loading code;
- `leanc` and the host C compiler/linker.

As with any proof-producing compiler pipeline, a bug in code generation or the native toolchain could make the executable differ from the checked Lean semantics.

## Python Post-Conditions

For convenience, a Python post-condition can be supplied instead:

```python
def adds_one(result: int, value: int) -> bool:
    return result == value + 1


@ai_verified_compile(post_condition=adds_one)
def increment(value: int) -> int:
    """Return one more than value."""
```

In this mode the model also translates the Python validator into a Lean proposition. Lean verifies the generated proposition, but the Python-to-Lean translation is not automatically guaranteed to preserve the user's intent. Review `lean_source_path`, or promote the reviewed proposition into a `LeanSpec`, before relying on the stronger trust claim.

Exactly one of `lean_spec` and `post_condition` is required. `post_condition` may also be a non-empty sequence of callables.

## Supported ABI

The initial ABI intentionally stays small:

| Python type | Lean type | Direction |
| --- | --- | --- |
| `int` | `Int` | input and output |
| `bool` | `Bool` | input and output |
| `float` | `Float` | input and output |
| `list[int]` | `Array Int` | input only |

Python integers and list elements cross the FFI boundary as signed 64-bit values. Lean calculations still use arbitrary-precision `Int`; an output outside the signed 64-bit range raises `OverflowError` rather than truncating.

Only synchronous, non-generator Python functions are supported. Variadic parameters and collection return values are rejected before generation.

## Toolchain and Configuration

The feature supports macOS and Linux and requires:

- Lean installed through `elan`, with `lake` and `leanc` available on `PATH`;
- a native C compiler/linker usable by `leanc`.

`VerifiedCompileConfig` controls the model, retry count, cache, and pinned Lean environment:

```python
VerifiedCompileConfig(
    model=None,
    max_attempts=5,
    compile_on="first_call",
    lean_toolchain="leanprover/lean4:v4.33.1",
    mathlib_revision="v4.33.1",
)
```

Set `mathlib_revision=None` for Lean-core-only proofs. This avoids downloading Mathlib and is useful for small proofs and hermetic tests. The default enables pinned Mathlib tactics for broader proof search.

## Inspecting Artifacts

The wrapper exposes all accepted artifacts:

```python
sum_values.compile()

print(sum_values.implementation_source_path)
print(sum_values.implementation_object_path)
print(sum_values.lean_source_path)
print(sum_values.proof_object_path)
print(sum_values.c_source_path)
print(sum_values.ffi_shim_path)
print(sum_values.shared_library_path)
print(sum_values.metadata_path)
```

Artifacts are content-addressed by the function, specification, captured post-condition state, toolchain, and host platform. Cache population is serialized across threads and Unix processes, and the completion marker is written only after the full artifact set exists.
