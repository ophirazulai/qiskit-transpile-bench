"""Human-readable progress log, kept next to ``report.md``.

Every progress message is echoed as before and appended to ``progress.log`` with a
wall-clock timestamp and the time elapsed since the process started. ``step`` brackets a
stage: it logs the start, the end and the duration, and ``summary`` writes a table of
every step's duration at the end of the run. A resumed run appends to the same file.
"""

import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path


def duration(seconds):
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


class RunLog:
    def __init__(self, path, echo=print):
        self.path = Path(path)
        self.echo = echo
        self.started = time.monotonic()
        self.steps = []
        self._lock = threading.Lock()
        self._depth = threading.local()
        self._write(f"=== {'Resumed' if self.path.exists() else 'Started'} ===")

    def _write(self, message, depth=0):
        now = datetime.now().astimezone()
        elapsed = duration(time.monotonic() - self.started)
        line = f"{now:%Y-%m-%d %H:%M:%S}  +{elapsed:>9}  {'  ' * depth}{message}\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def __call__(self, message):
        self._write(message, getattr(self._depth, "value", 0))
        if self.echo:
            self.echo(message)

    def depth(self):
        return getattr(self._depth, "value", 0)

    @contextmanager
    def step(self, name, depth=None):
        """Log and time one stage; ``depth`` places a step started from another thread."""
        outer = getattr(self._depth, "value", 0)
        depth = outer if depth is None else depth
        entry = {"name": name, "depth": depth, "status": "running", "seconds": None}
        with self._lock:
            self.steps.append(entry)
        self(f"▶ {name}")
        self._depth.value = depth + 1
        start = time.monotonic()
        try:
            yield
        except BaseException:
            entry.update(status="failed", seconds=time.monotonic() - start)
            self._depth.value = depth
            self(f"✗ {name} failed after {duration(entry['seconds'])}")
            self._depth.value = outer
            raise
        entry.update(status="done", seconds=time.monotonic() - start)
        self._depth.value = depth
        self(f"✓ {name} ({duration(entry['seconds'])})")
        self._depth.value = outer

    def summary(self):
        total = time.monotonic() - self.started
        lines = ["", "Step durations", "-" * 72]
        for entry in self.steps:
            seconds = entry["seconds"]
            share = f"{100 * seconds / total:5.1f}%" if seconds is not None and total else ""
            label = ("  " * entry["depth"] + entry["name"])[:50]
            shown = duration(seconds) if seconds is not None else entry["status"]
            flag = "" if entry["status"] == "done" else f"  ({entry['status']})"
            lines.append(f"{label:<50} {shown:>10} {share:>7}{flag}")
        lines += ["-" * 72, f"{'Total (this process)':<50} {duration(total):>10}", ""]
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")


def step(comparison, name, depth=None):
    """``comparison.progress.step(name)``, or no-op when progress is a plain callable."""
    method = getattr(getattr(comparison, "progress", None), "step", None)
    return method(name, depth) if method else nullcontext()


def depth(comparison):
    method = getattr(getattr(comparison, "progress", None), "depth", None)
    return method() if method else 0
