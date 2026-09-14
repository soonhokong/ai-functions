"""Parse, assemble, and audit generated Lean source."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

from ._types import FunctionShape
from .errors import LeanVerificationError, VerifiedCompileConfigurationError

_SECTION_TEMPLATE = r"--\s*BEGIN\s+AI_FUNCTIONS_{name}\s*\n(.*?)--\s*END\s+AI_FUNCTIONS_{name}"
_FENCED_LEAN = re.compile(r"```(?:lean)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_FORBIDDEN = (
    (re.compile(r"\bsorry\b"), "`sorry`"),
    (re.compile(r"\badmit\b"), "`admit`"),
    (re.compile(r"\baxiom\b"), "an `axiom` declaration"),
    (re.compile(r"\bopaque\b"), "an `opaque` declaration"),
    (re.compile(r"\bpartial\b"), "a `partial` definition"),
    (re.compile(r"\bunsafe\b"), "an `unsafe` declaration"),
    (re.compile(r"\bnative_decide\b"), "`native_decide`"),
    (re.compile(r"\brun_tac\b"), "`run_tac`"),
    (re.compile(r"\bLean\.addDecl\b"), "`Lean.addDecl`"),
    (re.compile(r"\bimplemented_by\b"), "`implemented_by`"),
    (re.compile(r"\bextern\b"), "an `extern` compiler override"),
    (re.compile(r"@\[[^\]]*\bexport\b"), "an `export` compiler attribute"),
    (re.compile(r"\b_root_\b"), "a root-namespace escape"),
    (
        re.compile(r"^\s*(?:@\[[^\]\n]*\]\s*)*(?:builtin_)?initialize\b", re.MULTILINE),
        "a module initializer",
    ),
    (re.compile(r"^\s*#", re.MULTILINE), "a command-time `#` directive"),
    (re.compile(r"\binclude_(?:str|bytes)\b"), "a compile-time file include"),
    (re.compile(r"^\s*(?:import|namespace|section|end|export)\b", re.MULTILINE), "a scope/import command"),
    (
        re.compile(r"\b(?:macro|macro_rules|syntax|syntax_cat|elab|elab_rules)\b"),
        "custom syntax or elaboration code",
    ),
)


def _model_implementation_name(shape: FunctionShape) -> str:
    """Choose a fixed internal name distinct from the exported wrapper."""
    preferred = "aiFunctionsImplementation"
    if shape.lean_name != preferred:
        return preferred
    return "aiFunctionsGeneratedImplementation"


@dataclass(frozen=True)
class LeanSpec:
    """A user-reviewed Lean post-condition.

    ``proposition`` is inserted after a local ``result`` binding, so it can
    refer to ``result`` and to the decorated function's Lean parameter names.
    ``prelude`` may contain helper definitions used by the proposition.
    """

    proposition: str
    prelude: str = ""

    def __post_init__(self) -> None:
        if not self.proposition.strip():
            raise ValueError("LeanSpec.proposition must not be empty")
        _reject_unsafe_source(self.prelude, "LeanSpec.prelude")
        _reject_unsafe_source(self.proposition, "LeanSpec.proposition")


@dataclass(frozen=True)
class LeanCandidate:
    """Model-produced implementation, formalization, and proof body."""

    implementation: str
    proof: str
    spec_prelude: str = ""
    proposition: str = ""


@dataclass(frozen=True)
class RenderedLeanSource:
    """Complete implementation and verification modules."""

    module_name: str
    proof_module_name: str
    namespace: str
    theorem_name: str
    export_name: str
    implementation: str
    verification: str
    candidate: LeanCandidate


def _mask_lean_comments_and_strings(source: str) -> str:
    """Replace comments and string contents with spaces while preserving lines."""
    masked = list(source)
    index = 0
    block_depth = 0
    in_string = False

    while index < len(source):
        if block_depth:
            if source.startswith("/-", index):
                masked[index : index + 2] = "  "
                block_depth += 1
                index += 2
            elif source.startswith("-/", index):
                masked[index : index + 2] = "  "
                block_depth -= 1
                index += 2
            else:
                if source[index] != "\n":
                    masked[index] = " "
                index += 1
            continue

        if in_string:
            if source[index] == "\\" and index + 1 < len(source):
                if source[index] != "\n":
                    masked[index] = " "
                if source[index + 1] != "\n":
                    masked[index + 1] = " "
                index += 2
            else:
                if source[index] == '"':
                    in_string = False
                if source[index] != "\n":
                    masked[index] = " "
                index += 1
            continue

        if source.startswith("--", index):
            while index < len(source) and source[index] != "\n":
                masked[index] = " "
                index += 1
            continue
        if source.startswith("/-", index):
            masked[index : index + 2] = "  "
            block_depth = 1
            index += 2
            continue
        if source[index] == '"':
            masked[index] = " "
            in_string = True
        index += 1

    return "".join(masked)


def _reject_unsafe_source(source: str, label: str) -> None:
    source_without_comments = _mask_lean_comments_and_strings(source)
    for pattern, description in _FORBIDDEN:
        if pattern.search(source_without_comments):
            raise LeanVerificationError(f"{label} contains {description}, which is outside the verified trust policy")


def _extract_section(
    response: str,
    name: str,
    *,
    required: bool = True,
    allow_empty: bool = False,
) -> str:
    pattern = re.compile(_SECTION_TEMPLATE.format(name=re.escape(name)), re.DOTALL | re.IGNORECASE)
    match = pattern.search(response)
    if match is None:
        if required:
            raise LeanVerificationError(
                f"Missing `-- BEGIN AI_FUNCTIONS_{name}` / `-- END AI_FUNCTIONS_{name}` section",
            )
        return ""
    value = match.group(1).strip()
    if required and not allow_empty and not value:
        raise LeanVerificationError(f"AI_FUNCTIONS_{name} section must not be empty")
    return value


def parse_candidate(response: str, *, model_generates_spec: bool) -> LeanCandidate:
    """Parse the model's marker-delimited response."""
    fenced = _FENCED_LEAN.search(response)
    if fenced is not None:
        response = fenced.group(1)

    implementation = _extract_section(response, "IMPLEMENTATION")
    proof = _extract_section(response, "PROOF")
    spec_prelude = _extract_section(
        response,
        "SPECIFICATION",
        required=model_generates_spec,
        allow_empty=True,
    )
    proposition = _extract_section(response, "POSTCONDITION", required=model_generates_spec)

    for label, source in (
        ("implementation", implementation),
        ("proof", proof),
        ("specification", spec_prelude),
        ("post-condition", proposition),
    ):
        _reject_unsafe_source(source, f"Generated {label}")

    if re.match(r"^\s*by\b", proof):
        proof = re.sub(r"^\s*by\b", "", proof, count=1).lstrip()
        if not proof:
            raise LeanVerificationError("AI_FUNCTIONS_PROOF must contain the proof body, without an outer `by`")

    return LeanCandidate(
        implementation=implementation,
        proof=proof,
        spec_prelude=spec_prelude,
        proposition=proposition,
    )


