"""Recording graph boundary used to characterize successful and partial writes."""

import asyncio
from copy import deepcopy
from pathlib import Path


FILES = {
    "brain/engine.py": "from brain.store import write\n\ndef run():\n    return write()\n",
    "brain/store.py": "def write():\n    return 1\n",
    "apps/api/routers/items.py": (
        "from brain.engine import run\n\n@router.get('/items')\nasync def list_items():\n    return run()\n"
    ),
    "tests/test_engine.py": "from brain.engine import run\n\ndef test_run():\n    assert run() == 1\n",
    "scripts/check_engine.py": "from brain.engine import run\n\nrun()\n",
    "README.md": "# Graph fixture\n",
}


def prepare_repository(root: Path, fallback=False):
    for relative, content in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "rules").mkdir(exist_ok=True)
    mapping = "features:\n  engine:\n    - brain/*\n  api:\n    - apps/api/*\n"
    (root / "rules/feature_map.yaml").write_text("[broken" if fallback else mapping)
    if fallback:
        (root / "feature_map.yaml").write_text(mapping)
    return [root / relative for relative in FILES]


class RecordingRows(list):
    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    def clear(self):
        self.calls.append({"kind": "clear_buffer"})
        super().clear()


class RecordingGraph:
    def __init__(self, failure=None, cancel=False):
        self.calls = []
        self.rows = RecordingRows(self.calls)
        self.failure = failure
        self.cancel = cancel
        self.fired = False

    def record(self, kind, values):
        item = {"kind": kind, **deepcopy(values)}
        self.calls.append(item)
        if not self.fired and self.failure and all(item.get(k) == v for k, v in self.failure.items()):
            self.fired = True
            if self.cancel:
                raise asyncio.CancelledError()
            raise RuntimeError("write rejected")
        self.rows.append(item)

    async def create_node(self, **kwargs):
        self.record("node", kwargs)

    async def create_relationship(self, **kwargs):
        self.record("relationship", kwargs)

    async def flush(self):
        self.record("flush", {})
        self.rows.clear()


def normalized_calls(calls, root):
    result = deepcopy(calls)
    for item in result:
        if item.get("properties", {}).get("path") == root.as_posix():
            item["properties"]["path"] = "$repo"
    return result
