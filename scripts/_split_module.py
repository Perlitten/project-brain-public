"""Split a flat module into a package, moving top-level defs verbatim.

Usage: py -3 scripts/_split_module.py <module.py> '<json spec>'

Spec maps submodule name -> {"defs": [...], "consts": [...], "doc": "..."}.
The script computes which of the original imports each submodule needs and
adds cross-imports between submodules. Writes <pkg>/__init__.py re-exporting
every original top-level name (with `as` aliases to satisfy F401), plus an
ordered `from . import <sub>` block when the spec has "eager": true (e.g.
router modules where decorators register on import).
"""
import ast
import json
import sys
from pathlib import Path


def bound_names(imp: ast.stmt) -> dict[str, ast.stmt]:
    out = {}
    names = imp.names if isinstance(imp, (ast.Import, ast.ImportFrom)) else []
    for a in names:
        name = a.asname or a.name.split(".")[0]
        out[name] = imp
    return out


def names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def main() -> None:
    src_path = Path(sys.argv[1])
    spec = json.loads(sys.argv[2])
    eager = spec.pop("__eager__", False)
    src = src_path.read_text(encoding="utf-8")
    mod = ast.parse(src)
    lines = src.splitlines()

    docstring = ast.get_docstring(mod, clean=False) or ""
    imports: list[ast.stmt] = []
    import_bind: dict[str, ast.stmt] = {}
    stmts: dict[str, ast.stmt] = {}
    stmt_order: list[str] = []

    for i, node in enumerate(mod.body):
        if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
            import_bind.update({k: node for k, node in bound_names(node).items() if k not in import_bind})
            continue
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for nm in names:
            stmts[nm] = node
            stmt_order.append(nm)

    owner = {}
    for sub, grp in spec.items():
        for nm in grp.get("defs", []) + grp.get("consts", []):
            owner[nm] = sub

    missing = [nm for nm in stmt_order if nm not in owner and nm != "__all__"]
    if missing:
        print("UNASSIGNED:", missing)

    pkg_dir = src_path.parent / src_path.stem
    pkg_dir.mkdir(exist_ok=True)

    def block_of(node: ast.stmt) -> str:
        start = node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([start] + [d.lineno for d in node.decorator_list])
        while start > 1 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        return "\n".join(lines[start - 1:node.end_lineno])

    for sub, grp in spec.items():
        members = grp.get("defs", []) + grp.get("consts", [])
        needed: set[str] = set()
        for nm in members:
            node = stmts[nm]
            needed |= names_in(node)
            needed -= {nm}
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                needed -= {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
        needed -= set(members)

        imp_lines = []
        for imp in imports:
            used = [a for a in imp.names if (a.asname or a.name.split(".")[0]) in needed]
            if not used:
                continue
            if isinstance(imp, ast.ImportFrom):
                dots = "." * imp.level + (imp.module or "")
                names_str = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in used)
                line = f"from {dots} import {names_str}"
                if len(line) > 118:
                    line = f"from {dots} import ({names_str})"
                imp_lines.append(line)
            else:
                imp_lines.append("import " + ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "") for a in used))

        cross = {}
        for nm in sorted(needed):
            if nm in owner and owner[nm] != sub:
                cross.setdefault(owner[nm], []).append(nm)
        for other in sorted(cross):
            names_str = ", ".join(sorted(cross[other]))
            line = f"from .{other} import {names_str}"
            if len(line) > 118:
                line = f"from .{other} import ({names_str})"
            imp_lines.append(line)

        body_parts = [block_of(stmts[nm]) for nm in stmt_order if nm in members]
        doc = grp.get("doc", "")
        out = f'"""{doc}"""\nfrom __future__ import annotations\n\n'
        if imp_lines:
            out += "\n".join(imp_lines) + "\n"
        out += "\n\n" + "\n\n\n".join(body_parts) + "\n"
        (pkg_dir / f"{sub}.py").write_text(out, encoding="utf-8")
        print(f"{sub}.py: {len(body_parts)} blocks, {len(imp_lines)} import lines")

    init = f'"""{docstring}"""\nfrom __future__ import annotations\n\n'
    if eager:
        subs = ", ".join(sub for sub in spec)
        init += f"from . import {subs}\n\n"
    for sub in spec:
        members = [nm for nm in stmt_order if owner.get(nm) == sub]
        if not members:
            continue
        names_str = ", ".join(m + " as " + m for m in members)
        line = f"from .{sub} import ({names_str})"
        init += line + "\n"
    if "__all__" in stmts:
        init += "\n" + ast.get_source_segment(src, stmts["__all__"]) + "\n"
    (pkg_dir / "__init__.py").write_text(init, encoding="utf-8")
    print("__init__.py written")


if __name__ == "__main__":
    main()
