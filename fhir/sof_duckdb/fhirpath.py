"""Parser for the subset of FHIRPath used by SQL on FHIR v2 ViewDefinitions.

Produces a small AST (tuples) that compiler.py turns into DuckDB SQL:

    ("literal", python_value)          'text', 1, 2.5, true, false
    ("empty",)                         {}
    ("this",)                          $this
    ("var", name)                      %name  (constants and %rowIndex)
    ("member", input_ast, name)        input.name   (input is None at the start of a path)
    ("call", input_ast, name, [args])  input.name(args)
    ("index", input_ast, index_ast)    input[index]
    ("binary", op, left, right)        = != < <= > >= and or + - * / |
"""

import re

TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<string>'(?:[^'\\]|\\.)*')
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<var>%[A-Za-z_][A-Za-z0-9_]*|%`[^`]+`)
  | (?P<this>\$this)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*|`[^`]+`)
  | (?P<op><=|>=|!=|[=<>+\-*/|().,\[\]{}])
""", re.VERBOSE)

# Binding power of infix operators (higher binds tighter), per the FHIRPath spec.
PRECEDENCE = {
    "or": 10, "xor": 10,
    "and": 20,
    "=": 30, "!=": 30,
    "<": 40, "<=": 40, ">": 40, ">=": 40,
    "|": 50,
    "+": 60, "-": 60,
    "*": 70, "/": 70,
}
KEYWORD_OPS = {"and", "or", "xor"}


class FHIRPathError(ValueError):
    pass


def tokenize(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise FHIRPathError(f"Unexpected character {text[pos]!r} at {pos} in {text!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        value = m.group()
        if kind == "ident" and value in KEYWORD_OPS | {"true", "false"}:
            kind = "op" if value in KEYWORD_OPS else "bool"
        tokens.append((kind, value))
    tokens.append(("eof", ""))
    return tokens


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, value: str):
        kind, tok = self.next()
        if tok != value:
            raise FHIRPathError(f"Expected {value!r}, got {tok!r} in {self.text!r}")

    def parse(self):
        node = self.expression(0)
        if self.peek()[0] != "eof":
            raise FHIRPathError(f"Unexpected {self.peek()[1]!r} in {self.text!r}")
        return node

    def expression(self, min_bp: int):
        left = self.postfix(self.primary())
        while True:
            kind, tok = self.peek()
            bp = PRECEDENCE.get(tok) if kind == "op" else None
            if bp is None or bp <= min_bp:
                return left
            self.next()
            left = ("binary", tok, left, self.expression(bp))

    def primary(self):
        kind, tok = self.next()
        if kind == "string":
            return ("literal", bytes(tok[1:-1], "utf-8").decode("unicode_escape"))
        if kind == "number":
            return ("literal", float(tok) if "." in tok else int(tok))
        if kind == "bool":
            return ("literal", tok == "true")
        if kind == "this":
            return ("this",)
        if kind == "var":
            return ("var", tok[1:].strip("`"))
        if kind == "ident":
            return self.invocation(None, tok)
        if tok == "(":
            node = self.expression(0)
            self.expect(")")
            return node
        if tok == "{":
            self.expect("}")
            return ("empty",)
        if tok == "-" and self.peek()[0] == "number":
            value = self.primary()[1]
            return ("literal", -value)
        raise FHIRPathError(f"Unexpected {tok!r} in {self.text!r}")

    def invocation(self, input_node, name: str):
        name = name.strip("`")
        if self.peek()[1] == "(":
            self.next()
            args = []
            if self.peek()[1] != ")":
                args.append(self.expression(0))
                while self.peek()[1] == ",":
                    self.next()
                    args.append(self.expression(0))
            self.expect(")")
            return ("call", input_node, name, args)
        return ("member", input_node, name)

    def postfix(self, node):
        while True:
            tok = self.peek()[1]
            if tok == ".":
                self.next()
                kind, name = self.next()
                if kind != "ident":
                    raise FHIRPathError(f"Expected a name after '.', got {name!r} in {self.text!r}")
                node = self.invocation(node, name)
            elif tok == "[":
                self.next()
                index = self.expression(0)
                self.expect("]")
                node = ("index", node, index)
            else:
                return node


def parse(text: str):
    if not isinstance(text, str) or not text.strip():
        raise FHIRPathError(f"FHIRPath expression must be a non-empty string, got {text!r}")
    return Parser(text).parse()
