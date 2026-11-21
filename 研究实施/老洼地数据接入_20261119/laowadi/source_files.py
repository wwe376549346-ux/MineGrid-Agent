"""Immutable archive source lookup shared by publication and evidence downloads."""

from pathlib import Path
import re

from laowadi.storage import sha


def verified_original(root, name, digest):
    if not name or Path(name).name != name or not re.fullmatch("[0-9a-f]{64}", digest):
        raise ValueError("无效归档原件名或哈希。")
    root = Path(root).resolve()
    for candidate in (root / digest / name, root / name):
        path = candidate.resolve()
        if path.is_relative_to(root) and path.is_file():
            content = path.read_bytes()
            if sha(content) == digest:
                return content
    raise ValueError(f"归档原件缺失或哈希不一致：{name}")


def document_manifest(db):
    return db.execute(
        "SELECT filename, sha256 FROM report UNION SELECT filename, sha256 FROM diagram ORDER BY filename"
    ).fetchall()

