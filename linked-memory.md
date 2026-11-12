# Linked memory notes

An agent's memory as ordinary Markdown notes that point back to the evidence they came from. `linkmem.py` is a small offline reference: it checks an edit before it lands, rebuilds views the writer never edits, and stitches a reading packet that moves from an old note to its current version.

The design follows one paper and adds two rules of its own. It is a reference implementation, not a memory service.

## Source

Fu, Qi, Zhang, Deng, Wang, Li, Wu, Zhao. *Can LLM Agents Organize Their Own Memory?* arXiv:2611.11929v1, 9 November 2026. Code: github.com/Qi202/SoMem (MIT). Nothing here is copied from that code.

According to the paper:

- Memory has three layers. **Source** is the original interactions, archived by code and never edited. **Topic** is Markdown the model writes and reorganises freely. **Core** is a small, size-bounded note promoted from Topic and loaded into every query.
- A paragraph is a memory unit: a stable paragraph id, the text, links to other paragraphs, and footnotes that tie each statement to an event time (year, month or day precision) and to Source records.
- Every edit is staged. The runtime checks that Source is unchanged, paragraph structure, that Source references and links resolve, time values and allowed paths, plus reference and capacity checks for Core. An accepted edit is installed atomically and the views are rebuilt. A rejected edit leaves the old state. Periodic passes revisit changed files with the same check.
- Code derives three views from Topic: **Graph** (links and backlinks between paragraphs), **Timeline** (one file per year, month or day, each entry pointing to its paragraph and Source records) and **Recent** (the last K paragraphs by creation order, preloaded).
- At query time the model gets Core, Recent, a file list and the question date, then chooses its own searches (BM25 or embeddings, with path and date filters) and reads, following links and checking Source. There is no fixed order.
- Reported results: +2.66, +3.10 and +2.00 points over the strongest baseline on LoCoMo, BEAM-100K and LongMemEval-S. Removing Source costs 8.64 points on LoCoMo (95% CI −11.67 to −6.11). Removing Core, Recent, Timeline or Graph changes the overall score by at most 1.30 points, within sampling uncertainty. Memory built by one model can be read by another.
- Reported cost: on full LoCoMo, $229.73 total at the stated reference prices against $6.75 for Mem0, of which $118.85 is one-time construction and about $0.07 per question. Querying averages 5.2 to 7.4 model calls and about 20 seconds per question.
- Stated limits: question answering over conversations only, no sustained task execution; LLM judge without human calibration; single-run ablations on frozen memory; the runtime checks structure, not whether a note is true.

## What this repository adds

1. **Supersede, never silently delete.** A footnote can say `supersedes: old-id`. The old paragraph stays as history. An edit that removes a paragraph id without a paragraph that supersedes it is refused. A supersede cycle is refused. The paper allows revision in place; this rule makes "what did we believe before, and what replaced it" a lookup.
2. **A record hit reaches its current paragraph.** Search covers paragraphs and raw records. When a raw record matches, the walk goes to the paragraphs that cite it and then to their current version, so an old record is never shown alone.

## Layout

```text
workspace/
  source/records.jsonl     append-only evidence: {"id", "time", "text"}
  topics/**/*.md           notes written by the agent
  core.md                  small standing note (default limit 2,000 characters)
  views/                   rebuilt by linkmem.py; do not edit
    graph.json  timeline/YYYY[/MM[/DD]].md  recent.md  inventory.md  order.json
```

A note paragraph:

```markdown
The March 18 meeting moved to the Community Center because the library is under repair.[^b] ^venue-v2

[^b]: time: 2026-03-12; sources: R1, R2; supersedes: venue-v1
```

## Commands

```sh
python3 linkmem.py validate examples/linked-memory
python3 linkmem.py install WORKSPACE STAGED_COPY      # validate against WORKSPACE, then swap in and rebuild views
python3 linkmem.py views examples/linked-memory
python3 linkmem.py search examples/linked-memory "Riverside Library"
python3 linkmem.py walk examples/linked-memory ^venue-v1
python3 linkmem.py packet examples/linked-memory "Where is the March 18 meeting and why did it move?" --budget 6000
```

`packet` runs BM25 (k=4) over paragraphs and records, then walks: the current version of each hit, its one-hop links and backlinks, the cited records, then superseded history. Blocks go in that order; the first block that does not fit the budget ends the packet. Exit code 0 is a result, 1 a refused edit, 2 a refused request.

The example workspace is synthetic.

## A first measurement, on a private set

On 16 questions from one person's own agent memory, each about a rule that had later been replaced, a pre-registered comparison used one model and one prompt per arm, with a blind model judge. It set the bar before any answer was generated: linked memory had to be at least 15 points more accurate than the flat top-10 fact list, with at most 20% more memory tokens.

| Arm | Accuracy | Memory tokens per question |
|---|---|---|
| Flat top-10 facts | 0.56 | 834 |
| Linked packet, as registered | 0.63 | 543 |
| Linked packet with rule 2 above, added after seeing the misses | 0.72 | 967 |

The registered arm did not meet the bar. Rule 2 was written after the misses were read and was scored on the same 16 questions, so its number is a lead to test on new cases, not a result. Two of the remaining misses were facts that existed only outside memory, which no layout can fix. Two were a writer that filed a replacement under another topic without marking the old rule replaced. The questions and memory are private and not in this repository.

## What this does not show

- No model runs in this repository. The writer that decides grouping, wording and what supersedes what is not included. Its quality decides most of the result.
- The validator checks structure. It cannot tell whether a note is true or whether a replacement was missed.
- Search is plain BM25 with no embeddings or reranker.
- Sizes are characters; token counts are characters divided by 4.
