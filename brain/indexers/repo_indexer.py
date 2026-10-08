import os
import json
import re
import fnmatch
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Union, List, Dict, Set, Optional
from loguru import logger

from brain.config.paths import reports_dir
from brain.memory.source_manifest import source_manifest_revision
from brain.search.filters import should_ignore_dir as _search_should_ignore_dir, is_secret_or_env_file
LANGUAGE_MAP = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (React)",
    ".js": "JavaScript",
    ".jsx": "JavaScript (React)",
    ".kt": "Kotlin",
    ".kts": "Kotlin Script",
    ".java": "Java",
    ".rs": "Rust",
    ".go": "Go",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".h": "C/C++ Header",
    ".hpp": "C++ Header",
    ".swift": "Swift",
    ".rb": "Ruby",
    ".php": "PHP",
    ".scala": "Scala",
    ".lua": "Lua",
    ".sh": "Shell Script",
    ".bash": "Shell Script",
    ".pl": "Perl",
    ".pm": "Perl",
    ".groovy": "Groovy",
    ".dart": "Dart",
    ".r": "R",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".clj": "Clojure",
    ".sql": "SQL",
    ".html": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less"
}

# Figma URL regex pattern matching files or design files
FIGMA_REGEX = re.compile(r'https?://(?:www\.)?figma\.com/(?:file|design|proto)/[a-zA-Z0-9_+\-%?=&]+')


def should_ignore_dir(name: str) -> bool:
    """Check if a directory should be ignored during the scan."""
    return _search_should_ignore_dir(name)


def should_ignore_file(name: str) -> bool:
    """Check if a file should be ignored during the scan."""
    lower = name.lower()
    if is_secret_or_env_file(lower):
        return True
    if fnmatch.fnmatch(lower, "*.lock"):
        return True
    ignored_exts = {
        ".db", ".sqlite", ".pyc", ".coverage", ".png", ".jpg", ".jpeg",
        ".gif", ".svg", ".ico", ".webp", ".bmp", ".tiff", ".tif", ".avif", ".heic",
        ".zip", ".tar", ".gz", ".7z", ".rar",
        ".npz", ".npy", ".pdf", ".bin", ".exe", ".dll", ".so", ".dylib",
        ".woff", ".woff2", ".eot", ".ttf", ".class", ".o", ".a", ".log", ".json",
        ".jks", ".jceks", ".keystore", ".p12", ".pfx", ".pem", ".key", ".crt",
        ".cer", ".csr", ".p8", ".mobileprovision", ".jar", ".war", ".aar",
        ".apk", ".aab", ".dex", ".nupkg", ".msix", ".appx", ".ipa"
    }
    ext = os.path.splitext(lower)[1]
    if ext in ignored_exts:
        return True
    if lower in {".ds_store", "thumbs.db", ".coverage", ".thumbnail"}:
        return True
    return False


