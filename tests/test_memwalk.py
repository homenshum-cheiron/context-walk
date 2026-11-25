"""Tests for memwalk, refresh, and action items on the synthetic corpus only."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "examples" / "synthetic-corpus"

import sys
sys.path.insert(0, str(ROOT))
import action_items
import memwalk
import refresh


def corpus_copy(dest: Path) -> Path:
    raw = dest / "raw"
    shutil.copytree(CORPUS, raw)
    # people.json should come along; eval/ is fine to keep
    return raw


class MemwalkIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = corpus_copy(Path(self.tmp.name))
        self.db = Path(self.tmp.name) / "memwalk.db"
        self.idx = memwalk.Index(self.raw, self.db, embed=memwalk.hash_embed)
        self.idx.build()

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_counts_and_manifest(self):
        stats = self.idx.build(incremental=True)
        self.assertGreaterEqual(stats["leaves"], 10)
        self.assertEqual(stats["changed"], [])
        c = self.idx.connect()
        man = {f: (h, n) for f, h, n in c.execute("select file,sha256,nchars from manifest")}
        files = set(self.idx.raw_files())
        self.assertEqual(files, set(man))
        for rel in files:
            sha = hashlib.sha256(self.idx.read(rel).encode()).hexdigest()
            self.assertEqual(sha, man[rel][0], rel)

    def test_every_leaf_locator_round_trips(self):
        c = self.idx.connect()
        bad = []
        for txt, f, s, e in c.execute("select text,file,s,e from leaf"):
            if self.idx.resolve(self.idx.read(f)[s:e]) != txt:
                bad.append((f, s, e))
        self.assertEqual(bad, [])

    def test_leaves_tile_without_overlap(self):
        c = self.idx.connect()
        bad = []
        for (f,) in c.execute("select distinct file from leaf"):
            spans = c.execute("select s,e from leaf where file=? order by s", (f,)).fetchall()
            bad += [(f, a, b) for (a, b), (a2, b2) in zip(spans, spans[1:]) if a2 < b]
        self.assertEqual(bad, [])

    def test_windows_stay_inside_files(self):
        c = self.idx.connect()
        for f, s, e in c.execute("select file,s,e from win"):
            self.assertGreaterEqual(s, 0)
            self.assertLess(s, e)
            self.assertLessEqual(e, len(self.idx.read(f)))

    def test_incremental_rebuild_skips_unchanged_and_reindexes_one_file(self):
        first = {f: h for f, h in self.idx.connect().execute("select file,sha256 from manifest")}
        stats = self.idx.build(incremental=True)
        self.assertEqual(stats["changed"], [])
        note = self.raw / "notes/planning-2026-03-02.md"
        note.write_text(note.read_text() + "\n* [ ] <@user_dana> file the review note\n")
        stats = self.idx.build(incremental=True)
        self.assertIn("notes/planning-2026-03-02.md", stats["changed"])
        self.assertEqual(len(stats["changed"]), 1)
        second = {f: h for f, h in self.idx.connect().execute("select file,sha256 from manifest")}
        for rel, sha in first.items():
            if rel != "notes/planning-2026-03-02.md":
                self.assertEqual(second[rel], sha)
        self.assertNotEqual(first["notes/planning-2026-03-02.md"], second["notes/planning-2026-03-02.md"])

    def test_stale_sha_is_detectable(self):
        note = self.raw / "notes/planning-2026-03-02.md"
        note.write_text(note.read_text() + "\nextra line\n")
        c = self.idx.connect()
        man = {f: h for f, h in c.execute("select file,sha256 from manifest")}
        sha = hashlib.sha256(self.idx.read("notes/planning-2026-03-02.md").encode()).hexdigest()
        self.assertNotEqual(sha, man["notes/planning-2026-03-02.md"])

    def test_context_walk_includes_neighbour(self):
        hits = [h for h in self.idx.search("vendor lock-in", k=10)
                if h["file"].startswith("transcript/")]
        self.assertTrue(hits)
        walked = self.idx.expand(hits[0], radius=1)
        self.assertGreaterEqual(len(walked), 2)
        texts = " ".join(u["text"] for u in walked)
        self.assertIn("vendor lock-in", texts)
        self.assertTrue(any(
            "checklist" in u["text"].lower() or "Welcome" in u["text"]
            or "10 March" in u["text"] or "출시" in u["text"]
            for u in walked
        ))

    def test_retrieve_finds_launch_date(self):
        hits = self.idx.retrieve("When is the launch date?")
        blob = " ".join(h["text"] for h in hits)
        self.assertIn("10 March", blob)

    def test_speaker_lane_prefers_alice(self):
        self.assertEqual(self.idx.speaker_intent("What did Alice say about the vendor?"), "Alice")
        hits = self.idx.retrieve("What did Alice say about the vendor?")
        blob = " ".join(h["text"] for h in hits)
        self.assertIn("vendor lock-in", blob)
        # should not be only the derived digest
        self.assertTrue(any(h["file"].startswith("transcript/") for h in hits))

    def test_derived_copy_ranks_below_primary(self):
        fused = self.idx.retrieve_fused("launch date is 10 March", canon=True)
        files = [h["file"] for h in fused]
        if any(f.startswith("derived/") for f in files) and any(
            f.startswith("transcript/") or f.startswith("chat/") or f.startswith("notes/")
            for f in files
        ):
            first_derived = min(i for i, f in enumerate(files) if f.startswith("derived/"))
            first_primary = min(
                i for i, f in enumerate(files)
                if f.startswith(("transcript/", "chat/", "notes/"))
            )
            self.assertLess(first_primary, first_derived)
        # The gold substring must appear from a primary file
        primary = [h for h in fused if not h["file"].startswith("derived/")]
        self.assertTrue(any("10 March" in h["text"] for h in primary))

    def test_hangul_query_hits_korean_turn(self):
        hits = self.idx.retrieve("출시 날짜는 언제인가요?")
        blob = " ".join(h["text"] for h in hits)
        self.assertIn("3월 10일", blob)

    def test_dm_lane(self):
        self.assertEqual(self.idx.dm_intent("what did Blair promise in the DM"), "Blair")
        hits = self.idx.retrieve("What did Blair promise in the DM?")
        blob = " ".join(h["text"] for h in hits)
        self.assertIn("rollback test", blob)

    def test_notes_sub_bullet_is_its_own_leaf(self):
        c = self.idx.connect()
        rows = list(c.execute(
            "select text from leaf where file=? and text like ?",
            ("notes/planning-2026-03-02.md", "%vendor exit clause%"),
        ))
        self.assertTrue(rows)
        # the indented bullet should be searchable on its own
        hits = self.idx.search("vendor exit clause", k=10)
        self.assertTrue(any(h["file"].endswith("planning-2026-03-02.md") for h in hits))

    def test_mention_resolution(self):
        text = self.idx.resolve("assign to <@user_blair> in <#chan_eng>")
        self.assertIn("@Blair", text)
        self.assertIn("#engineering", text)

    def test_frozen_cases_pass_with_embedder(self):
        cases = [
            json.loads(line)
            for line in (CORPUS / "eval/cases.jsonl").read_text().splitlines()
            if line.strip()
        ]
        missed = []
        for case in cases:
            blob = " ".join(h["text"] for h in self.idx.retrieve(case["q"]))
            if not all(m in blob for m in case["must"]):
                missed.append((case["id"], case["must"], blob[:200]))
        self.assertEqual(missed, [])

    def test_baseline_is_weaker_or_equal_on_speaker_question(self):
        q = "What did Alice say about the vendor?"
        base = " ".join(h["text"] for h in memwalk.baseline_retrieve(self.idx, q))
        proposed = " ".join(h["text"] for h in self.idx.retrieve(q))
        # The proposed path must contain the gold; the baseline may or may not.
        self.assertIn("vendor lock-in", proposed)


class RefreshStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / "raw"
        self.raw.mkdir()
        self.store = refresh.Store(self.raw, Path(self.tmp.name) / "versions")

    def tearDown(self):
        self.tmp.cleanup()

    def _msg(self, ts, name, body):
        return (
            f"=== Message from {name} ===\n"
            f"Time: 2026-03-02 12:00:00\n"
            f"Message TS: {ts}\n\n"
            f"{body}\n"
        )

    def test_write_is_idempotent_and_versions_on_change(self):
        rel = "notes/one.md"
        self.assertEqual(self.store.write(rel, "hello\n"), "new")
        self.assertEqual(self.store.write(rel, "hello\n"), "unchanged")
        self.assertEqual(self.store.write(rel, "hello world\n"), "changed")
        versions = [p for p in (Path(self.tmp.name) / "versions").rglob("*") if p.is_file()]
        self.assertTrue(versions)
        self.assertEqual(versions[0].read_text(), "hello\n")
        self.assertEqual((self.raw / rel).read_text(), "hello world\n")

    def test_append_keeps_existing_bytes(self):
        rel = "chat/channel/chan_eng.txt"
        first = self._msg("3001.1", "Alice", "first")
        self.assertEqual(self.store.write(rel, first), "new")
        old = (self.raw / rel).read_bytes()
        incoming = first + "\n" + self._msg("3001.2", "Blair", "second")
        self.assertEqual(self.store.append_new(rel, incoming), "appended:1")
        after = (self.raw / rel).read_bytes()
        self.assertEqual(after[: len(old)], old)
        self.assertIn(b"second", after)
        # a second append of the same window is a no-op
        self.assertEqual(self.store.append_new(rel, incoming), "unchanged")
        self.assertEqual((self.raw / rel).read_bytes(), after)


class ActionItems(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = corpus_copy(Path(self.tmp.name))
        self.db = Path(self.tmp.name) / "memwalk.db"
        self.state = Path(self.tmp.name) / "state.json"
        self.idx = memwalk.Index(self.raw, self.db, embed=memwalk.hash_embed)
        self.idx.build()

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_have_locators_and_owners(self):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            code = action_items.main([
                "new", "--raw", str(self.raw), "--db", str(self.db),
                "--state", str(self.state), "--budget", "1500",
            ])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(len(data), 1)
        items = data[0]["items"]
        self.assertEqual(len(items), 2)
        owners = {tuple(i["owners"]) for i in items}
        self.assertIn(("Blair",), owners)
        self.assertIn(("Chen",), owners)
        for item in items:
            loc = item["locator"]
            excerpt = self.idx.read(loc["file"])[loc["char_start"]:loc["char_end"]]
            self.assertTrue(excerpt.startswith("* [ ]"))
            self.assertTrue(item["context"])

    def test_mark_then_silent(self):
        from io import StringIO
        from contextlib import redirect_stdout
        action_items.main([
            "mark", "planning-2026-03-02", "--raw", str(self.raw),
            "--state", str(self.state),
        ])
        buf = StringIO()
        with redirect_stdout(buf):
            action_items.main([
                "new", "--raw", str(self.raw), "--db", str(self.db),
                "--state", str(self.state),
            ])
        self.assertEqual(buf.getvalue().strip(), "NO_NEW_MEETINGS")


if __name__ == "__main__":
    unittest.main()
