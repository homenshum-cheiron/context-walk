#!/usr/bin/env python3
"""Append-only writer for a memwalk raw directory. Stdlib only.

Same content is a no-op. A whole-file rewrite moves the previous bytes to
`versions/` so old locators stay resolvable. Chat files are append-only: only
messages with a new `Message TS:` are added, and existing bytes must stay
identical (the writer restores and exits if they do not).

  refresh.py write <rel> <text_path>
  refresh.py append <rel> <text_path>   # chat/channel, chat/thread, or chat/dm
  refresh.py cursor get|set <key> [value]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

TS = re.compile(r"Message TS: (\d+\.\d+)")
SPLIT = {
    "channel": r"(?==== Message from )",
    "dm": r"(?==== Message from )",
    "thread": r"(?==== THREAD PARENT|--- Reply )",
}


def kind_of(rel: str) -> str:
    if "/thread/" in rel or rel.startswith("chat/thread/"):
        return "thread"
    if "/dm/" in rel or rel.startswith("chat/dm/"):
        return "dm"
    return "channel"


class Store:
    def __init__(self, raw_dir: str | os.PathLike, versions_dir: str | os.PathLike | None = None,
                 state_path: str | os.PathLike | None = None):
        self.raw = Path(raw_dir)
        self.versions = Path(versions_dir) if versions_dir else self.raw.parent / "versions"
        self.state_path = Path(state_path) if state_path else self.raw.parent / "refresh-state.json"

    def state(self) -> dict:
        if self.state_path.is_file():
            return json.loads(self.state_path.read_text())
        return {}

    def save_state(self, s: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")

    def write(self, rel: str, text: str) -> str:
        path = self.raw / rel
        new = hashlib.sha256(text.encode()).hexdigest()
        if path.is_file():
            old = path.read_text(encoding="utf-8", errors="replace")
            if hashlib.sha256(old.encode()).hexdigest() == new:
                return "unchanged"
            dest = self.versions / f"{rel}.{hashlib.sha256(old.encode()).hexdigest()[:8]}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, dest)
            status = "changed"
        else:
            status = "new"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
        return status

    def append_new(self, rel: str, incoming: str) -> str:
        """Add only messages whose timestamp is not already in the file."""
        path = self.raw / rel
        if not path.is_file():
            return self.write(rel, incoming)
        old = path.read_bytes()
        have = set(TS.findall(old.decode("utf-8", "replace")))
        kind = kind_of(rel)
        blocks = [b for b in re.split(SPLIT[kind], incoming) if TS.search(b)]
        new, seen = [], set()
        for block in blocks:
            ts = TS.search(block).group(1)
            if ts in have or ts in seen:
                continue
            seen.add(ts)
            new.append((float(ts), block.strip("\n")))
        if not new:
            return "unchanged"
        add = (
            (b"" if old.endswith(b"\n\n") else (b"\n" if old.endswith(b"\n") else b"\n\n"))
            + ("\n\n".join(b for _, b in sorted(new)) + "\n").encode()
        )
        with path.open("ab") as fh:
            fh.write(add)
        after = path.read_bytes()
        if after[: len(old)] != old:
            path.write_bytes(old)
            raise SystemExit("append changed existing bytes; restored " + rel)
        return f"appended:{len(new)}"


def main(argv: list[str] | None = None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 2 or a[0] in {"-h", "--help"}:
        print(__doc__)
        return 2
    raw = Path(os.environ.get("MEMWALK_RAW", "examples/synthetic-corpus"))
    store = Store(raw)
    cmd = a[0]
    if cmd == "write":
        print(store.write(a[1], Path(a[2]).read_text(encoding="utf-8", errors="replace")))
        return 0
    if cmd == "append":
        print(store.append_new(a[1], Path(a[2]).read_text(encoding="utf-8", errors="replace")))
        return 0
    if cmd == "cursor":
        s = store.state()
        if a[1] == "get":
            print(json.dumps(s.get(a[2])))
        else:
            s[a[2]] = a[3]
            store.save_state(s)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