def classify_file(path: Path, repo_path: Path) -> str:
    """Classify file into structural/domain categories."""
    rel_path = path.relative_to(repo_path)
    rel_parts = [p.lower() for p in rel_path.parts]
    name = path.name.lower()
    ext = path.suffix.lower()

    # 1. CI Config
    if ".github" in rel_parts and "workflows" in rel_parts:
        return "ci_config"
    if name in [".gitlab-ci.yml", ".gitlab-ci.yaml", "circle.yml", "appveyor.yml", "travis.yml"]:
        return "ci_config"

    # 2. Test
    if any("test" in p or "spec" in p for p in rel_parts[:-1]) or "test" in name or "spec" in name:
        return "test"

    # 3. Migration
    if any("migration" in p for p in rel_parts[:-1]) or re.match(r"^\d{4}_", name) or (name.startswith("v") and "__" in name and ext == ".sql"):
        return "migration"

    # 4. API Spec
    if name in ["openapi.json", "openapi.yaml", "openapi.yml", "swagger.json", "swagger.yaml", "swagger.yml", "schema.graphql", "schema.gql"]:
        return "api_spec"
    if ext in [".proto", ".graphql", ".gql"]:
        return "api_spec"

    # 5. Design Token / Theme files
    if "design-token" in name or "design_token" in name or name in ["theme.json", "theme.js", "theme.ts", "theme.css", "theme.scss", "variables.scss", "variables.css", "variables.less"]:
        return "design_token"

    # 6. Asset
    asset_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".tiff",
        ".mp3", ".wav", ".mp4", ".mov", ".avi", ".mkv",
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z"
    }
    if ext in asset_exts:
        return "asset"

    # 7. Script
    script_exts = {".sh", ".bash", ".ps1", ".bat", ".cmd"}
    tooling_dirs = {"scripts", "bin", "eval", "benchmarks"}
    if ext in script_exts or tooling_dirs.intersection(rel_parts[:-1]):
        if ext in {".py", ".js", ".ts", ".sh", ".bash", ".ps1", ".bat", ".cmd"}:
            return "script"

    # 8. Config
    config_names = {
        "package.json", "tsconfig.json", "pyproject.toml", "setup.cfg", "requirements.txt",
        "cargo.toml", "build.gradle", "pom.xml", "makefile", ".gitignore", "docker-compose.yml",
        "docker-compose.yaml", "dockerfile", "webpack.config.js", "webpack.config.ts",
        "babel.config.js", "nest-cli.json", "angular.json", "tailwind.config.js",
        "postcss.config.js", "vite.config.ts", "vite.config.js", "next.config.js",
        "next.config.mjs", "nuxt.config.js", "nuxt.config.ts"
    }
    if name in config_names or name.startswith(".env") or ext in [".toml", ".yaml", ".yml", ".ini", ".conf", ".cfg", ".json"]:
        return "config"

    # 9. Documentation
    if ext in [".md", ".markdown", ".rst", ".adoc"] or "docs" in rel_parts or "doc" in rel_parts:
        return "documentation"

    # 10. Source Code
    src_exts = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".kts", ".java", ".rs", ".go",
        ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".rb", ".php", ".scala",
        ".sql", ".html", ".css", ".scss", ".sass", ".less", ".lua", ".sh"
    }
    if ext in src_exts:
        return "source_code"

    return "unknown"


def count_lines(path: Path) -> int:
    """Helper to count lines of code in a file, avoiding huge files."""
    try:
        if path.stat().st_size > 10 * 1024 * 1024:  # 10MB limit
            return 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def parse_package_json(path: Path) -> dict:
    """Parse dependencies from package.json."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_requirements_txt(path: Path) -> List[str]:
    """Parse dependencies from requirements.txt."""
    deps = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                name = re.split(r'[=<>~!;]', line)[0].strip()
                if name:
                    deps.append(name)
    except Exception:
        pass
    return deps


def extract_toml_dependencies(content: str) -> List[str]:
    """Parse dependencies from pyproject.toml / Cargo.toml content."""
    deps = []
    sections = re.split(r'\[([^\]]+)\]', content)
    for i in range(1, len(sections), 2):
        sec_name = sections[i].strip()
        if "dependencies" in sec_name or "project" in sec_name:
            sec_content = sections[i+1]
            for line in sec_content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    parts = line.split("=", 1)
                    key = parts[0].strip()
                    if key not in {"dependencies", "requires", "name", "version", "description", "readme", "requires-python", "license", "authors", "classifiers"}:
                        deps.append(key)
                else:
                    for match in re.finditer(r'"([^"]+)"|\'([^\']+)\'', line):
                        val = match.group(1) or match.group(2)
                        if val:
                            name = re.split(r'[=<>~!]', val)[0].strip()
                            if name:
                                deps.append(name)
    return deps


def parse_cargo_toml(path: Path) -> List[str]:
    """Parse Cargo.toml dependencies."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return extract_toml_dependencies(f.read())
    except Exception:
        return []


