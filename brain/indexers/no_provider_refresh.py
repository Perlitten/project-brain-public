"""Preflight policy for a genuine incremental refresh of an unchanged index.

No timestamp is renewed by this module. The native indexer must still complete
source revalidation, graph projection and full embedding verification.
"""
from dataclasses import dataclass
import selectors
import subprocess
import time
import os
import tarfile


def bounded_git_output(command, timeout_s, max_bytes=8 * 1024 * 1024):
    """No secret-bearing stderr, unbounded communicate(), or surviving child."""
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout_s
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    output = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise NoProviderRefreshRejected("Local Git inspection timed out")
            block = proc.stdout.read1(16384)
            if not block:
                break
            output.extend(block)
            if len(output) > max_bytes:
                raise NoProviderRefreshRejected("Local Git metadata exceeds resource limit")
        if proc.wait(timeout=max(0.1, deadline-time.monotonic())):
            raise NoProviderRefreshRejected("Local Git inspection failed")
        return bytes(output)
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


class NoProviderRefreshRejected(ValueError):
    pass


@dataclass(frozen=True)
class NoProviderRefreshLimits:
    max_files: int = 5000
    max_source_bytes: int = 128 * 1024 * 1024
    timeout_s: int = 300

    def __post_init__(self):
        for value, ceiling in ((self.max_files, 10000), (self.max_source_bytes, 256 * 1024 * 1024), (self.timeout_s, 600)):
            if type(value) is not int or not 0 < value <= ceiling:
                raise NoProviderRefreshRejected("Invalid no-provider resource limit")


def bounded_source_candidates(root, limits, ignore_dir):
    """Bound raw tree enumeration before native eligibility filters run."""
    git = ['git', '-c', f'safe.directory={root}', '-C', str(root)]
    try:
        probe = subprocess.run([*git, 'rev-parse', '--is-inside-work-tree'],
                               capture_output=True, timeout=min(limits.timeout_s, 10))
    except FileNotFoundError:
        if (root / '.git').exists():
            raise NoProviderRefreshRejected("Git is required for this checkout") from None
        probe = None
    except subprocess.TimeoutExpired:
        raise NoProviderRefreshRejected("Git source inspection timed out") from None
    candidates = []
    size = 0

    def append(path):
        nonlocal size
        candidates.append(path)
        if path.is_file() and not path.is_symlink():
            size += path.stat().st_size
        if len(candidates) > limits.max_files or size > limits.max_source_bytes:
            raise NoProviderRefreshRejected("Source tree exceeds resource limits")

    if probe is not None and probe.returncode == 0 and probe.stdout.strip() == b'true':
        data = bounded_git_output([*git, 'ls-files', '--cached', '-z'], min(limits.timeout_s, 30))
        for item in data.split(b'\0'):
            if not item:
                continue
            relative = root.__class__(os.fsdecode(item))
            if relative.is_absolute() or '..' in relative.parts:
                raise NoProviderRefreshRejected("Tracked source path escapes repository")
            append(root / relative)
    else:
        if (root / '.git').exists():
            raise NoProviderRefreshRejected("Cannot inspect checkout metadata")
        deadline = time.monotonic() + min(limits.timeout_s, 30)
        directories = 0
        def reject_walk_error(error):
            raise error
        for current, dirs, files in os.walk(root, followlinks=False, onerror=reject_walk_error):
            directories += 1
            if directories > limits.max_files * 2 or time.monotonic() > deadline:
                raise NoProviderRefreshRejected("Source traversal exceeds resource limits")
            current = root.__class__(current)
            dirs[:] = [d for d in dirs if not ignore_dir(d) and not (current / d).is_symlink()]
            for name in files:
                append(current / name)
    return candidates


def validate_inventory(source, indexed, max_chunk_chars):
    """Exact eligible path set, normalized native hash and chunk skip gates.

source maps paths to (hash, nonblank). indexed rows have path/hash/chunks/longest.
Reject deleted, newly eligible, duplicate, changed or incomplete files before
any run/status mutation. Exclusions must be determined by the native scanner.
"""
    rows = {row["path"]: row for row in indexed}
    if len(rows) != len(indexed):
        raise NoProviderRefreshRejected("Duplicate indexed paths")
    if set(source) != set(rows):
        raise NoProviderRefreshRejected("Eligible source/index path sets differ; normal indexing required")
    for path, (digest, nonblank) in source.items():
        row = rows[path]
        if (row["hash"] != digest or (nonblank and not row["chunks"])
                or (row["longest"] or 0) > max_chunk_chars):
            raise NoProviderRefreshRejected("Source changed or indexed chunks incomplete; normal indexing required")


def bound_mirror_tree(mirror, revision, limits):
    """Bound local archive inputs before exporting; never fetches a ref.

    Count the entire Git tree, including paths the indexer later excludes,
    because the native exporter materializes those paths as well. Git stdout
    has a separate hard bound and timeout; repository contents are never logged.
    """
    proc = subprocess.Popen(['git', f'--git-dir={mirror}', 'ls-tree', '--full-tree', '-rlz', revision],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + min(limits.timeout_s, 30)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    pending = b''
    metadata_bytes = files = size = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise NoProviderRefreshRejected("Mirror tree inspection timed out")
            block = proc.stdout.read1(16384)
            if not block:
                break
            metadata_bytes += len(block)
            if metadata_bytes > 8 * 1024 * 1024:
                raise NoProviderRefreshRejected("Mirror tree metadata exceeds resource limit")
            pending += block
            while b'\0' in pending:
                record, pending = pending.split(b'\0', 1)
                metadata, path = record.split(b'\t', 1)
                fields = metadata.split()
                if len(fields) != 4 or path.startswith(b'/') or b'..' in path.split(b'/'):
                    raise NoProviderRefreshRejected("Invalid mirror tree metadata")
                files += 1
                if fields[1] == b'blob':
                    size += int(fields[3])
                if files > limits.max_files or size > limits.max_source_bytes:
                    raise NoProviderRefreshRejected("Mirror export exceeds resource limits")
        if pending or proc.wait(timeout=max(0.1, deadline-time.monotonic())):
            raise NoProviderRefreshRejected("Mirror revision is unavailable")
        return {'files': files, 'bytes': size}
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def export_mirror_bounded(mirror, revision, target, limits):
    """Bound both Git archive output and extracted files, including attributes.

    export-subst may expand a small blob, so tree byte totals alone are not a
    disk bound. No unbounded tar is created before the native file preflight.
    """
    bound_mirror_tree(mirror, revision, limits)
    archive = target / '.brain-mirror.tar'
    command = ['git', f'--git-dir={mirror}', 'archive', '--format=tar', revision]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + min(limits.timeout_s, 60)
    size = 0
    try:
        with archive.open('wb') as stream:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise NoProviderRefreshRejected("Local mirror archive timed out")
                block = proc.stdout.read1(16384)
                if not block:
                    break
                size += len(block)
                if size > limits.max_source_bytes + limits.max_files * 8192 + 10240:
                    raise NoProviderRefreshRejected("Mirror archive exceeds resource limits")
                stream.write(block)
        if proc.wait(timeout=max(0.1, deadline-time.monotonic())):
            raise NoProviderRefreshRejected("Local mirror export failed")
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            if (len(members) > limits.max_files * 2
                    or sum(item.size for item in members) > limits.max_source_bytes):
                raise NoProviderRefreshRejected("Expanded mirror export exceeds resource limits")
            tar.extractall(target, members=members, filter='data')
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        archive.unlink(missing_ok=True)
