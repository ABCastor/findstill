const byId = (id) => document.getElementById(id);
const form = byId('search-form');
const gallery = byId('gallery');
const dialog = byId('detail-dialog');
const countFormat = new Intl.NumberFormat();
const dateFormat = new Intl.DateTimeFormat(undefined, {year: 'numeric', month: 'short', day: 'numeric'});
const updateTimeFormat = new Intl.DateTimeFormat(undefined, {year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
let searchController;
let searchRevision = 0;
let detailController;
let selectedAsset;
let activeTile;
let lastIndexCount;
let hasSearched = false;

function readableDate(value) {
  if (!value) return 'Date unknown';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Date unknown' : dateFormat.format(date);
}

function readableUpdateTime(value) {
  if (!value) return 'No successful update yet';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Last successful update time unavailable' : `Last successful update ${updateTimeFormat.format(date)}`;
}

function localThumbnail(value) {
  if (typeof value !== 'string') return '';
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.pathname.startsWith('/api/assets/') ? url.href : '';
  } catch { return ''; }
}

async function request(url, options = {}) {
  const response = await fetch(url, {cache: 'no-store', ...options});
  let payload;
  try { payload = await response.json(); } catch { throw new Error('The local service returned an unreadable response. Try again.'); }
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'The local service could not complete this request.');
  return payload;
}

function errorText(error) {
  return error instanceof TypeError ? 'The local service is unreachable. Check that Media Search is running, then try again.' : error.message;
}

function showSkeletons() {
  gallery.replaceChildren();
  for (let i = 0; i < 12; i++) {
    const tile = document.createElement('div');
    tile.className = 'skeleton';
    tile.setAttribute('aria-hidden', 'true');
    const frame = document.createElement('div');
    frame.className = 'photo-frame';
    const line = document.createElement('span');
    line.className = 'skeleton-line';
    tile.append(frame, line);
    gallery.append(tile);
  }
}

function assetTile(asset, index) {
  const tile = document.createElement('button');
  tile.type = 'button';
  tile.className = 'photo-tile';
  tile.setAttribute('aria-label', `Inspect ${asset.filename || 'photo'}, ${readableDate(asset.date)}${asset.place ? `, ${asset.place}` : ''}`);
  const frame = document.createElement('span');
  frame.className = 'photo-frame';
  const image = document.createElement('img');
  image.alt = '';
  image.loading = index < 12 ? 'eager' : 'lazy';
  image.decoding = 'async';
  const unavailable = document.createElement('span');
  unavailable.className = 'preview-unavailable';
  unavailable.textContent = 'Preview unavailable';
  unavailable.hidden = true;
  image.addEventListener('error', () => { image.hidden = true; unavailable.hidden = false; });
  const thumbnail = localThumbnail(asset.thumbnail_url);
  if (thumbnail) image.src = thumbnail;
  else { image.hidden = true; unavailable.hidden = false; }
  frame.append(image, unavailable);
  if (asset.media_type === 'video') {
    const badge = document.createElement('span');
    badge.className = 'media-badge';
    badge.textContent = 'Video';
    frame.append(badge);
  }
  const caption = document.createElement('span');
  caption.className = 'photo-caption';
  const filename = document.createElement('span');
  filename.className = 'photo-filename';
  filename.textContent = asset.filename || 'Untitled photo';
  const metadata = document.createElement('span');
  metadata.className = 'photo-meta';
  metadata.textContent = [readableDate(asset.date), asset.place].filter(Boolean).join(' · ');
  caption.append(filename, metadata);
  tile.append(frame, caption);
  tile.addEventListener('click', () => openDetail(asset, tile));
  return tile;
}

