#!/usr/bin/env python3
"""Hybrid hierarchical retrieval over a local corpus of notes, transcripts, and chat.

Keeps every word in raw files. Cuts them into leaves that remember their exact
character range. Searches leaves and slightly larger windows together, then reads
outward from each hit (the hit plus one neighbour in the same parent). Never
returns a whole document. Optional lanes handle speaker questions, direct
messages, Hangul, and a meaning fallback when keyword ranking is weak.

Stdlib only. An optional embedding function can be supplied; without one, the
meaning lanes switch off cleanly. Python 3.10+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

WIN = 1000
DEFAULT_BUDGET = 8000
FLAT = 1.2
LANE_MEANING = 2
RRF_K = 60
STOP = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "is",
    "was", "were", "be", "been", "what", "which", "who", "whom", "whose", "how",
    "why", "when", "did", "do", "does", "after", "before", "about", "from",
    "with", "by", "as", "that", "this", "it", "its", "their", "they", "he",
    "she", "his", "her", "we", "our", "us", "you", "your", "into", "than",
    "then", "there", "here", "any", "some", "all", "each", "per", "vs", "via",
    "said", "say", "says", "ask", "asked", "get", "got", "need", "needs",
}
HAN = re.compile(r"[\uac00-\ud7a3]+")
PARTICLES = (
    "에서는", "으로는", "에게서", "까지는", "부터는", "이라고", "에서", "으로",
    "에게", "까지", "부터", "처럼", "보다", "하고", "이랑", "라고", "은", "는",
    "이", "가", "을", "를", "에", "의", "도", "로", "와", "과", "만", "요",
)
MON = {m: i + 1 for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split()
)}
TOD = {
    "early morning": (0, 7), "early-morning": (0, 7), "morning": (5, 12),
    "afternoon": (12, 17), "evening": (17, 23), "night": (19, 24),
    "overnight": (0, 6),
}
SAY = (
    r"(?:say|said|think|thought|mention|suggest|propose|want|wants|ask|claim|"
    r"complain|argue|tell|promise|hand|paste|note|report|commit|worry|worried|"
    r"flag|raise|push|pushed|recommend|warn|object|prefer|insist|admit|"
    r"concede|expect)"
)
MENTION_USER = re.compile(r"<@([A-Za-z0-9_]+)(\|[^>]*)?>")
MENTION_CHAN = re.compile(r"<#([A-Za-z0-9_]+)>")
URL_MARK = re.compile(r"<(https?://[^|>]+)(\|[^>]*)?>")


def terms(q: str) -> list[str]:
    return [
        t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-\.]*", q.lower())
        if t not in STOP and len(t) > 1
    ]


def fts_query(q: str) -> str:
    ts = [t.strip("'.-") for t in re.findall(
        r"[A-Za-z0-9\uac00-\ud7a3][A-Za-z0-9\uac00-\ud7a3'\-\.]*", q.lower()
    ) if t not in STOP and len(t) > 1]
    ts = [t.replace('"', "") for t in ts if t]
    return " OR ".join(f'"{t}"' for t in ts)


def dates_in(t: str) -> set[str]:
    ds: set[str] = set()
    for m in re.finditer(r"\b(20\d\d)-(\d\d)-(\d\d)\b", t):
        ds.add(f"{m.group(2)}-{m.group(3)}")
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b", t):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= 12 and 1 <= b <= 31:
            ds.add(f"{a:02d}-{b:02d}")
    for m in re.finditer(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2})\b",
        t.lower(),
    ):
        ds.add(f"{MON[m.group(1)]:02d}-{int(m.group(2)):02d}")
    return ds


def tod_in(q: str) -> tuple[int, int] | None:
    ql = q.lower()
    for key in sorted(TOD, key=len, reverse=True):
        if key in ql:
            return TOD[key]
    return None


def hour_of(t: str) -> int | None:
    m = re.search(r"\b(\d{1,2}):(\d\d)\s?(AM|PM)\b", t, re.I)
    if m:
        h = int(m.group(1)) % 12
        return h + 12 if m.group(3).upper() == "PM" else h
    m = re.search(r"\b20\d\d-\d\d-\d\d (\d\d):\d\d", t)
    if m:
        return int(m.group(1))
    return None


def ko_tokens(t: str) -> str:
    out: list[str] = []
    for w in HAN.findall(t):
        st = w
        for p in PARTICLES:
            if len(w) > len(p) + 1 and w.endswith(p):
                st = w[: -len(p)]
                break
        out.append("w_" + st)
        out += [f"b_{st[i:i+2]}" for i in range(len(st) - 1)] or ["b_" + st]
    return " ".join(out)


def spans_by(s: str, pat: str) -> list[tuple[int, int]]:
    idx = [m.start() for m in re.finditer(pat, s)]
    if not idx or idx[0] != 0:
        idx = [0] + idx
    return [(a, b) for a, b in zip(idx, idx[1:] + [len(s)]) if s[a:b].strip()]


def hash_embed(texts: Iterable[str], dim: int = 256) -> list[list[float]]:
    """Tiny character-trigram hash embedder. For tests and as a plug-in default."""
    vecs: list[list[float]] = []
    for text in texts:
        v = [0.0] * dim
        padded = f"  {text.lower()}  "
        for i in range(len(padded) - 2):
            h = int(hashlib.sha256(padded[i:i + 3].encode()).hexdigest(), 16)
            v[h % dim] += 1.0 if (h >> 8) % 2 == 0 else -1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        vecs.append([x / norm for x in v])
    return vecs


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class Index:
    """A SQLite FTS5 index over a raw directory. Incremental by file sha256."""

    def __init__(
        self,
        raw_dir: str | os.PathLike,
        db_path: str | os.PathLike | None = None,
        *,
        embed: Callable[[list[str]], list[list[float]]] | None = None,
        derived_prefixes: tuple[str, ...] | None = None,
    ):
        self.raw = Path(raw_dir)
        self.db_path = Path(db_path) if db_path else self.raw.parent / "memwalk.db"
        cfg = {}
        people_path = self.raw / "people.json"
        if people_path.is_file():
            cfg = json.loads(people_path.read_text())
        self.people: dict[str, str] = dict(cfg.get("people") or {})
        self.channels: dict[str, str] = dict(cfg.get("channels") or {})
        self.owner: str = cfg.get("owner") or next(iter(self.people.values()), "")
        self.first = {v.split()[0].lower(): v for v in self.people.values()}
        self.uid = {v: k for k, v in self.people.items()}
        prefixes = derived_prefixes or tuple(cfg.get("derived_prefixes") or ("derived/",))
        self.derived = prefixes
        self.embed_fn = embed
        self._canon: dict[tuple[str, str], dict | None] = {}
        self.last: dict = {}
        self._leaf_vec: dict[int, list[float]] | None = None
        self._ko_ids: list[int] | None = None

    def read(self, rel: str) -> str:
        return (self.raw / rel).read_text(encoding="utf-8", errors="replace")

    def raw_files(self) -> list[str]:
        out: list[str] = []
        for path in sorted(self.raw.rglob("*")):
            if path.is_file() and path.name != "people.json":
                out.append(str(path.relative_to(self.raw)))
        return out

    def resolve(self, t: str) -> str:
        t = MENTION_USER.sub(lambda m: "@" + self.people.get(m.group(1), m.group(1)), t)
        t = MENTION_CHAN.sub(lambda m: self.channels.get(m.group(1), m.group(1)), t)
        return URL_MARK.sub("<url>", t)

    def people_in(self, t: str) -> set[str]:
        tl = t.lower()
        ps: set[str] = set()
        for full in self.people.values():
            if full.lower() in tl or re.search(
                r"\b%s\b" % re.escape(full.split()[0].lower()), tl
            ):
                ps.add(full)
        return ps

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path)

    def _ensure_schema(self, c: sqlite3.Connection) -> None:
        c.execute(
            "create virtual table if not exists leaf using fts5("
            "text, hdr, file unindexed, s unindexed, e unindexed, "
            "parent unindexed, speaker unindexed, date unindexed, "
            "docdate unindexed, tokenize='porter unicode61')"
        )
        c.execute(
            "create virtual table if not exists win using fts5("
            "text, hdr, file unindexed, s unindexed, e unindexed, "
            "parent unindexed, date unindexed, docdate unindexed, "
            "seq unindexed, tokenize='porter unicode61')"
        )
        c.execute(
            "create table if not exists manifest("
            "file text primary key, sha256 text, nchars int)"
        )
        c.execute(
            "create table if not exists node("
            "id text primary key, level text, file text, s int, e int, "
            "parent text, header text)"
        )
        c.execute(
            "create virtual table if not exists ko using fts5("
            "tok, leafid unindexed, tokenize='unicode61')"
        )

    def _delete_file(self, c: sqlite3.Connection, rel: str) -> None:
        c.execute("delete from leaf where file=?", (rel,))
        c.execute("delete from win where file=?", (rel,))
        c.execute("delete from node where file=?", (rel,))
        c.execute("delete from manifest where file=?", (rel,))
        # ko rows are rebuilt for touched files after leaf insert

    def segment(self, rel: str, s: str):
        """Yield (start, end, parent_key, speaker, date)."""
        if rel.startswith("transcript/"):
            for a, b in spans_by(s, r"(?m)^\["):
                m = re.match(r"\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d)", s[a:b])
                sp = re.match(r"\[[^\]]+\] ([^:]+):", s[a:b])
                win = f"{m.group(4)}:{int(m.group(5)) // 3}" if m else "hdr"
                yield a, b, win, (sp.group(1) if sp else ""), (
                    f"{m.group(2)}-{m.group(3)}" if m else ""
                )
        elif rel.startswith("chat/"):
            for a, b in spans_by(s, r"(=== Message from |--- Reply |=== THREAD PARENT)"):
                blk = s[a:b]
                m = re.search(r"(20\d\d)-(\d\d)-(\d\d) \d\d:\d\d", blk)
                sp = re.search(r"(?:Message from|From:) ([^<(\n]+)", blk)
                parent = "thread" if "thread/" in rel else str(a)
                yield a, b, parent, (sp.group(1).strip() if sp else ""), (
                    f"{m.group(2)}-{m.group(3)}" if m else ""
                )
        elif rel.startswith("notes/"):
            sec = "top"
            for a, b in spans_by(s, r"(?m)^"):
                if not s[a].isspace():
                    sec = str(a)
                yield a, b, sec, "", ""
        else:
            sec = "top"
            for a, b in spans_by(s, r"(?m)^(?=\S)"):
                line = s[a:b]
                if line.startswith("#"):
                    sec = str(a)
                i = a
                while i < b:
                    j = min(b, i + 800)
                    if j < b:
                        k = s.rfind(" ", i, j)
                        j = k if k > i + 200 else j
                    yield i, j, sec, "", ""
                    i = j

    def doc_header(self, rel: str, s: str) -> str:
        first = s.split("\n", 1)[0][:200]
        if rel.startswith("chat/"):
            m = re.search(r"Channel: (#\S+)", s)
            ch = m.group(1) if m else ""
            if "thread/" in rel:
                key = rel.split("/")[-1].split("_")[0]
                ch = self.channels.get(key, ch)
            if "/dm/" in rel:
                key = rel.split("/")[-1].split("_")[0]
                ch = "DM with " + self.people.get(key, key)
            par = re.search(r"> \[?([^\n]{0,120})", s)
            return self.resolve(f"{ch} | {par.group(1) if par else ''}")
        if rel.startswith("transcript/"):
            linked = re.search(r"notes file ([A-Za-z0-9._-]+)", first)
            title = ""
            if linked:
                note = f"notes/{linked.group(1)}.md"
                if (self.raw / note).is_file():
                    title = self.read(note).split("\n", 1)[0]
            return self.resolve("Meeting transcript | " + title)
        return self.resolve(first)

    def _insert_file(self, c: sqlite3.Connection, rel: str) -> int:
        s = self.read(rel)
        hdr = self.doc_header(rel, s)
        dd = sorted(dates_in(s[:400]))
        docdate = dd[0] if dd else ""
        c.execute(
            "insert or replace into node values(?,?,?,?,?,?,?)",
            (rel, "T2", rel, 0, len(s), "", hdr),
        )
        c.execute(
            "insert or replace into manifest values(?,?,?)",
            (rel, hashlib.sha256(s.encode()).hexdigest(), len(s)),
        )
        n = 0
        for a, b, pk, sp, dt in self.segment(rel, s):
            txt = self.resolve(s[a:b])
            pid = f"{rel}#{pk}"
            if rel.startswith("notes/") and not sp:
                mm = re.match(r"\s*\* (?:\[ \] )?<@([A-Za-z0-9_]+)", s[a:b])
                if mm:
                    sp = self.people.get(mm.group(1), "")
            c.execute(
                "insert into leaf values(?,?,?,?,?,?,?,?,?)",
                (txt, hdr, rel, a, b, pid, sp, dt or docdate, docdate),
            )
            n += 1
        return n

    def _rebuild_windows(self, c: sqlite3.Connection, files: Iterable[str] | None = None) -> None:
        if files is not None:
            for rel in files:
                c.execute("delete from win where file=?", (rel,))
            file_set = set(files)
            rows = c.execute(
                "select text,hdr,file,s,e,parent,speaker,date,docdate from leaf "
                "order by file,parent,s"
            ).fetchall()
            rows = [r for r in rows if r[2] in file_set]
        else:
            c.execute("delete from win")
            rows = c.execute(
                "select text,hdr,file,s,e,parent,speaker,date,docdate from leaf "
                "order by file,parent,s"
            ).fetchall()
        cur = None
        seq = 0

        def flush(w):
            if w:
                c.execute(
                    "insert into win values(?,?,?,?,?,?,?,?,?)",
                    (w["t"], w["h"], w["f"], w["s"], w["e"], w["p"], w["d"], w["dd"], w["q"]),
                )

        for t, h, f, a, b, pid, sp, dt, dd in rows:
            if cur and (cur["p"] != pid or len(cur["t"]) + len(t) > WIN):
                flush(cur)
                cur = None
            if not cur:
                seq += 1
                cur = dict(t="", h=h, f=f, s=a, e=b, p=pid, d=dt, dd=dd, q=seq)
            cur["t"] += t
            cur["e"] = max(cur["e"], b)
        flush(cur)

    def _rebuild_ko(self, c: sqlite3.Connection, files: Iterable[str] | None = None) -> int:
        if files is None:
            c.execute("delete from ko")
            rows = c.execute("select rowid,text,file from leaf").fetchall()
        else:
            file_set = set(files)
            # FTS5 has no file column on ko; drop and rebuild those leaf ids
            # cheapest correct path: rebuild the whole ko table (tiny).
            c.execute("delete from ko")
            rows = c.execute("select rowid,text,file from leaf").fetchall()
            rows = rows  # full rebuild is cheap and keeps leafid in sync
        n = 0
        for rid, text, _f in rows:
            tok = ko_tokens(text)
            if tok:
                c.execute("insert into ko values(?,?)", (tok, rid))
                n += 1
        self._ko_ids = None
        return n

    def build(self, incremental: bool = True) -> dict:
        """Index raw files. Only re-segments files whose sha256 changed."""
        existed = self.db_path.is_file()
        c = self.connect()
        self._ensure_schema(c)
        current = set(self.raw_files())
        manifest = {
            f: (h, n) for f, h, n in c.execute("select file,sha256,nchars from manifest")
        }
        changed: list[str] = []
        if not incremental or not existed or not manifest:
            for rel in list(manifest):
                self._delete_file(c, rel)
            for rel in sorted(current):
                self._insert_file(c, rel)
                changed.append(rel)
            self._rebuild_windows(c, None)
            ko = self._rebuild_ko(c, None)
        else:
            for rel in list(manifest):
                if rel not in current:
                    self._delete_file(c, rel)
                    changed.append(rel)
            for rel in sorted(current):
                text = self.read(rel)
                sha = hashlib.sha256(text.encode()).hexdigest()
                if rel in manifest and manifest[rel][0] == sha:
                    continue
                self._delete_file(c, rel)
                self._insert_file(c, rel)
                changed.append(rel)
            touched = [f for f in changed if f in current]
            if touched or any(f not in current for f in changed):
                self._rebuild_windows(c, touched or None)
                ko = self._rebuild_ko(c, None)
            else:
                ko = c.execute("select count(*) from ko").fetchone()[0]
        c.commit()
        leaves = c.execute("select count(*) from leaf").fetchone()[0]
        c.close()
        self._leaf_vec = None
        self._canon.clear()
        return {
            "files": len(current),
            "leaves": leaves,
            "changed": changed,
            "ko_leaves": ko,
        }

    def search(self, q: str, k: int = 40, dates=None, people=None, files=None) -> list[dict]:
        fq = fts_query(q)
        if not fq:
            return []
        c = self.connect()
        rows = c.execute(
            "select rowid,file,s,e,parent,speaker,date,docdate,bm25(leaf,1.0,0.6),"
            "substr(text,1,200) from leaf where leaf match ? "
            "order by bm25(leaf,1.0,0.6) limit ?",
            (fq, k * 3),
        ).fetchall()
        c.close()
        out = []
        for r in rows:
            sc = -r[8]
            if dates and (r[6] in dates or r[7] in dates):
                sc *= 1.6
            if people and any(p.lower() in r[9].lower() for p in people):
                sc *= 1.15
            if files and not any(r[1].startswith(f) for f in files):
                continue
            out.append(dict(
                rowid=r[0], file=r[1], char_start=r[2], char_end=r[3],
                parent=r[4], speaker=r[5], date=r[6], score=sc, snippet=r[9],
            ))
        return sorted(out, key=lambda h: -h["score"])[:k]

    def search_win(self, q: str, k: int = 40, dates=None, people=None) -> list[dict]:
        fq = fts_query(q)
        if not fq:
            return []
        c = self.connect()
        rows = c.execute(
            "select rowid,file,s,e,parent,date,docdate,bm25(win,1.0,0.6),seq,text "
            "from win where win match ? order by bm25(win,1.0,0.6) limit ?",
            (fq, k * 3),
        ).fetchall()
        c.close()
        out = []
        for r in rows:
            sc = -r[7]
            if dates and (r[5] in dates or r[6] in dates):
                sc *= 1.6
            if people and any(p.lower() in r[9].lower() for p in people):
                sc *= 1.15
            out.append(dict(
                rowid=r[0], file=r[1], char_start=r[2], char_end=r[3],
                parent=r[4], seq=r[8], score=sc,
            ))
        return sorted(out, key=lambda h: -h["score"])[:k]

    def expand(self, h: dict, radius: int = 1) -> list[dict]:
        c = self.connect()
        rows = c.execute(
            "select rowid,file,s,e,text from leaf where parent=? order by s",
            (h["parent"],),
        ).fetchall()
        c.close()
        i = next((j for j, r in enumerate(rows) if r[0] == h["rowid"]), 0)
        return [
            dict(rowid=r[0], file=r[1], char_start=r[2], char_end=r[3], text=r[4])
            for r in rows[max(0, i - radius): i + radius + 1]
        ]

    def expand_win(self, h: dict, radius: int = 1) -> list[dict]:
        c = self.connect()
        rows = c.execute(
            "select rowid,file,s,e,text,seq from win where parent=? order by s",
            (h["parent"],),
        ).fetchall()
        c.close()
        i = next((j for j, r in enumerate(rows) if r[0] == h["rowid"]), 0)
        order = [i] + [
            j for d in range(1, radius + 1) for j in (i - d, i + d) if 0 <= j < len(rows)
        ]
        return [
            dict(rowid=rows[j][0], file=rows[j][1], char_start=rows[j][2],
                 char_end=rows[j][3], text=rows[j][4])
            for j in order
        ]

    def tree_up(self, file: str) -> str:
        c = self.connect()
        row = c.execute("select header from node where id=?", (file,)).fetchone()
        c.close()
        return row[0] if row else ""

    def read_loc(self, file: str, start: int, end: int) -> str:
        return self.read(file)[start:end]

    def _tod_boost(self, hits, rng, c):
        if not rng:
            return hits
        for h in hits:
            tbl = "leaf" if "speaker" in h else "win"
            t = c.execute(
                f"select substr(text,1,300) from {tbl} where rowid=?", (h["rowid"],)
            ).fetchone()[0]
            hr = hour_of(t)
            if hr is not None and rng[0] <= hr < rng[1]:
                h["score"] *= 1.5
        return sorted(hits, key=lambda h: -h["score"])

    def _canonical(self, file: str, text: str, c) -> dict | None:
        key = (file, text[:200])
        if key in self._canon:
            return self._canon[key]
        dt = set(terms(text))
        best = None
        if len(dt) >= 5:
            q = " OR ".join(f'"{t.replace(chr(34), "")}"' for t in sorted(dt, key=len, reverse=True)[:24])
            try:
                rows = c.execute(
                    "select rowid,file,s,e,parent,text from leaf where leaf match ? "
                    "order by bm25(leaf) limit 40",
                    (q,),
                )
            except sqlite3.OperationalError:
                rows = []
            for rid, f, a, b, par, txt in rows:
                if f.startswith(self.derived):
                    continue
                ratio = len(dt & set(terms(txt))) / len(dt)
                if ratio >= 0.4 and (not best or ratio > best[0]):
                    best = (ratio, dict(
                        rowid=rid, file=f, char_start=a, char_end=b,
                        parent=par, speaker="", score=0,
                    ))
        self._canon[key] = best[1] if best else None
        return self._canon[key]

    def retrieve_fused(
        self, q: str, budget: int = DEFAULT_BUDGET, radius: int = 1, K: int = 60,
        canon: bool = True, second: bool = False, tod: bool = True,
    ) -> list[dict]:
        """Reciprocal-rank fusion of leaves and windows, then a ±radius walk."""
        c = self.connect()
        ds, ps = dates_in(q), self.people_in(q)
        rng = tod_in(q) if tod else None
        L = self._tod_boost(self.search(q, k=K, dates=ds, people=ps), rng, c)
        W = self._tod_boost(self.search_win(q, k=K, dates=ds, people=ps), rng, c)
        units: dict = {}
        for r, h in enumerate(L):
            units.setdefault(("L", h["rowid"]), [0, h])[0] += 1 / (RRF_K + r)
        for r, h in enumerate(W):
            units.setdefault(("W", h["rowid"]), [0, h])[0] += 1 / (RRF_K + r)
        if canon:
            for key, (sc, h) in list(units.items()):
                if any(h["file"].startswith(p) for p in self.derived):
                    tbl = "leaf" if key[0] == "L" else "win"
                    t = c.execute(
                        f"select text from {tbl} where rowid=?", (h["rowid"],)
                    ).fetchone()[0]
                    prim = self._canonical(h["file"], t, c)
                    if prim:
                        units[key][0] *= 0.15
                        pk = ("L", prim["rowid"])
                        if pk not in units:
                            units[pk] = [sc, prim]
                        else:
                            units[pk][0] = max(units[pk][0], sc)
        ranked = sorted(units.items(), key=lambda kv: -kv[1][0])
        if second:
            topf: list[str] = []
            for (_, _), (sc, h) in ranked:
                if h["file"] not in topf:
                    topf.append(h["file"])
                if len(topf) == 2:
                    break
            extra = []
            for f in topf:
                hs = self._tod_boost(
                    self.search(q, k=3, dates=ds, people=ps, files=[f]), rng, c
                )
                extra += [(("L", h["rowid"]), [0, h]) for h in hs]
            ranked = ranked[:2] + extra + ranked[2:]
        out: list[dict] = []
        used = 0
        hdrs: set[str] = set()
        cover: list[tuple[str, int, int]] = []

        def covered(u):
            return any(f == u["file"] and a <= u["char_start"] and u["char_end"] <= b for f, a, b in cover)

        def add(u):
            nonlocal used
            if covered(u):
                return
            t = u["text"] if u["file"] in hdrs else self.tree_up(u["file"]) + " :: " + u["text"]
            if used + len(t) > budget:
                return
            hdrs.add(u["file"])
            used += len(t)
            cover.append((u["file"], u["char_start"], u["char_end"]))
            out.append(dict(file=u["file"], char_start=u["char_start"], char_end=u["char_end"], text=t))

        def walk(kind, h):
            if kind == "W":
                return self.expand_win(h, 0)
            us = self.expand(h, radius)
            ids = [u["rowid"] for u in us]
            if h["rowid"] in ids:
                i = ids.index(h["rowid"])
                us = [us[i]] + us[:i] + us[i + 1:]
            return us

        seenf: set[str] = set()
        later = []
        nfirst = 0
        for (kind, _), (sc, h) in ranked:
            if nfirst >= 6 or (h["file"] in seenf and kind != "L"):
                later.append((kind, h))
                continue
            seenf.add(h["file"])
            nfirst += 1
            for u in walk(kind, h):
                add(u)
        for kind, h in later:
            for u in walk(kind, h)[:1]:
                add(u)
            if used >= budget * 0.97:
                break
        c.close()
        return out

    def search_ko(self, q: str, k: int = 60) -> list[dict]:
        tok = ko_tokens(q)
        if not tok:
            return []
        c = self.connect()
        ids = [
            r[0] for r in c.execute(
                "select leafid from ko where ko match ? limit ?",
                (" OR ".join(tok.split()), k),
            )
        ]
        out = []
        for rid in ids:
            row = c.execute(
                "select rowid,file,s,e,parent,speaker,date from leaf where rowid=?",
                (rid,),
            ).fetchone()
            if row:
                out.append(dict(
                    rowid=row[0], file=row[1], char_start=row[2], char_end=row[3],
                    parent=row[4], speaker=row[5], date=row[6], score=0,
                ))
        c.close()
        return out

    def speaker_intent(self, q: str) -> str | None:
        ql = q.lower()
        cands: list[str] = []
        for m in re.finditer(r"\b(?:did|does|do|would|will)\s+([a-z]+)\s+" + SAY, ql):
            cands.append(m.group(1))
        for m in re.finditer(
            r"\b([a-z]+)'s\s+(?:complaint|point|view|take|suggestion|concern|update|claim)",
            ql,
        ):
            cands.append(m.group(1))
        for m in re.finditer(
            r"\b([a-z]+)\s+(?:said|says|thinks|suggested|proposed|claimed|"
            r"complained|wants|worried|pushed|flagged|warned|recommended)\b",
            ql,
        ):
            cands.append(m.group(1))
        for m in re.finditer(
            r"\b(?:was|is|were|are)\s+([a-z]+)\s+"
            r"(?:concerned|worried|unhappy|pushing|complaining|asking|arguing)",
            ql,
        ):
            cands.append(m.group(1))
        for m in re.finditer(r"\b(?:per|according to)\s+([a-z]+)\b", ql):
            cands.append(m.group(1))
        for n in cands:
            if n in ("i", "we"):
                return self.owner or None
            if n in self.first:
                return self.first[n]
        return None

    def dm_intent(self, q: str) -> str | None:
        ql = q.lower()
        m = re.search(r"\bdms?\s+with\s+([a-z]+)", ql)
        if m and m.group(1) in self.first:
            return self.first[m.group(1)]
        if re.search(r"\b(?:our|my|the|a)\s+dms?\b", ql):
            ps = [p for p in self.people_in(q) if p != self.owner]
            if len(ps) == 1:
                return ps[0]
        return None

    def search_speaker(self, q: str, name: str, k: int = 3, dates=None) -> list[dict]:
        first = name.split()[0]
        qq = " ".join(t for t in re.findall(r"\S+", q) if first.lower() not in t.lower())
        fq = fts_query(qq or q)
        if not fq:
            return []
        c = self.connect()
        try:
            rows = c.execute(
                "select rowid,file,s,e,parent,speaker,date,docdate,bm25(leaf,1.0,0.6) "
                "from leaf where leaf match ? and (speaker like ? or text like ?) "
                "order by bm25(leaf,1.0,0.6) limit 60",
                (fq, first + "%", "%* @" + first + "%"),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        c.close()
        hs = [
            dict(
                rowid=r[0], file=r[1], char_start=r[2], char_end=r[3], parent=r[4],
                speaker=r[5], date=r[6],
                score=-r[8] * (1.6 if dates and (r[6] in dates or r[7] in dates) else 1),
            )
            for r in rows
        ]
        return sorted(hs, key=lambda h: -h["score"])[:k]

    def search_dm(self, q: str, name: str, k: int = 3) -> list[dict]:
        uid = self.uid.get(name)
        return self.search(q, k=k, files=[f"chat/dm/{uid}_", f"chat/dm/{uid}."]) if uid else []

    def _rows(self, sql: str, args) -> list[dict]:
        c = self.connect()
        rows = c.execute(sql, args).fetchall()
        c.close()
        return [
            dict(rowid=r[0], file=r[1], char_start=r[2], char_end=r[3],
                 parent=r[4], speaker=r[5], date=r[6], score=0)
            for r in rows
        ]

    def speaker_pool(self, name: str, kw_hits: list[dict], dates) -> list[dict]:
        first = name.split()[0] + "%"
        if dates:
            ds = sorted(dates)
            marks = ",".join("?" * len(ds))
            return self._rows(
                f"select rowid,file,s,e,parent,speaker,date from leaf "
                f"where (speaker like ? or text like ?) and "
                f"(date in ({marks}) or docdate in ({marks})) limit 800",
                [first, "%* @" + first[:-1] + "%"] + ds + ds,
            )
        files = list(dict.fromkeys(h["file"] for h in kw_hits))[:3]
        if not files:
            return []
        marks = ",".join("?" * len(files))
        return self._rows(
            f"select rowid,file,s,e,parent,speaker,date from leaf "
            f"where (speaker like ? or text like ?) and file in ({marks}) limit 800",
            [first, "%* @" + first[:-1] + "%"] + files,
        )

    def dm_pool(self, name: str) -> list[dict]:
        uid = self.uid.get(name)
        if not uid:
            return []
        return self._rows(
            "select rowid,file,s,e,parent,speaker,date from leaf where file like ?",
            (f"chat/dm/{uid}%",),
        )

    def _ensure_vectors(self) -> dict[int, list[float]] | None:
        if self.embed_fn is None:
            return None
        if self._leaf_vec is not None:
            return self._leaf_vec
        c = self.connect()
        rows = c.execute("select rowid,text from leaf").fetchall()
        c.close()
        if not rows:
            self._leaf_vec = {}
            return self._leaf_vec
        vecs = self.embed_fn([r[1] for r in rows])
        self._leaf_vec = {int(r[0]): v for r, v in zip(rows, vecs)}
        return self._leaf_vec

    def _dense_scores(self, q: str, rowids: list[int]) -> dict[int, float]:
        vecs = self._ensure_vectors()
        if not vecs:
            raise RuntimeError("no embedder")
        qv = self.embed_fn([q])[0]
        return {r: _cosine(qv, vecs[r]) for r in rowids if r in vecs}

    def lane_rerank(self, q: str, kw_hits: list[dict], pool: list[dict]) -> list[dict]:
        cand = {h["rowid"]: h for h in pool}
        cand.update({h["rowid"]: h for h in kw_hits})
        try:
            ds = self._dense_scores(q, list(cand))
        except Exception:
            return kw_hits[:4]
        drank = {r: i for i, r in enumerate(sorted(ds, key=lambda r: -ds[r]))}
        krank = {h["rowid"]: i for i, h in enumerate(kw_hits)}
        sc = {
            r: 1 / (60 + drank.get(r, 999)) + 0.5 / (60 + krank.get(r, 999))
            for r in cand
        }
        rr = [cand[r] for r in sorted(sc, key=lambda r: -sc[r])[:8]]
        out = kw_hits[:2]
        seen = {h["rowid"] for h in out}
        nm = 0
        for h in rr:
            if nm >= LANE_MEANING:
                break
            if h["rowid"] not in seen:
                seen.add(h["rowid"])
                out.append(h)
                nm += 1
        return out

    def korean_source_hits(self, q: str, n: int = 2, kth: int = 10) -> list[dict]:
        vecs = self._ensure_vectors()
        if not vecs:
            return []
        c = self.connect()
        if self._ko_ids is None:
            self._ko_ids = [r[0] for r in c.execute("select leafid from ko")]
        qv = self.embed_fn([q])[0]
        scores = {rid: _cosine(qv, v) for rid, v in vecs.items()}
        if len(scores) < kth:
            cut = min(scores.values()) if scores else 0
        else:
            cut = sorted(scores.values(), reverse=True)[kth - 1]
        best = sorted(
            ((scores[r], r) for r in self._ko_ids if r in scores), reverse=True
        )[:n]
        out = []
        for v, r in best:
            if v < cut:
                continue
            row = c.execute(
                "select file,s,e,parent,speaker,date from leaf where rowid=?", (r,)
            ).fetchone()
            if row:
                out.append(dict(
                    rowid=r, file=row[0], char_start=row[1], char_end=row[2],
                    parent=row[3], speaker=row[4], date=row[5], score=float(v),
                ))
        c.close()
        return out

    def weak_keyword(self, q: str, hits: list[dict]) -> tuple[bool, str]:
        if HAN.search(q):
            return True, "hangul"
        if len(hits) < 10:
            return True, "few-hits"
        ratio = hits[0]["score"] / max(hits[9]["score"], 1e-9)
        return (ratio < FLAT), f"flat {ratio:.2f}"

    def retrieve(
        self,
        q: str,
        budget: int = DEFAULT_BUDGET,
        *,
        speaker: bool = True,
        dm: bool = True,
        kosrc: bool = True,
        ko: bool = True,
        fallback: bool = True,
        ndense: int = 3,
        rerank: bool = True,
        r10_dm_join: bool = True,
        **kw,
    ) -> list[dict]:
        """Production retrieve: fused keyword walk plus speaker, DM, Hangul, and meaning lanes."""
        c = self.connect()
        ds, ps = dates_in(q), self.people_in(q)
        pre: list[dict] = []
        why: list[str] = []
        if dm:
            n = self.dm_intent(q)
            if n:
                kh = self.search_dm(q, n, k=20)
                pre += self.lane_rerank(q, kh, self.dm_pool(n)) if rerank else kh[:3]
                why.append("dm:" + n)
        if speaker:
            n = self.speaker_intent(q)
            if n:
                kh = self.search_speaker(q, n, k=60, dates=ds)
                pre += self.lane_rerank(q, kh, self.speaker_pool(n, kh, ds)) if rerank else kh[:3]
                why.append("speaker:" + n)
                if dm and not self.dm_intent(q) and r10_dm_join:
                    for p in sorted(self.people_in(q) | {n}):
                        if p != self.owner and self.uid.get(p) and self.dm_pool(p):
                            kh2 = self.search_dm(q, p, k=20)
                            pre += self.lane_rerank(q, kh2, self.dm_pool(p)) if rerank else kh2[:3]
                            why.append("dm-join:" + p)
        if kosrc and not HAN.search(q):
            try:
                h = self.korean_source_hits(q)
                pre += h
                if h:
                    why.append("ko-source")
            except Exception:
                why.append("ko-source-unavailable")
        if ko and HAN.search(q):
            pre += self.search_ko(q, k=4)
            why.append("ko-fts")
        if fallback:
            L = self.search(q, k=12, dates=ds, people=ps)
            trig, w = self.weak_keyword(q, L)
            if trig:
                try:
                    vecs = self._ensure_vectors()
                    if vecs:
                        qv = self.embed_fn([q])[0]
                        ranked = sorted(
                            (( _cosine(qv, v), rid) for rid, v in vecs.items()),
                            reverse=True,
                        )[:20]
                        for sc, rid in ranked[:ndense]:
                            row = c.execute(
                                "select rowid,file,s,e,parent,speaker,date "
                                "from leaf where rowid=?",
                                (rid,),
                            ).fetchone()
                            if row:
                                pre.append(dict(
                                    rowid=row[0], file=row[1], char_start=row[2],
                                    char_end=row[3], parent=row[4], speaker=row[5],
                                    date=row[6], score=float(sc),
                                ))
                        why.append("dense:" + w)
                    else:
                        why.append("dense-unavailable")
                except Exception:
                    why.append("dense-unavailable")
        self.last = dict(q=q, why=why)
        base = self.retrieve_fused(q, budget=budget, **kw)
        if not pre:
            c.close()
            return base
        out: list[dict] = []
        used = 0
        cover: list[tuple[str, int, int]] = []

        def add(f, a, b, t):
            nonlocal used
            if any(f == F and A <= a and b <= B for F, A, B in cover) or used + len(t) > budget:
                return
            used += len(t)
            cover.append((f, a, b))
            out.append(dict(file=f, char_start=a, char_end=b, text=t))

        for x in base[:2]:
            add(x["file"], x["char_start"], x["char_end"], x["text"])
        for h in pre:
            if "parent" not in h:
                continue
            us = self.expand(h, 1)
            ids = [u["rowid"] for u in us]
            if h["rowid"] in ids:
                i = ids.index(h["rowid"])
                us = [us[i]] + us[:i] + us[i + 1:]
            for j, u in enumerate(us):
                add(
                    u["file"], u["char_start"], u["char_end"],
                    (self.tree_up(u["file"]) + " :: " if j == 0 else "") + u["text"],
                )
        for x in base[2:]:
            add(x["file"], x["char_start"], x["char_end"], x["text"])
        c.close()
        return out


def baseline_retrieve(index: Index, q: str, budget: int = DEFAULT_BUDGET) -> list[dict]:
    """Plain leaf BM25 fill, no walk, no lanes. Used as an eval baseline."""
    hits = index.search(q, k=40, dates=dates_in(q), people=index.people_in(q))
    out, used, hdrs = [], 0, set()
    c = index.connect()
    for h in hits:
        text = c.execute("select text from leaf where rowid=?", (h["rowid"],)).fetchone()[0]
        t = text if h["file"] in hdrs else index.tree_up(h["file"]) + " :: " + text
        if used + len(t) > budget:
            continue
        hdrs.add(h["file"])
        used += len(t)
        out.append(dict(file=h["file"], char_start=h["char_start"], char_end=h["char_end"], text=t))
    c.close()
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build or query a memwalk index.")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--raw", required=True)
    b.add_argument("--db")
    b.add_argument("--full", action="store_true", help="rebuild every file, ignoring shas")
    b.add_argument("--embed", action="store_true", help="use the built-in hash embedder")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--raw", required=True)
    s.add_argument("--db")
    s.add_argument("--k", type=int, default=8)
    r = sub.add_parser("retrieve")
    r.add_argument("query")
    r.add_argument("--raw", required=True)
    r.add_argument("--db")
    r.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    r.add_argument("--embed", action="store_true")
    args = p.parse_args(argv)
    embed = hash_embed if getattr(args, "embed", False) else None
    db = args.db
    idx = Index(args.raw, db, embed=embed)
    if args.cmd == "build":
        stats = idx.build(incremental=not args.full)
        print(json.dumps(stats, indent=1))
        return 0
    if args.cmd == "search":
        print(json.dumps(idx.search(args.query, k=args.k), indent=1, ensure_ascii=False))
        return 0
    hits = idx.retrieve(args.query, budget=args.budget)
    print(json.dumps(hits, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
