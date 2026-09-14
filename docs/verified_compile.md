# Lean-Verified Compilation

`@ai_verified_compile` deterministically translates a restricted Python
post-condition into Lean, uses an AI Function to synthesize an implementation
and proof, and exposes the compiled result as an ordinary synchronous Python
callable.

## Recommended Usage

Review an ordinary Python predicate:

```python
from ai_functions import ai_verified_compile


def adds_one(result: int, value: int) -> bool:
    return result == value + 1


@ai_verified_compile(post_condition=adds_one)
def increment(value: int) -> int:
    """Return one more than value."""


assert increment(41) == 42
```

Compilation is lazy by default: the first call generates, verifies, compiles, caches, and loads the native library. Pass `compile_on="import_time"` to compile while applying the decorator.

The Python contract is parsed and translated before the model is invoked. The
model receives the fixed Lean theorem but cannot change it. The original Python
predicate is also re-run after each native call as a translation and FFI
backstop.

## Pipeline

For each uncached function, the library:

1. Maps the supported Python signature to Lean and a fixed native ABI.
2. Parses each Python contract into a typed, fail-closed contract expression.
3. Deterministically emits one fixed Lean proposition.
4. Uses an `@ai_function` to generate only a Lean-core implementation and proof body.
5. Rejects generated trust-widening constructs such as `sorry`, axioms, opaque or unsafe declarations, compiler overrides, initializers, and custom elaborators.
6. Compiles the implementation to both `.olean` and C in one Lean invocation.
7. Imports that exact `.olean` into the proof module, checks the theorem, and audits its declarations for non-foundational axioms and generated opaque constants.
8. Links the generated C with a library-generated FFI shim and loads the shared library with `ctypes`.
9. Re-runs the Python contract on the native result.

Any parsing, Lean, code-generation, or linking failure becomes post-condition feedback for the next AI Function attempt. `max_attempts` is the total number of model responses, including the first.

## Trust Model

With a reviewed Python contract, the model is outside the trust boundary. It
can propose an implementation and proof, but it cannot change the
deterministically generated theorem or make Lean accept an invalid proof.

The trust base still includes:

- the reviewed Python contract;
- the deterministic Python-contract-to-Lean translator and its Python-semantics model;
- Lean's kernel, compiler, runtime, and core primitives;
- the deterministic FFI shim and Python loading code;
- `leanc` and the host C compiler/linker.

As with any proof-producing compiler pipeline, a bug in contract translation,
code generation, or the native toolchain could make the checked theorem differ
from Python intent or the executable differ from checked Lean semantics. The
runtime Python check catches many translator and FFI discrepancies on values
that are actually executed.

## Verified Python Contract Subset

The initial language is intentionally small, pure, and total:

- Types: `int`, `bool`, and `list[int]`.
- One side-effect-free `return <boolean expression>` statement.
- Integer literals and immutable captured `int`/`bool` constants.
- Integer `+`, `-`, `*`, unary `+`/`-`.
- `==`, `!=`, `<`, `<=`, `>`, `>=`, and chained comparisons.
- Boolean `and`, `or`, and `not`.
- Conditional expressions (`x if condition else y`).
- `len(values)`, `needle in values`, and `needle not in values`.
- `all(...)` and `any(...)` over one `list[int]` generator, including filters.
- Multiple post-conditions, combined by conjunction.

Unsupported syntax fails while applying the decorator; it is never handed to
the model for informal translation. Division, modulo, indexing, mutation,
arbitrary calls, exceptions, async code, and floating-point contracts are
currently rejected because they require additional Python-semantics models.
The native ABI can still expose `float` when an explicit `LeanSpec` is used.

Exactly one of `lean_spec` and `post_condition` is required. `post_condition` may also be a non-empty sequence of callables.
The current AI Functions API exposes post-conditions; separate pre-condition
and loop-invariant decorators are not yet part of this integration.

The contract IR and fail-closed translation strategy are inspired by
[Strata-Python](https://github.com/strata-org/Strata-Python)'s `SpecExpr`.
This package implements the focused Lean backend locally rather than installing
Strata's complete Laurel/Core/SMT verification stack.

## Explicit Lean Specifications

`LeanSpec` remains available as an advanced escape hatch for contracts outside
the Python subset:

```python
from ai_functions import LeanSpec, ai_verified_compile


@ai_verified_compile(lean_spec=LeanSpec(proposition="result = value + value"))
def double(value: int) -> int:
    """Return twice value."""
```

The proposition can refer to the local `result` binding and the Lean parameter
names. Python snake-case names are converted to lower camel case, so
`max_value` becomes `maxValue`. `LeanSpec.prelude` can contain reviewed helper
definitions used by the proposition.

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

The feature supports macOS and Linux. By default, it downloads a checksum-pinned
`elan` bootstrap and the configured Lean release into the AI Functions cache.
It does not modify the user's shell `PATH`, home elan installation, or shell
startup files. Lean's bundled `leanc` toolchain performs native compilation.

`VerifiedCompileConfig` controls the model, retry count, cache, and pinned Lean environment:

```python
VerifiedCompileConfig(
    model=None,
    max_attempts=5,
    compile_on="first_call",
    toolchain_mode="managed",
    lean_toolchain="leanprover/lean4:v4.33.1",
    mathlib_revision=None,
)
```

Set `toolchain_mode="system"` to require `lake` on `PATH` instead. Set
`mathlib_revision` to a pinned Mathlib revision when broader proof tactics are
needed; the default uses Lean core only.

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
