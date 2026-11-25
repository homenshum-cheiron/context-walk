# Synthetic corpus

A tiny, invented project used only to test `memwalk.py`. Nothing in these files is a real meeting, a real person, or a real product.

Files:

- `notes/planning-2026-03-02.md` — huddle notes with two unchecked action items and one indented sub-bullet.
- `transcript/planning-2026-03-02.txt` — the matching transcript, including one Hangul turn.
- `chat/channel`, `chat/thread`, and `chat/dm` — follow-up messages with timestamps.
- `derived/digest.md` — a later summary that should rank below the primary transcript and notes.
- `people.json` — display names and a corpus owner (`Alice`).
- `eval/cases.jsonl` — six frozen questions written against this corpus. Each names a substring that should appear in a retrieve() result and the file that holds it.

Rebuild the index:

```sh
python3 memwalk.py build --raw examples/synthetic-corpus --db /tmp/memwalk.db --embed
python3 memwalk.py retrieve "What did Alice say about the vendor?" --raw examples/synthetic-corpus --db /tmp/memwalk.db --embed
```
