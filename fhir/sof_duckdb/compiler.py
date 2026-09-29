"""Compile SQL on FHIR v2 ViewDefinitions into DuckDB SQL.

Strategy: every FHIRPath expression becomes a DuckDB expression of type JSON[]
(a FHIRPath collection). A select node becomes a JSON[] of row objects, built
with list functions and lambdas: forEach maps over a collection, unionAll
concatenates lists, and nested selects are combined with a cartesian product.
The final query unnests the rows of each resource and projects the columns.

Everything is a scalar expression over the resource, so no joins are needed
(DuckDB does not allow subqueries inside lambdas).

Not supported: lowBoundary()/highBoundary() and FHIRPath functions outside the
set used by the specification's shareable tests. `repeat` is unrolled to a
fixed depth (REPEAT_DEPTH, 10 levels).
"""

import itertools
import json
import re
from dataclasses import dataclass, field, replace

from .fhirpath import FHIRPathError, parse

REPEAT_DEPTH = 10
EMPTY = "[]::JSON[]"
NUMERIC = "('BIGINT', 'UBIGINT', 'HUGEINT', 'DOUBLE')"
COLUMN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# FHIR primitive types -> DuckDB projection for typed output (dbt models).
TEXT_TYPES = {"string", "code", "id", "uri", "url", "canonical", "oid", "uuid", "markdown",
              "base64Binary", "date", "dateTime", "instant", "time"}
INTEGER_TYPES = {"integer", "positiveInt", "unsignedInt", "integer64"}
PRIMITIVE_JSON_TYPES = {
    "string": "('VARCHAR')", "code": "('VARCHAR')", "id": "('VARCHAR')", "uri": "('VARCHAR')",
    "url": "('VARCHAR')", "canonical": "('VARCHAR')", "date": "('VARCHAR')",
    "dateTime": "('VARCHAR')", "instant": "('VARCHAR')", "time": "('VARCHAR')",
    "boolean": "('BOOLEAN')", "integer": "('BIGINT', 'UBIGINT')",
    "decimal": "('BIGINT', 'UBIGINT', 'DOUBLE')",
}


class ViewDefinitionError(ValueError):
    pass


_counter = itertools.count()


def fresh(prefix: str = "v") -> str:
    return f"_{prefix}{next(_counter)}"


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def json_literal(value) -> str:
    return f"{sql_string(json.dumps(value))}::JSON"


def bind(sql: str, body) -> str:
    """Evaluate `sql` once and use it through a variable: list_transform([x], lambda v: ...)[1]."""
    var = fresh("b")
    return f"list_transform([{sql}], lambda {var}: {body(var)})[1]"


def items(value: str) -> str:
    """A JSON value (possibly an array or missing) as a FHIRPath collection."""
    return (f"(CASE WHEN {value} IS NULL OR json_type({value}) = 'NULL' THEN {EMPTY} "
            f"WHEN json_type({value}) = 'ARRAY' THEN CAST({value} AS JSON[]) ELSE [{value}] END)")


def nav(coll: str, name: str) -> str:
    x = fresh("x")
    path = "$." + json.dumps(name)
    return f"flatten(list_transform({coll}, lambda {x}: {items(f'json_extract({x}, {sql_string(path)})')}))"


def to_bool(coll: str) -> str:
    """Singleton collection -> SQL BOOLEAN (NULL when empty)."""
    return f"(CASE WHEN len({coll}) = 0 THEN NULL ELSE CAST({coll}[1] AS VARCHAR) = 'true' END)"


def bool_result(bool_sql: str) -> str:
    return bind(bool_sql, lambda b: f"(CASE WHEN {b} IS NULL THEN {EMPTY} ELSE [to_json({b})] END)")


def scalar_text(item: str) -> str:
    return f"json_extract_string({item}, '$')"


@dataclass
class Ctx:
    this: str                       # SQL variable holding the current item (JSON)
    resource: str                   # resource type, e.g. 'Patient'
    constants: dict = field(default_factory=dict)
    row_index: str = "0"


def compile_expr(node, ctx: Ctx) -> str:
    kind = node[0]
    if kind == "literal":
        return f"[{json_literal(node[1])}]"
    if kind == "empty":
        return EMPTY
    if kind == "this":
        return f"[{ctx.this}]"
    if kind == "var":
        name = node[1]
        if name == "rowIndex":
            return f"[to_json({ctx.row_index})]"
        if name not in ctx.constants:
            raise ViewDefinitionError(f"Unknown constant %{name}")
        return f"[{ctx.constants[name]}]"
    if kind == "member":
        _, input_node, name = node
        if input_node is None:
            if name == ctx.resource:          # 'Patient.name' at the root of a path
                return f"[{ctx.this}]"
            return nav(f"[{ctx.this}]", name)
        return nav(compile_expr(input_node, ctx), name)
    if kind == "index":
        _, input_node, index_node = node
        coll = compile_expr(input_node, ctx)
        idx = compile_expr(index_node, ctx)
        return bind(idx, lambda i: bind(coll, lambda c: (
            f"(CASE WHEN len({i}) = 0 THEN {EMPTY} "
            f"ELSE list_slice({c}, CAST({i}[1] AS INTEGER) + 1, CAST({i}[1] AS INTEGER) + 1) END)")))
    if kind == "binary":
        return compile_binary(node, ctx)
    if kind == "call":
        return compile_call(node, ctx)
    raise ViewDefinitionError(f"Unsupported FHIRPath node {kind}")


