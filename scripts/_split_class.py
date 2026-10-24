"""Split a pydantic-style class into per-domain field mixins.

Usage: py -3 scripts/_split_class.py <module.py> <ClassName> '<json>'

json: {"<sub>": {"doc": "...", "fields": [...], "methods": [...]}, ...,
       "__final_class__": {"fields": [...], "methods": [...]}}

Each member block is moved verbatim (comment run above it included).
Every <sub> becomes a module with `class <ClassName><SubTitle>:` mixin.
The "__final_class__" spec (optional) gets members for the assembled class.
Prints per-module line counts and any unassigned member names.
"""
import ast
import json
import sys
from pathlib import Path


def main() -> None:
    src_path = Path(sys.argv[1])
    cls_name = sys.argv[2]
    spec = json.loads(sys.argv[3])
    src = src_path.read_text(encoding="utf-8")
    lines = src.splitlines()
    mod = ast.parse(src)
    cls = next(n for n in mod.body if isinstance(n, ast.ClassDef) and n.name == cls_name)

    member_node = {}
    member_order = []
    for m in cls.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nm = m.name
        elif isinstance(m, ast.Assign):
            nm = m.targets[0].id if isinstance(m.targets[0], ast.Name) else None
        elif isinstance(m, ast.AnnAssign):
            nm = m.target.id if isinstance(m.target, ast.Name) else None
        else:
            nm = None
        if nm:
            member_node[nm] = m
            member_order.append(nm)

    owner = {}
    for sub, grp in spec.items():
        for nm in grp.get("fields", []) + grp.get("methods", []):
            owner[nm] = sub

    unassigned = [nm for nm in member_order if nm not in owner]
    if unassigned:
        print("UNASSIGNED:", unassigned)

    # imports used by members per module
    imports = [n for n in mod.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    bind = {}
    for imp in imports:
        for a in imp.names:
            bind.setdefault(a.asname or a.name.split(".")[0], imp)

    def block_of(node: ast.stmt) -> str:
        start = node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([start] + [d.lineno for d in node.decorator_list])
        while start > 1 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        return "\n".join(lines[start - 1:node.end_lineno])

    pkg_dir = src_path.parent / src_path.stem
    pkg_dir.mkdir(exist_ok=True)

    for sub, grp in spec.items():
        members = grp.get("fields", []) + grp.get("methods", [])
        needed = set()
        for nm in members:
            node = member_node[nm]
            needed |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}
        needed -= set(members)
        imp_lines = []
        for imp in imports:
            used = [a for a in imp.names if (a.asname or a.name.split(".")[0]) in needed]
            if not used:
                continue
            if isinstance(imp, ast.ImportFrom):
                dots = "." * imp.level + (imp.module or "")
                ns = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in used)
                imp_lines.append(f"from {dots} import ({ns})" if len(f"from {dots} import {ns}") > 118
                                 else f"from {dots} import {ns}")
            else:
                imp_lines.append("import " + ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "") for a in used))
        if sub == "__final_class__":
            continue
        title = "".join(p.title() for p in sub.lstrip("_").split("_"))
        body = [block_of(member_node[nm]) for nm in member_order if nm in members]
        out = f'"""{grp["doc"]}"""\nfrom __future__ import annotations\n\n'
        if imp_lines:
            out += "\n".join(imp_lines) + "\n"
        out += f"\n\nclass {cls_name}{title}:\n" + "\n\n".join(body) + "\n"
        (pkg_dir / f"{sub}.py").write_text(out, encoding="utf-8")
        print(f"{sub}.py: {len(body)} members -> class {cls_name}{title}")

    # final class file (_model.py) — keeps model_config + cross-domain validators
    fin = spec.get("__final_class__")
    if fin:
        members = fin.get("fields", []) + fin.get("methods", [])
        needed = set()
        for nm in members:
            needed |= {x.id for x in ast.walk(member_node[nm]) if isinstance(x, ast.Name)}
        needed -= set(members)
        imp_lines = []
        for imp in imports:
            used = [a for a in imp.names if (a.asname or a.name.split(".")[0]) in needed]
            if not used:
                continue
            if isinstance(imp, ast.ImportFrom):
                dots = "." * imp.level + (imp.module or "")
                ns = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in used)
                imp_lines.append(f"from {dots} import ({ns})" if len(f"from {dots} import {ns}") > 118
                                 else f"from {dots} import {ns}")
            else:
                imp_lines.append("import " + ", ".join(
                    a.name + (f" as {a.asname}" if a.asname else "") for a in used))
        mixins = [s for s in spec if s != "__final_class__"]
        mixin_names = [cls_name + "".join(p.title() for p in s.lstrip("_").split("_")) for s in mixins]
        for sub, mx in zip(mixins, mixin_names):
            imp_lines.append(f"from .{sub} import {mx}")
        body = [block_of(member_node[nm]) for nm in member_order if nm in members]
        out = '"""Assembled Settings class: domain field mixins plus cross-domain model validators."""\n'
        out += "from __future__ import annotations\n\n"
        if imp_lines:
            out += "\n".join(imp_lines) + "\n"
        out += f"\n\nclass {cls_name}(" + ", ".join(mixin_names) + ", BaseSettings):\n" + "\n\n".join(body) + "\n"
        (pkg_dir / "_model.py").write_text(out, encoding="utf-8")
        print(f"_model.py: {len(body)} members -> class {cls_name}({', '.join(mixin_names)})")


if __name__ == "__main__":
    main()
