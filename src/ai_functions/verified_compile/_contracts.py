"""Deterministic translation from a small Python contract language to Lean.

The translation follows the same fail-closed approach as Strata-Python's
``SpecExpr``: every accepted AST node is type-checked against an explicit
semantics model before target logic is emitted. This module intentionally
implements only the subset needed by ``@ai_verified_compile`` instead of
depending on Strata's Laurel/Core/SMT pipeline.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import textwrap
import types
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, get_args, get_origin

from ._source import LeanSpec
from ._types import FunctionShape, _lean_identifier
from .errors import PythonContractError


class _ContractType(Enum):
    INT = "int"
    BOOL = "bool"
    ARRAY_INT = "list[int]"


@dataclass(frozen=True)
class _Term:
    """A typed Lean value and, for booleans, its proposition-level form."""

    type: _ContractType
    value: str
    proposition: str | None = None
    references: frozenset[str] = frozenset()

    def as_proposition(self) -> str:
        if self.type is not _ContractType.BOOL:
            raise AssertionError("only Boolean terms denote contract propositions")
        return self.proposition or f"({self.value}) = true"


@dataclass(frozen=True)
class _ContractSource:
    expression: ast.expr
    parameter_names: tuple[str, ...]


def _error(condition: Callable[..., Any], node: ast.AST | None, message: str) -> PythonContractError:
    location = ""
    if node is not None and hasattr(node, "lineno"):
        location = f" at line {node.lineno}, column {getattr(node, 'col_offset', 0) + 1}"
    name = getattr(condition, "__qualname__", getattr(condition, "__name__", "post_condition"))
    return PythonContractError(f"Unsupported Python contract {name!r}{location}: {message}")


def _extract_contract_source(condition: Callable[..., Any]) -> _ContractSource:
    if not isinstance(condition, types.FunctionType):
        raise _error(condition, None, "contracts must be ordinary Python functions")
    if inspect.iscoroutinefunction(condition) or inspect.isgeneratorfunction(condition):
        raise _error(condition, None, "contracts must be synchronous, non-generator functions")

    try:
        source = textwrap.dedent(inspect.getsource(condition))
    except (OSError, TypeError) as exc:
        raise _error(
            condition,
            None,
            "source is unavailable; define the contract in a Python module rather than an interactive shell",
        ) from exc

    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise _error(condition, None, f"could not parse its source: {exc.msg}") from exc

    function_defs = [
        statement
        for statement in module.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == condition.__name__
    ]
    if function_defs:
        function = function_defs[0]
        if isinstance(function, ast.AsyncFunctionDef):
            raise _error(condition, function, "async contracts are not supported")
        if function.decorator_list:
            raise _error(condition, function, "decorated contract functions are not supported")
        body = list(function.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                body.pop(0)
        if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
            raise _error(
                condition,
                function,
                "use a single side-effect-free `return <boolean expression>` statement",
            )
        parameters = [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ]
        if function.args.vararg is not None or function.args.kwarg is not None:
            raise _error(condition, function, "*args and **kwargs are not supported")
        return _ContractSource(body[0].value, tuple(parameter.arg for parameter in parameters))

    for statement in module.body:
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Lambda):
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.value, ast.Lambda):
            value = statement.value
        if isinstance(value, ast.Lambda):
            if value.args.vararg is not None or value.args.kwarg is not None:
                raise _error(condition, value, "*args and **kwargs are not supported")
            parameters = [
                *value.args.posonlyargs,
                *value.args.args,
                *value.args.kwonlyargs,
            ]
            return _ContractSource(value.body, tuple(parameter.arg for parameter in parameters))

    raise _error(
        condition,
        None,
        "expected a function with one return statement or a directly assigned lambda",
    )


def _annotation_type(annotation: object, *, condition: Callable[..., Any]) -> _ContractType:
    if annotation is int:
        return _ContractType.INT
    if annotation is bool:
        return _ContractType.BOOL
    if get_origin(annotation) is list and get_args(annotation) == (int,):
        return _ContractType.ARRAY_INT
    raise _error(
        condition,
        None,
        f"type {annotation!r} is outside the verified contract subset; use int, bool, or list[int]",
    )


def _shape_types(shape: FunctionShape, condition: Callable[..., Any]) -> dict[str, _ContractType]:
    result = {
        parameter.python_name: _annotation_type(parameter.python_type, condition=condition)
        for parameter in shape.parameters
    }
    result["$result"] = _annotation_type(shape.return_python_type, condition=condition)
    return result


def _literal_term(
    value: object,
    *,
    condition: Callable[..., Any],
    node: ast.AST | None,
) -> _Term:
    if type(value) is bool:
        rendered = "true" if value else "false"
        proposition = "True" if value else "False"
        return _Term(_ContractType.BOOL, rendered, proposition)
    if type(value) is int:
        rendered = str(value) if value >= 0 else f"({value})"
        return _Term(_ContractType.INT, rendered)
    raise _error(
        condition,
        node,
        "captured constants may only be int or bool",
    )


class _Translator:
    def __init__(
        self,
        condition: Callable[..., Any],
        *,
        shape: FunctionShape,
        source: _ContractSource,
    ) -> None:
        self.condition = condition
        self.shape = shape
        self.source = source
        self.environment: dict[str, _Term] = {}
        self.captured: dict[str, object] = {}

        signature = inspect.signature(condition)
        parameters = list(signature.parameters.values())
        if not parameters:
            raise _error(condition, None, "the first parameter must receive the compiled result")
        if len(parameters) != len(source.parameter_names):
            raise _error(condition, None, "the inspected signature does not match the parsed source")
        if any(
            parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for parameter in parameters
        ):
            raise _error(condition, None, "*args and **kwargs are not supported")
        if parameters[0].kind is inspect.Parameter.KEYWORD_ONLY:
            raise _error(condition, None, "the compiled-result parameter must be positional")

        shape_types = _shape_types(shape, condition)
        result_name = parameters[0].name
        self.environment[result_name] = _Term(
            shape_types["$result"],
            "result",
            references=frozenset({"result"}),
        )
        seen_inputs: set[str] = set()
        shape_by_name = {parameter.python_name: parameter for parameter in shape.parameters}
        for parameter in parameters[1:]:
            target = shape_by_name.get(parameter.name)
            if target is None:
                raise _error(
                    condition,
                    None,
                    f"parameter {parameter.name!r} is not an input of {shape.python_name!r}",
                )
            if parameter.name in seen_inputs:
                raise _error(condition, None, f"input parameter {parameter.name!r} is repeated")
            seen_inputs.add(parameter.name)
            self.environment[parameter.name] = _Term(
                shape_types[parameter.name],
                target.lean_name,
                references=frozenset({parameter.name}),
            )

        try:
            hints = typing.get_type_hints(condition)
        except Exception:
            hints = {}
        if "return" in hints and hints["return"] is not bool:
            raise _error(condition, None, "the return annotation must be bool")
        for parameter in parameters:
            annotation = hints.get(parameter.name)
            if annotation is None:
                continue
            actual = self.environment[parameter.name].type
            annotated = _annotation_type(annotation, condition=condition)
            if annotated is not actual:
                raise _error(
                    condition,
                    None,
                    f"parameter {parameter.name!r} is annotated as {annotation!r}, "
                    f"but the corresponding compiled value has type {actual.value}",
                )

        try:
            closure = inspect.getclosurevars(condition)
        except TypeError:
            closure = None
        if closure is not None:
            self.captured.update(closure.globals)
            self.captured.update(closure.nonlocals)
            if closure.unbound:
                names = ", ".join(sorted(closure.unbound))
                raise _error(condition, None, f"contains unresolved names: {names}")

    def translate(self) -> _Term:
        term = self._expression(self.source.expression)
        if term.type is not _ContractType.BOOL:
            raise _error(self.condition, self.source.expression, "the returned expression must have type bool")
        if "result" not in term.references:
            raise _error(
                self.condition,
                self.source.expression,
                "the contract must depend on its first (compiled-result) parameter",
            )
        return term

    def _expect(self, term: _Term, expected: _ContractType, node: ast.AST, description: str) -> _Term:
        if term.type is not expected:
            raise _error(
                self.condition,
                node,
                f"{description} requires {expected.value}, got {term.type.value}",
            )
        return term

    def _expression(self, node: ast.expr) -> _Term:
        if isinstance(node, ast.Name):
            if node.id in self.environment:
                return self.environment[node.id]
            if node.id in self.captured:
                return _literal_term(self.captured[node.id], condition=self.condition, node=node)
            raise _error(self.condition, node, f"name {node.id!r} is not a contract parameter or supported constant")

        if isinstance(node, ast.Constant):
            return _literal_term(node.value, condition=self.condition, node=node)

        if isinstance(node, ast.List):
            elements = [
                self._expect(self._expression(item), _ContractType.INT, item, "list literals") for item in node.elts
            ]
            if not elements:
                raise _error(self.condition, node, "empty list literals are ambiguous and not supported")
            references = frozenset().union(*(element.references for element in elements))
            return _Term(
                _ContractType.ARRAY_INT,
                f"#[{', '.join(element.value for element in elements)}]",
                references=references,
            )

        if isinstance(node, ast.UnaryOp):
            operand = self._expression(node.operand)
            if isinstance(node.op, ast.USub):
                self._expect(operand, _ContractType.INT, node, "unary minus")
                return _Term(_ContractType.INT, f"-({operand.value})", references=operand.references)
            if isinstance(node.op, ast.UAdd):
                self._expect(operand, _ContractType.INT, node, "unary plus")
                return operand
            if isinstance(node.op, ast.Not):
                self._expect(operand, _ContractType.BOOL, node, "`not`")
                return _Term(
                    _ContractType.BOOL,
                    f"!({operand.value})",
                    f"¬ ({operand.as_proposition()})",
                    operand.references,
                )
            raise _error(self.condition, node, f"unary operator {type(node.op).__name__} is not supported")

        if isinstance(node, ast.BinOp):
            left = self._expect(self._expression(node.left), _ContractType.INT, node.left, "arithmetic")
            right = self._expect(self._expression(node.right), _ContractType.INT, node.right, "arithmetic")
            operators: dict[type[ast.operator], str] = {
                ast.Add: "+",
                ast.Sub: "-",
                ast.Mult: "*",
            }
            operator = operators.get(type(node.op))
            if operator is None:
                raise _error(
                    self.condition,
                    node,
                    "only integer +, -, and * are supported; division, modulo, power, and bit operations "
                    "need explicit Python-semantics models",
                )
            return _Term(
                _ContractType.INT,
                f"({left.value} {operator} {right.value})",
                references=left.references | right.references,
            )

        if isinstance(node, ast.BoolOp):
            values = [
                self._expect(self._expression(value), _ContractType.BOOL, value, "boolean operators")
                for value in node.values
            ]
            if isinstance(node.op, ast.And):
                value_operator = " && "
                prop_operator = " ∧ "
            elif isinstance(node.op, ast.Or):
                value_operator = " || "
                prop_operator = " ∨ "
            else:
                raise _error(self.condition, node, f"boolean operator {type(node.op).__name__} is not supported")
            return _Term(
                _ContractType.BOOL,
                f"({value_operator.join(value.value for value in values)})",
                f"({prop_operator.join(value.as_proposition() for value in values)})",
                frozenset().union(*(value.references for value in values)),
            )

        if isinstance(node, ast.Compare):
            operands = [self._expression(node.left), *(self._expression(comparator) for comparator in node.comparators)]
            comparisons: list[_Term] = []
            for left, operator, right in zip(operands[:-1], node.ops, operands[1:], strict=True):
                comparisons.append(self._comparison(left, operator, right, node))
            if len(comparisons) == 1:
                return comparisons[0]
            return _Term(
                _ContractType.BOOL,
                f"({' && '.join(comparison.value for comparison in comparisons)})",
                f"({' ∧ '.join(comparison.as_proposition() for comparison in comparisons)})",
                frozenset().union(*(comparison.references for comparison in comparisons)),
            )

        if isinstance(node, ast.IfExp):
            condition = self._expect(
                self._expression(node.test),
                _ContractType.BOOL,
                node.test,
                "conditional expressions",
            )
            then_term = self._expression(node.body)
            else_term = self._expression(node.orelse)
            if then_term.type is not else_term.type:
                raise _error(
                    self.condition,
                    node,
                    f"conditional branches have different types: {then_term.type.value} and {else_term.type.value}",
                )
            references = condition.references | then_term.references | else_term.references
            value = f"(if {condition.value} then {then_term.value} else {else_term.value})"
            proposition = f"({value}) = true" if then_term.type is _ContractType.BOOL else None
            return _Term(then_term.type, value, proposition, references)

        if isinstance(node, ast.Call):
            return self._call(node)

        raise _error(
            self.condition,
            node,
            f"{type(node).__name__} is outside the pure contract subset",
        )

    def _comparison(
        self,
        left: _Term,
        operator: ast.cmpop,
        right: _Term,
        node: ast.Compare,
    ) -> _Term:
        references = left.references | right.references
        if isinstance(operator, (ast.Eq, ast.NotEq)):
            if left.type is not right.type:
                raise _error(
                    self.condition,
                    node,
                    f"equality compares {left.type.value} with {right.type.value}",
                )
            prop_operator = "=" if isinstance(operator, ast.Eq) else "≠"
            proposition = f"({left.value} {prop_operator} {right.value})"
            return _Term(
                _ContractType.BOOL,
                f"decide {proposition}",
                proposition,
                references,
            )

        if isinstance(operator, (ast.In, ast.NotIn)):
            if left.type is not _ContractType.INT or right.type is not _ContractType.ARRAY_INT:
                raise _error(self.condition, node, "`in` requires an int on the left and list[int] on the right")
            contains = f"({right.value}).contains ({left.value})"
            if isinstance(operator, ast.NotIn):
                return _Term(
                    _ContractType.BOOL,
                    f"!({contains})",
                    f"({contains}) = false",
                    references,
                )
            return _Term(_ContractType.BOOL, contains, f"({contains}) = true", references)

        self._expect(left, _ContractType.INT, node, "ordered comparisons")
        self._expect(right, _ContractType.INT, node, "ordered comparisons")
        operators: dict[type[ast.cmpop], str] = {
            ast.Lt: "<",
            ast.LtE: "<=",
            ast.Gt: ">",
            ast.GtE: ">=",
        }
        rendered = operators.get(type(operator))
        if rendered is None:
            raise _error(self.condition, node, f"comparison {type(operator).__name__} is not supported")
        proposition = f"({left.value} {rendered} {right.value})"
        return _Term(_ContractType.BOOL, f"decide {proposition}", proposition, references)

    def _call(self, node: ast.Call) -> _Term:
        if not isinstance(node.func, ast.Name):
            raise _error(self.condition, node, "method calls and attribute calls are not supported")
        name = node.func.id
        if node.keywords:
            raise _error(self.condition, node, "keyword arguments in contract calls are not supported")
        if name in self.environment or name in self.captured:
            raise _error(self.condition, node, f"calls to user value {name!r} are not supported")

        expected_builtin = {"len": builtins.len, "all": builtins.all, "any": builtins.any}.get(name)
        if expected_builtin is None:
            raise _error(self.condition, node, "only len(), all(), and any() calls are supported")

        if name == "len":
            if len(node.args) != 1:
                raise _error(self.condition, node, "len() requires exactly one argument")
            value = self._expect(self._expression(node.args[0]), _ContractType.ARRAY_INT, node, "len()")
            return _Term(
                _ContractType.INT,
                f"Int.ofNat ({value.value}).size",
                references=value.references,
            )

        if len(node.args) != 1 or not isinstance(node.args[0], ast.GeneratorExp):
            raise _error(self.condition, node, f"{name}() requires exactly one generator expression")
        return self._quantifier(node.args[0], universal=name == "all")

    def _quantifier(self, generator: ast.GeneratorExp, *, universal: bool) -> _Term:
        if len(generator.generators) != 1:
            raise _error(self.condition, generator, "nested or multiple-generator comprehensions are not supported")
        clause = generator.generators[0]
        if clause.is_async:
            raise _error(self.condition, generator, "async comprehensions are not supported")
        if not isinstance(clause.target, ast.Name):
            raise _error(self.condition, clause.target, "the generator target must be one simple name")
        collection = self._expect(
            self._expression(clause.iter),
            _ContractType.ARRAY_INT,
            clause.iter,
            "all()/any() generators",
        )
        binder = _lean_identifier(clause.target.id)
        previous = self.environment.get(clause.target.id)
        self.environment[clause.target.id] = _Term(
            _ContractType.INT,
            binder,
            references=frozenset({f"binder:{clause.target.id}"}),
        )
        try:
            predicate = self._expect(
                self._expression(generator.elt),
                _ContractType.BOOL,
                generator.elt,
                "all()/any() predicates",
            )
            guards = [
                self._expect(self._expression(guard), _ContractType.BOOL, guard, "generator filters")
                for guard in clause.ifs
            ]
        finally:
            if previous is None:
                self.environment.pop(clause.target.id, None)
            else:
                self.environment[clause.target.id] = previous

        predicate_value = predicate.value
        if guards:
            guard_value = " && ".join(guard.value for guard in guards)
            if universal:
                predicate_value = f"(!({guard_value}) || ({predicate_value}))"
            else:
                predicate_value = f"(({guard_value}) && ({predicate_value}))"
        operation = "all" if universal else "any"
        value = f"({collection.value}).{operation} (fun {binder} => {predicate_value})"
        references = collection.references | predicate.references
        for guard in guards:
            references |= guard.references
        return _Term(
            _ContractType.BOOL,
            value,
            f"({value}) = true",
            references,
        )


def translate_post_conditions(
    post_conditions: Sequence[Callable[..., Any]],
    *,
    shape: FunctionShape,
) -> LeanSpec:
    """Translate pure Python post-conditions into one fixed Lean proposition."""
    if not post_conditions:
        raise PythonContractError("at least one Python post-condition is required")
    propositions: list[str] = []
    for condition in post_conditions:
        source = _extract_contract_source(condition)
        term = _Translator(condition, shape=shape, source=source).translate()
        propositions.append(term.as_proposition())
    proposition = propositions[0]
    if len(propositions) > 1:
        proposition = " ∧\n    ".join(f"({item})" for item in propositions)
    return LeanSpec(proposition=proposition)