def parse_gradle_file(path: Path) -> List[str]:
    """Parse build.gradle dependency artifact IDs."""
    deps = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            matches = re.findall(r'(?:implementation|api|compile|testImplementation|runtimeOnly)\s*\(?\s*["\']([^"\']+)["\']', content)
            for m in matches:
                parts = m.split(":")
                if len(parts) > 1:
                    deps.append(parts[1])
                else:
                    deps.append(parts[0])
    except Exception:
        pass
    return deps


def parse_pom_xml(path: Path) -> List[str]:
    """Parse pom.xml dependency artifact IDs."""
    deps = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            dep_blocks = re.findall(r'<dependency>(.*?)</dependency>', content, re.DOTALL)
            for block in dep_blocks:
                art_match = re.search(r'<artifactId>(.*?)</artifactId>', block)
                if art_match:
                    deps.append(art_match.group(1).strip())
    except Exception:
        pass
    return deps


def extract_imports_from_file(path: Path) -> Set[str]:
    """Extract standard import library names from the first 100 lines of a source file."""
    imports: Set[str] = set()
    ext = path.suffix.lower()
    try:
        if path.stat().st_size > 1 * 1024 * 1024:  # 1MB limit for imports reading
            return imports
        lines = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for _ in range(100):
                line = f.readline()
                if not line:
                    break
                lines.append(line)
        content = "".join(lines)

        if ext == ".py":
            for match in re.finditer(r'^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)', content, re.MULTILINE):
                imports.add(match.group(1).split('.')[0])
        elif ext in {".js", ".jsx", ".ts", ".tsx"}:
            for match in re.finditer(r'(?:import|from)\s+["\']([^"\']+)["\']', content):
                val = match.group(1)
                if not val.startswith("."):
                    imports.add(val.split('/')[0])
            for match in re.finditer(r'require\s*\(\s*["\']([^"\']+)["\']\s*\)', content):
                val = match.group(1)
                if not val.startswith("."):
                    imports.add(val.split('/')[0])
        elif ext in {".java", ".kt"}:
            for match in re.finditer(r'^\s*import\s+([a-zA-Z0-9_\.]+)', content, re.MULTILINE):
                imports.add(match.group(1))
        elif ext == ".go":
            for match in re.finditer(r'"([^"]+)"', content):
                imports.add(match.group(1))
        elif ext == ".rs":
            for match in re.finditer(r'^\s*use\s+([a-zA-Z0-9_]+)', content, re.MULTILINE):
                imports.add(match.group(1))
    except Exception:
        pass
    return imports


def find_figma_urls_in_file(path: Path) -> Set[str]:
    """Scan file for Figma URLs."""
    urls: Set[str] = set()
    try:
        if path.stat().st_size > 1 * 1024 * 1024:
            return urls
        text_extensions = {
            ".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".json", ".html", ".css", ".scss",
            ".yaml", ".yml", ".txt", ".java", ".kt", ".kts", ".rs", ".go", ".c", ".cpp",
            ".h", ".hpp", ".cs", ".swift", ".rb", ".php", ".scala", ".xml", ".gradle", ".toml"
        }
        if path.suffix.lower() not in text_extensions:
            return urls
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            for match in FIGMA_REGEX.finditer(content):
                urls.add(match.group(0))
    except Exception:
        pass
    return urls


