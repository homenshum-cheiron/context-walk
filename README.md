# Context walk

Read a long document by place, not by ranked fragments.

Search finds the document. After that, an agent can outline it, find an exact string, read the whole section that holds the hit, and widen or narrow from there. Every read returns exact character offsets, so a quoted passage can be checked against the source.

This repository holds a small offline tool (`docwalk.py`), a real trace on a real regulation, and the method notes. It is a research notebook in public: results are added as they are measured, and each one says what it does not show.

## A real trace

**Document:** 21 CFR Part 314, the United States regulation on new drug applications, as shown by the eCFR on 10 November 2026. 414,661 characters, 97 sections. See [the provenance note](examples/README.md).

**Question:** An applicant wants to relax an acceptance criterion in the approved specification of a drug product. Which kind of supplement applies?

**Why it is hard:** the answer needs a rule and its exception, and they sit about 5,200 characters apart in one section. Paragraph (b)(2)(i) says a change to the approved specifications is a major change and needs approval first. Paragraph (c)(2)(iii) says a relaxation that brings the product in line with an official compendium is a moderate change. A reader who sees only the exception answers "moderate". That is wrong for the general case.

### The walk: two calls

```console
$ python3 docwalk.py find examples/21-cfr-part-314.md "acceptance criterion" --ignore-case
{
 "total": 1,
 "hits": [
  {
   "line": 1067,
   "char_start": 145168,
   "char_end": 145188,
   "section": "1.5.10",
   "path": "PART 314—APPLICATIONS FOR FDA APPROVAL TO MARKET A NEW DRUG > Subpart B—Applications > § 314.70 Supplements and other changes to an approved NDA.",
   "line_text": "(iii) Relaxation of an acceptance criterion or deletion of a test to comply with an official compendium that is consistent with FDA statutory and regulatory requirements."
  }
 ]
}

$ python3 docwalk.py read examples/21-cfr-part-314.md --line 1067 --max-chars 20000
{
 "char_start": 136985,
 "char_end": 153972,
 "line_start": 977,
 "line_end": 1145,
 "path": "... > § 314.70 Supplements and other changes to an approved NDA.",
 "est_tokens": 4247,
 "truncated": false,
 "text": "#### § 314.70 Supplements and other changes to an approved NDA. ..."
}
```

(Output shortened: `page` and `shown` fields and the text body are left out.)

The second call returns the whole of section 314.70. It holds the rule, the exception, and the two paragraph headings that name them as major and moderate.

### Against ranked chunks of the same size

The baseline splits the document into 417 chunks of about 1,200 characters, ranks them with BM25 using the question as the query, and takes the best ones up to a size limit.

| Method | Characters returned | Major heading | Rule (b)(2)(i) | Moderate heading | Exception (c)(2)(iii) |
|---|---|---|---|---|---|
| Walk: find, then read the section | 17,960 | yes | yes | yes | yes |
| BM25 chunks, same size | 17,893 | no | no | no | yes |
| BM25 chunks, 2 times the size | 35,430 | no | no | yes | yes |
| BM25 chunks, 4 times the size | 70,960 | yes | yes | yes | yes |

At the same size, ranked chunks return the exception without the rule. That is the half that leads to the wrong answer. The walk read 4.1% of the document.

Reproduce: `python3 bench/trace_case.py` writes [the full trace](traces/relax-acceptance-criterion.json).

### What this trace does not show

- It is **one question on one document**. It is not a benchmark, and it says nothing about how often the walk wins.
- The phrase had exactly one hit, which makes it an easy case for find. Many questions share no exact string with the passage that decides them.
- The baseline is lexical only: no embeddings, no reranker, no parent or neighbour expansion. Stronger baselines exist.
- No model is in the loop. A person chose the phrase to find. Whether an agent picks a good phrase is not measured here.
- Sizes are characters. Token estimates are characters divided by 4.
- The outline was not needed. For this document the full outline costs 41,738 characters, more than the section itself. `--depth 2` gives ten rows. Find first; outline when find fails.

## A second measurement: what to add after a hit

A separate experiment on 400 Natural Questions documents compared nine rules for adding text around a first hit, at limits from 2,000 to 64,000 tokens. [Summary and limits](bench/natural-questions-context-expansion.md).

