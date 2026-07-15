# scout t3 — inventory of public declarations

## OBJECTIVE
Build a complete inventory of every public declaration in the provided
`depot/` package: classes, module-level functions, class methods, and
properties.

## CONTEXT
The depot fixture is a small Python package. A declaration is public when its
name does not start with `_`. This is a mechanical source inventory; do not
infer an API from imports, documentation, or runtime behavior.

## INPUTS
- The fixture only: `depot/*.py` in the root of the provided workspace.
- Consider every top-level Python module in that `depot/` directory, including
  modules whose inventory happens to be empty.

## OUTPUT
Return a JSON array, ordered first by file name (alphabetically) and then by
source line (ascending). Each object must contain exactly these five fields:

```json
{"file":"booking.py","line":67,"scope":"BookingEngine","name":"create_booking","kind":"method"}
```

- `file` is the basename of the source module.
- `line` is the 1-based line on which the `class` or `def` keyword appears.
- `scope` is `module` for a top-level declaration, or the immediately enclosing
  class name for a method or property.
- `name` is the declaration name exactly as written.
- `kind` is exactly one of `class`, `function`, `method`, or `property`.
  A function defined directly in a module is `function`; a function defined
  directly in a class is `method`; a class function bearing `@property` is
  `property`.

Include classes as well as callable declarations. Exclude every declaration
whose name begins with `_`, including `__init__`; do not recurse into functions
or methods to look for nested declarations.

Return only the JSON array as the complete final answer, with no prose before
or after it. If you can write files, you may also save the same array as
`answer.json` at the workspace root, but the final answer is authoritative.

## TOOLS
Read and search the fixture files. Choose the tools yourself.

## BOUNDARIES
- Use `depot/*.py` from the fixture and nothing else. Other materials may be
  present in the workspace; using anything except the fixture is out of bounds.
- Do not change fixture files. Creating `answer.json` at the workspace root is
  the only permitted write.
- Do not count imported names, assignments, enum members, decorators by
  themselves, nested definitions, or private declarations.

## ESCAPE HATCH
If the expected `depot/` directory or Python source files are absent, return
`NEEDS_CLARIFICATION: <what you found instead>` and nothing else.

## ACCEPTANCE
The array is checked against a deterministic key. Precision and recall must
each be at least 0.95, with exact agreement on `file`, `line`, `scope`, `name`,
and `kind` for every matched declaration.
