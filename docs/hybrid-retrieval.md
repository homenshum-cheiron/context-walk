# Hybrid corpus retrieval

A local, offline index for a corpus of meeting notes, transcripts, and chat, built on the same locate-then-read idea as `docwalk.py`. Every piece of text keeps a locator `{file, char_start, char_end}` into a raw file you control. Nothing is summarized away. A later meaning layer, if you add one, only ranks; it does not replace the raw text.

This is a research notebook entry: the method and a synthetic check live here. Measured scores from any private corpus are not published.

## Why a tree, not one bag of chunks

- **T0 leaf:** one chat message, one transcript turn, or one notes line, including an indented sub-bullet (about 800 characters at most).
- **T1 window:** consecutive leaves from the same parent, packed up to 1,000 characters. Good for a short thread or a three-minute slice of a transcript.
- **T2 document:** one meeting, thread, channel, or file, with a short rule-built header (title, channel, date, people).

Search fuses the leaf ranking and the window ranking with reciprocal rank fusion. Date and person words in the question boost matching leaves. After a hit, the default walk is: the hit, then at most one neighbour inside the same T1 parent, plus the T2 header once per file. The walk stops at a character budget (8,000 by default). It never expands to a whole document. Citations are locators, never a summary.

A copy under a configured `derived/` prefix is ranked below a primary leaf that carries the same terms, so a later digest does not outrank the meeting it describes.

## Lanes

These run after the first two keyword units and only when the question calls for them:

- **Speaker.** Questions such as "what did Alice say" or "Alice worried about" search that person's turns, then optionally re-rank the pool by meaning.
- **Direct messages.** "in the DM" or "DMs with Blair" restrict the search to that conversation and, when a person is named, also join their DM file.
- **Hangul.** Hangul in the query is matched with character bigrams and particle-stripped words in a second FTS table. No extra package is required.
- **Meaning fallback.** Used when the query itself is Hangul, when keyword ranking is flat, or when an English question should still surface a Hangul source. A real multilingual embedder can be plugged in. The repository ships a tiny character-trigram hash embedder so the path can be tested without a model. Without any embedder, these lanes switch off and keyword search still runs.

## Ingest is append-only and incremental

Collectors write into `raw/`. `refresh.py` is idempotent: identical content is a no-op; a whole-file rewrite copies the previous bytes to `versions/` so an old locator still resolves; chat files only append messages with a new `Message TS:` and fail loudly if existing bytes would change. `memwalk.py build` re-segments only files whose sha256 changed, then rebuilds windows and the Hangul table. A freshness test requires the raw set and the manifest to match, byte-for-byte, and every leaf locator to round-trip.

Edited or deleted chat messages change the file sha, so that file is re-segmented. Locators into the previous version resolve in `versions/`, not in `raw/`.

## Meeting action items

`action_items.py` reads unchecked `* [ ]` lines from `notes/`, skips notes already marked in a state file, and returns each item with its locator, owner mentions, and a short `retrieve()` context. It prints `NO_NEW_MEETINGS` when there is nothing new. It drafts only and sends nothing.

## Frozen-holdout loop

When you iterate on a retriever, do not write questions from memory and do not tune on the same set you report.

1. Read the raw files. Write the next holdout set from those files, with a gold locator or substring, and freeze the file (hash it) **before** changing any retrieval code.
2. Score a frozen baseline and the current candidate on every earlier set and on the new holdout. Keep a change only when it does not regress a previous set, or when you can name the loss and accept it.
3. Merge a strength from the baseline when the candidate lost it. Drop a candidate that only helps the set you just fitted.
4. Stop when a new holdout no longer improves, or when the remaining misses need a different kind of evidence (for example a per-speaker gist written by a model).

The six questions in `examples/synthetic-corpus/eval/cases.jsonl` are a miniature of that idea: they were written against the synthetic files, not against the implementation. They are not a benchmark.

## What this does not show

- It is not a published score on a private or customer corpus.
- The hash embedder is a test stand-in, not a retrieval model.
- No claim is made against LangChain, Mem0, or any hosted memory product. Those were used as design references only.
- Hosts that delete local session logs after a fixed period are a capture problem, not a retrieval problem. Keep a copy you control before that window closes; see the `transcript-capture` skill in the sibling agent-skills repository.
