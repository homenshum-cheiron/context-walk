#!/usr/bin/env python3
"""Linked memory notes: validate an edit, derive views, and walk links back to evidence. Offline; standard library only.

A workspace has three parts:

  source/records.jsonl   append-only evidence, one JSON object per line: {"id", "time", "text"}
  topics/**/*.md         editable notes; each paragraph ends with " ^id" and cites evidence in a footnote
  core.md                small, always-loaded note; same paragraph rules, size-bounded

A footnote definition reads:

  [^label]: time: 2026-08; sources: R1, R2; supersedes: old-id

`time` is YYYY, YYYY-MM or YYYY-MM-DD. `supersedes` is optional. Links to other paragraphs use
[text](other.md#^id) or [text](#^id). The writer decides grouping and wording; this file only enforces
structure and rebuilds views. It cannot tell whether a note is true.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile

UNIT_ID = re.compile(r"\s\^([A-Za-z0-9][A-Za-z0-9_-]{0,63})\s*$")
FOOTREF = re.compile(r"\[\^([^\]\s]+)\](?!:)")
FOOTDEF = re.compile(r"^\[\^([^\]\s]+)\]:\s*(.*)$")
LINK = re.compile(r"\]\(([^)\s]*)#\^([A-Za-z0-9_-]+)\)")
TIME = re.compile(r"^\d{4}(?:-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?)?$")
WORD = re.compile(r"[a-z0-9]+")
CORE_LIMIT = 2000
RECENT_K = 8


class Refused(Exception):
    pass


def _hash(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_sources(ws):
    path = Path(ws) / "source" / "records.jsonl"
    records = {}
    if not path.exists():
        return records
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if not isinstance(rec, dict) or not rec.get("id") or "text" not in rec:
            raise Refused(f"source line {n}: needs id and text")
        if rec["id"] in records:
            raise Refused(f"source line {n}: duplicate id {rec['id']}")
        records[rec["id"]] = rec
    return records


def parse_note(path, rel):
    """Return the paragraph units of one Markdown note and any structural errors."""
    text = Path(path).read_text(encoding="utf-8")
    defs, units, errors = {}, [], []
    blocks, current, heading = [], [], ""
    for line in text.split("\n") + [""]:
        m = FOOTDEF.match(line)
        if m:
            defs[m.group(1)] = m.group(2)
            continue
        if line.startswith("#"):
            if current:
                blocks.append((heading, current))
                current = []
            heading = line.lstrip("#").strip()
        elif line.strip():
            current.append(line)
        elif current:
            blocks.append((heading, current))
            current = []
    for heading, lines in blocks:
        body = " ".join(l.strip() for l in lines)
        m = UNIT_ID.search(body)
        if not m:
            errors.append(f"{rel}: paragraph without ^id: {body[:60]!r}")
            continue
        content = body[: m.start()].strip()
        notes = []
        for label in FOOTREF.findall(content):
            if label not in defs:
                errors.append(f"{rel}#^{m.group(1)}: footnote [^{label}] has no definition")
                continue
            fields = {}
            for part in defs[label].split(";"):
                if ":" in part:
                    k, v = part.split(":", 1)
                    fields[k.strip().lower()] = v.strip()
            notes.append({
                "label": label,
                "time": fields.get("time", ""),
                "sources": [s.strip() for s in fields.get("sources", "").split(",") if s.strip()],
                "supersedes": [s.strip().lstrip("^") for s in fields.get("supersedes", "").split(",") if s.strip()],
            })
        links = []
        for target, uid in LINK.findall(content):
            links.append({"file": target, "id": uid})
        units.append({
            "id": m.group(1), "file": rel, "heading": heading,
            "text": FOOTREF.sub("", content).strip(), "notes": notes, "links": links,
        })
    return units, errors


def collect(ws):
    ws = Path(ws)
    units, errors = {}, []
    files = sorted((ws / "topics").rglob("*.md")) if (ws / "topics").exists() else []
    if (ws / "core.md").exists():
        files.append(ws / "core.md")
    for path in files:
        rel = path.relative_to(ws).as_posix()
        found, errs = parse_note(path, rel)
        errors += errs
        for u in found:
            if u["id"] in units:
                errors.append(f"{rel}: duplicate ^{u['id']} (also in {units[u['id']]['file']})")
            else:
                units[u["id"]] = u
    return units, errors


def validate(ws, previous=None, core_limit=CORE_LIMIT):
    """Check a workspace. `previous` is the installed workspace the edit replaces, if any."""
    ws = Path(ws).resolve()
    errors = []
    for path in ws.rglob("*"):
        if path.is_symlink():
            errors.append(f"symlink not allowed: {path.relative_to(ws)}")
    try:
        sources = load_sources(ws)
    except (Refused, json.JSONDecodeError) as exc:
        return [f"source: {exc}"]
    units, errs = collect(ws)
    errors += errs
    for uid, u in units.items():
        if u["file"] != "core.md" and not u["notes"]:
            errors.append(f"{u['file']}#^{uid}: no evidence footnote")
        for note in u["notes"]:
            if not TIME.match(note["time"]):
                errors.append(f"{u['file']}#^{uid}: bad time {note['time']!r}")
            if not note["sources"]:
                errors.append(f"{u['file']}#^{uid}: footnote [^{note['label']}] cites no source")
            for s in note["sources"]:
                if s not in sources:
                    errors.append(f"{u['file']}#^{uid}: source {s} not found")
            for old in note["supersedes"]:
                if old == uid:
                    errors.append(f"{u['file']}#^{uid}: supersedes itself")
                elif old not in units:
                    errors.append(f"{u['file']}#^{uid}: supersedes missing ^{old}")
        for link in u["links"]:
            if link["id"] not in units:
                errors.append(f"{u['file']}#^{uid}: link to missing ^{link['id']}")
            elif link["file"]:
                target = (ws / Path(u["file"]).parent / link["file"]).resolve()
                if not target.is_relative_to(ws):
                    errors.append(f"{u['file']}#^{uid}: link leaves workspace")
                elif target.relative_to(ws).as_posix() != units[link["id"]]["file"]:
                    errors.append(f"{u['file']}#^{uid}: link names {link['file']} but ^{link['id']} is in {units[link['id']]['file']}")
        if u["file"] == "core.md" and not u["notes"] and not u["links"]:
            errors.append(f"core.md#^{uid}: core paragraph needs a source footnote or a link to a topic")
    graph = supersede_map(units)
    for uid in units:
        seen, cur = set(), uid
        while cur in graph["by"]:
            if cur in seen:
                errors.append(f"supersede cycle through ^{uid}")
                break
            seen.add(cur)
            cur = sorted(graph["by"][cur])[0]
    core = ws / "core.md"
    if core.exists() and len(core.read_text(encoding="utf-8")) > core_limit:
        errors.append(f"core.md over capacity: {len(core.read_text(encoding='utf-8'))} > {core_limit} characters")
    if previous is not None:
        prev = Path(previous)
        old_sources = load_sources(prev)
        for sid, rec in old_sources.items():
            if sid not in sources:
                errors.append(f"source {sid} was removed; source is append-only")
            elif _hash(rec) != _hash(sources[sid]):
                errors.append(f"source {sid} was changed; source is append-only")
        old_units, _ = collect(prev)
        replaced = {o for u in units.values() for n in u["notes"] for o in n["supersedes"]}
        for uid in old_units:
            if uid not in units and uid not in replaced:
                errors.append(f"^{uid} disappeared; keep it or mark it superseded")
    return sorted(set(errors))


def supersede_map(units):
    by = {}
    for uid, u in units.items():
        for n in u["notes"]:
            for old in n["supersedes"]:
                by.setdefault(old, set()).add(uid)
    return {"by": by}


def head(uid, by):
    seen = set()
    while uid in by and uid not in seen:
        seen.add(uid)
        uid = sorted(by[uid])[0]
    return uid


def build_views(ws, recent_k=RECENT_K):
    """Rebuild graph, timeline, recent and inventory from the notes. The writer never edits these."""
    ws = Path(ws)
    units, errors = collect(ws)
    if errors:
        raise Refused("; ".join(errors))
    sources = load_sources(ws)
    views = ws / "views"
    order_path = views / "order.json"
    order = json.loads(order_path.read_text()) if order_path.exists() else {}
    for uid in sorted(units, key=lambda i: (units[i]["file"], i)):
        if uid not in order:
            order[uid] = max(order.values(), default=0) + 1
    if views.exists():
        shutil.rmtree(views)
    views.mkdir()
    order_path.write_text(json.dumps(order, indent=1, sort_keys=True))
    by = supersede_map(units)["by"]
    graph = {}
    for uid, u in units.items():
        graph[uid] = {"file": u["file"], "out": sorted({l["id"] for l in u["links"]}), "in": [],
                      "supersedes": sorted({o for n in u["notes"] for o in n["supersedes"]}),
                      "superseded_by": sorted(by.get(uid, [])),
                      "sources": sorted({s for n in u["notes"] for s in n["sources"]})}
    for uid, g in graph.items():
        for target in g["out"]:
            graph[target]["in"].append(uid)
    (views / "graph.json").write_text(json.dumps(graph, indent=1, sort_keys=True))
    timeline = {}
    for uid, u in units.items():
        for n in u["notes"]:
            timeline.setdefault(n["time"], []).append((uid, u, n))
    for t, entries in timeline.items():
        parts = t.split("-")
        path = views / "timeline" / Path(*parts[:-1]) / f"{parts[-1]}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {t}", ""]
        for uid, u, n in sorted(entries, key=lambda e: e[0]):
            status = f" (superseded by ^{', ^'.join(graph[uid]['superseded_by'])})" if graph[uid]["superseded_by"] else ""
            lines.append(f"- {_short(u['text'])} [{u['file']}#^{uid}]{status} sources: {', '.join(n['sources'])}")
        path.write_text("\n".join(lines) + "\n")
    current = [u for u in units if not graph[u]["superseded_by"]]
    recent = sorted(current, key=lambda u: order[u], reverse=True)[:recent_k]
    lines = ["# Recent", ""] + [f"- {_short(units[u]['text'])} [{units[u]['file']}#^{u}] sources: {', '.join(graph[u]['sources'])}" for u in recent]
    (views / "recent.md").write_text("\n".join(lines) + "\n")
    inv = ["# Inventory", ""]
    for f in sorted({u["file"] for u in units.values()}):
        ids = [u for u in units if units[u]["file"] == f]
        inv.append(f"- {f}: {len(ids)} paragraphs, {sum(1 for i in ids if graph[i]['superseded_by'])} superseded")
    inv.append(f"- source/records.jsonl: {len(sources)} records")
    (views / "inventory.md").write_text("\n".join(inv) + "\n")
    return {"units": len(units), "superseded": sum(1 for g in graph.values() if g["superseded_by"]),
            "timeline_files": len(timeline), "recent": len(recent)}


def install(ws, staged, core_limit=CORE_LIMIT):
    """Validate a staged copy against the installed workspace, then swap it in. A refused edit changes nothing."""
    ws, staged = Path(ws), Path(staged)
    errors = validate(staged, previous=ws if (ws / "source").exists() else None, core_limit=core_limit)
    if errors:
        return {"installed": False, "errors": errors}
    tmp = Path(tempfile.mkdtemp(dir=ws.parent, prefix=f".{ws.name}-staging-"))
    for part in ("source", "topics", "core.md"):
        if (staged / part).is_dir():
            shutil.copytree(staged / part, tmp / part)
        elif (staged / part).exists():
            shutil.copy2(staged / part, tmp / part)
    if (ws / "views" / "order.json").exists():
        (tmp / "views").mkdir()
        shutil.copy2(ws / "views" / "order.json", tmp / "views" / "order.json")
    summary = build_views(tmp)
    backup = ws.parent / f".{ws.name}-previous"
    if backup.exists():
        shutil.rmtree(backup)
    if ws.exists():
        ws.rename(backup)
    tmp.rename(ws)
    if backup.exists():
        shutil.rmtree(backup)
    return {"installed": True, "errors": [], **summary}


def _short(text, n=160):
    text = re.sub(r"\]\([^)]*\)", "]", text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _tokens(text):
    return WORD.findall(text.lower())


def search(ws, query, k=5):
    """BM25 over paragraphs and source records. Each hit says whether its paragraph is current or superseded."""
    units, _ = collect(ws)
    sources = load_sources(ws)
    by = supersede_map(units)["by"]
    docs = [("unit", uid, u["heading"] + " " + u["text"]) for uid, u in units.items()]
    docs += [("source", sid, r["text"]) for sid, r in sources.items()]
    toks = [_tokens(d[2]) for d in docs]
    n = len(docs) or 1
    avg = sum(len(t) for t in toks) / n or 1
    df = {}
    for t in toks:
        for w in set(t):
            df[w] = df.get(w, 0) + 1
    q = _tokens(query)
    scored = []
    for (kind, did, _), t in zip(docs, toks):
        tf, s = {}, 0.0
        for w in t:
            tf[w] = tf.get(w, 0) + 1
        for w in q:
            if w in tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(t) / avg))
        if s > 0:
            hit = {"kind": kind, "id": did, "score": round(s, 3)}
            if kind == "unit":
                hit["status"] = "superseded" if did in by else "current"
            scored.append(hit)
    scored.sort(key=lambda h: -h["score"])
    return scored[:k]


def walk(ws, ids, hops=1, budget=6000):
    """Stitch a reading packet: the current version of each paragraph, its evidence, its neighbours, then history.

    A superseded paragraph is never returned alone: the walk moves to the paragraph that replaced it and keeps
    the old one as history. Records that only matched by search are added as raw evidence.
    """
    ws = Path(ws)
    units, _ = collect(ws)
    sources = load_sources(ws)
    by = supersede_map(units)["by"]
    back = {}
    for uid, u in units.items():
        for l in u["links"]:
            back.setdefault(l["id"], set()).add(uid)
    citing = {}
    for uid, u in units.items():
        for n in u["notes"]:
            for s in n["sources"]:
                citing.setdefault(s, set()).add(uid)
    expanded = []
    for start in ids:
        expanded.append(start)
        if start in sources:
            # A raw record may be out of date. Reach the paragraphs that cite it, so the walk can move to their
            # current version instead of showing old evidence alone.
            expanded += [u for u in sorted(citing.get(start, [])) if u not in expanded]
    order, history, cited, seen = [], [], [], set()
    for start in expanded:
        if start in sources:
            cited.append(start)
            continue
        if start not in units:
            continue
        cur = head(start, by)
        if cur != start:
            history.append(start)
        frontier = [cur]
        for depth in range(hops + 1):
            nxt = []
            for uid in frontier:
                if uid in seen:
                    continue
                seen.add(uid)
                order.append((uid, depth))
                if depth < hops:
                    nxt += [head(l["id"], by) for l in units[uid]["links"]] + sorted(back.get(uid, []))
            frontier = nxt
        for uid in list(seen):
            for n in units[uid]["notes"]:
                history += [o for o in n["supersedes"] if o not in history]
    parts, used, full = [], 0, False

    def add(block):
        # Blocks go in priority order; the first block that does not fit ends the packet, so a lower-priority
        # block never displaces a higher one.
        nonlocal used, full
        cost = len(block) + (1 if parts else 0)
        if full or used + cost > budget:
            full = True
            return False
        parts.append(block)
        used += cost
        return True

    for uid, depth in order:
        u = units[uid]
        times = ", ".join(sorted({n["time"] for n in u["notes"]})) or "none"
        srcs = sorted({s for n in u["notes"] for s in n["sources"]})
        tag = "CURRENT" if depth == 0 else "LINKED"
        if not add(f"[{tag} ^{uid} | {u['file']} > {u['heading']} | time {times}]\n{u['text']}\n"):
            break
        for s in srcs:
            if s not in cited:
                cited.append(s)
    for s in cited:
        r = sources.get(s)
        if r and not add(f"[EVIDENCE {s} | {r.get('time', '')}]\n{r['text']}\n"):
            break
    for old in history:
        if old in units and old not in seen:
            nb = sorted(by.get(old, []))
            if not add(f"[HISTORY ^{old}, superseded by ^{', ^'.join(nb)}]\n{units[old]['text']}\n"):
                break
    text = "\n".join(parts)
    return {"chars": len(text), "est_tokens": len(text) // 4, "truncated": full, "packet": text}


def packet(ws, query, k=4, hops=1, budget=6000):
    hits = search(ws, query, k=k)
    return {"hits": hits, **walk(ws, [h["id"] for h in hits], hops=hops, budget=budget)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("ws"); v.add_argument("--previous")
    v.add_argument("--core-limit", type=int, default=CORE_LIMIT)
    i = sub.add_parser("install"); i.add_argument("ws"); i.add_argument("staged")
    i.add_argument("--core-limit", type=int, default=CORE_LIMIT)
    b = sub.add_parser("views"); b.add_argument("ws"); b.add_argument("--recent", type=int, default=RECENT_K)
    s = sub.add_parser("search"); s.add_argument("ws"); s.add_argument("query"); s.add_argument("-k", type=int, default=5)
    w = sub.add_parser("walk"); w.add_argument("ws"); w.add_argument("ids", nargs="+")
    w.add_argument("--hops", type=int, default=1); w.add_argument("--budget", type=int, default=6000)
    p = sub.add_parser("packet"); p.add_argument("ws"); p.add_argument("query"); p.add_argument("-k", type=int, default=4)
    p.add_argument("--hops", type=int, default=1); p.add_argument("--budget", type=int, default=6000)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "validate":
            errors = validate(a.ws, previous=a.previous, core_limit=a.core_limit)
            out = {"valid": not errors, "errors": errors}
        elif a.cmd == "install":
            out = install(a.ws, a.staged, core_limit=a.core_limit)
        elif a.cmd == "views":
            out = build_views(a.ws, recent_k=a.recent)
        elif a.cmd == "search":
            out = {"hits": search(a.ws, a.query, k=a.k)}
        elif a.cmd == "walk":
            out = walk(a.ws, [x.lstrip("^") for x in a.ids], hops=a.hops, budget=a.budget)
        else:
            out = packet(a.ws, a.query, k=a.k, hops=a.hops, budget=a.budget)
    except (Refused, OSError, json.JSONDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 1 if out.get("valid") is False or out.get("installed") is False else 0


if __name__ == "__main__":
    sys.exit(main())
