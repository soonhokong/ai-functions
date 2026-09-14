"""Use an AI Function to synthesize Lean code and feed Lean errors back."""

from __future__ import annotations

import inspect
import textwrap
from collections.abc import Callable, Sequence
from typing import Any

from ..ai_thread import ai_function
from ._source import LeanCandidate, LeanSpec, RenderedLeanSource, _model_implementation_name, parse_candidate
from ._types import FunctionShape
from .config import VerifiedCompileConfig
from .errors import VerifiedCompileConfigurationError

_SYSTEM_PROMPT = """\
You are an expert Lean 4 programmer and proof engineer. Produce only the marker-delimited
Lean fragments requested by the user.

Hard requirements:
- Every function must be total. Do not use partial, unsafe, opaque, axiom, sorry, or admit.
- Do not use native_decide, run_tac, custom syntax, macros, elaborators, @[extern], or implemented_by.
- Do not emit imports, namespaces, sections, end commands, exports, or `_root_` declarations.
- The implementation and every helper it calls must use Lean core only. Mathlib may be used by the proof.
- Python list[int] is represented as Lean Array Int.
- Keep the exact requested implementation name and signature. The caller supplies the exported ABI wrapper.
- Output complete code fragments. A Lean kernel check and an axiom audit will reject invalid answers.
"""


def _callable_source(callable_: Callable[..., Any], label: str) -> str:
    try:
        return textwrap.dedent(inspect.getsource(callable_)).strip()
    except (OSError, TypeError) as exc:
        raise VerifiedCompileConfigurationError(
            f"Could not read source for {label}; define it in a Python module rather than an interactive shell",
        ) from exc


def _theorem_preview(shape: FunctionShape, lean_spec: LeanSpec) -> str:
    binders = f" {shape.lean_parameters}" if shape.lean_parameters else ""
    call = shape.lean_name
    if shape.lean_arguments:
        call += f" {shape.lean_arguments}"
    prelude = lean_spec.prelude.strip()
    return f"""\
{prelude}

theorem {shape.lean_name}_spec{binders} :
    let result : {shape.return_lean_type} := {call}
    {lean_spec.proposition.strip()} := by
  -- your AI_FUNCTIONS_PROOF fragment is inserted here
"""


def build_generation_prompt(
    func: Callable[..., Any],
    *,
    shape: FunctionShape,
    post_conditions: Sequence[Callable[..., Any]],
    lean_spec: LeanSpec | None,
) -> str:
    """Build the single synthesis prompt used by the retrying AI Function."""
    function_source = _callable_source(func, f"function {shape.python_name!r}")
    implementation_name = _model_implementation_name(shape)
    signature = f"{implementation_name} {shape.lean_parameters} : {shape.return_lean_type}".replace("  ", " ")

    if lean_spec is not None:
        task = f"""\
The user supplied and reviewed this exact Lean specification:

```lean
{_theorem_preview(shape, lean_spec)}
```

Return exactly these sections:

-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
def {signature} := ...
-- END AI_FUNCTIONS_IMPLEMENTATION

-- BEGIN AI_FUNCTIONS_PROOF
...commands inside the theorem's `by`; omit the outer `by`...
-- END AI_FUNCTIONS_PROOF

Do not restate or modify the specification.
"""
    else:
        rendered_conditions = "\n\n".join(
            f"Python post-condition {index + 1}:\n```python\n"
            f"{_callable_source(condition, getattr(condition, '__name__', 'post_condition'))}\n```"
            for index, condition in enumerate(post_conditions)
        )
        task = f"""\
Formalize every Python post-condition below as one Lean proposition. The proposition may
refer to `result` and to the Lean parameter names. Put helper definitions used by that
proposition in AI_FUNCTIONS_SPECIFICATION, and put only the proposition expression in
AI_FUNCTIONS_POSTCONDITION. Leave AI_FUNCTIONS_SPECIFICATION empty when no helpers are needed.

{rendered_conditions}

Return exactly these sections:

-- BEGIN AI_FUNCTIONS_IMPLEMENTATION
def {signature} := ...
-- END AI_FUNCTIONS_IMPLEMENTATION

-- BEGIN AI_FUNCTIONS_SPECIFICATION
...Lean definitions that formalize the Python checks...
-- END AI_FUNCTIONS_SPECIFICATION

-- BEGIN AI_FUNCTIONS_POSTCONDITION
...one Prop expression using result and the parameters...
-- END AI_FUNCTIONS_POSTCONDITION

-- BEGIN AI_FUNCTIONS_PROOF
...commands inside the generated theorem's `by`; omit the outer `by`...
-- END AI_FUNCTIONS_PROOF
"""

    return f"""\
Implement this Python AI Function in Lean and prove its post-condition.

Python source:
```python
{function_source}
```

Required Lean implementation type:
```lean
{signature}
```

{task}
"""


def generate_source(
    func: Callable[..., Any],
    *,
    shape: FunctionShape,
    post_conditions: Sequence[Callable[..., Any]],
    lean_spec: LeanSpec | None,
    config: VerifiedCompileConfig,
    validate: Callable[[str], RenderedLeanSource],
) -> RenderedLeanSource:
    """Generate a candidate through ``@ai_function`` and validate every turn."""
    prompt = build_generation_prompt(
        func,
        shape=shape,
        post_conditions=post_conditions,
        lean_spec=lean_spec,
    )
    accepted: dict[str, RenderedLeanSource] = {}

    def validate_lean(response: str) -> None:
        accepted["source"] = validate(response)

    def synthesize() -> str:
        return prompt

    synthesize.__name__ = f"compile_{shape.python_name}"
    synthesize.__doc__ = f"Generate verified Lean source for {shape.python_name}."

    compiler = ai_function[str](
        model=config.model,
        system_prompt=_SYSTEM_PROMPT,
        structured_output=False,
        post_conditions=[validate_lean],
        max_attempts=config.max_attempts - 1,
        coordinator_tools_enabled=False,
        summarization_enabled=False,
    )(synthesize)
    response = compiler.run_sync()
    source = accepted.get("source")
    if source is not None:
        return source
    return validate(response)


def parse_generated_response(response: str, *, lean_spec: LeanSpec | None) -> LeanCandidate:
    """Small indirection used by tests and the pipeline."""
    return parse_candidate(response, model_generates_spec=lean_spec is None)
