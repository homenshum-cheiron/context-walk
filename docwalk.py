#!/usr/bin/env python3
"""Outline, find and read exact ranges of one local text or Markdown file. Offline; standard library only.

Every result carries character offsets into the file as read (UTF-8, newlines normalised to "\\n"), so a quoted
passage can be checked against the source. Token counts are estimates (characters / 4), not a tokenizer.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ATX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
NUMBERED = re.compile(r"^[ \t]*((?:\d+\.)*\d+)\.?[ \t]+(\S.{0,98})$")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
WINDOW_LINES = 80


class Doc:
    def __init__(self, text, numbered=False):
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = self.text.split("\n")
        self.starts = [0]                       # character offset of each line start
        for line in self.lines[:-1]:
            self.starts.append(self.starts[-1] + len(line) + 1)
        self.page_starts = [0] + [m.end() for m in re.finditer("\f", self.text)]
        self.source, self.sections = self._sections(numbered)

    def _headings(self, numbered):
        found, fenced = [], False
        for i, line in enumerate(self.lines):
            if FENCE.match(line):
                fenced = not fenced
            if fenced:
                continue
            m = ATX.match(line)
            if m:
                found.append((i, len(m.group(1)), m.group(2)))
            elif numbered:
                m = NUMBERED.match(line)
                if m and not line.rstrip().endswith((".", ",", ";", ":")):
                    found.append((i, m.group(1).count(".") + 1, line.strip()))
        return found

    def _sections(self, numbered):
        heads, source = self._headings(numbered), "headings"
        if not heads:  # no headings: fixed line windows, labelled as such
            source = "windows"
            heads = [(i, 1, f"lines {i + 1}-{min(i + WINDOW_LINES, len(self.lines))}")
                     for i in range(0, len(self.lines), WINDOW_LINES)]
        out, counters, stack = [], [], []
        for k, (line, level, title) in enumerate(heads):
            end = next((h[0] for h in heads[k + 1:] if h[1] <= level), len(self.lines))
            stack = [s for s in stack if s[0] < level] + [(level, title)]
            counters = counters[:len(stack) - 1] + [(counters[len(stack) - 1] if len(counters) >= len(stack) else 0) + 1]
            out.append({"id": ".".join(map(str, counters)), "level": level, "title": title,
                        "path": " > ".join(t for _, t in stack), "line_start": line + 1, "line_end": end})
        for s in out:
            self._measure(s)
        return source, out

    def _measure(self, s):
        a, b = self.span(s["line_start"], s["line_end"])
        s.update(char_start=a, char_end=b, est_tokens=(b - a + 3) // 4, page_start=self.page(a), page_end=self.page(max(a, b - 1)))

    def span(self, line_start, line_end):
        """Character span of 1-based inclusive lines."""
        line_start = max(1, line_start)
        line_end = min(len(self.lines), line_end)
        if line_start > line_end:
            raise ValueError("empty line range")
        return self.starts[line_start - 1], self.starts[line_end - 1] + len(self.lines[line_end - 1])

    def line_of(self, offset):
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if self.starts[mid] <= offset else (lo, mid - 1)
        return lo + 1

    def page(self, offset):
        return sum(1 for p in self.page_starts if p <= offset)

    def section(self, ident):
        for s in self.sections:
            if s["id"] == ident:
                return s
        raise ValueError(f"no section {ident!r}; run outline for valid ids")

    def enclosing(self, line, up=0):
        """Deepest section holding a 1-based line, then `up` levels towards the root."""
        holders = [s for s in self.sections if s["line_start"] <= line <= s["line_end"]]
        if not holders:
            return None
        return holders[max(0, len(holders) - 1 - up)]


def outline(doc, depth):
    rows = [s for s in doc.sections if s["level"] <= depth]
    return {"source": doc.source, "lines": len(doc.lines), "chars": len(doc.text), "pages": len(doc.page_starts),
            "est_tokens": (len(doc.text) + 3) // 4, "sections": rows}


def find(doc, pattern, regex, ignore_case, limit):
    rx = re.compile(pattern if regex else re.escape(pattern), (re.I if ignore_case else 0) | re.M)
    hits = []
    for m in rx.finditer(doc.text):
        if m.end() == m.start():
            continue
        line = doc.line_of(m.start())
        sec = doc.enclosing(line)
        hits.append({"line": line, "char_start": m.start(), "char_end": m.end(), "page": doc.page(m.start()),
                     "section": sec["id"] if sec else None, "path": sec["path"] if sec else None,
                     "line_text": doc.lines[line - 1][:240]})
    return {"total": len(hits), "shown": min(limit, len(hits)), "hits": hits[:limit]}


def read(doc, a, b, max_chars):
    """Verbatim text[a:b]. A range longer than max_chars is cut just after a line end, or mid-line when the
    window holds no line end, so every read makes progress. `next` continues the read."""
    a, b = max(0, a), min(len(doc.text), b)
    if a >= b:
        raise ValueError("empty range")
    end = b
    if b - a > max_chars:
        cut = doc.text.rfind("\n", a, a + max_chars)
        end = cut + 1 if cut != -1 else a + max_chars
    sec = doc.enclosing(doc.line_of(a))
    return {"char_start": a, "char_end": end, "line_start": doc.line_of(a), "line_end": doc.line_of(max(a, end - 1)),
            "page_start": doc.page(a), "page_end": doc.page(max(a, end - 1)), "path": sec["path"] if sec else None,
            "est_tokens": (end - a + 3) // 4, "truncated": end < b,
            "next": {"chars": f"{end}:{b}"} if end < b else None, "text": doc.text[a:end]}


def pair(value):
    left, _, right = value.partition(":")
    return int(left), int(right or left)


def resolve(doc, args):
    """Turn one of the range options into a character span."""
    chosen = [n for n in ("section", "lines", "pages", "chars", "line") if getattr(args, n) is not None]
    if len(chosen) != 1:
        raise ValueError("give exactly one of --section, --lines, --pages, --chars, --line")
    if args.section is not None:
        first, last = doc.section(args.section), doc.section(args.to_section or args.section)
        if last["char_start"] < first["char_start"]:
            raise ValueError("--to-section starts before --section; swap them")
        return first["char_start"], max(first["char_end"], last["char_end"])
    if args.lines is not None:
        return doc.span(*pair(args.lines))
    if args.pages is not None:
        first, last = pair(args.pages)
        if not 1 <= first <= last <= len(doc.page_starts):
            raise ValueError(f"pages run from 1 to {len(doc.page_starts)}")
        return doc.page_starts[first - 1], (doc.page_starts[last] if last < len(doc.page_starts) else len(doc.text))
    if args.chars is not None:
        return pair(args.chars)
    if args.scope == "lines":
        return doc.span(args.line - args.before, args.line + args.after)
    sec = doc.enclosing(args.line, up=1 if args.scope == "parent" else 0)
    if sec is None:
        raise ValueError("line is before the first heading; use --scope lines")
    return sec["char_start"], sec["char_end"]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("outline", "find", "read"):
        s = sub.add_parser(name)
        s.add_argument("file")
        s.add_argument("--numbered", action="store_true", help="also treat lines such as '3.2 Title' as headings")
        if name == "outline":
            s.add_argument("--depth", type=int, default=6)
        elif name == "find":
            s.add_argument("pattern")
            s.add_argument("--regex", action="store_true")
            s.add_argument("--ignore-case", action="store_true")
            s.add_argument("--limit", type=int, default=20)
        else:
            s.add_argument("--section")
            s.add_argument("--to-section")
            s.add_argument("--lines", help="A:B, 1-based inclusive")
            s.add_argument("--pages", help="A:B, pages split on form feed")
            s.add_argument("--chars", help="A:B, end exclusive")
            s.add_argument("--line", type=int, help="read around this line; see --scope")
            s.add_argument("--scope", choices=("section", "parent", "lines"), default="section")
            s.add_argument("--before", type=int, default=5)
            s.add_argument("--after", type=int, default=5)
            s.add_argument("--max-chars", type=int, default=8000)
    args = p.parse_args(argv)
    try:
        doc = Doc(Path(args.file).read_text(encoding="utf-8-sig"), args.numbered)
        if args.cmd == "outline":
            result = outline(doc, args.depth)
        elif args.cmd == "find":
            result = find(doc, args.pattern, args.regex, args.ignore_case, args.limit)
        else:
            if args.max_chars < 1:
                raise ValueError("--max-chars must be positive")
            result = read(doc, *resolve(doc, args), args.max_chars)
    except (OSError, UnicodeError, ValueError, re.error) as error:
        print(f"docwalk: {error}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