def compile_binary(node, ctx: Ctx) -> str:
    _, op, left, right = node
    a_sql, b_sql = compile_expr(left, ctx), compile_expr(right, ctx)

    def both(body):
        return bind(a_sql, lambda a: bind(b_sql, lambda b: body(a, b)))

    if op in ("and", "or", "xor"):
        sql_op = {"and": "AND", "or": "OR", "xor": "<>"}[op]
        return both(lambda a, b: bool_result(f"({to_bool(a)} {sql_op} {to_bool(b)})"))

    if op in ("=", "!="):
        def eq(a, b):
            norm = lambda coll: (lambda i: f"list_transform({coll}, lambda {i}: CASE WHEN json_type({i}) IN {NUMERIC} "
                                           f"THEN CAST(CAST({i} AS DOUBLE) AS VARCHAR) ELSE CAST({i} AS VARCHAR) END)")(fresh("n"))
            result = (f"(CASE WHEN len({a}) = 0 OR len({b}) = 0 THEN NULL "
                      f"WHEN len({a}) <> len({b}) THEN false ELSE {norm(a)} = {norm(b)} END)")
            return bool_result(result if op == "=" else f"(NOT {result})")
        return both(eq)

    if op in ("<", "<=", ">", ">="):
        return both(lambda a, b: bool_result(
            f"(CASE WHEN len({a}) = 0 OR len({b}) = 0 THEN NULL "
            f"WHEN json_type({a}[1]) IN {NUMERIC} AND json_type({b}[1]) IN {NUMERIC} "
            f"THEN CAST({a}[1] AS DOUBLE) {op} CAST({b}[1] AS DOUBLE) "
            f"ELSE {scalar_text(f'{a}[1]')} {op} {scalar_text(f'{b}[1]')} END)"))

    if op in ("+", "-", "*", "/"):
        def arith(a, b):
            ints = f"json_type({a}[1]) IN ('BIGINT', 'UBIGINT') AND json_type({b}[1]) IN ('BIGINT', 'UBIGINT')"
            dec = f"CAST({a}[1] AS DECIMAL(38, 10)) {op} CAST({b}[1] AS DECIMAL(38, 10))"
            value = dec if op == "/" else (
                f"CASE WHEN {ints} THEN to_json(CAST({a}[1] AS BIGINT) {op} CAST({b}[1] AS BIGINT)) "
                f"ELSE to_json(({dec})::DOUBLE) END")
            if op == "/":
                value = f"to_json(({value})::DOUBLE)"
            return f"(CASE WHEN len({a}) = 0 OR len({b}) = 0 THEN {EMPTY} ELSE [{value}] END)"
        return both(arith)

    if op == "|":
        return f"list_distinct(list_concat({a_sql}, {b_sql}))"
    raise ViewDefinitionError(f"Unsupported operator {op}")


def type_name(arg) -> str:
    if arg[0] == "member" and arg[1] is None:
        return arg[2]
    raise ViewDefinitionError("ofType() expects a type name")


