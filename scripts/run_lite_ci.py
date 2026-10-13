"""Run portable Lite acceptance and preserve diagnostics in check annotations."""
import os
import subprocess
import sys


def main() -> int:
    result = subprocess.run([sys.executable, "-m", "pytest", "tests/test_lite_mode.py", "-q"],
                            env={**os.environ, "PYTHONUTF8":"1"}, capture_output=True, encoding="utf-8", errors="replace")
    output = result.stdout + result.stderr
    print(output)
    if result.returncode and os.environ.get("GITHUB_ACTIONS"):
        diagnostic = output[-6000:].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error title=Lite acceptance failed::" + diagnostic)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
