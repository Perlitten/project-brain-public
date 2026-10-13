"""Reproducible Python responsibility/coupling metrics (no runtime dependencies).

Usage: python scripts/architecture_metrics.py --root . --output metrics.json
Cyclomatic proxy: 1 + branches, loops, handlers, ternaries, comprehension
filters/loops and short-circuit operands. Nesting score: branches/loops/handlers
weighted by enclosing control depth, plus short-circuit operands. The latter
is a local cognitive-complexity proxy, not a certified Sonar score.
Dependency edges are explicit internal imports, including imports inside
functions; dynamic imports/call graphs are deliberately not inferred.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


class Complexity(ast.NodeVisitor):
    def __init__(self) -> None:
        self.cyclomatic = 1
        self.nesting = 0
        self.depth = 0

    def visit_FunctionDef(self, node):
        pass  # Nested functions are measured independently.

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def control(self, node):
        self.cyclomatic += 1
        self.nesting += 1 + self.depth
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_If = control
    visit_For = control
    visit_AsyncFor = control
    visit_While = control
    visit_ExceptHandler = control
    visit_IfExp = control

    def visit_BoolOp(self, node):
        self.cyclomatic += len(node.values) - 1
        self.nesting += len(node.values) - 1
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self.cyclomatic += 1 + len(node.ifs)
        self.nesting += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node):
        self.cyclomatic += max(0, len(node.cases) - 1)
        self.nesting += 1 + self.depth
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1


def functions(tree, prefix=""):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visitor = Complexity()
            for statement in node.body:
                visitor.visit(statement)
            yield {
                "name": prefix + node.name,
                "lines": node.end_lineno - node.lineno + 1,
                "cyclomatic_proxy": visitor.cyclomatic,
                "nesting_score": visitor.nesting,
            }
            yield from functions(node, prefix + node.name + ".")
        elif isinstance(node, ast.ClassDef):
            yield from functions(node, prefix + node.name + ".")


def measure(root: Path):
    modules = {}
    dependencies = {}
    for directory in ("brain", "apps"):
        for path in sorted((root / directory).rglob("*.py")):
            if any(part in {"node_modules", ".venv", ".next", "__pycache__"} for part in path.parts):
                continue
            relative = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            module = relative[:-3].replace("/", ".").removesuffix(".__init__")
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    imports.add(node.module)
            imports = {name for name in imports if name.startswith(("brain.", "apps."))}
            dependencies[module] = imports
            modules[module] = {"path": relative, "lines": len(source.splitlines()),
                               "functions": list(functions(tree)), "imports": sorted(imports)}
    for name, metrics in modules.items():
        metrics["fan_out"] = len(dependencies[name])
        metrics["fan_in"] = sum(name in targets for targets in dependencies.values())
    return {"method": __doc__, "modules": modules}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(measure(args.root), indent=2) + "\n", encoding="utf-8")
