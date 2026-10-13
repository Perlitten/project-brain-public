"""Run portable Lite acceptance and preserve diagnostics in check annotations."""
import os
import subprocess
import sys


def main() -> int:
    commands = [[sys.executable, "-m", "pytest", "tests/test_lite_mode.py", "-q"]]
    if "--install" in sys.argv:
        commands = [[sys.executable, "-m", "pip", "install", "--require-hashes", "-r", "requirements-dev.lock", "-r", "requirements-windows.lock"],
                    [sys.executable, "-m", "pip", "install", "--no-deps", "-e", "."]]
    for command in commands:
        code = run(command)
        if code:
            return code
    return 0


def run(command: list[str]) -> int:
    result = subprocess.run(command,
                            env={**os.environ, "PYTHONUTF8":"1"}, capture_output=True, encoding="utf-8", errors="replace")
    output = result.stdout + result.stderr
    print(output)
    if result.returncode and os.environ.get("GITHUB_ACTIONS"):
        diagnostic = output[-6000:].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error title=Lite verification failed::" + diagnostic)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
