# Scout inventory notes

Reviewed the package module by module and kept the output ordered by module
name and source position. The inventory includes public classes along with
public functions and class members, while omitting constructors and other
underscore-prefixed implementation details.

For member classification, declarations under a class were recorded as methods
unless they use the standard property decorator. Imports, enum values,
assignments, and nested definitions were intentionally left out because they
are not declarations in this inventory.

The final JSON is in `analysis.json`. It is ready to use as the structured
inventory for the task.
