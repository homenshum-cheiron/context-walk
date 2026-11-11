# Context expansion after a hit: Natural Questions, 400 documents

Status: summary only. The experiment was run in another workspace. Its code and row-level results are **not** in this repository (see "What is not here"). Every number below was recomputed from the row-level results on 10 November 2026 with a separate standard-library script and matched the experiment's own report.

## What was tested

This experiment does not test the two-call walk in the README. It tests a narrower question: after a first hit inside a known document, which rule for adding surrounding text keeps the most gold evidence within a token limit?

- **Data:** 400 Natural Questions validation examples (rows 100 to 499), each with its Wikipedia source page. 290 have gold evidence; 110 have no answer.
- **Document size:** median 6,057 tokens, largest 46,849. 61 documents have 16,000 tokens or more. Of those, 38 have gold evidence. Only 8 documents with gold evidence have 32,000 tokens or more.
- **First hit:** BM25 inside the known document. No search across a corpus.
- **Rules compared (9):** fill the limit with BM25-ranked passages; add nearby passages; several rules that add the parent block or the heading section of the hit; and one mixed rule ("parent, heading or window").
- **Limits (6):** 2,000 to 64,000 tokens, counted with the `o200k_base` tokenizer on the final rendered context.
- **Score:** share of the gold long-answer tokens that the context holds. This is literal text retention. It is not answer accuracy.
- **Size:** 21,600 contexts, plus 2,052 diagnostic contexts where the gold label picks the first hit. 23,652 in total. None exceeds its limit.

## Result 1: no rule wins at every limit

Gold-evidence recall on the 38 large documents (16,000 tokens or more), BM25 first hit:

| Rule | 2,000 | 4,000 | 8,000 | 16,000 | 32,000 |
|---|---|---|---|---|---|
| BM25 fill | 0.368 | 0.561 | 0.601 | 0.727 | 0.947 |
| Nearby passages | 0.370 | 0.561 | 0.611 | 0.743 | 0.948 |
| Heading section first | 0.295 | 0.449 | 0.610 | 0.772 | 0.949 |
| Mixed: parent, heading or window | 0.366 | 0.499 | 0.663 | 0.771 | 0.947 |

- The mixed rule against BM25 fill: 0.002 lower at 2,000, 0.062 lower at 4,000, 0.062 higher at 8,000.
- At 8,000 the mixed rule wins on 5 documents, ties on 31 and loses on 2. The experiment's report says two questions supply most of the gain, and its interval is not corrected for the many comparisons made.
- Reading the heading section first loses at 2,000 and 4,000. A section around a wrong hit uses the limit on the wrong place.
- At 64,000 every rule scores 1.000, because all 38 documents fit.

**Do not read this as "the hybrid wins" or "sections lose".** The results are mixed, the sample is 38 documents, and the differences are small against that sample.

## Result 2: the first hit decides most of it

The BM25 first hit overlaps the gold evidence in 54 of the 290 answerable examples, and in 4 of the 38 large ones.

When the gold label picks the first hit instead (a diagnostic, not something a system can do), recall on the same 38 documents is:

| Rule | 2,000, BM25 hit | 2,000, gold hit | 8,000, BM25 hit | 8,000, gold hit |
|---|---|---|---|---|
| BM25 fill | 0.368 | 0.709 | 0.601 | 0.737 |
| Nearby passages | 0.370 | 0.796 | 0.611 | 0.804 |
| Heading section first | 0.295 | 0.952 | 0.610 | 1.000 |
| Mixed: parent, heading or window | 0.366 | 0.903 | 0.663 | 0.906 |

With a relevant hit, reading the enclosing heading section is the best rule at 2,000 tokens. With a BM25 hit it is the worst. That is the same pattern as the single trace in the README: the walk worked there because the exact-string find landed in the right section.

## What this does not show

- **Not answer quality.** No model wrote or judged an answer.
- **Not the agent walk.** No agent chose a phrase, a section or a stop point. No outline was read.
- **Not reading cost.** The whole document was indexed first, so nothing here measures how little of a document an agent must read.
- **Not corpus search.** The document was known in advance.
- **Not long documents in general.** 38 large documents with evidence, 8 of them at 32,000 tokens or more. Wikipedia pages, not regulations, PDFs or office files.
- **Not a test of other systems.** LightRAG, PageIndex, Jev and the OpenAI Decisions API were **not run**. They were read about and listed as future comparisons. No result here says anything about them.
- **Not the original run.** This is a labelled reconstruction after the first workspace was lost. The reconstruction has its own completed independent replay. The first run's audit was interrupted and is not claimed.
- The gold-hit rows use labels. They show room for a better first hit, not what a selector achieves.

## What is not here

- The experiment's code. Its licence has not been set, so it is not copied.
- Row-level results and the source snapshots. They hold question text and Wikipedia text, which carry the dataset's share-alike terms.
- Timing data. It covers local selection on 10 documents only.

## Attribution

Natural Questions, Google Research, CC BY-SA 3.0: <https://github.com/google-research-datasets/natural-questions>. The source pages are Wikipedia revisions. Only aggregate numbers are reproduced here.
