import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "synthetic-guideline.md"
REAL = ROOT / "examples" / "21-cfr-part-314.md"
spec = importlib.util.spec_from_file_location("docwalk", ROOT / "docwalk.py")
docwalk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docwalk)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = docwalk.main([str(a) for a in argv])
    return code, (json.loads(out.getvalue()) if code == 0 else err.getvalue())


class DocwalkTests(unittest.TestCase):
    def setUp(self):
        self.text = SAMPLE.read_text(encoding="utf-8")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, text):
        path = self.tmp / "doc.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_outline_nests_and_parent_covers_children(self):
        code, out = run("outline", SAMPLE)
        self.assertEqual(code, 0)
        by_id = {s["id"]: s for s in out["sections"]}
        self.assertEqual(by_id["1.2.2"]["title"], "2.2 Moderate changes")
        self.assertEqual(by_id["1.2.2"]["path"].split(" > ")[1], "2 Change categories")
        self.assertLessEqual(by_id["1.2"]["char_start"], by_id["1.2.1"]["char_start"])
        self.assertGreaterEqual(by_id["1.2"]["char_end"], by_id["1.2.3"]["char_end"])
        self.assertEqual(out["source"], "headings")

    def test_outline_depth_limits_rows(self):
        _, out = run("outline", SAMPLE, "--depth", 2)
        self.assertTrue(all(s["level"] <= 2 for s in out["sections"]))

    def test_find_reports_section_and_exact_offsets(self):
        _, out = run("find", SAMPLE, "B.4.c")
        self.assertEqual(out["total"], 2)
        first = out["hits"][0]
        self.assertEqual(first["section"], "1.2.2")
        self.assertEqual(self.text[first["char_start"]:first["char_end"]], "B.4.c")

    def test_find_literal_is_not_a_regex(self):
        _, out = run("find", self.write("a.c\nabc\n"), "a.c")
        self.assertEqual(out["total"], 1)

    def test_find_limit_keeps_true_total(self):
        _, out = run("find", SAMPLE, "change", "--ignore-case", "--limit", 2)
        self.assertEqual(out["shown"], 2)
        self.assertGreater(out["total"], 2)

    def test_read_section_is_verbatim_at_reported_offsets(self):
        _, out = run("read", SAMPLE, "--section", "1.2.2")
        self.assertEqual(out["text"], self.text[out["char_start"]:out["char_end"]])
        self.assertIn("B.4.c", out["text"])
        self.assertNotIn("2.3 Major changes", out["text"])
        self.assertFalse(out["truncated"])

    def test_budget_cuts_at_a_line_and_next_reads_the_rest(self):
        _, whole = run("read", SAMPLE, "--section", "1.2", "--max-chars", 100000)
        pieces, cursor = [], ("--section", "1.2")
        for _ in range(50):
            _, out = run("read", SAMPLE, *cursor, "--max-chars", 200)
            self.assertLessEqual(len(out["text"]), 200)
            pieces.append(out["text"])
            if not out["truncated"]:
                break
            cursor = ("--chars", out["next"]["chars"])
        self.assertEqual("".join(pieces), whole["text"])
        self.assertGreater(len(pieces), 2)

    def test_continued_read_reports_its_own_line_and_section(self):
        lines = self.text.split("\n")
        cursor, seen_heading_piece = ("--section", "1.2"), False
        for _ in range(50):
            _, out = run("read", SAMPLE, *cursor, "--max-chars", 200)
            first = out["text"].split("\n")[0]
            self.assertEqual(lines[out["line_start"] - 1], first)
            if first.startswith("#"):
                seen_heading_piece = True
                self.assertTrue(out["path"].endswith(first.lstrip("# ")), out["path"])
            if not out["truncated"]:
                break
            cursor = ("--chars", out["next"]["chars"])
        self.assertTrue(seen_heading_piece)

    def test_one_long_line_is_cut_mid_line_and_still_read_once(self):
        path = self.write("x" * 450)
        pieces, cursor = [], ("--lines", "1:1")
        for _ in range(10):
            _, out = run("read", path, *cursor, "--max-chars", 200)
            pieces.append(out["text"])
            if not out["truncated"]:
                break
            cursor = ("--chars", out["next"]["chars"])
        self.assertEqual([len(p) for p in pieces], [200, 200, 50])

    def test_reversed_section_range_is_refused(self):
        code, message = run("read", SAMPLE, "--section", "1.2.3", "--to-section", "1.2.2")
        self.assertEqual(code, 2)
        self.assertIn("swap", message)

    def test_regex_anchor_matches_line_starts(self):
        _, out = run("find", SAMPLE, "^### ", "--regex")
        self.assertEqual(out["total"], 5)

    def test_byte_order_mark_does_not_hide_the_first_heading(self):
        path = self.tmp / "bom.md"
        path.write_bytes(b"\xef\xbb\xbf# Title\ntext\n")
        _, out = run("outline", path)
        self.assertEqual(out["source"], "headings")

    def test_real_regulation_section_holds_rule_and_exception(self):
        _, hit = run("find", REAL, "Relaxation of an acceptance criterion")
        self.assertEqual(hit["total"], 1)
        _, out = run("read", REAL, "--line", hit["hits"][0]["line"], "--max-chars", 20000)
        self.assertTrue(out["text"].startswith("#### § 314.70 "))
        self.assertIn("in the specifications provided in the approved NDA", out["text"])
        self.assertFalse(out["truncated"])
        self.assertEqual(out["text"], REAL.read_text(encoding="utf-8")[out["char_start"]:out["char_end"]])

    def test_line_scope_widens_from_section_to_parent(self):
        line = self.text.split("\n").index("| B.4.c | Widening of an approved specification limit | Major |") + 1
        _, near = run("read", SAMPLE, "--line", line, "--scope", "lines", "--before", 1, "--after", 0)
        _, section = run("read", SAMPLE, "--line", line)
        _, parent = run("read", SAMPLE, "--line", line, "--scope", "parent")
        self.assertEqual(near["text"].count("\n"), 1)
        self.assertTrue(section["text"].startswith("### 2.2"))
        self.assertTrue(parent["text"].startswith("## 2 Change categories"))
        self.assertIn("2.3 Major changes", parent["text"])

    def test_section_range_reads_both_ends(self):
        _, out = run("read", SAMPLE, "--section", "1.2.2", "--to-section", "1.2.3")
        self.assertIn("### 2.2", out["text"])
        self.assertIn("even though the table sits under moderate", out["text"])

    def test_pages_split_on_form_feed(self):
        path = self.write("page one\n\fpage two\n\fpage three\n")
        _, out = run("read", path, "--pages", "2:2")
        self.assertEqual(out["text"], "page two\n\f")
        self.assertEqual((out["page_start"], out["page_end"]), (2, 2))
        code, _ = run("read", path, "--pages", "4:4")
        self.assertEqual(code, 2)

    def test_no_headings_falls_back_to_labelled_windows(self):
        path = self.write("\n".join(f"row {i}" for i in range(200)))
        _, out = run("outline", path)
        self.assertEqual(out["source"], "windows")
        self.assertEqual(len(out["sections"]), 3)

    def test_numbered_headings_only_when_asked(self):
        path = self.write("1 Scope\ntext here.\n1.1 Limits\nmore text.\n2 Filing\nlast.\n")
        _, plain = run("outline", path)
        _, numbered = run("outline", path, "--numbered")
        self.assertEqual(plain["source"], "windows")
        self.assertEqual([s["id"] for s in numbered["sections"]], ["1", "1.1", "2"])

    def test_heading_inside_code_fence_is_ignored(self):
        path = self.write("# Real\n```\n# not a heading\n```\n")
        _, out = run("outline", path)
        self.assertEqual(len(out["sections"]), 1)

    def test_bad_requests_exit_2_with_a_reason(self):
        for argv in (("read", SAMPLE, "--section", "9.9"), ("read", SAMPLE), ("read", SAMPLE, "--lines", "5:2"),
                     ("read", SAMPLE, "--section", "1", "--lines", "1:2"), ("find", SAMPLE, "(", "--regex"),
                     ("outline", self.tmp / "missing.md"), ("read", SAMPLE, "--chars", "10:10")):
            code, message = run(*argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(message.startswith("docwalk: "), argv)

    def test_crlf_and_non_ascii_offsets_stay_exact(self):
        path = self.write("# 제목\r\n본문 한 줄\r\n## 하위\r\n둘째 줄\r\n")
        _, hit = run("find", path, "둘째")
        _, out = run("read", path, "--line", hit["hits"][0]["line"])
        self.assertEqual(out["text"], "## 하위\n둘째 줄\n")
        self.assertEqual(hit["hits"][0]["path"], "제목 > 하위")


if __name__ == "__main__":
    unittest.main()