def _audit_source(namespace: str) -> str:
    """Build a Lean command that rejects non-foundational axioms and opaques."""
    return f"""\
namespace AIVerifiedCompileAudit

open Lean Elab Command

def allowedAxioms : List Name := [``propext, ``Classical.choice, ``Quot.sound]

def offendersOf (n : Name) : CoreM (Array Name) := do
  let axioms ← collectAxioms n
  return axioms.filter fun axiomName => !allowedAxioms.contains axiomName

def opaquesOf (root : Name) : CoreM (Array Name) := do
  let env ← getEnv
  let mut found : Array Name := #[]
  for (name, info) in env.constants.toList do
    unless root.isPrefixOf name do continue
    if name.isInternal then continue
    if let .opaqueInfo _ := info then found := found.push name
  return found

elab "#auditAxioms " ns:ident : command => do
  let root := ns.getId
  let env ← getEnv
  let mut findings : Array (Name × Array Name) := #[]
  for (name, info) in env.constants.toList do
    unless root.isPrefixOf name do continue
    if name.isInternal then continue
    match info with
    | .thmInfo _ | .defnInfo _ =>
        let offenders ← liftCoreM (offendersOf name)
        if !offenders.isEmpty then findings := findings.push (name, offenders)
    | _ => pure ()
  let opaques ← liftCoreM (opaquesOf root)
  unless opaques.isEmpty do
    throwError m!"verified compilation rejected opaque declarations: {{opaques.toList}}"
  unless findings.isEmpty do
    throwError m!"verified compilation found non-foundational axioms: {{findings.toList}}"

end AIVerifiedCompileAudit

open AIVerifiedCompileAudit
#auditAxioms {namespace}
"""


