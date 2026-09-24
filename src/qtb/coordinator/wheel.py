"""Repackage a regular installed harness when the source checkout is unavailable."""

import base64
import csv
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from qtb.errors import HarnessError

PACKAGES = ("qtb", "qtb_worker", "qtb_verifier")


def _record_hash(data):
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def repack_installed_harness(destination):
    """Write a deterministic wheel from verified installed distribution files.

    A wheel is not normally retained after installation. The distribution's
    RECORD lists the installed package payload; we verify its hashes and write
    a fresh wheel RECORD. Editable installs are deliberately excluded because
    their RECORD contains only a pointer back to a source checkout.
    """
    try:
        distribution = importlib.metadata.distribution("qiskit-transpile-bench")
    except importlib.metadata.PackageNotFoundError as exc:
        raise HarnessError("Cannot locate installed harness distribution") from exc
    files = list(distribution.files or [])
    direct_url = distribution.read_text("direct_url.json") or ""
    if direct_url:
        try:
            editable = json.loads(direct_url).get("dir_info", {}).get("editable")
        except (AttributeError, ValueError) as exc:
            raise HarnessError("Invalid installed harness direct_url.json") from exc
        if editable is True:
            raise HarnessError("An editable harness install requires its source checkout")
    if not files:
        raise HarnessError("Installed harness has no file inventory")
    for package in PACKAGES:
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.submodule_search_locations:
            raise HarnessError(f"Missing installed harness package: {package}")
        imported = Path(next(iter(spec.submodule_search_locations))).resolve()
        installed = Path(distribution.locate_file(package)).resolve()
        if imported != installed:
            raise HarnessError(f"Imported {package} differs from installed distribution")

    info_dirs = {file.parts[0] for file in files if file.name == "WHEEL" and file.parts}
    if len(info_dirs) != 1:
        raise HarnessError("Installed harness has no unique wheel metadata directory")
    info_dir = info_dirs.pop()
    if not info_dir.endswith(".dist-info"):
        raise HarnessError("Invalid installed wheel metadata directory")
    payload = {}
    for file in files:
        parts = file.parts
        if not parts or ".." in parts or file.is_absolute():
            continue
        in_package = parts[0] in PACKAGES
        in_metadata = parts[0] == info_dir and (
            file.name in {"METADATA", "WHEEL", "entry_points.txt"}
            or len(parts) > 2 and parts[1] == "licenses"
        )
        if not (in_package or in_metadata) or file.name == "RECORD":
            continue
        source = Path(distribution.locate_file(file))
        if not source.is_file():
            raise HarnessError(f"Missing installed harness file: {file}")
        data = source.read_bytes()
        claimed = getattr(file, "hash", None)
        if claimed is not None and (
            claimed.mode != "sha256" or _record_hash(data) != f"sha256={claimed.value}"
        ):
            raise HarnessError(f"Installed harness RECORD hash mismatch: {file}")
        payload[file.as_posix()] = data
    required = {
        "qtb/__init__.py",
        "qtb_worker/__init__.py",
        "qtb_verifier/__init__.py",
        "qtb/data/profiles/iterations-profile/manifest.json",
        "qtb/data/profiles/confirm-profile/manifest.json",
        f"{info_dir}/METADATA",
        f"{info_dir}/WHEEL",
        f"{info_dir}/entry_points.txt",
    }
    if not required <= payload.keys():
        raise HarnessError(f"Installed harness is incomplete: {sorted(required - payload.keys())}")
    tags = [
        line.split(":", 1)[1].strip()
        for line in payload[f"{info_dir}/WHEEL"].decode().splitlines()
        if line.startswith("Tag:")
    ]
    if tags != ["py3-none-any"]:
        raise HarnessError("Installed harness has an unsupported wheel tag")
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(payload.items()):
        writer.writerow((name, _record_hash(data), len(data)))
    record_name = f"{info_dir}/RECORD"
    writer.writerow((record_name, "", ""))
    payload[record_name] = record.getvalue().encode()

    name = re.sub(r"[-_.]+", "_", distribution.metadata["Name"]).lower()
    version = distribution.version.replace("-", "_")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    wheel = destination / f"{name}-{version}-{tags[0]}.whl"
    fd, temp_name = tempfile.mkstemp(dir=destination, suffix=".whl.tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, data in sorted(payload.items()):
                entry = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o644 << 16
                archive.writestr(entry, data)
        os.replace(temp_name, wheel)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return wheel