def get_git_commit_hash(repo_path: Path) -> str:
    """Return a verifiable Git or snapshot revision."""
    try:
        resolved_repo = repo_path.resolve()
        # Production snapshots are mounted read-only from a host-owned directory,
        # while the API/worker containers run as root. Trust only the exact path
        # being inspected; no global safe.directory exception is introduced.
        git_command = ["git", "-c", f"safe.directory={resolved_repo}"]
        res = subprocess.run(
            [*git_command, "rev-parse", "--is-inside-work-tree"],
            cwd=str(resolved_repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        if "true" in res.stdout.lower():
            commit_res = subprocess.run(
                [*git_command, "rev-parse", "HEAD"],
                cwd=str(resolved_repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            return commit_res.stdout.strip()
    except Exception as e:
        logger.warning(f"Could not retrieve git commit hash: {e}")
    manifest_revision = source_manifest_revision(repo_path.resolve())
    if manifest_revision:
        return manifest_revision
    return "unverifiable"


def make_file_url(path: Path) -> str:
    """Create a valid file:/// link from a path."""
    return f"file:///{path.resolve().as_posix()}"


class RepoIndexer:
    def __init__(self, repo_path: Optional[Union[str, Path]] = None):
        self.repo_path = Path(repo_path) if repo_path else None

    def scan(self, repo_path: Optional[Union[str, Path]] = None, report_path: Optional[Union[str, Path]] = None) -> dict:
        """Scan a repository and generate a structured audit report."""
        target_path = Path(repo_path) if repo_path else self.repo_path
        if not target_path:
            raise ValueError("Repository path must be provided in constructor or scan method.")

        target_path = target_path.resolve()
        if not target_path.exists() or not target_path.is_dir():
            raise FileNotFoundError(f"Target repository {target_path} is not a directory or does not exist.")

        logger.info(f"Audit Engine: Scanning directory: {target_path}")

        scanned_files: List[Path] = []
        ignored_dirs_found: Set[str] = set()

        for root, dirs, files in os.walk(str(target_path)):
            # Prune ignored directories in-place
            for d in list(dirs):
                if should_ignore_dir(d):
                    ignored_dirs_found.add(d)
                    dirs.remove(d)

            for file_name in files:
                if should_ignore_file(file_name):
                    continue
                scanned_files.append(Path(root) / file_name)

        logger.info(f"Audit Engine: Scanned {len(scanned_files)} files. Extracting dependencies and imports...")

        # Collect dependencies
        package_deps: Set[str] = set()
        for f in scanned_files:
            name = f.name.lower()
            if name == "package.json":
                pkg_data = parse_package_json(f)
                deps = pkg_data.get("dependencies", {})
                dev_deps = pkg_data.get("devDependencies", {})
                package_deps.update(deps.keys())
                package_deps.update(dev_deps.keys())
            elif name == "requirements.txt":
                package_deps.update(parse_requirements_txt(f))
            elif name == "pyproject.toml":
                try:
                    with open(f, "r", encoding="utf-8", errors="ignore") as file:
                        package_deps.update(extract_toml_dependencies(file.read()))
                except Exception:
                    pass
            elif name in {"build.gradle", "build.gradle.kts"}:
                package_deps.update(parse_gradle_file(f))
            elif name == "pom.xml":
                package_deps.update(parse_pom_xml(f))
            elif name == "cargo.toml":
                package_deps.update(parse_cargo_toml(f))

        source_imports: Set[str] = set()
        figma_urls_by_file: Dict[Path, Set[str]] = {}

        # Classification & Language/Line counting & Figma URL search
        files_by_category: Dict[str, List[Path]] = {
            "source_code": [],
            "test": [],
            "config": [],
            "documentation": [],
            "api_spec": [],
            "design_token": [],
            "asset": [],
            "migration": [],
            "script": [],
            "ci_config": [],
            "unknown": []
        }

        total_source_files = 0
        lang_stats: Dict[str, Dict[str, int]] = {}

        for f in scanned_files:
            category = classify_file(f, target_path)
            if category in files_by_category:
                files_by_category[category].append(f)
            else:
                files_by_category["unknown"].append(f)

            ext = f.suffix.lower()
            if ext in LANGUAGE_MAP:
                lang = LANGUAGE_MAP[ext]
                if lang not in lang_stats:
                    lang_stats[lang] = {"count": 0, "lines": 0}
                lang_stats[lang]["count"] += 1
                lang_stats[lang]["lines"] += count_lines(f)
                total_source_files += 1

            if category in {"source_code", "test", "script"}:
                source_imports.update(extract_imports_from_file(f))

            urls = find_figma_urls_in_file(f)
            if urls:
                figma_urls_by_file[f] = urls

        # Determine framework, build, and test metrics
        build_systems = self._detect_build_systems(scanned_files)
        frameworks = self._detect_frameworks(package_deps, source_imports, scanned_files)
        test_suites = self._detect_test_suites(scanned_files, package_deps, source_imports)

        # Get Git Commit hash
        commit_hash = get_git_commit_hash(target_path)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        report_data = {
            "timestamp": timestamp,
            "repo_path": target_path.as_posix(),
            "commit_hash": commit_hash,
            "ignored_directories": sorted(list(ignored_dirs_found)),
            "languages": lang_stats,
            "total_source_files": total_source_files,
            "frameworks": frameworks,
            "build_systems": build_systems,
            "test_suites": test_suites,
            "files_by_category": files_by_category,
            "figma_urls": figma_urls_by_file
        }

        # Write Markdown Report
        self._write_markdown_report(report_data, target_path, report_path)

        logger.info("Audit Engine: Successfully generated report.")
        return report_data

    def _detect_build_systems(self, scanned_files: List[Path]) -> Set[str]:
        build_systems = set()
        filenames = {f.name.lower() for f in scanned_files}

        pyproject_path = None
        for f in scanned_files:
            if f.name.lower() == "pyproject.toml":
                pyproject_path = f
                break

        if "build.gradle" in filenames or "build.gradle.kts" in filenames or "settings.gradle" in filenames:
            build_systems.add("Gradle")
        if "pom.xml" in filenames:
            build_systems.add("Maven")
        if "package.json" in filenames:
            build_systems.add("NPM")
        if "yarn.lock" in filenames:
            build_systems.add("Yarn")
        if "pnpm-lock.yaml" in filenames:
            build_systems.add("PNPM")
        if "cargo.toml" in filenames:
            build_systems.add("Cargo")
        if "go.mod" in filenames:
            build_systems.add("Go Modules")
        if "pipfile" in filenames or "pipfile.lock" in filenames:
            build_systems.add("Pipenv")
        if "requirements.txt" in filenames or "setup.py" in filenames or "setup.cfg" in filenames:
            build_systems.add("Pip")
        if "makefile" in filenames:
            build_systems.add("Make")
        if "cmakelists.txt" in filenames:
            build_systems.add("CMake")

        if pyproject_path:
            try:
                with open(pyproject_path, "r", encoding="utf-8", errors="ignore") as fp:
                    content = fp.read()
                    if "tool.poetry" in content:
                        build_systems.add("Poetry")
                    if "hatchling" in content or "tool.hatch" in content:
                        build_systems.add("Hatch")
                    if "flit" in content:
                        build_systems.add("Flit")
                    if "setuptools" in content:
                        build_systems.add("Setuptools")
            except Exception:
                pass

        return build_systems

    def _detect_frameworks(self, package_deps: Set[str], source_imports: Set[str], scanned_files: List[Path]) -> Set[str]:
        frameworks = set()
        filenames = {f.name.lower() for f in scanned_files}

        if "next" in package_deps or "next.config.js" in filenames or "next.config.mjs" in filenames:
            frameworks.add("Next.js")
        if "react" in package_deps or "react" in source_imports:
            frameworks.add("React")
        if "fastapi" in package_deps or "fastapi" in source_imports:
            frameworks.add("FastAPI")
        if any("spring-boot" in d for d in package_deps) or any("springframework.boot" in imp for imp in source_imports):
            frameworks.add("Spring Boot")
        if "django" in package_deps or "django" in source_imports:
            frameworks.add("Django")
        if "flask" in package_deps or "flask" in source_imports:
            frameworks.add("Flask")
        if "express" in package_deps or "express" in source_imports:
            frameworks.add("Express")
        if "@nestjs/core" in package_deps or "@nestjs" in package_deps:
            frameworks.add("NestJS")
        if "@angular/core" in package_deps:
            frameworks.add("Angular")
        if "vue" in package_deps or "vue" in source_imports:
            frameworks.add("Vue")
        if "svelte" in package_deps or "@sveltejs/kit" in package_deps:
            frameworks.add("Svelte")
        if "actix-web" in package_deps:
            frameworks.add("Actix Web")
        if "axum" in package_deps:
            frameworks.add("Axum")
        if "rocket" in package_deps:
            frameworks.add("Rocket")
        if any("gin" in imp for imp in source_imports) or "gin" in package_deps:
            frameworks.add("Gin")
        if any("echo" in imp for imp in source_imports):
            frameworks.add("Echo")
        if any("fiber" in imp for imp in source_imports):
            frameworks.add("Fiber")

        return frameworks

    def _detect_test_suites(self, scanned_files: List[Path], package_deps: Set[str], source_imports: Set[str]) -> Set[str]:
        suites = set()
        filenames = {f.name.lower() for f in scanned_files}

        if "pytest" in package_deps or "pytest" in source_imports or "conftest.py" in filenames or "pytest.ini" in filenames:
            suites.add("Pytest")
        if "jest" in package_deps or "jest.config.js" in filenames or "jest.config.ts" in filenames:
            suites.add("Jest")
        if any("junit" in d for d in package_deps) or any("junit" in imp for imp in source_imports):
            suites.add("JUnit")
        if "@playwright/test" in package_deps or "playwright.config.ts" in filenames or "playwright.config.js" in filenames:
            suites.add("Playwright")
        if "cypress" in package_deps or "cypress.config.js" in filenames or "cypress.config.ts" in filenames:
            suites.add("Cypress")
        if any(f.name.endswith("_test.go") for f in scanned_files):
            suites.add("Go Testing (testing package)")
        if "cargo.toml" in filenames and any("test" in f.name for f in scanned_files if f.suffix == ".rs"):
            suites.add("Cargo Test")

        if not suites and any("test" in f.name or "spec" in f.name for f in scanned_files):
            suites.add("Generic Test Files")

        return suites

    def _write_markdown_report(self, data: dict, target_path: Path, report_path: Optional[Union[str, Path]]) -> None:
        if report_path is None:
            report_path = reports_dir(target_path) / "initial-audit.md"
        else:
            report_path = Path(report_path)

        report_path.parent.mkdir(parents=True, exist_ok=True)

        md = []
        md.append("# Repository Initial Audit Report")
        md.append("")
        md.append("## Summary Information")
        md.append(f"- **Audit Timestamp**: `{data['timestamp']}`")
        md.append(f"- **Target Repository Path**: `{data['repo_path']}`")
        md.append(f"- **Git Commit Hash**: `{data['commit_hash']}`")
        md.append("")

        md.append("## Ignored Patterns")
        md.append("The repository scanner ignored standard dependency, build, and IDE configurations:")
        md.append("- `node_modules`")
        md.append("- `.git`")
        md.append("- `.gradle`")
        md.append("- `build`")
        md.append("- `dist`")
        md.append("- `.next`")
        md.append("- `out`")
        md.append("- `coverage`")
        md.append("- `.idea`")
        md.append("- `.vscode`")
        md.append("- `*.lock` files")
        md.append("")
        md.append("**Actually found and skipped in this repository:**")
        if data["ignored_directories"]:
            for d in data["ignored_directories"]:
                md.append(f"- `{d}`")
        else:
            md.append("- *None*")
        md.append("")

        md.append("## Detected Languages")
        if data["languages"]:
            md.append("| Language | File Count | Total Lines of Code | Percentage of Source Files |")
            md.append("|---|---|---|---|")
            sorted_langs = sorted(data["languages"].items(), key=lambda x: x[1]["count"], reverse=True)
            for lang, stats in sorted_langs:
                pct = (stats["count"] / data["total_source_files"]) * 100 if data["total_source_files"] > 0 else 0
                md.append(f"| {lang} | {stats['count']} | {stats['lines']:,} | {pct:.1f}% |")
        else:
            md.append("*No source languages detected.*")
        md.append("")

        md.append("## Detected Frameworks")
        if data["frameworks"]:
            md.append("The following frameworks were identified:")
            for fw in sorted(list(data["frameworks"])):
                md.append(f"- **{fw}**")
        else:
            md.append("*No major frameworks detected.*")
        md.append("")

        md.append("## Build Systems")
        if data["build_systems"]:
            md.append("The following build system configurations were detected:")
            for bs in sorted(list(data["build_systems"])):
                md.append(f"- **{bs}**")
        else:
            md.append("*No standard build configurations detected.*")
        md.append("")

        md.append("## Test Suites & Frameworks")
        if data["test_suites"]:
            md.append("The repository contains configuration or code for:")
            for ts in sorted(list(data["test_suites"])):
                md.append(f"- **{ts}**")
        else:
            md.append("*No test suites detected.*")
        md.append("")

        md.append("## API Specifications")
        api_files = data["files_by_category"].get("api_spec", [])
        if api_files:
            md.append("| Specification File | Type |")
            md.append("|---|---|")
            for f in sorted(api_files):
                rel = f.relative_to(target_path).as_posix()
                ext = f.suffix.lower()
                spec_type = "OpenAPI Spec"
                if ext == ".proto":
                    spec_type = "Protocol Buffer"
                elif ext in {".graphql", ".gql"}:
                    spec_type = "GraphQL Schema"
                md.append(f"| [{rel}]({make_file_url(f)}) | {spec_type} |")
        else:
            md.append("*No API specifications found.*")
        md.append("")

        md.append("## CI Configurations")
        ci_files = data["files_by_category"].get("ci_config", [])
        if ci_files:
            md.append("| Configuration File | CI Provider |")
            md.append("|---|---|")
            for f in sorted(ci_files):
                rel = f.relative_to(target_path).as_posix()
                provider = "GitHub Actions"
                if ".gitlab-ci" in rel:
                    provider = "GitLab CI"
                elif "circle" in rel:
                    provider = "CircleCI"
                elif "travis" in rel:
                    provider = "Travis CI"
                elif "appveyor" in rel:
                    provider = "AppVeyor"
                md.append(f"| [{rel}]({make_file_url(f)}) | {provider} |")
        else:
            md.append("*No CI configuration files found.*")
        md.append("")

        md.append("## Documentation Files")
        doc_files = data["files_by_category"].get("documentation", [])
        if doc_files:
            md.append(f"Detected {len(doc_files)} documentation file(s):")
            # Limit list to first 100 for readability
            for f in sorted(doc_files)[:100]:
                rel = f.relative_to(target_path).as_posix()
                md.append(f"- [{rel}]({make_file_url(f)})")
            if len(doc_files) > 100:
                md.append(f"- *... and {len(doc_files) - 100} more documentation files.*")
        else:
            md.append("*No documentation files found.*")
        md.append("")

        md.append("## Design-Related Assets & Configs")
        theme_files = data["files_by_category"].get("design_token", [])
        md.append("### Theme / Style / Token Files")
        if theme_files:
            for f in sorted(theme_files):
                rel = f.relative_to(target_path).as_posix()
                md.append(f"- [{rel}]({make_file_url(f)})")
        else:
            md.append("*No specific theme or token files found.*")
        md.append("")

        md.append("### Figma References")
        figma_urls = data["figma_urls"]
        if figma_urls:
            md.append("| Figma Link | Found in File |")
            md.append("|---|---|")
            for f, urls in sorted(figma_urls.items()):
                rel = f.relative_to(target_path).as_posix()
                for url in sorted(list(urls)):
                    md.append(f"| [Figma Link]({url}) | [{rel}]({make_file_url(f)}) |")
        else:
            md.append("*No Figma references found in text files.*")
        md.append("")

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md))

        logger.info(f"Audit Engine: Report written to {report_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Repository Initial Audit Engine")
    from brain.config.paths import resolve_repo_path

    parser.add_argument("repo_path", nargs="?", default=None, help="Path to target repository")
    parser.add_argument("--report", help="Output path for the audit markdown report")
    args = parser.parse_args()

    indexer = RepoIndexer()
    try:
        indexer.scan(str(resolve_repo_path(args.repo_path)), args.report)
        print("Success! Audit report generated.")
    except Exception as e:
        print(f"Error during scan: {e}")
        raise e
