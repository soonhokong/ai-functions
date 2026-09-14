"""Portable C shim generation and ``ctypes`` bindings for Lean exports."""

from __future__ import annotations

import array
import ctypes
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._types import FunctionShape, Parameter
from .errors import LeanFFIError

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_LOAD_LOCK = threading.Lock()


def _parameter_c_parts(parameter: Parameter, index: int) -> tuple[list[str], str, str]:
    name = f"p{index}"
    if parameter.lean_type == "Int":
        return [f"int64_t {name}"], f"lean_object* a{index} = lean_int64_to_int({name});", "lean_object*"
    if parameter.lean_type == "Array Int":
        return (
            [f"const int64_t* {name}_data", f"size_t {name}_len"],
            f"lean_object* a{index} = aivc_int64_array({name}_data, {name}_len);",
            "lean_object*",
        )
    if parameter.lean_type == "Bool":
        return [f"uint8_t {name}"], f"uint8_t a{index} = {name};", "uint8_t"
    if parameter.lean_type == "Float":
        return [f"double {name}"], f"double a{index} = {name};", "double"
    raise LeanFFIError(f"Unsupported Lean FFI parameter type: {parameter.lean_type}")


def generate_c_shim(shape: FunctionShape, *, export_name: str, module_name: str) -> str:
    """Generate one plain-C facade for a compiled Lean function."""
    c_parameters: list[str] = []
    conversions: list[str] = []
    lean_parameter_types: list[str] = []
    call_arguments: list[str] = []
    needs_array_helper = False

    for index, parameter in enumerate(shape.parameters):
        declarations, conversion, lean_c_type = _parameter_c_parts(parameter, index)
        c_parameters.extend(declarations)
        conversions.append(f"  {conversion}")
        lean_parameter_types.append(lean_c_type)
        call_arguments.append(f"a{index}")
        needs_array_helper |= parameter.lean_type == "Array Int"

    init_name = f"{export_name}_init"
    ffi_name = f"{export_name}_ffi"
    lean_params = ", ".join(lean_parameter_types) or "void"
    c_params = ", ".join(c_parameters)
    call = f"{export_name}({', '.join(call_arguments)})" if call_arguments else export_name

    if shape.return_lean_type == "Int":
        lean_return = "lean_object*"
        out_decl = "int64_t* out"
        full_c_params = f"{c_params}, {out_decl}" if c_params else out_decl
        body = [
            f"LEAN_EXPORT uint8_t {ffi_name}({full_c_params}) {{",
            "  lean_initialize_thread();",
            *conversions,
            f"  lean_object* result = {call};",
            *(["  lean_inc(result);"] if not shape.parameters else []),
            "  int64_t value = (int64_t)lean_int64_of_int(result);",
            "  lean_object* roundtrip = lean_int64_to_int(value);",
            "  uint8_t fits = lean_int_dec_eq(result, roundtrip);",
            "  lean_dec(roundtrip);",
            "  if (!fits) {",
            "    lean_dec(result);",
            "    return 0;",
            "  }",
            "  *out = value;",
            "  lean_dec(result);",
            "  return 1;",
            "}",
        ]
    elif shape.return_lean_type == "Bool":
        lean_return = "uint8_t"
        body = [
            f"LEAN_EXPORT uint8_t {ffi_name}({c_params or 'void'}) {{",
            "  lean_initialize_thread();",
            *conversions,
            f"  return {call};",
            "}",
        ]
    elif shape.return_lean_type == "Float":
        lean_return = "double"
        body = [
            f"LEAN_EXPORT double {ffi_name}({c_params or 'void'}) {{",
            "  lean_initialize_thread();",
            *conversions,
            f"  return {call};",
            "}",
        ]
    else:
        raise LeanFFIError(f"Unsupported Lean FFI return type: {shape.return_lean_type}")

    if shape.parameters:
        export_declaration = f"extern {lean_return} {export_name}({lean_params});"
    else:
        export_declaration = f"extern {lean_return} {export_name};"

    array_helper = ""
    if needs_array_helper:
        array_helper = """\
static lean_object* aivc_int64_array(const int64_t* values, size_t size) {
  lean_object* result = lean_mk_empty_array_with_capacity(lean_box(size));
  for (size_t index = 0; index < size; ++index) {
    result = lean_array_push(result, lean_int64_to_int(values[index]));
  }
  return result;
}

"""

    return f"""\
#include <lean/lean.h>
#include <stddef.h>
#include <stdint.h>

extern void lean_initialize_runtime_module(void);
extern void lean_initialize_thread(void);
extern lean_object* initialize_{module_name}(uint8_t builtin);
{export_declaration}

static uint8_t aivc_initialized = 0;

LEAN_EXPORT uint8_t {init_name}(void) {{
  if (aivc_initialized) return 1;
  lean_initialize_runtime_module();
  lean_initialize_thread();
  lean_object* result = initialize_{module_name}(1);
  if (!lean_io_result_is_ok(result)) {{
    lean_io_result_show_error(result);
    lean_dec(result);
    return 0;
  }}
  lean_dec_ref(result);
  aivc_initialized = 1;
  return 1;
}}

{array_helper}{chr(10).join(body)}
"""


def _int64(value: object) -> ctypes.c_int64:
    if type(value) is not int:
        raise TypeError(f"expected int, got {type(value).__name__}")
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise OverflowError(f"{value} does not fit the verified compiler's signed 64-bit FFI")
    return ctypes.c_int64(value)