def compile_call(node, ctx: Ctx) -> str:
    _, input_node, name, args = node

    # ofType on a choice element: value.ofType(Quantity) -> valueQuantity
    if name == "ofType":
        t = type_name(args[0]) if len(args) == 1 else None
        if t is None:
            raise ViewDefinitionError("ofType() expects one argument")
        if input_node is not None and input_node[0] == "member":
            parent = input_node[1]
            parent_sql = f"[{ctx.this}]" if parent is None else compile_expr(parent, ctx)
            return nav(parent_sql, input_node[2] + t[0].upper() + t[1:])
        coll = compile_expr(input_node, ctx) if input_node else f"[{ctx.this}]"
        x = fresh("x")
        if t in PRIMITIVE_JSON_TYPES:
            return f"list_filter({coll}, lambda {x}: json_type({x}) IN {PRIMITIVE_JSON_TYPES[t]})"
        return f"list_filter({coll}, lambda {x}: json_extract_string({x}, '$.resourceType') = {sql_string(t)})"

    coll = compile_expr(input_node, ctx) if input_node is not None else f"[{ctx.this}]"

    if name == "where":
        if len(args) != 1:
            raise ViewDefinitionError("where() expects one argument")
        x = fresh("x")
        cond = compile_expr(args[0], replace(ctx, this=x))
        return f"list_filter({coll}, lambda {x}: coalesce({to_bool(cond)}, false))"
    if name == "exists":
        if args:
            coll = compile_call(("call", input_node, "where", args), ctx)
        return bool_result(f"(len({coll}) > 0)")
    if name == "empty":
        return bool_result(f"(len({coll}) = 0)")
    if name == "count":
        return f"[to_json(len({coll}))]"
    if name == "first":
        return f"list_slice({coll}, 1, 1)"
    if name == "last":
        return f"list_slice({coll}, -1, -1)"
    if name == "not":
        return bind(coll, lambda c: bool_result(f"(NOT {to_bool(c)})"))
    if name == "extension":
        if len(args) != 1 or args[0][0] != "literal":
            raise ViewDefinitionError("extension() expects a URL string")
        x = fresh("x")
        return (f"list_filter({nav(coll, 'extension')}, "
                f"lambda {x}: json_extract_string({x}, '$.url') = {sql_string(args[0][1])})")
    if name == "join":
        sep = args[0][1] if args and args[0][0] == "literal" else ""
        x = fresh("x")
        return bind(coll, lambda c: (
            f"(CASE WHEN len({c}) = 0 THEN {EMPTY} ELSE [to_json(array_to_string("
            f"list_transform({c}, lambda {x}: {scalar_text(x)}), {sql_string(sep)}))] END)"))
    if name == "getResourceKey":
        x = fresh("x")
        return f"list_transform({coll}, lambda {x}: to_json(json_extract_string({x}, '$.id')))"
    if name == "getReferenceKey":
        x = fresh("x")
        ref = f"json_extract_string({x}, '$.reference')"
        keep = f"{ref} IS NOT NULL"
        if args:
            keep += f" AND regexp_matches({ref}, {sql_string('(^|/)' + type_name(args[0]) + '/[^/]+$')})"
        y = fresh("x")
        return (f"list_transform(list_filter({coll}, lambda {x}: {keep}), "
                f"lambda {y}: to_json(regexp_replace(json_extract_string({y}, '$.reference'), '^.*[/:]', '')))")
    raise ViewDefinitionError(f"Unsupported FHIRPath function {name}()")


def fhirpath(text, ctx: Ctx) -> str:
    try:
        return compile_expr(parse(text), ctx)
    except FHIRPathError as exc:
        raise ViewDefinitionError(str(exc)) from exc


# --- ViewDefinition structure ---------------------------------------------------------------

def column_names(node) -> list[str]:
    names = [c["name"] for c in node.get("column", [])]
    for child in node.get("select", []):
        names += column_names(child)
    union = node.get("unionAll", [])
    if union:
        branches = [column_names(b) for b in union]
        if any(b != branches[0] for b in branches):
            raise ViewDefinitionError("unionAll branches must define the same columns in the same order")
        names += branches[0]
    return names


def validate_node(node) -> None:
    if not isinstance(node, dict):
        raise ViewDefinitionError("select must be an object")
    for key in ("forEach", "forEachOrNull"):
        if key in node and not isinstance(node[key], str):
            raise ViewDefinitionError(f"{key} must be a FHIRPath string")
    if "forEach" in node and "forEachOrNull" in node:
        raise ViewDefinitionError("forEach and forEachOrNull are mutually exclusive")
    for col in node.get("column", []):
        if not isinstance(col.get("path"), str) or not COLUMN_NAME_RE.match(str(col.get("name", ""))):
            raise ViewDefinitionError(f"Invalid column {col!r}")
    for child in node.get("select", []) + node.get("unionAll", []):
        validate_node(child)


def column_value(col, coll: str) -> str:
    name = col["name"]
    if col.get("collection"):
        return f"to_json({coll})"
    return bind(coll, lambda c: (
        f"(CASE WHEN len({c}) > 1 THEN error({sql_string(f'Column {name} returned more than one value')}) "
        f"WHEN len({c}) = 0 THEN NULL::JSON ELSE {c}[1] END)"))


def cross(a: str, b: str) -> str:
    x, y = fresh("r"), fresh("r")
    return f"flatten(list_transform({a}, lambda {x}: list_transform({b}, lambda {y}: json_merge_patch({x}, {y}))))"


def body_rows(node, ctx: Ctx) -> str:
    parts = []
    if node.get("column"):
        pairs = ", ".join(f"{sql_string(c['name'])}, {column_value(c, fhirpath(c['path'], ctx))}"
                          for c in node["column"])
        parts.append(f"[json_object({pairs})]")
    for child in node.get("select", []):
        parts.append(select_rows(child, ctx))
    if node.get("unionAll"):
        parts.append("flatten([" + ", ".join(select_rows(b, ctx) for b in node["unionAll"]) + "])")
    if not parts:
        return "['{}'::JSON]"
    result = parts[0]
    for part in parts[1:]:
        result = cross(result, part)
    return result


