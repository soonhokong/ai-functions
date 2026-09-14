"""Python-to-Lean signature mapping for verified compilation."""

from __future__ import annotations

import inspect
import re
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_args, get_origin

from .errors import VerifiedCompileConfigurationError

_ASCII_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LEAN_KEYWORDS = {
    "abbrev",
    "axiom",
    "attribute",
    "by",
    "class",
    "def",
    "deriving",
    "do",
    "else",
    "end",
    "example",
    "export",
    "extends",
    "for",
    "from",
    "fun",
    "if",
    "include",
    "import",
    "in",
    "inductive",
    "infix",
    "infixl",
    "infixr",
    "initialize",
    "instance",
    "let",
    "local",
    "macro",
    "match",
    "mutual",
    "namespace",
    "noncomputable",
    "omit",
    "opaque",
    "open",
    "partial",
    "postfix",
    "prefix",
    "private",
    "protected",
    "scoped",
    "section",
    "structure",
    "syntax",
    "theorem",
    "then",
    "unsafe",
    "universe",
    "variable",
    "where",
    "with",
}


@dataclass(frozen=True)
class Parameter:
    """One Python parameter and its Lean counterpart."""

    python_name: str
    lean_name: str
    python_type: object
    lean_type: str


@dataclass(frozen=True)
class FunctionShape:
    """The ABI-relevant shape of a decorated Python function."""

    python_name: str
    lean_name: str
    parameters: tuple[Parameter, ...]
    return_python_type: object
    return_lean_type: str
    signature: inspect.Signature

    @property
    def lean_parameters(self) -> str:
        """Render theorem/function binders in declaration order."""
        return " ".join(f"({p.lean_name} : {p.lean_type})" for p in self.parameters)

    @property
    def lean_arguments(self) -> str:
        """Render argument names in declaration order."""
        return " ".join(p.lean_name for p in self.parameters)

    @property
    def lean_function_type(self) -> str:
        """Render the curried Lean type expected of the implementation."""
        parts = [p.lean_type for p in self.parameters]
        parts.append(self.return_lean_type)
        return " → ".join(parts)


def _lean_identifier(name: str, *, function_name: bool = False) -> str:
    if not _ASCII_IDENTIFIER.fullmatch(name):
        raise VerifiedCompileConfigurationError(
            f"@ai_verified_compile currently requires ASCII identifiers; got {name!r}",
        )
    pieces = name.split("_")
    converted = pieces[0] + "".join(piece[:1].upper() + piece[1:] for piece in pieces[1:] if piece)
    if not converted:
        raise VerifiedCompileConfigurationError(
            f"Could not derive a Lean identifier from Python name {name!r}",
        )
    if converted in _LEAN_KEYWORDS:
        if function_name:
            return f"aivc{converted[:1].upper()}{converted[1:]}"
        return f"«{converted}»"
    return converted


def python_to_lean_type(annotation: object, *, for_return: bool = False) -> str:
    """Map a supported Python annotation to its Lean type."""
    if annotation is int:
        return "Int"
    if annotation is bool:
        return "Bool"
    if annotation is float:
        return "Float"

    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        if args == (int,):
            if not for_return:
                return "Array Int"
            raise VerifiedCompileConfigurationError(
                "list[int] return values are not supported yet; use int, bool, or float",
            )

    raise VerifiedCompileConfigurationError(
        f"Unsupported type annotation {annotation!r}; supported types are int, bool, float, and list[int] parameters",
    )


def inspect_function(func: Callable[..., Any]) -> FunctionShape:
    """Validate a Python stub and derive its Lean/FFI signature."""
    if not isinstance(func, (types.FunctionType, types.MethodType)):
        raise VerifiedCompileConfigurationError("@ai_verified_compile can only decorate Python functions")
    if inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func) or inspect.isgeneratorfunction(func):
        raise VerifiedCompileConfigurationError("@ai_verified_compile requires a synchronous, non-generator function")

    signature = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func)
    except Exception as exc:
        raise VerifiedCompileConfigurationError(
            f"Could not resolve annotations for {getattr(func, '__name__', func)!r}: {exc}",
        ) from exc

    if "return" not in hints or hints["return"] is None:
        raise VerifiedCompileConfigurationError(
            f"{getattr(func, '__name__', 'function')!r} must have a return annotation",
        )

    parameters: list[Parameter] = []
    lean_parameter_names: dict[str, str] = {}
    for parameter in signature.parameters.values():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise VerifiedCompileConfigurationError("*args and **kwargs are not supported")
        if parameter.name not in hints:
            raise VerifiedCompileConfigurationError(
                f"Parameter {parameter.name!r} must have a type annotation",
            )
        lean_name = _lean_identifier(parameter.name)
        previous = lean_parameter_names.get(lean_name)
        if previous is not None:
            raise VerifiedCompileConfigurationError(
                f"Parameters {previous!r} and {parameter.name!r} both map to Lean name {lean_name!r}",
            )
        lean_parameter_names[lean_name] = parameter.name
        parameters.append(
            Parameter(
                python_name=parameter.name,
                lean_name=lean_name,
                python_type=hints[parameter.name],
                lean_type=python_to_lean_type(hints[parameter.name]),
            ),
        )

    python_name = getattr(func, "__name__", "verified_function")
    return FunctionShape(
        python_name=python_name,
        lean_name=_lean_identifier(python_name, function_name=True),
        parameters=tuple(parameters),
        return_python_type=hints["return"],
        return_lean_type=python_to_lean_type(hints["return"], for_return=True),
        signature=signature,
    )
