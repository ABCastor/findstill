<p><img src="docs/findstill-mark.svg" width="40" alt=""><img src="docs/findstill-title.svg" width="172" alt="Findstill"></p>

Findstill is a local search engine for Apple Photos, with a browser gallery and a JSON command-line client. Describe a picture, then narrow the results by people, places or dates.

[![Checks](https://github.com/ABCastor/findstill/actions/workflows/checks.yml/badge.svg)](https://github.com/ABCastor/findstill/actions/workflows/checks.yml)

**Experimental, macOS only.** Search reads locally available photo previews and video poster images from one selected Photos library. It does not search every video frame or every folder on your Mac.

## Run it

Requires macOS 14 or newer on Apple Silicon, Python 3.12 and [uv](https://docs.astral.sh/uv/). Intel Macs are unsupported by the locked PyTorch build. CPU inference is available on Apple Silicon. Allow your terminal to read your Photos library in macOS Privacy & Security if access is denied.

```sh
git clone https://github.com/ABCastor/findstill.git
cd findstill
uv sync --frozen --extra test --python 3.12
uv tool install osxphotos
uv run findstill install --photos-python "$HOME/.local/share/uv/tools/osxphotos/bin/python" --photos-library "$HOME/Pictures/Photos Library.photoslibrary"
uv run findstill index
```

Substitute your actual library path if it differs. Open [the dashboard](http://127.0.0.1:18473). Installation starts the dashboard at login and refreshes the index every six hours. It preserves existing service configurations; a conflicting installation requires review rather than silent replacement.

Run the manual index command once before relying on scheduled indexing. It downloads the pinned [Google SigLIP2 Base model](https://huggingface.co/google/siglip2-base-patch16-224). Search and scheduled indexing use cached weights offline. If you clear the model cache, run `uv run findstill prepare-model` to restore it; this loads the pinned model on CPU without reading Photos or rebuilding the index. Photos stay on the Mac; indexing does not request missing originals from iCloud. The dashboard creates smaller private display previews with image metadata removed.

[osxphotos](https://github.com/RhetTbull/osxphotos) reads the library metadata through a separate interpreter. The installation writes its path and your library path to `~/Library/Application Support/MediaSearch/settings.json`. The existing directory and `media-search` command remain compatible. You can also run the server directly with `uv run findstill serve`.

## Search from a person or an agent

```sh
uv run findstill search 'a bicycle on a beach' --limit 10
uv run findstill search 'a church' --place London --date-from 2020-01-01 --date-to 2025-12-31
uv run findstill search --person 'Ada Example' --media-type image
uv run findstill status
uv run findstill options
uv run findstill asset 12345678-1234-1234-1234-123456789012 --preview-path
uv run findstill open 12345678-1234-1234-1234-123456789012
```

The CLI returns JSON. `asset --preview-path` adds an existing local preview path for an agent to inspect. Use `options` to discover available people and places. A similarity score ranks matches; it is not the probability that a result is correct. Inspect the image before relying on a match. Named people come from Photos metadata, not face recognition by the model.

The dashboard and CLI use the same index. The server accepts local clients and binds to loopback. It has no remote authentication or cloud endpoint. An agent needs shell access on the Mac and the installed command. The optional [skill](agent-skill/SKILL.md) describes the interface for compatible agent harnesses; install it in that harness's skill directory.

`open` selects the exact asset through osxphotos and may require Photos automation permission. A compatible signed Photos helper can be supplied with `install --photos-helper-app /path/to/Helper.app`. **The helper is not distributed here.** The optional `export` command requires that helper's resource-export protocol; a standard installation supports search, previews, reopening and page OCR without it. `export --download-missing` requests only the selected iCloud resource, after explicit selection.

## Read a paper page

```sh
uv run findstill read-page /absolute/path/to/page.jpg
```

[Apple Vision](https://developer.apple.com/documentation/vision/recognizing-text-in-images) returns raw English text lines, bounding boxes and native recognition scores. This requires the macOS command-line developer tools. The first call compiles the included Swift source and caches the helper locally. It does not produce validated logbook entries or verify signatures. Handwriting can be wrong even when recognition scores are high.

## Limits and local data

Small objects and handwriting may be unreadable in a preview. Screenshot text has no separate OCR index. Video search covers poster images only. There is no folder/document ingestion, transcription or general media index. First semantic searches load the model and take longer than warm requests. Set `MEDIA_SEARCH_DEVICE=cpu` for CPU inference.

Metadata, vectors, settings, display previews and logs stay in `~/Library/Application Support/MediaSearch`, outside the repository. Downloaded model weights use the Hugging Face cache, normally `~/.cache/huggingface`. Set `MEDIA_SEARCH_HOME` for a separate runtime directory. Hidden and trashed Photos items are excluded. Library changes take effect after the next successful refresh; failed refreshes preserve earlier results and report staleness. No Photos database is written.

## Verify changes

```sh
uv run pytest -q
uv run python scripts/check_source.py
uv run python scripts/check_privacy.py
uv build
```

Tests use synthetic media and cover incremental indexing, removals, metadata filters, cached-vector invalidation, preview fallback, exact reopening identifiers, local request boundaries and native OCR integration. They do not establish retrieval accuracy on your library.

Owned code is licensed under [Apache 2.0](LICENSE). Bundled fonts retain their SIL Open Font License notices. Model weights are downloaded separately under their own Apache 2.0 licence. Dependencies retain their licences; no model weights, photo library or search data are included in the package.

<p><a href="https://abcastor.com"><img src="docs/castor-footer.svg" width="350" alt="Chip, the Castor beaver, by Castor, we give a dam"></a></p>