def repeat_closure(paths: list[str], ctx: Ctx) -> str:
    """Nodes reached by applying the paths repeatedly, in depth-first pre-order
    (each node, then its descendants). Unrolled to REPEAT_DEPTH levels."""
    def expand(node: str, depth: int) -> str:
        if depth == 0:
            return EMPTY
        inner = replace(ctx, this=node)
        children = "flatten([" + ", ".join(fhirpath(p, inner) for p in paths) + "])"
        c = fresh("c")
        return f"flatten(list_transform({children}, lambda {c}: list_concat([{c}], {expand(c, depth - 1)})))"

    return expand(ctx.this, REPEAT_DEPTH)


def select_rows(node, ctx: Ctx) -> str:
    if "repeat" in node:
        foci = repeat_closure(node["repeat"], ctx)
        or_null = False
    elif "forEach" in node or "forEachOrNull" in node:
        foci = fhirpath(node.get("forEach") or node.get("forEachOrNull"), ctx)
        or_null = "forEachOrNull" in node
    else:
        return body_rows(node, ctx)
    f, i = fresh("f"), fresh("i")
    body = body_rows(node, replace(ctx, this=f, row_index=f"({i} - 1)"))
    if or_null:
        # No items: evaluate the select once on a null focus (columns become null, %rowIndex 0).
        foci = bind(foci, lambda c: f"(CASE WHEN len({c}) = 0 THEN [NULL::JSON] ELSE {c} END)")
    return f"flatten(list_transform({foci}, lambda {f}, {i}: {body}))"


def constant_values(view) -> dict:
    constants = {}
    for const in view.get("constant", []):
        values = [v for k, v in const.items() if k.startswith("value")]
        if "name" not in const or len(values) != 1:
            raise ViewDefinitionError(f"Invalid constant {const!r}")
        constants[const["name"]] = json_literal(values[0])
    return constants


def output_column(col_name: str, col: dict, typed: bool) -> str:
    extract = f"json_extract(_row, {sql_string('$.' + json.dumps(col_name))})"
    quoted = '"' + col_name + '"'
    if not typed or col.get("collection") or "type" not in col:
        return f"{extract} AS {quoted}"
    t = col["type"]
    text = f"json_extract_string(_row, {sql_string('$.' + json.dumps(col_name))})"
    if t in TEXT_TYPES:
        return f"{text} AS {quoted}"
    if t == "boolean":
        return f"CAST({text} AS BOOLEAN) AS {quoted}"
    if t in INTEGER_TYPES:
        return f"CAST({text} AS BIGINT) AS {quoted}"
    if t == "decimal":
        return f"CAST({text} AS DOUBLE) AS {quoted}"
    return f"{extract} AS {quoted}"


def all_columns(node) -> dict:
    cols = {c["name"]: c for c in node.get("column", [])}
    for child in node.get("select", []) + node.get("unionAll", [])[:1]:
        cols.update(all_columns(child))
    return cols


def compile_view(view: dict, relation: str, resource_column: str = "resource", typed: bool = True) -> str:
    """Compile a ViewDefinition into a SELECT over `relation` (one JSON resource per row)."""
    global _counter
    _counter = itertools.count()      # deterministic variable names for the same input
    if not isinstance(view, dict) or not isinstance(view.get("resource"), str):
        raise ViewDefinitionError("ViewDefinition must name a resource")
    top = {"select": view.get("select", [])}
    if not top["select"]:
        raise ViewDefinitionError("ViewDefinition must have at least one select")
    validate_node(top)
    names = column_names(top)
    if len(names) != len(set(names)):
        raise ViewDefinitionError(f"Duplicate column names in {names}")

    ctx = Ctx(this=f"_r.{resource_column}", resource=view["resource"], constants=constant_values(view))
    filters = [f"json_extract_string(_r.{resource_column}, '$.resourceType') = {sql_string(view['resource'])}"]
    for w in view.get("where", []):
        cond = fhirpath(w.get("path"), ctx)
        filters.append(bind(cond, lambda c: (
            f"(CASE WHEN len({c}) = 0 THEN false WHEN json_type({c}[1]) = 'BOOLEAN' "
            f"THEN CAST({c}[1] AS VARCHAR) = 'true' ELSE error('where path must evaluate to a boolean') END)")))

    cols = all_columns(top)
    projection = ",\n    ".join(output_column(n, cols[n], typed) for n in names)
    return (f"SELECT\n    {projection}\nFROM (\n"
            f"    SELECT unnest({select_rows(top, ctx)}) AS _row\n"
            f"    FROM {relation} AS _r\n"
            f"    WHERE {' AND '.join(filters)}\n)")
