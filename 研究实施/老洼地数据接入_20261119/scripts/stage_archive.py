"""Stage the approved source snapshot and immutable originals for a read-only mount."""

import argparse
from contextlib import closing
from pathlib import Path
import shutil

from laowadi.monitor import EVENT_DB
from laowadi.source_files import document_manifest, verified_original
from laowadi.standards import REGULATION_TXT
from laowadi.storage import DB, ROOT, connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--events-db", type=Path, default=EVENT_DB)
    parser.add_argument("--originals", type=Path, default=ROOT.parent.parent / "项目知识库" / "资料" / "老洼地")
    args = parser.parse_args()
    root = args.output
    if root.exists() and any(root.iterdir()):
        parser.error("目标目录必须为空")
    with closing(connect(args.db)) as db:
        manifest = [
            (row["filename"], row["sha256"], verified_original(args.originals, row["filename"], row["sha256"]))
            for row in document_manifest(db)
        ]
    for source in (args.db, args.events_db):
        with closing(connect(source)) as db:
            if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                parser.error("输入 SQLite 完整性校验失败")
    seed = root / "seed"
    seed.mkdir(parents=True)
    shutil.copyfile(args.db, seed / "laowadi.sqlite3")
    shutil.copyfile(args.events_db, seed / "laowadi_events.sqlite3")
    for name, digest, content in manifest:
        target = root / "sources" / digest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if REGULATION_TXT.is_file():
        target = root / "standards" / "核验记录" / REGULATION_TXT.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REGULATION_TXT, target)
    print(f"已准备两份只读种子库和 {len(manifest)} 份已验哈希原件。")


if __name__ == "__main__":
    main()

