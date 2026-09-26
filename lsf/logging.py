"""Shared logging for everything under ``lsf/``.

Each process writes its own pair of files, so concurrent jobs and resumed managers never
interleave records: a readable chronological ``<name>.log`` and structured
``<name>.events.jsonl``. Records also go to stderr, which LSF keeps as the job's output.

Every event carries a UTC timestamp, severity, component and event name, plus the bound
correlation fields (run, session, stage, attempt, nonce, job ID and name, host, process ID)
and its own fields. Durations are measured with the monotonic clock by the caller.

The level comes from ``--log-level``, else ``LSF_LOG_LEVEL``, else ``INFO``.
"""

import json
import logging
import os
import re
import shlex
import socket
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LEVEL = "INFO"
ENVIRONMENT = "LSF_LOG_LEVEL"
NAMESPACE = "qtb.lsf"
SECRET = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE|COOKIE", re.I)
REDACTED = "<redacted>"

_fields = {}
_paths = {}
_component = {"default": "lsf"}
# Configurations replaced by ``setup`` and restored by ``close``, innermost last.
_stack = []
# Always shown in the readable log; every field is shown for warnings and errors.
_SHOWN = {"stage", "attempt", "job_id", "job_key", "window"}


def resolve_level(argument=None, environ=None):
    """``--log-level`` wins over ``LSF_LOG_LEVEL``; the default is ``INFO``."""
    environ = os.environ if environ is None else environ
    value = (argument or environ.get(ENVIRONMENT) or DEFAULT_LEVEL).upper()
    if value not in LEVELS:
        raise ValueError(f"Unknown log level {value!r}; choose one of {', '.join(LEVELS)}")
    return value


def bind(**fields):
    """Correlation fields added to every later event of this process."""
    _fields.update({key: value for key, value in fields.items() if value is not None})


def bound():
    return dict(_fields)


def redact(value, key=""):
    """Mask credential-looking values, recursively; keeps the key so the field is visible."""
    if key and SECRET.search(str(key)):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def quote(argv):
    """The exact shell-quoted command, for reproduction."""
    return shlex.join(str(part) for part in argv)


def _now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Readable(logging.Formatter):
    def format(self, record):
        fields = {**_fields, **getattr(record, "fields", {})}
        shown = " ".join(
            f"{key}={value}"
            for key, value in fields.items()
            if (key in _SHOWN or record.levelno >= logging.WARNING)
            and not isinstance(value, (dict, list))
        )
        line = (
            f"{_now()} {record.levelname:<8} {getattr(record, 'component', '-'):<9} "
            f"{getattr(record, 'event', '-')}: {record.getMessage()}"
        )
        if shown:
            line += f" [{shown}]"
        if record.exc_info:
            line += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return line


class _Events(logging.Handler):
    def __init__(self, path):
        super().__init__()
        self.stream = Path(path).open("a", encoding="utf-8")

    def emit(self, record):
        entry = {
            **_fields,
            **getattr(record, "fields", {}),
            "ts": _now(),
            "level": record.levelname,
            "component": getattr(record, "component", None),
            "event": getattr(record, "event", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["traceback"] = "".join(traceback.format_exception(*record.exc_info))
        self.stream.write(json.dumps(entry, default=str, sort_keys=True) + "\n")
        self.stream.flush()

    def flush(self):
        self.stream.flush()
        try:
            os.fsync(self.stream.fileno())
        except OSError:
            pass

    def close(self):
        self.stream.close()
        super().close()


def _unique(directory, name):
    """Never append to another process's files: a reused name gets the process ID."""
    if (directory / f"{name}.log").exists() or (directory / f"{name}.events.jsonl").exists():
        name = f"{name}.{os.getpid()}"
    return name


def setup(directory, name, level, component, console=True):
    """Log this process to ``directory/<name>.log`` and ``<name>.events.jsonl``."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = _unique(directory, name)
    logger = logging.getLogger(NAMESPACE)
    _stack.append(
        (list(logger.handlers), logger.level, dict(_fields), dict(_paths), dict(_component))
    )
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    readable = logging.FileHandler(directory / f"{name}.log", encoding="utf-8")
    readable.setFormatter(_Readable())
    logger.addHandler(readable)
    logger.addHandler(_Events(directory / f"{name}.events.jsonl"))
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(_Readable())
        logger.addHandler(stream)
    _paths.clear()
    _paths.update(log=directory / f"{name}.log", events=directory / f"{name}.events.jsonl")
    bind(host=socket.gethostname(), pid=os.getpid())
    _component["default"] = component
    event(
        "logging.started",
        f"level {level}; log {_paths['log']}; events {_paths['events']}",
        component=component,
        level_name=level,
        log=str(_paths["log"]),
        events=str(_paths["events"]),
    )
    return logger


def paths():
    return dict(_paths)


def event(name, message="", *, level="INFO", component=None, exc_info=False, **fields):
    logger = logging.getLogger(NAMESPACE)
    logger.log(
        logging.getLevelName(level),
        message or name,
        exc_info=exc_info,
        extra={
            "event": name,
            "component": component or _component["default"],
            "fields": redact(fields),
        },
    )


def debug(name, message="", **fields):
    event(name, message, level="DEBUG", **fields)


def warning(name, message="", **fields):
    event(name, message, level="WARNING", **fields)


def error(name, message="", **fields):
    event(name, message, level="ERROR", **fields)


def enabled(level):
    return logging.getLogger(NAMESPACE).isEnabledFor(logging.getLevelName(level))


def flush():
    """Flush and sync every handler: before submission, cancellation and exit."""
    for handler in logging.getLogger(NAMESPACE).handlers:
        handler.flush()


def close():
    """Close this process's files and restore the configuration ``setup`` replaced."""
    logger = logging.getLogger(NAMESPACE)
    for handler in list(logger.handlers):
        handler.flush()
        logger.removeHandler(handler)
        handler.close()
    if _stack:
        handlers, level, fields, paths_, component = _stack.pop()
        for handler in handlers:
            logger.addHandler(handler)
        logger.setLevel(level)
        for target, saved in ((_fields, fields), (_paths, paths_), (_component, component)):
            target.clear()
            target.update(saved)
