---
name: media-search
description: Find pictures in the local Apple Photos library by visual description, with person, place and date filters. Use for finding a photo or image for an agent or a vault note.
---

Use the installed `media-search` CLI. It returns JSON from the private local index.

```sh
media-search status
media-search search 'a bicycle on a sandy beach' --limit 10
media-search options
media-search search 'a church' --place London --date-from 2020-01-01 --date-to 2025-12-31
media-search asset <uuid> --preview-path
media-search open <uuid>
media-search export <uuid> --out /absolute/path/to/new-directory
media-search read-page /absolute/path/to/page.jpg
```

Inspect a returned local preview before claiming a match. Use metadata filters for named people and dates. Scores rank visual similarity, they are not confidence percentages. The index covers local photo previews and video posters; small handwriting and full video contents are not indexed. A stale/partial index is reported by `status`.

`export` requires a separately configured compatible signed Photos helper, which Findstill does not distribute. With that helper, it fetches exactly the selected full image/video resource through the signed Photos helper, into a new private directory, with a SHA-256 provenance hash. Add `--download-missing` only when fetching that selected iCloud resource is in scope. It never downloads the whole library. Keep exports and personal OCR drafts outside Git. OCR and signature/licence accuracy require source review.

For an Obsidian embed, send the exact UUID through the existing Apple Photos plugin's `photo-link.sh`, so the app updates its own mapping. Keep Photos as the original store.

`read-page` reads an image locally with Apple Vision and returns raw text lines and geometry. It does not create logbook entries. Handwriting is unreliable; inspect the source before using extracted data.

Use `media-search --help` for commands. Dashboard: http://127.0.0.1:18473. Findstill source: https://github.com/ABCastor/findstill.
