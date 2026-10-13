"""Reject local credentials and generated outputs in wheel/source archives."""

from pathlib import Path, PurePosixPath
import sys
import tarfile
import zipfile


FORBIDDEN_DIRECTORIES = {".git", ".venv", ".vercel", "__pycache__", "node_modules", ".next"}


def unsafe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        path.is_absolute()
        or ".." in path.parts
        or bool(FORBIDDEN_DIRECTORIES.intersection(path.parts))
        or path.name in {".env", ".deploy.env", ".npmrc"}
        or (path.name.startswith(".env.") and not path.name.endswith(".example"))
        or path.name.endswith(".tsbuildinfo")
        or path.name.endswith(".pyc")
    )


def check_archive(path: Path) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        if any(name.startswith("apps/web/") for name in names):
            raise ValueError(f"{path.name}: separately deployed frontend must not ship in runtime wheel")
    else:
        with tarfile.open(path) as archive:
            names = archive.getnames()
    count = sum(unsafe_member(name) for name in names)
    if count:
        raise ValueError(f"{path.name}: {count} unsafe archive members")
    print(f"PASS: {path.name}: {len(names)} safe members")


def main() -> int:
    paths = [Path(name) for name in sys.argv[1:]]
    if not paths:
        print("Supply wheel and source archive paths", file=sys.stderr)
        return 1
    try:
        for path in paths:
            check_archive(path)
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
