"""Exact source identity and atomic, idempotent storage; no fuzzy matching."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def source_id(value):
    if value is None or isinstance(value, (bool, float)) or not str(value).strip():
        raise ValueError("missing or lossy source ID")
    return str(value)


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    lines = text.splitlines()
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return {}
    head = "\n".join(lines[1:closing])
    fields = {}
    parent = None
    for line in head.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            if line.startswith("  ") and parent == "source_ids":
                key = "source_ids." + key.strip()
            elif not line.startswith(" "):
                parent = key if not value.strip() else None
            value = value.strip()
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                value = value.strip("\"'")
            fields[key] = str(value)
    return fields


def validate_tree_path(boundary, path):
    boundary, path = Path(boundary).absolute(), Path(path).absolute()
    if not path.is_relative_to(boundary) or not path.resolve().is_relative_to(boundary.resolve()):
        raise ValueError("path escapes declared vault root")
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError("symlink in declared vault path")
        if part == boundary:
            break


class IdentityIndex:
    def __init__(self, root, source, id_field, boundary=None):
        self.root, self.source, self.id_field = Path(root), source, id_field
        validate_tree_path(boundary or self.root, self.root)
        self.paths = {}
        # rglob does not normally recurse into directory symlinks. Reject them
        # explicitly so hidden owners cannot be bypassed by a later output path.
        if self.root.is_symlink() or any(path.is_symlink() for path in self.root.rglob("*")):
            raise ValueError("symlink in source output")
        for path in sorted(self.root.rglob("*.md")):
            if path.is_symlink():
                raise ValueError("symlink in source output")
            fields = frontmatter(path)
            identity = fields.get(id_field) if fields.get("source_system") == source else None
            if source == "getnote" and fields.get("source_system") in (None, "getnote"):
                # Recognize the plugin's exact API namespace, never title/fingerprint matches.
                canonical = fields.get("canonicalId", "")
                api_id = fields.get("source_ids.api")
                identity = identity or api_id or (canonical[4:] if canonical.startswith("api:") else None)
                if not identity and fields.get("note_type"):
                    identity = fields.get("note_id")
                if api_id and canonical.startswith("api:") and api_id != canonical[4:]:
                    raise ValueError("conflicting Get API identities")
            if identity:
                key = source_id(identity)
                if key in self.paths:
                    raise ValueError("duplicate source ID in existing files; reconcile before syncing")
                self.paths[key] = path

    def path(self, value, candidate):
        key = source_id(value)
        if key in self.paths:
            return self.paths[key]
        candidate = Path(candidate)
        if not candidate.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("identity path escapes source output")
        if candidate.exists() or candidate in self.paths.values():
            suffix = hashlib.sha256(key.encode()).hexdigest()[:16]
            candidate = candidate.with_name(candidate.stem + "-" + suffix + ".md")
        if candidate.exists() or candidate in self.paths.values():
            raise ValueError("unrecognized file occupies identity path")
        self.paths[key] = candidate
        return candidate


def write_if_changed(path, content):
    path = Path(path)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".sync-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True


def attachment_manifest(note):
    # Keep upstream asset metadata intact, including signed URLs, only in private output.
    return {key: note[key] for key in ("attachments", "images", "files", "resources", "audio") if key in note}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