async function search() {
  searchController?.abort();
  searchController = new AbortController();
  const revision = ++searchRevision;
  const data = new FormData(form);
  const from = data.get('date_from');
  const to = data.get('date_to');
  byId('search-error').hidden = true;
  byId('empty-state').hidden = true;
  if (from && to && from > to) {
    byId('search-error-text').textContent = 'The start date is after the end date. Change the dates and search again.';
    byId('search-error').hidden = false;
    byId('results-summary').textContent = '';
    gallery.replaceChildren();
    gallery.setAttribute('aria-busy', 'false');
    byId('search-button').textContent = 'Search';
    return;
  }
  const params = new URLSearchParams();
  for (const [key, value] of data) if (String(value).trim()) params.set(key, String(value).trim());
  params.set('limit', '60');
  hasSearched = true;
  byId('results-title').textContent = params.get('q') ? 'Search results' : 'Your library';
  byId('results-summary').textContent = 'Searching local previews…';
  byId('search-button').textContent = 'Searching…';
  gallery.setAttribute('aria-busy', 'true');
  showSkeletons();
  try {
    const payload = await request(`/api/search?${params}`, {signal: searchController.signal});
    if (revision !== searchRevision) return;
    const results = Array.isArray(payload.results) ? payload.results : [];
    gallery.replaceChildren(...results.map(assetTile));
    const total = Number.isFinite(payload.total) ? payload.total : results.length;
    const elapsed = Number.isFinite(payload.elapsed_ms) ? ` · ${(payload.elapsed_ms / 1000).toFixed(2)} s` : '';
    byId('results-summary').textContent = params.get('q')
      ? `${countFormat.format(results.length)} ranked ${results.length === 1 ? 'preview' : 'previews'} · ${countFormat.format(total)} searched${elapsed}`
      : `${countFormat.format(results.length)} of ${countFormat.format(total)} ${total === 1 ? 'item' : 'items'}${elapsed}`;
    byId('empty-state').hidden = results.length > 0;
    byId('empty-title').textContent = lastIndexCount === 0 ? 'Your index is getting started' : 'No matching previews';
    byId('empty-description').textContent = lastIndexCount === 0 ? 'Photos appear here as their local previews are indexed. Search again after coverage increases.' : 'Try a broader description, check the date range, or remove a filter.';
  } catch (error) {
    if (error.name === 'AbortError' || revision !== searchRevision) return;
    gallery.replaceChildren();
    byId('results-summary').textContent = '';
    byId('search-error-text').textContent = errorText(error);
    byId('search-error').hidden = false;
  } finally {
    if (revision === searchRevision) {
      gallery.setAttribute('aria-busy', 'false');
      byId('search-button').textContent = 'Search';
    }
  }
}

function metadataPair(label, value) {
  const pair = document.createElement('div');
  pair.className = 'metadata-pair';
  const term = document.createElement('dt');
  term.textContent = label;
  const definition = document.createElement('dd');
  definition.textContent = value;
  pair.append(term, definition);
  return pair;
}

function renderDetail(asset) {
  byId('detail-title').textContent = asset.filename || 'Photo details';
  const image = byId('detail-image');
  image.alt = `Local preview of ${asset.filename || 'selected photo'}`;
  const thumbnail = localThumbnail(asset.preview_url || asset.thumbnail_url);
  image.hidden = !thumbnail;
  byId('detail-preview-error').hidden = Boolean(thumbnail);
  if (thumbnail) image.src = thumbnail;
  else image.removeAttribute('src');
  const pairs = [
    metadataPair('Date', readableDate(asset.date)),
    metadataPair('People', Array.isArray(asset.persons) && asset.persons.length ? asset.persons.join(', ') : 'No people identified'),
    metadataPair('Place', asset.place || 'No place recorded'),
    metadataPair('Media', asset.media_type === 'video' ? 'Video preview' : 'Photo'),
  ];
  if (Number.isFinite(asset.score)) pairs.push(metadataPair('Similarity score', `${asset.score.toFixed(3)} (ranking, not confidence)`));
  byId('detail-metadata').replaceChildren(...pairs);
  byId('detail-scope').textContent = 'This is a local derivative preview. Photos opens the exact original.';
}

async function openDetail(asset, tile) {
  detailController?.abort();
  detailController = new AbortController();
  const controller = detailController;
  selectedAsset = asset;
  activeTile = tile;
  byId('detail-error').hidden = true;
  byId('detail-loading').hidden = false;
  byId('open-photos').disabled = false;
  byId('open-photos').querySelector('span').textContent = 'Open in Photos';
  renderDetail(asset);
  dialog.showModal();
  byId('close-detail').focus();
  try {
    const detail = await request(`/api/assets/${encodeURIComponent(asset.uuid)}`, {signal: controller.signal});
    if (controller !== detailController || !dialog.open) return;
    selectedAsset = {...asset, ...detail, score: Number.isFinite(detail.score) ? detail.score : asset.score};
    renderDetail(selectedAsset);
  } catch (error) {
    if (error.name === 'AbortError' || controller !== detailController) return;
    byId('detail-error').textContent = `Details could not refresh. ${errorText(error)}`;
    byId('detail-error').hidden = false;
  } finally {
    if (controller === detailController) byId('detail-loading').hidden = true;
  }
}

