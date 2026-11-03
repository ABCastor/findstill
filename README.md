<p><img src="docs/findstill-mark.svg" width="40" alt=""><img src="docs/findstill-title.svg" width="172" alt="Findstill"></p>

Findstill is a local search engine for Apple Photos. Describe a picture, then narrow the results by people, places or dates, from a browser gallery, a JSON command line, an AI chat over [MCP](https://modelcontextprotocol.io) or [Raycast](https://www.raycast.com).

It matches your words against the pictures themselves, using Google's [SigLIP2](https://huggingface.co/google/siglip2-base-patch16-224) image model running on your Mac. Apple Photos search works differently: it matches the labels Photos assigned to each picture, and Apple offers no way for other apps to call it.

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

The dashboard and CLI use the same index. The server accepts local clients and binds to loopback. It has no remote authentication or cloud endpoint. An agent with shell access can use the command directly; the optional [skill](agent-skill/SKILL.md) describes it for compatible agent harnesses. Agents without a shell can use the MCP server below.

`open` selects the exact asset through osxphotos and may require Photos automation permission. A compatible signed Photos helper can be supplied with `install --photos-helper-app /path/to/Helper.app`. **The helper is not distributed here.** The optional `export` command requires that helper's resource-export protocol; a standard installation supports search, previews, reopening and page OCR without it. `export --download-missing` requests only the selected iCloud resource, after explicit selection.

## Search from an AI chat

`findstill-mcp` is an [MCP](https://modelcontextprotocol.io) server for chat apps such as Claude. Install it once, then point your chat app at it:

```sh
uv sync --frozen --extra mcp --python 3.12
claude mcp add findstill -- "$PWD/.venv/bin/findstill-mcp"
```

For Claude Desktop, add the same command under `mcpServers` in its configuration file:

```json
{ "mcpServers": { "findstill": { "command": "/absolute/path/to/findstill/.venv/bin/findstill-mcp" } } }
```

Ask for a picture in plain words ("find the photo of the red tram in Lisbon") and the chat calls `search_photos`. In apps that support [MCP Apps](https://github.com/modelcontextprotocol/ext-apps), the results appear as a gallery inside the conversation, and clicking a picture opens it in Apple Photos. Other apps receive a ranked text list plus the first few previews as images, which the model can look at before it answers.

| Tool | What it does |
|---|---|
| `search_photos` | Search by description, person, place, date range and media type |
| `get_photo` | Metadata and a larger preview of one photo |
| `open_in_photos` | Show one photo in Apple Photos |
| `photo_filters` | Exact person and place names to filter on |
| `photo_status` | How much of the library is searchable and how fresh the index is |

The MCP server is a thin client of the running Findstill service (`--url` or `FINDSTILL_URL` to change its address). It never reads the Photos library itself. Previews sent to the chat are small JPEGs without location or camera metadata, but they do leave your Mac if your chat app runs its model in the cloud.

## Search from Raycast

The [`raycast/`](raycast) folder holds a Raycast extension: type a description and pick from a grid of matching photos. Enter opens the selection in Apple Photos; other actions show a larger preview or copy the photo ID. It talks to the same local service, so start Findstill first.

```sh
cd raycast
npm ci
npm run dev
```

`npm run dev` loads the extension into Raycast. It caches up to 600 small previews in Raycast's private support folder for this extension.

## Read a paper page

```sh
uv run findstill read-page /absolute/path/to/page.jpg
```

[Apple Vision](https://developer.apple.com/documentation/vision/recognizing-text-in-images) returns raw English text lines, bounding boxes and native recognition scores. This requires the macOS command-line developer tools. The first call compiles the included Swift source and caches the helper locally. It does not produce validated logbook entries or verify signatures. Handwriting can be wrong even when recognition scores are high.

## Limits and local data

Small objects and handwriting may be unreadable in a preview. Screenshot text has no separate OCR index. Video search covers poster images only. There is no folder/document ingestion, transcription or general media index. First semantic searches load the model and take longer than warm requests. Set `MEDIA_SEARCH_DEVICE=cpu` for CPU inference.

The query model unloads after five minutes without a semantic search, checked every 30 seconds. Active searches finish before release; the next search reloads the cached weights automatically. Metadata browsing, thumbnails and status checks do not keep it loaded. This releases model tensors and unused MPS GPU allocations without deleting weights, photos or the index; the Python/PyTorch runtime can still retain memory. The local status API reports `query_model_loaded` and `query_model_idle_seconds`.

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

<p><a href="https://abcastor.com"><img src="docs/castor-footer.svg" width="350" alt="Chip, the Castor beaver, Castor, we give a dam"></a></p>
