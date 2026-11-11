#!/usr/bin/env python3
"""One real case on one real document: walk the document against ranked fixed-size chunks at the same size.

Offline, standard library only. This is a single worked case, not a benchmark: one question, one document,
one lexical baseline. Run from the repository root:  python3 bench/trace_case.py
Writes traces/relax-acceptance-criterion.json and prints a short summary.
"""
from collections import Counter
import importlib.util
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "examples" / "21-cfr-part-314.md"
spec = importlib.util.spec_from_file_location("docwalk", ROOT / "docwalk.py")
docwalk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docwalk)

QUESTION = ("An applicant wants to relax an acceptance criterion in the approved specification of a drug product. "
            "Which kind of supplement applies?")
# The passages a correct answer rests on, copied from the document. All four sit in section 314.70.
NEEDED = {
    "major-changes heading": "(b) _Changes requiring supplement submission and approval prior to distribution",
    "rule: specification changes": "or in the specifications provided in the approved NDA;",
    "moderate-changes heading": "(c) _Changes requiring supplement submission at least 30 days prior to distribution",
    "exception: compendium": "(iii) Relaxation of an acceptance criterion or deletion of a test to comply with an official compendium",
}
CHUNK = 1200


def words(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def chunks(text):
    """Greedy paragraph packing up to CHUNK characters; returns (start, end) spans."""
    out, start, pos = [], 0, 0
    for para in text.split("\n\n"):
        end = pos + len(para)
        if end - start > CHUNK and pos > start:
            out.append((start, pos))
            start = pos
        pos = end + 2
    out.append((start, len(text)))
    return out


def bm25_order(texts, query, k1=1.5, b=0.75):
    tfs = [Counter(words(t)) for t in texts]
    lens = [sum(tf.values()) for tf in tfs]
    avg = sum(lens) / len(lens)
    df = Counter(w for tf in tfs for w in tf)
    n = len(texts)
    idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
    score = [sum(idf[w] * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * lens[i] / avg)) for w in set(words(query)) if tf[w])
             for i, tf in enumerate(tfs)]
    return sorted(range(n), key=lambda i: (-score[i], i))


def holds(text):
    return {name: needle in text for name, needle in NEEDED.items()}


def main():
    doc = docwalk.Doc(DOC.read_text(encoding="utf-8"))
    steps = []

    def step(label, command, result, text=""):
        steps.append({"step": label, "command": command, "result_chars": len(json.dumps(result, ensure_ascii=False)),
                      "document_chars_read": len(text), "summary": {k: v for k, v in result.items() if k not in ("text", "hits", "sections")}})
        return result

    # Walk: find the exact phrase from the question, then read the whole section that holds the hit.
    hit = step("find", 'find DOC "acceptance criterion" --ignore-case',
               docwalk.find(doc, "acceptance criterion", False, True, 20))
    in_supplements = [h for h in hit["hits"] if "Supplements and other changes to an approved NDA" in (h["path"] or "")]
    sec = doc.enclosing(in_supplements[0]["line"])
    got = docwalk.read(doc, sec["char_start"], sec["char_end"], 20000)
    step("read", f'read DOC --line {in_supplements[0]["line"]} --max-chars 20000', got, got["text"])
    walk_chars = sum(s["result_chars"] for s in steps)

    # Baseline: BM25 over fixed-size chunks with the question as the query, at 1x, 2x and 4x the walk's size.
    spans = chunks(doc.text)
    order = bm25_order([doc.text[a:b] for a, b in spans], QUESTION)
    baseline = []
    for mult in (1, 2, 4):
        budget, used, picked = walk_chars * mult, 0, []
        for i in order:
            size = spans[i][1] - spans[i][0]
            if used + size > budget:
                break
            picked.append(i)
            used += size
        text = "\n\n".join(doc.text[spans[i][0]:spans[i][1]] for i in picked)
        baseline.append({"budget_chars": budget, "chunks": len(picked), "chars": used, "holds": holds(text),
                         "from_section_314_70": sum(1 for i in picked if sec["char_start"] <= spans[i][0] < sec["char_end"])})
    out = {"question": QUESTION, "document": DOC.name, "document_chars": len(doc.text), "document_sections": len(doc.sections),
           "hits_for_phrase": {"total": hit["total"], "in_section_314_70": len(in_supplements),
                               "sections": sorted({h["path"].split(" > ")[-1] for h in hit["hits"]})},
           "walk": {"steps": steps, "total_result_chars": walk_chars, "share_of_document": round(got["char_end"] - got["char_start"], 0) / len(doc.text),
                    "read_span": {k: got[k] for k in ("char_start", "char_end", "line_start", "line_end", "path", "truncated")},
                    "holds": holds(got["text"])},
           "baseline_bm25_chunks": {"chunk_chars": CHUNK, "chunks_in_document": len(spans), "runs": baseline},
           "limits": "one question, one document, one lexical baseline; token counts not measured; no model in the loop"}
    (ROOT / "traces").mkdir(exist_ok=True)
    (ROOT / "traces" / "relax-acceptance-criterion.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: out[k] for k in ("document_chars", "document_sections", "hits_for_phrase")}, indent=1))
    print("walk:", walk_chars, "chars returned;", out["walk"]["read_span"], out["walk"]["holds"])
    for r in baseline:
        print("bm25 chunks:", r["budget_chars"], "budget;", r["chunks"], "chunks;", r["from_section_314_70"], "from 314.70;", r["holds"])


if __name__ == "__main__":
    main()
