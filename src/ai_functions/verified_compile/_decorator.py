"""Public decorator and lazy compiled-function wrapper."""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import inspect
import json
import platform
import sys
import threading
import types
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Unpack, overload

from ._contracts import translate_post_conditions
from ._ffi import load_compiled_function
from ._generation import generate_source, parse_generated_response
from ._lean import LeanProject, shared_library_suffix
from ._locking import exclusive_file_lock
from ._source import LeanSpec, RenderedLeanSource, render_sources, validate_lean_spec_names
from ._types import FunctionShape, inspect_function
from .config import VerifiedCompileConfig, VerifiedCompileKwargs
from .errors import PythonContractViolation, VerifiedCompileConfigurationError

_CACHE_FORMAT = "ai-functions-verified-compile-v2"


def _source_text(callable_: Callable[..., Any]) -> str:
    try:
        return inspect.getsource(callable_)
    except (OSError, TypeError) as exc:
        raise VerifiedCompileConfigurationError(
            f"Could not read source for {getattr(callable_, '__name__', callable_)!r}",
        ) from exc


def _value_fingerprint(value: object, seen: set[int]) -> str:
    """Describe values that can affect a callable's Python semantics."""
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return repr(value)
    if isinstance(value, Path):
        return f"path:{value}"
    if isinstance(value, (tuple, list, dict, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return f"{type(value).__name__}-cycle"
        seen.add(identity)
        try:
            if isinstance(value, tuple):
                return "tuple:" + repr(tuple(_value_fingerprint(item, seen) for item in value))
            if isinstance(value, list):
                return "list:" + repr(tuple(_value_fingerprint(item, seen) for item in value))
            if isinstance(value, dict):
                items = sorted(
                    (
                        _value_fingerprint(key, seen),
                        _value_fingerprint(item, seen),
                    )
                    for key, item in value.items()
                )
                return f"dict:{items!r}"
            return f"set:{sorted(_value_fingerprint(item, seen) for item in value)!r}"
        finally:
            seen.remove(identity)
    if isinstance(value, types.ModuleType):
        file_name = getattr(value, "__file__", None)
        file_stamp = ""
        if file_name:
            try:
                stat = Path(file_name).stat()
                file_stamp = f":{file_name}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                file_stamp = f":{file_name}"
        return f"module:{value.__name__}:{getattr(value, '__version__', '')!r}{file_stamp}"
    if inspect.ismethod(value):
        return f"method:{_value_fingerprint(value.__func__, seen)}:{_value_fingerprint(value.__self__, seen)}"
    if inspect.isfunction(value):
        identity = id(value)
        if identity in seen:
            return f"function-cycle:{value.__module__}.{value.__qualname__}"
        seen.add(identity)
        try:
            parts = [
                f"function:{value.__module__}.{value.__qualname__}",
                _source_text(value),
                f"defaults:{_value_fingerprint(value.__defaults__, seen)}",
                f"kwdefaults:{_value_fingerprint(value.__kwdefaults__, seen)}",
            ]
            try:
                closure = inspect.getclosurevars(value)
            except TypeError:
                closure = None
            if closure is not None:
                for scope_name, values in (("nonlocal", closure.nonlocals), ("global", closure.globals)):
                    for name, captured in sorted(values.items()):
                        parts.append(f"{scope_name}:{name}:{_value_fingerprint(captured, seen)}")
            return "\0".join(parts)
        finally:
            seen.remove(identity)

    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    try:
        representation = repr(value)
    except Exception:
        representation = "<unrepresentable>"
    return f"object:{type_name}:{representation}"


def _callable_fingerprint(callable_: Callable[..., Any]) -> str:
    return _value_fingerprint(callable_, set())


def _normalize_post_conditions(
    post_condition: Callable[..., Any] | Sequence[Callable[..., Any]] | None,
) -> tuple[Callable[..., Any], ...]:
    if post_condition is None:
        return ()
    if callable(post_condition):
        return (post_condition,)
    conditions = tuple(post_condition)
    if not conditions or not all(callable(condition) for condition in conditions):
        raise VerifiedCompileConfigurationError("post_condition must be a callable or a non-empty sequence")
    return conditions


def _cache_key(
    func: Callable[..., Any],
    *,
    shape: FunctionShape,
    post_conditions: tuple[Callable[..., Any], ...],
    lean_spec: LeanSpec | None,
    config: VerifiedCompileConfig,
) -> str:
    digest = hashlib.sha256()
    parts = [
        _CACHE_FORMAT,
        _callable_fingerprint(func),
        repr(shape.signature),
        repr(tuple((parameter.python_name, parameter.lean_type) for parameter in shape.parameters)),
        shape.return_lean_type,
        config.lean_toolchain,
        config.mathlib_revision or "",
        config.toolchain_mode,
        sys.platform,
        platform.machine(),
    ]
    if lean_spec is not None:
        parts.extend((lean_spec.prelude, lean_spec.proposition))
    if post_conditions:
        parts.extend(_callable_fingerprint(condition) for condition in post_conditions)
    for part in parts:
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_symbol(name: str) -> str:
    return "".join(character if character.isascii() and character.isalnum() else "_" for character in name)


@dataclasses.dataclass(frozen=True)
class _CompiledArtifact:
    directory: Path
    implementation_path: Path
    verification_path: Path
    implementation_object_path: Path
    proof_object_path: Path
    c_source_path: Path
    ffi_shim_path: Path
    library_path: Path
    metadata_path: Path
    export_name: str


def _load_cached_artifact(artifact_dir: Path, *, export_name: str, module_name: str) -> _CompiledArtifact | None:
    implementation = artifact_dir / f"{module_name}.lean"
    verification = artifact_dir / f"{module_name}Proof.lean"
    implementation_object = artifact_dir / f"{module_name}.olean"
    proof_object = artifact_dir / f"{module_name}Proof.olean"
    c_source = artifact_dir / "implementation.c"
    ffi_shim = artifact_dir / "ffi_shim.c"
    library = artifact_dir / f"compiled{shared_library_suffix()}"
    metadata = artifact_dir / "metadata.json"
    marker = artifact_dir / ".complete"
    required = (
        marker,
        implementation,
        verification,
        implementation_object,
        proof_object,
        c_source,
        ffi_shim,
        library,
        metadata,
    )
    if not all(path.exists() for path in required):
        return None
    return _CompiledArtifact(
        directory=artifact_dir,
        implementation_path=implementation,
        verification_path=verification,
        implementation_object_path=implementation_object,
        proof_object_path=proof_object,
        c_source_path=c_source,
        ffi_shim_path=ffi_shim,
        library_path=library,
        metadata_path=metadata,
        export_name=export_name,
    )


def _compile_artifact(
    func: Callable[..., Any],
    *,
    shape: FunctionShape,
    post_conditions: tuple[Callable[..., Any], ...],
    lean_spec: LeanSpec,
    config: VerifiedCompileConfig,
) -> _CompiledArtifact:
    key = _cache_key(
        func,
        shape=shape,
        post_conditions=post_conditions,
        lean_spec=lean_spec,
        config=config,
    )
    build_id = key[:16]
    module_name = f"AIVerified{build_id}"
    export_name = f"aivc_{_safe_symbol(shape.python_name)}_{key[:12]}"
    cache_root = Path(config.cache_dir).expanduser().resolve()
    artifact_dir = cache_root / "functions" / key
    lock_path = cache_root / "functions" / ".locks" / f"{key}.lock"

    with exclusive_file_lock(lock_path):
        cached = _load_cached_artifact(artifact_dir, export_name=export_name, module_name=module_name)
        if cached is not None:
            return cached

        marker = artifact_dir / ".complete"
        marker.unlink(missing_ok=True)
        project = LeanProject(config)
        compiled_library: dict[str, Path] = {}

        def validate(response: str) -> RenderedLeanSource:
            candidate = parse_generated_response(response)
            rendered = render_sources(
                candidate,
                shape=shape,
                build_id=build_id,
                export_name=export_name,
                lean_spec=lean_spec,
                use_mathlib=config.mathlib_revision is not None,
            )
            compiled_library["path"] = project.compile(
                rendered,
                shape=shape,
                artifact_dir=artifact_dir,
            )
            return rendered

        rendered = generate_source(
            func,
            shape=shape,
            lean_spec=lean_spec,
            config=config,
            validate=validate,
        )
        library_path = compiled_library["path"]
        metadata = {
            "format": _CACHE_FORMAT,
            "function": shape.python_name,
            "module": rendered.module_name,
            "proof_module": rendered.proof_module_name,
            "theorem": rendered.theorem_name,
            "export": rendered.export_name,
            "lean_toolchain": config.lean_toolchain,
            "toolchain_mode": config.toolchain_mode,
            "mathlib_revision": config.mathlib_revision,
            "specification_origin": "python-contract" if post_conditions else "lean-spec",
            "lean_proposition": lean_spec.proposition,
            "platform": sys.platform,
            "machine": platform.machine(),
        }
        metadata_path = artifact_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        marker.write_text("ok\n")
        return _CompiledArtifact(
            directory=artifact_dir,
            implementation_path=artifact_dir / f"{rendered.module_name}.lean",
            verification_path=artifact_dir / f"{rendered.proof_module_name}.lean",
            implementation_object_path=artifact_dir / f"{rendered.module_name}.olean",
            proof_object_path=artifact_dir / f"{rendered.proof_module_name}.olean",
            c_source_path=artifact_dir / "implementation.c",
            ffi_shim_path=artifact_dir / "ffi_shim.c",
            library_path=library_path,
            metadata_path=metadata_path,
            export_name=rendered.export_name,
        )


class AIVerifiedFunction[**P, T]:
    """A lazily generated, proved, and natively compiled Python callable."""

    def __init__(
        self,
        func: Callable[P, T],
        *,
        post_conditions: tuple[Callable[..., Any], ...],
        lean_spec: LeanSpec | None,
        config: VerifiedCompileConfig,
    ) -> None:
        self._func = func
        self._shape = inspect_function(func)
        self._post_conditions = post_conditions
        self._lean_spec = lean_spec or translate_post_conditions(post_conditions, shape=self._shape)
        self._config = config
        self._lock = threading.Lock()
        self._compiled: Callable[..., object] | None = None
        self._artifact: _CompiledArtifact | None = None
        functools.update_wrapper(self, func)

        validate_lean_spec_names(self._lean_spec, self._shape)
        if config.compile_on == "import_time":
            self.compile()

    @property
    def config(self) -> VerifiedCompileConfig:
        """The immutable compiler configuration."""
        return self._config

    @property
    def lean_spec(self) -> LeanSpec:
        """The fixed Lean contract, deterministically generated from Python when requested."""
        return self._lean_spec

    @property
    def artifact_dir(self) -> Path:
        """Directory containing the checked Lean, generated C, and library."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.directory

    @property
    def implementation_source_path(self) -> Path:
        """Path to the Init-only Lean implementation that is compiled."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.implementation_path

    @property
    def lean_source_path(self) -> Path:
        """Path to the Lean theorem and axiom audit reviewers should inspect."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.verification_path

    @property
    def lean_source(self) -> str:
        """The complete checked Lean proof source."""
        return self.lean_source_path.read_text()

    @property
    def implementation_object_path(self) -> Path:
        """Path to the exact compiled ``.olean`` imported by the proof."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.implementation_object_path

    @property
    def proof_object_path(self) -> Path:
        """Path to the kernel-checked proof module object."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.proof_object_path

    @property
    def c_source_path(self) -> Path:
        """Path to C generated from the verified Lean implementation."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.c_source_path

    @property
    def ffi_shim_path(self) -> Path:
        """Path to the portable C facade used by Python."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.ffi_shim_path

    @property
    def shared_library_path(self) -> Path:
        """Path to the loaded native shared library."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.library_path

    @property
    def metadata_path(self) -> Path:
        """Path to compiler, platform, and artifact metadata."""
        self.compile()
        assert self._artifact is not None
        return self._artifact.metadata_path

    def compile(self) -> AIVerifiedFunction[P, T]:
        """Generate, kernel-check, compile, and load this function once."""
        if self._compiled is not None:
            return self
        with self._lock:
            if self._compiled is not None:
                return self
            artifact = _compile_artifact(
                self._func,
                shape=self._shape,
                post_conditions=self._post_conditions,
                lean_spec=self._lean_spec,
                config=self._config,
            )
            compiled = load_compiled_function(
                artifact.library_path,
                shape=self._shape,
                export_name=artifact.export_name,
            )
            self._artifact = artifact
            self._compiled = compiled
        return self

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> T:
        """Call the verified native implementation."""
        self.compile()
        assert self._compiled is not None
        bound = self._shape.signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = [bound.arguments[parameter.python_name] for parameter in self._shape.parameters]
        result = self._compiled(*values)
        self._check_python_contracts(result, bound.arguments)
        return result  # type: ignore[return-value]

    def _check_python_contracts(self, result: object, bound_arguments: dict[str, object]) -> None:
        """Re-run reviewed Python contracts as a translation/FFI backstop."""
        for condition in self._post_conditions:
            signature = inspect.signature(condition)
            positional: list[object] = [result]
            keyword: dict[str, object] = {}
            for index, parameter in enumerate(signature.parameters.values()):
                if index == 0:
                    continue
                value = bound_arguments[parameter.name]
                if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                    keyword[parameter.name] = value
                else:
                    positional.append(value)
            try:
                outcome = condition(*positional, **keyword)
            except Exception as exc:
                raise PythonContractViolation(
                    f"Compiled result raised in Python contract {condition.__qualname__!r}: {exc}",
                ) from exc
            if type(outcome) is not bool:
                raise PythonContractViolation(
                    f"Python contract {condition.__qualname__!r} returned {type(outcome).__name__}, expected bool",
                )
            if not outcome:
                raise PythonContractViolation(
                    f"Compiled result violated Python contract {condition.__qualname__!r}",
                )


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


def ai_verified_compile[**P, T](
    func: Callable[P, T] | None = None,
    /,
    *,
    post_condition: Callable[..., Any] | Sequence[Callable[..., Any]] | None = None,
    lean_spec: LeanSpec | None = None,
    config: VerifiedCompileConfig | None = None,
    **config_overrides: Unpack[VerifiedCompileKwargs],
) -> AIVerifiedFunction[P, T] | Callable[[Callable[P, T]], AIVerifiedFunction[P, T]]:
    """Compile an AI Function to Lean, prove its post-condition, and call it via FFI.

    Exactly one of ``post_condition`` and ``lean_spec`` is required. A Python
    post-condition is translated deterministically into a fixed Lean theorem
    before model-driven implementation and proof search begins.
    """
    post_conditions = _normalize_post_conditions(post_condition)
    if bool(post_conditions) == (lean_spec is not None):
        raise VerifiedCompileConfigurationError("provide exactly one of post_condition or lean_spec")

    base = config or VerifiedCompileConfig()
    try:
        resolved = dataclasses.replace(base, **config_overrides)
    except TypeError as exc:
        raise VerifiedCompileConfigurationError(str(exc)) from exc

    def decorate(target: Callable[P, T]) -> AIVerifiedFunction[P, T]:
        return AIVerifiedFunction(
            target,
            post_conditions=post_conditions,
            lean_spec=lean_spec,
            config=resolved,
        )

    if func is not None:
        return decorate(func)
    return decorate