async function openPhotos() {
  if (!selectedAsset) return;
  const button = byId('open-photos');
  const assetId = selectedAsset.uuid;
  button.disabled = true;
  button.querySelector('span').textContent = 'Opening…';
  byId('detail-error').hidden = true;
  try {
    const result = await request(`/api/assets/${encodeURIComponent(assetId)}/open`, {method: 'POST'});
    if (result.ok !== true) throw new Error('Photos did not confirm the open request. Try again.');
    if (selectedAsset?.uuid === assetId && dialog.open) button.querySelector('span').textContent = 'Opened in Photos';
  } catch (error) {
    if (selectedAsset?.uuid === assetId && dialog.open) {
      byId('detail-error').textContent = `Couldn’t open this original. ${errorText(error)}`;
      byId('detail-error').hidden = false;
      button.querySelector('span').textContent = 'Open in Photos';
    }
  } finally {
    if (selectedAsset?.uuid === assetId) button.disabled = false;
  }
}

async function refreshStatus() {
  try {
    const status = await request('/api/status');
    const indexed = Number.isFinite(status.indexed) ? status.indexed : 0;
    const total = Number.isFinite(status.library_total) ? status.library_total : 0;
    const failed = Number.isFinite(status.failed) ? status.failed : 0;
    const updateFailed = typeof status.error === 'string' && status.error.trim().length > 0;
    const savedState = indexed > 0 ? 'Saved results may be out of date' : 'No indexed previews yet';
    const state = status.indexing
      ? `Indexing in progress${updateFailed ? ` · Previous update failed · ${savedState}` : ''}`
      : updateFailed ? `Update failed · ${savedState}` : indexed >= total && total > 0 ? 'Index ready' : 'Partial index';
    const lastUpdate = readableUpdateTime(status.indexed_at);
    const statusText = `${countFormat.format(indexed)} of ${countFormat.format(total)} library items indexed · ${state}${failed ? ` · ${countFormat.format(failed)} failed` : ''}${updateFailed ? ` · ${lastUpdate}` : ''}`;
    if (byId('index-status').textContent !== statusText) byId('index-status').textContent = statusText;
    byId('status-dot').className = `status-dot ${status.indexing ? 'indexing' : updateFailed ? 'error' : 'ready'}`;
    byId('refresh-status').hidden = true;
    byId('index-detail').textContent = [status.model, status.device, lastUpdate].filter(Boolean).join(' · ');
    const firstAvailable = lastIndexCount === 0 && indexed > 0;
    lastIndexCount = indexed;
    if (firstAvailable && hasSearched && !form.querySelector('#query').value && !dialog.open) search();
  } catch (error) {
    byId('index-status').textContent = `Index status unavailable. ${errorText(error)}`;
    byId('status-dot').className = 'status-dot error';
    byId('refresh-status').hidden = false;
  }
}

async function loadOptions() {
  try {
    const options = await request('/api/options');
    for (const [id, key] of [['person', 'persons'], ['place', 'places']]) {
      const select = byId(id);
      while (select.options.length > 1) select.remove(1);
      for (const value of options[key] || []) {
        if (typeof value !== 'string') continue;
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        select.append(option);
      }
    }
    byId('options-error').hidden = true;
  } catch (error) {
    byId('options-error').textContent = `Person and place filters could not load. ${errorText(error)}`;
    byId('options-error').hidden = false;
  }
}

form.addEventListener('submit', (event) => { event.preventDefault(); search(); });
form.querySelectorAll('select, input[type="date"]').forEach((control) => control.addEventListener('change', search));
byId('clear-filters').addEventListener('click', () => {
  const query = byId('query').value;
  form.reset();
  byId('query').value = query;
  search();
});
byId('empty-reset').addEventListener('click', () => { form.reset(); search(); });
byId('retry-search').addEventListener('click', search);
byId('refresh-status').addEventListener('click', () => { refreshStatus(); loadOptions(); });
byId('close-detail').addEventListener('click', () => dialog.close());
dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
dialog.addEventListener('close', () => { detailController?.abort(); selectedAsset = null; activeTile?.focus(); });
byId('detail-image').addEventListener('error', () => { byId('detail-image').hidden = true; byId('detail-preview-error').hidden = false; });
byId('open-photos').addEventListener('click', openPhotos);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStatus(); });
setInterval(() => { if (!document.hidden) refreshStatus(); }, 10000);
refreshStatus();
loadOptions();
search();