- No rule wins at every limit. On the 38 large documents, a mixed parent-and-heading rule is 0.062 below plain BM25 fill at 4,000 tokens and 0.062 above it at 8,000.
- The first hit decides most of it. Reading the heading section around a BM25 hit scores 0.295 at 2,000 tokens. Around a hit that the gold label picked, it scores 0.952.
- It measures how much gold evidence the context keeps. It does not measure answers, and it does not test the agent walk above.

## Linked memory notes

`linkmem.py` applies the same idea to an agent's own memory: notes that point back to their evidence, an edit check, views rebuilt by code, and a read path that moves from an old note to the one that replaced it. See [linked-memory.md](linked-memory.md). A first pre-registered comparison on a private set did not meet its bar; the page says what it does and does not show.

## The method

1. **Locate.** Search the full text for an exact string from the question or from a fragment that search returned: a code, an article number, a defined term. Every hit reports its section path.
2. **Read the smallest enclosing unit.** Read the whole section that holds the hit. A list, a table and its lead-in belong together.
3. **Widen or narrow on evidence.** Widen to the parent or the next section when the text points elsewhere or states a rule beside its exception. Narrow to a line range when the section is larger than the budget.
4. **Respect a budget.** Each read has a size limit and returns a cursor for the rest. Count every call.
5. **Cite by offset.** Quote only text a read returned, with its character range and section path. An outline entry or a summary is never the cited span.
6. **Stop.** When the passage answers the question and names its own conditions. Also when two widenings add nothing: report "not found in this document".

## The tool

`docwalk.py` works on one local UTF-8 text or Markdown file. Python 3.10+, no packages, no network, no model.

```sh
python3 docwalk.py outline FILE --depth 2
python3 docwalk.py find FILE "exact phrase" --ignore-case
python3 docwalk.py read FILE --section 1.5.10
python3 docwalk.py read FILE --section 1.5.10 --to-section 1.5.11
python3 docwalk.py read FILE --line 1067 --scope parent
python3 docwalk.py read FILE --lines 1040:1070
python3 docwalk.py read FILE --pages 3:4
python3 docwalk.py read FILE --chars 136985:153972 --max-chars 8000
```

- `outline` lists sections with line, character and page ranges and an estimated size. With no headings it falls back to 80-line windows and says so. `--numbered` also treats lines such as `3.2 Title` as headings.
- `find` is literal unless `--regex` is given. It returns the true total and up to `--limit` hits.
- `read` takes exactly one range. A range longer than `--max-chars` (default 8000) is cut just after a line end, or mid-line when one line is longer than the limit. `next.chars` continues the read, and the pieces add up to the range exactly once.
- Pages are split on the form feed character. The tool does not convert PDF, HTML or scans; convert first.
- Exit code 0 is a result. Exit code 2 is a refused request, with the reason on standard error.

## Checks

```sh
python3 -m unittest discover -s tests -v
python3 bench/trace_case.py
python3 linkmem.py validate examples/linked-memory
```

The tests cover section nesting, exact offsets, budget cuts and their cursor, page and line ranges, files without headings, non-ASCII text, refused requests, and the real regulation case above.

## Status of the research

| Item | Status |
|---|---|
| Offline tool and tests | In this repository |
| One real trace on a public regulation | In this repository, reproducible |
| Context expansion after a hit, 400 Natural Questions documents, 9 rules, 6 token limits | [Summary](bench/natural-questions-context-expansion.md) in this repository; code and row-level results not included. Results are mixed and measure evidence retention, not answers |
| LightRAG, PageIndex, Jev, OpenAI Decisions API | Not run. Listed as future comparisons only |
| Long-document benchmark with stronger baselines (hybrid search, reranking, tree and graph indexes) | Not done |
| A model choosing the phrase, the section and the stop point | Not done |
| Linked memory notes: offline validator, views and read path, synthetic example, tests | In this repository ([linked-memory.md](linked-memory.md)) |
| Linked memory vs a flat fact list, 16 private questions, pre-registered | Bar not met; aggregate only, data not included |

Nothing in this repository is a claim about a product or a customer.

## Related

The method is also packaged as a skill, `walk-the-document`, in [agent-skills](https://github.com/homenshum-cheiron/agent-skills).

## License

MIT for the code and the writing. The regulation text is a work of the United States Government; see [the provenance note](examples/README.md).