def _array_int64(value: object) -> tuple[Any, ctypes.c_size_t, object]:
    if not isinstance(value, list) or not all(type(item) is int for item in value):
        raise TypeError("expected list[int]")
    for item in value:
        if not _INT64_MIN <= item <= _INT64_MAX:
            raise OverflowError(f"{item} does not fit the verified compiler's signed 64-bit FFI")

    storage = array.array("q", value)
    if storage.itemsize != ctypes.sizeof(ctypes.c_int64):
        fallback = (ctypes.c_int64 * len(value))(*value)
        return ctypes.cast(fallback, ctypes.POINTER(ctypes.c_int64)), ctypes.c_size_t(len(value)), fallback
    if not storage:
        return ctypes.POINTER(ctypes.c_int64)(), ctypes.c_size_t(0), storage
    view = (ctypes.c_int64 * len(storage)).from_buffer(storage)
    return ctypes.cast(view, ctypes.POINTER(ctypes.c_int64)), ctypes.c_size_t(len(storage)), storage


def _convert_argument(parameter: Parameter, value: object) -> tuple[list[object], object | None]:
    if parameter.lean_type == "Int":
        return [_int64(value)], None
    if parameter.lean_type == "Array Int":
        pointer, size, keeper = _array_int64(value)
        return [pointer, size], keeper
    if parameter.lean_type == "Bool":
        if not isinstance(value, bool):
            raise TypeError(f"expected bool, got {type(value).__name__}")
        return [ctypes.c_uint8(value)], None
    if parameter.lean_type == "Float":
        if type(value) not in (int, float):
            raise TypeError(f"expected float, got {type(value).__name__}")
        return [ctypes.c_double(float(value))], None
    raise LeanFFIError(f"Unsupported Lean FFI parameter type: {parameter.lean_type}")


def _ctypes_parameter_types(parameter: Parameter) -> list[Any]:
    if parameter.lean_type == "Int":
        return [ctypes.c_int64]
    if parameter.lean_type == "Array Int":
        return [ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t]
    if parameter.lean_type == "Bool":
        return [ctypes.c_uint8]
    if parameter.lean_type == "Float":
        return [ctypes.c_double]
    raise LeanFFIError(f"Unsupported Lean FFI parameter type: {parameter.lean_type}")


def load_compiled_function(
    library_path: Path,
    *,
    shape: FunctionShape,
    export_name: str,
) -> Callable[..., object]:
    """Load a compiled library and return its ordered-argument Python wrapper."""
    with _LOAD_LOCK:
        try:
            library = ctypes.CDLL(str(library_path))
        except OSError as exc:
            raise LeanFFIError(f"Could not load compiled Lean library {library_path}: {exc}") from exc

        try:
            initialize = getattr(library, f"{export_name}_init")
            ffi_function = getattr(library, f"{export_name}_ffi")
        except AttributeError as exc:
            raise LeanFFIError(f"Compiled library is missing its generated FFI symbols: {exc}") from exc

        initialize.argtypes = []
        initialize.restype = ctypes.c_uint8
        if initialize() != 1:
            raise LeanFFIError(f"Lean module initialization failed for {library_path}")

    library_handle = library
    argument_types: list[object] = []
    for parameter in shape.parameters:
        argument_types.extend(_ctypes_parameter_types(parameter))

    if shape.return_lean_type == "Int":
        ffi_function.argtypes = [*argument_types, ctypes.POINTER(ctypes.c_int64)]
        ffi_function.restype = ctypes.c_uint8

        def call(*values: object) -> int:
            c_arguments: list[object] = []
            keepers: list[object] = []
            for parameter, value in zip(shape.parameters, values, strict=True):
                converted, keeper = _convert_argument(parameter, value)
                c_arguments.extend(converted)
                if keeper is not None:
                    keepers.append(keeper)
            output = ctypes.c_int64()
            status = ffi_function(*c_arguments, ctypes.byref(output))
            _ = library_handle, keepers
            if status != 1:
                raise OverflowError("compiled Lean Int result does not fit signed 64-bit FFI")
            return int(output.value)

    elif shape.return_lean_type == "Bool":
        ffi_function.argtypes = argument_types
        ffi_function.restype = ctypes.c_uint8

        def call(*values: object) -> bool:
            c_arguments: list[object] = []
            keepers: list[object] = []
            for parameter, value in zip(shape.parameters, values, strict=True):
                converted, keeper = _convert_argument(parameter, value)
                c_arguments.extend(converted)
                if keeper is not None:
                    keepers.append(keeper)
            result = bool(ffi_function(*c_arguments))
            _ = library_handle, keepers
            return result

    elif shape.return_lean_type == "Float":
        ffi_function.argtypes = argument_types
        ffi_function.restype = ctypes.c_double

        def call(*values: object) -> float:
            c_arguments: list[object] = []
            keepers: list[object] = []
            for parameter, value in zip(shape.parameters, values, strict=True):
                converted, keeper = _convert_argument(parameter, value)
                c_arguments.extend(converted)
                if keeper is not None:
                    keepers.append(keeper)
            result = float(ffi_function(*c_arguments))
            _ = library_handle, keepers
            return result

    else:
        raise LeanFFIError(f"Unsupported Lean FFI return type: {shape.return_lean_type}")

    return call
