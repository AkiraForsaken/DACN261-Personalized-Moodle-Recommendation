"""Finished bundles, addressed by the hash of their manifest.

A full run writes the course's ``bundle/``; a per-learner refresh writes
``refresh/<stamp>/bundle/``. Each bundle's id is the first 20 hex digits of
the SHA-256 of its ``manifest.json``, so an id names exactly one content and a
bundle replaced by a newer run is simply no longer found (410 for Moodle).
"""

import hashlib
import io
import json
import zipfile
from pathlib import Path

ID_LENGTH = 20


def bundle_id(bundle_dir):
    return hashlib.sha256((Path(bundle_dir) / "manifest.json").read_bytes()).hexdigest()[:ID_LENGTH]


def describe(bundle_dir):
    manifest = json.loads((Path(bundle_dir) / "manifest.json").read_text(encoding="utf-8"))
    return {
        "bundle_id": bundle_id(bundle_dir),
        "kind": manifest["kind"],
        "course_id": manifest["course_id"],
        "learners": manifest.get("learners"),
        "collected_at": manifest.get("collected_at"),
        "created_at": manifest.get("created_at"),
        "rows": {name: entry["rows"] for name, entry in manifest["files"].items()},
    }


def candidates(workspace):
    """Every bundle directory of the course, newest first."""
    refreshes = sorted(
        (path / "bundle" for path in workspace.refresh.glob("*") if (path / "bundle" / "manifest.json").is_file()),
        reverse=True,
    )
    snapshot = [workspace.bundle] if (workspace.bundle / "manifest.json").is_file() else []
    return snapshot + refreshes


def find(workspace, wanted):
    for directory in candidates(workspace):
        if bundle_id(directory) == wanted:
            return directory
    return None


def zipped(bundle_dir):
    """The bundle as a zip (flat: manifest.json and the CSVs at the top level)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(Path(bundle_dir).iterdir()):
            if path.is_file():
                archive.write(path, path.name)
    return buffer.getvalue()


class PublishTransport:
    """Instead of pushing into Moodle: record the bundle so Moodle can fetch it."""

    name = "publish"

    def push(self, bundle_dir, course_id):
        return describe(bundle_dir)