def render_sources(
    candidate: LeanCandidate,
    *,
    shape: FunctionShape,
    build_id: str,
    export_name: str,
    lean_spec: LeanSpec | None,
    use_mathlib: bool,
) -> RenderedLeanSource:
    """Assemble fixed modules around a model-produced candidate."""
    suffix = build_id[:16]
    module_name = f"AIVerified{suffix}"
    proof_module_name = f"{module_name}Proof"
    namespace = module_name
    theorem_name = f"{shape.lean_name}_spec"
    model_implementation_name = _model_implementation_name(shape)

    expected_implementation = re.compile(
        rf"\bdef\s+{re.escape(model_implementation_name)}(?![A-Za-z0-9_])",
    )
    if expected_implementation.search(_mask_lean_comments_and_strings(candidate.implementation)) is None:
        raise LeanVerificationError(
            f"Implementation must define `def {model_implementation_name} ...`",
        )

    if lean_spec is None:
        spec_prelude = candidate.spec_prelude
        proposition = candidate.proposition
        if not proposition.strip():
            raise LeanVerificationError("Generated Lean post-condition must not be empty")
        try:
            validate_lean_spec_names(
                LeanSpec(proposition=proposition, prelude=spec_prelude),
                shape,
            )
        except VerifiedCompileConfigurationError as exc:
            raise LeanVerificationError(str(exc)) from exc
    else:
        spec_prelude = lean_spec.prelude.strip()
        proposition = lean_spec.proposition.strip()

    _reject_unsafe_source(spec_prelude, "Lean specification prelude")
    _reject_unsafe_source(proposition, "Lean post-condition")

    implementation_call = model_implementation_name
    if shape.lean_arguments:
        implementation_call += f" {shape.lean_arguments}"
    wrapper_binders = f" {shape.lean_parameters}" if shape.lean_parameters else ""
    implementation = f"""\
import Init

namespace {namespace}

{candidate.implementation.strip()}

@[export {export_name}]
def {shape.lean_name}{wrapper_binders} : {shape.return_lean_type} :=
  {implementation_call}

end {namespace}
"""

    imports = [f"import {module_name}", "import Lean"]
    if use_mathlib:
        imports.append("import Mathlib.Tactic")

    binders = f" {shape.lean_parameters}" if shape.lean_parameters else ""
    call = shape.lean_name
    if shape.lean_arguments:
        call += f" {shape.lean_arguments}"
    proof = textwrap.indent(candidate.proof.strip(), "  ")
    prelude = f"\n{spec_prelude}\n" if spec_prelude else ""
    verification = f"""\
{chr(10).join(imports)}

namespace {namespace}

{prelude}
#check ({shape.lean_name} : {shape.lean_function_type})

theorem {theorem_name}{binders} :
    let result : {shape.return_lean_type} := {call}
    {proposition} := by
{proof}

end {namespace}

{_audit_source(namespace)}
"""

    return RenderedLeanSource(
        module_name=module_name,
        proof_module_name=proof_module_name,
        namespace=namespace,
        theorem_name=f"{namespace}.{theorem_name}",
        export_name=export_name,
        implementation=implementation,
        verification=verification,
        candidate=candidate,
    )


def validate_lean_spec_names(spec: LeanSpec, shape: FunctionShape) -> None:
    """Catch the common mistake of using Python rather than mapped Lean names."""
    masked_proposition = _mask_lean_comments_and_strings(spec.proposition)
    if re.search(r"\bresult\b", masked_proposition) is None:
        raise VerifiedCompileConfigurationError(
            "LeanSpec.proposition must refer to the local `result` binding",
        )
    for parameter in shape.parameters:
        if parameter.python_name == parameter.lean_name:
            continue
        python_name = re.escape(parameter.python_name)
        proposition_without_lean_name = masked_proposition.replace(parameter.lean_name, "")
        if re.search(
            rf"(?<![A-Za-z0-9_]){python_name}(?![A-Za-z0-9_])",
            proposition_without_lean_name,
        ):
            raise VerifiedCompileConfigurationError(
                f"LeanSpec uses Python parameter name {parameter.python_name!r}; "
                f"use its Lean name {parameter.lean_name!r}",
            )
