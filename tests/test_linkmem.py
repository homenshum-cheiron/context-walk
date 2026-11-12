import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "linked-memory"
spec = importlib.util.spec_from_file_location("linkmem", ROOT / "linkmem.py")
linkmem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(linkmem)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = linkmem.main([str(a) for a in argv])
    return code, (json.loads(out.getvalue()) if out.getvalue() else err.getvalue())


class LinkedMemoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ws = self.tmp / "ws"
        shutil.copytree(EXAMPLE, self.ws)
        shutil.rmtree(self.ws / "views", ignore_errors=True)

    def note(self, rel="topics/community/reading-group.md"):
        return self.ws / rel

    def staged(self):
        st = self.tmp / "staged"
        shutil.rmtree(st, ignore_errors=True)
        shutil.copytree(self.ws, st)
        return st

    def test_example_is_valid(self):
        self.assertEqual(linkmem.validate(self.ws), [])

    def test_views_follow_notes(self):
        summary = linkmem.build_views(self.ws)
        self.assertEqual(summary["superseded"], 1)
        graph = json.loads((self.ws / "views" / "graph.json").read_text())
        self.assertEqual(graph["venue-v1"]["superseded_by"], ["venue-v2"])
        self.assertIn("venue-v2", graph["priya"]["in"])
        self.assertTrue((self.ws / "views" / "timeline" / "2026" / "03" / "12.md").exists())
        self.assertTrue((self.ws / "views" / "timeline" / "2026" / "02.md").exists())
        recent = (self.ws / "views" / "recent.md").read_text()
        self.assertIn("^venue-v2", recent)
        self.assertNotIn("^venue-v1", recent)

    def test_rejects_paragraph_without_evidence(self):
        with self.note().open("a") as f:
            f.write("\nThe group also likes poetry. ^poetry\n")
        self.assertTrue(any("no evidence footnote" in e for e in linkmem.validate(self.ws)))

    def test_rejects_unknown_source_bad_time_and_broken_link(self):
        with self.note().open("a") as f:
            f.write("\nSee [old note](people.md#^nobody).[^z] ^bad\n\n[^z]: time: 2026-13; sources: R99\n")
        errors = " | ".join(linkmem.validate(self.ws))
        self.assertIn("source R99 not found", errors)
        self.assertIn("bad time", errors)
        self.assertIn("link to missing ^nobody", errors)

    def test_rejects_link_that_names_the_wrong_file(self):
        with self.note().open("a") as f:
            f.write("\nSnacks again by [Priya](reading-group.md#^priya).[^y] ^wrongfile\n\n[^y]: time: 2026-03; sources: R3\n")
        self.assertTrue(any("but ^priya is in" in e for e in linkmem.validate(self.ws)))

    def test_rejects_path_outside_workspace(self):
        with self.note().open("a") as f:
            f.write("\nOutside [x](../../../elsewhere.md#^priya).[^y] ^escape\n\n[^y]: time: 2026-03; sources: R3\n")
        self.assertTrue(any("leaves workspace" in e for e in linkmem.validate(self.ws)))

    def test_rejects_supersede_cycle(self):
        text = self.note().read_text().replace("[^a]: time: 2026-03-05; sources: R1",
                                               "[^a]: time: 2026-03-05; sources: R1; supersedes: venue-v2")
        self.note().write_text(text)
        self.assertTrue(any("cycle" in e for e in linkmem.validate(self.ws)))

    def test_core_capacity(self):
        self.assertTrue(any("over capacity" in e for e in linkmem.validate(self.ws, core_limit=50)))

    def test_install_refuses_changed_source_and_keeps_state(self):
        linkmem.build_views(self.ws)
        st = self.staged()
        rec = st / "source" / "records.jsonl"
        rec.write_text(rec.read_text().replace("Riverside Library", "Main Library"))
        before = self.note().read_text()
        result = linkmem.install(self.ws, st)
        self.assertFalse(result["installed"])
        self.assertTrue(any("source R1 was changed" in e for e in result["errors"]))
        self.assertEqual(self.note().read_text(), before)

    def test_install_refuses_silent_deletion(self):
        linkmem.build_views(self.ws)
        st = self.staged()
        note = st / "topics" / "community" / "reading-group.md"
        note.write_text(note.read_text().replace(
            "The March meeting is on March 18 at the Riverside Library.[^a] ^venue-v1\n", "").replace(
            "; supersedes: venue-v1", ""))
        result = linkmem.install(self.ws, st)
        self.assertFalse(result["installed"])
        self.assertTrue(any("^venue-v1 disappeared" in e for e in result["errors"]))

    def test_install_accepts_append_and_keeps_creation_order(self):
        linkmem.build_views(self.ws)
        st = self.staged()
        with (st / "source" / "records.jsonl").open("a") as f:
            f.write('{"id": "R5", "time": "2026-03-14", "text": "User: Priya cannot come; Sam will bring snacks."}\n')
        with (st / "topics" / "community" / "people.md").open("a") as f:
            f.write("\nSam is bringing snacks instead of Priya.[^s] ^snacks-sam\n\n[^s]: time: 2026-03-14; sources: R3, R5; supersedes: priya\n")
        result = linkmem.install(self.ws, st)
        self.assertTrue(result["installed"], result["errors"])
        order = json.loads((self.ws / "views" / "order.json").read_text())
        self.assertGreater(order["snacks-sam"], max(v for k, v in order.items() if k != "snacks-sam") - 1)
        self.assertTrue((self.ws / "views" / "recent.md").read_text().startswith("# Recent\n\n- Sam"))

    def test_walk_moves_from_old_paragraph_to_current_and_keeps_history(self):
        out = linkmem.walk(self.ws, ["venue-v1"])
        packet = out["packet"]
        self.assertTrue(packet.startswith("[CURRENT ^venue-v2"))
        self.assertIn("[EVIDENCE R2", packet)
        self.assertIn("[HISTORY ^venue-v1, superseded by ^venue-v2]", packet)
        self.assertIn("[LINKED ^priya", packet)

    def test_walk_from_a_raw_record_reaches_the_current_paragraph(self):
        packet = linkmem.walk(self.ws, ["R1"])["packet"]
        self.assertIn("[CURRENT ^venue-v2", packet)
        self.assertIn("[EVIDENCE R1", packet)

    def test_budget_cuts_whole_blocks(self):
        full = linkmem.walk(self.ws, ["venue-v2"])
        self.assertFalse(full["truncated"])
        first = full["packet"].split("\n\n[")[0]
        out = linkmem.walk(self.ws, ["venue-v2"], budget=len(first) + 5)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(out["chars"], len(first) + 5)
        self.assertTrue(out["packet"].startswith("[CURRENT ^venue-v2"))
        self.assertEqual(linkmem.walk(self.ws, ["venue-v2"], budget=10)["packet"], "")

    def test_search_marks_status(self):
        hits = linkmem.search(self.ws, "Riverside Library meeting", k=5)
        status = {h["id"]: h.get("status") for h in hits}
        self.assertEqual(status.get("venue-v1"), "superseded")

    def test_cli_exit_codes(self):
        code, out = run("validate", self.ws)
        self.assertEqual((code, out["valid"]), (0, True))
        with self.note().open("a") as f:
            f.write("\nNo id here.\n")
        code, out = run("validate", self.ws)
        self.assertEqual((code, out["valid"]), (1, False))
        code, err = run("views", self.ws)
        self.assertEqual(code, 2)
        self.assertIn("refused", err)


if __name__ == "__main__":
    unittest.main()
