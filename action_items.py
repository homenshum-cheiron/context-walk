#!/usr/bin/env python3
"""Extract new meeting action items and ground each one in the index.

Reads notes files for lines matching `* [ ]`. Skips items already marked in
the state file. Each item carries its exact locator and a short retrieve()
context. Prints `NO_NEW_MEETINGS` when there is nothing new. Drafts only;
this script sends nothing.

  action_items.py new --raw DIR [--db PATH] [--budget 1500] [--state PATH]
  action_items.py mark NOTE_ID...
  action_items.py seed
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from memwalk import DEFAULT_BUDGET, Index, hash_embed


ITEM = re.compile(r"(?m)^\* \[ \] (.*)$")


def note_ids(raw: Path) -> list[str]:
    notes = raw / "notes"
    if not notes.is_dir():
        return []
    return sorted(p.stem for p in notes.glob("*.md"))


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def save_state(path: Path, s: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extract new meeting action items.")
    p.add_argument("cmd", choices=["new", "mark", "seed"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--raw", default="examples/synthetic-corpus")
    p.add_argument("--db")
    p.add_argument("--state")
    p.add_argument("--budget", type=int, default=1500)
    p.add_argument("--embed", action="store_true")
    args = p.parse_args(argv)
    raw = Path(args.raw)
    state_path = Path(args.state) if args.state else raw.parent / "refresh-state.json"
    s = load_state(state_path)
    done = set(s.get("actions_done", []))
    if args.cmd == "seed":
        s["actions_done"] = note_ids(raw)
        save_state(state_path, s)
        print("seeded", len(s["actions_done"]))
        return 0
    if args.cmd == "mark":
        s["actions_done"] = sorted(done | set(args.ids))
        save_state(state_path, s)
        print("marked", args.ids)
        return 0
    idx = Index(raw, args.db, embed=hash_embed if args.embed else None)
    if not Path(idx.db_path).is_file():
        idx.build()
    out = []
    transcripts = s.get("transcripts", {})
    for nid in note_ids(raw):
        if nid in done:
            continue
        rel = f"notes/{nid}.md"
        txt = idx.read(rel)
        title = txt.split("\n", 1)[0]
        linked = [f"transcript/{t}.txt" for t, note in transcripts.items() if note == nid]
        # also accept a header that names the transcript
        items = []
        for m in ITEM.finditer(txt):
            line = idx.resolve(m.group(1))
            owners = re.findall(r"@([A-Z][\w]*(?: [A-Z][\w]*)?)", line)
            ctx = idx.retrieve(line, budget=args.budget)
            items.append(dict(
                text=line,
                owners=owners,
                locator=dict(file=rel, char_start=m.start(), char_end=m.end()),
                context=[
                    dict(file=x["file"], char_start=x["char_start"],
                         char_end=x["char_end"], text=x["text"][:600])
                    for x in ctx[:4]
                ],
            ))
        out.append(dict(note=nid, title=idx.resolve(title),
                        transcript_files=linked, items=items))
    if out:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("NO_NEW_MEETINGS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
