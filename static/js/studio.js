/**
 * Aura Hub — Desktop Metadata Studio & Command Center
 * State management, spreadsheet grid with Excel-style multi-cell selection & paste,
 * granular per-column broadcast, collapsible artist library tree, branded diff inspector,
 * and unified audio ingestion pipeline.
 */

(function () {
  'use strict';

  // ================= STATE =================
  const state = {
    currentView: 'studio', // 'studio' | 'downloader' | 'library'
    currentPath: '',
    isFolder: true,
    albumMeta: {
      album: '',
      artist: '',
      album_artist: '',
      year: '',
      genre: '',
      composers: [],
      producers: [],
    },
    originalAlbumMeta: {},
    tracks: [],
    originalTracks: [],
    selectedTrackIdx: 0,
    activeCell: { row: 0, col: 1 }, // row index, col index
    selectedCells: new Set(), // Set of 'r:c' strings
    selectionAnchor: { row: 0, col: 1 },
    isEditing: false,
    candidates: [],
    selectedCandidateIdx: -1,
    cover: {
      originalUrl: '',
      currentBase64: null,
      currentUrl: null,
      isModified: false,
      width: 0,
      height: 0,
    },
    libraryAlbums: [],
    downloadTasks: [],
    taskPollTimer: null,
    musicRequests: [],
    selectedQuality: 'auto',
  };

  // Column definitions: col index -> field key
  const COLUMNS = [
    { key: 'track_number', title: '#', editable: true, type: 'number' },
    { key: 'title', title: 'Title', editable: true, type: 'text' },
    { key: 'artist', title: 'Artist', editable: true, type: 'text' },
    { key: 'album_artist', title: 'Album Artist', editable: true, type: 'text' },
    { key: 'composers', title: 'Composer', editable: true, type: 'list' },
    { key: 'producers', title: 'Producer', editable: true, type: 'list' },
    { key: 'genre', title: 'Genre', editable: true, type: 'text' },
    { key: 'year', title: 'Year', editable: true, type: 'text' },
    { key: 'duration_seconds', title: 'Time', editable: false, type: 'duration' },
    { key: 'lrc', title: 'LRC', editable: false, type: 'badge' },
  ];

  // Source branding styles
  const SOURCE_BRANDS = {
    spotify: { color: '#1DB954', label: 'Spotify', icon: '🟢', cls: 'brand-spotify' },
    deezer: { color: '#A238FF', label: 'Deezer', icon: '🟣', cls: 'brand-deezer' },
    musicbrainz: { color: '#EB743B', label: 'MusicBrainz', icon: '🟠', cls: 'brand-musicbrainz' },
    discogs: { color: '#475569', label: 'Discogs', icon: '💿', cls: 'brand-discogs' },
    genius: { color: '#FFFF64', label: 'Genius', icon: '🟡', cls: 'brand-genius' },
    itunes: { color: '#FA243C', label: 'Apple Music', icon: '🔴', cls: 'brand-apple' },
    apple: { color: '#FA243C', label: 'Apple Music', icon: '🔴', cls: 'brand-apple' },
    lrclib: { color: '#10B981', label: 'LRCLIB', icon: '🎤', cls: 'brand-lrclib' },
    local: { color: '#38BDF8', label: 'Disk Tags', icon: '📁', cls: 'brand-local' },
  };

  // ================= API & AUTH HELPERS =================
  function getApiUrl(endpoint) {
    const clean = endpoint.replace(/^\/+/, '');
    let base = window.location.pathname
      .replace(/\/(studio|studio\.html|webapp|index\.html)\/?$/, '')
      .replace(/\/+$/, '');
    if (!base || base === '/') {
      return `/${clean}`;
    }
    return `${base}/${clean}`;
  }

  function getAuthToken() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('token')) return params.get('token');
    if (params.get('auth_token')) return params.get('auth_token');

    if (window.Telegram?.WebApp?.initData) {
      return window.Telegram.WebApp.initData;
    }
    return localStorage.getItem('aura_auth_token') || '';
  }

  function getAuthHeaders() {
    const token = getAuthToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    return headers;
  }

  async function apiRequest(endpoint, method = 'GET', body = null) {
    const url = getApiUrl(endpoint);
    const options = {
      method,
      headers: getAuthHeaders(),
    };
    if (body) {
      options.body = JSON.stringify(body);
    }

    const res = await fetch(url, options);
    if (res.status === 401 || res.status === 403) {
      showToast('Authentication required. Click the key icon to set your ADMIN_TOKEN.', 'error');
      openAuthModal();
      throw new Error(`Auth failed (${res.status})`);
    }

    const contentType = res.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      const text = await res.text();
      throw new Error(`Unexpected non-JSON response from ${url}: ${text.slice(0, 100)}`);
    }

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || data.message || `Request failed (${res.status})`);
    }
    return data;
  }

  function showToast(message, type = 'success') {
    const container = document.getElementById('toastContainer');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️';
    toast.innerHTML = `<span>${icon}</span><span>${escapeHtml(message)}</span>`;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      setTimeout(() => toast.remove(), 250);
    }, 3200);
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatDuration(sec) {
    if (!sec || isNaN(sec)) return '0:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${s < 10 ? '0' : ''}${s}`;
  }

  function getBrandInfo(sourceStr) {
    const key = (sourceStr || '').toLowerCase().replace(/[^a-z]/g, '');
    for (const k of Object.keys(SOURCE_BRANDS)) {
      if (key.includes(k)) return SOURCE_BRANDS[k];
    }
    return SOURCE_BRANDS.local;
  }

  // ================= VIEW SWITCHER =================
  function switchView(viewName) {
    state.currentView = viewName;
    document.querySelectorAll('.nav-view-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === viewName);
    });

    document.querySelectorAll('.app-view').forEach(viewEl => {
      viewEl.classList.remove('active');
    });

    if (viewName === 'studio') {
      document.getElementById('viewStudio').classList.add('active');
      stopTaskPolling();
    } else if (viewName === 'downloader') {
      document.getElementById('viewDownloader').classList.add('active');
      fetchDownloaderTasks();
      fetchMusicRequests();
      startTaskPolling();
    } else if (viewName === 'library') {
      document.getElementById('viewLibrary').classList.add('active');
      loadLibraryTree();
      stopTaskPolling();
    }
  }

  // ================= DATA LOADING & INSPECTION =================
  async function inspectPath(path) {
    if (!path) return;
    try {
      showToast('Inspecting metadata on disk...', 'info');
      const data = await apiRequest(`/api/studio/inspect?path=${encodeURIComponent(path)}`);
      
      state.currentPath = data.path;
      state.isFolder = !!data.is_folder;
      state.albumMeta = {
        album: data.album || '',
        artist: data.artist || '',
        album_artist: data.album_artist || data.artist || '',
        year: data.year || '',
        genre: data.genre || '',
        composers: Array.isArray(data.composers) ? [...data.composers] : [],
        producers: Array.isArray(data.producers) ? [...data.producers] : [],
      };
      state.originalAlbumMeta = JSON.parse(JSON.stringify(state.albumMeta));

      // Tracks normalization
      state.tracks = (data.tracks || []).map((t, idx) => ({
        filename: t.filename || `track_${idx + 1}`,
        track_number: t.track_number !== undefined ? t.track_number : idx + 1,
        total_tracks: t.total_tracks || data.tracks.length,
        disc_number: t.disc_number || 1,
        title: t.title || '',
        artist: t.artist || state.albumMeta.artist || '',
        album_artist: t.album_artist || state.albumMeta.album_artist || '',
        album: t.album || state.albumMeta.album || '',
        composers: Array.isArray(t.composers) ? [...t.composers] : [],
        producers: Array.isArray(t.producers) ? [...t.producers] : [],
        genre: t.genre || state.albumMeta.genre || '',
        year: t.year || state.albumMeta.year || '',
        duration_seconds: t.duration_seconds || 0,
        lyrics: t.lyrics || t.lyrics_synced || t.lyrics_unsynced || '',
        has_lrc: !!(t.has_lrc || (t.lyrics && t.lyrics.includes('['))),
      }));
      state.originalTracks = JSON.parse(JSON.stringify(state.tracks));

      // Reset selection & cell
      state.selectedTrackIdx = 0;
      state.activeCell = { row: 0, col: 1 };
      state.selectionAnchor = { row: 0, col: 1 };
      state.selectedCells.clear();
      state.selectedCells.add('0:1');
      state.isEditing = false;

      // Cover art setup
      state.cover = {
        originalUrl: data.cover_url || '',
        currentBase64: null,
        currentUrl: data.cover_url || '',
        isModified: false,
        width: 0,
        height: 0,
      };

      // Reset search candidates
      state.candidates = [];
      state.selectedCandidateIdx = -1;

      // Update UI components
      updateNavbarHeader();
      updateArtworkView();
      updateCommonAlbumForm();
      renderSpreadsheetGrid();
      updateLyricsEditorView();
      renderCandidatesBar();

      // Ensure studio view is visible
      switchView('studio');

      // Update URL query path without reload
      const newUrl = new URL(window.location);
      newUrl.searchParams.set('path', state.currentPath);
      window.history.replaceState({}, '', newUrl);

      showToast(`Loaded ${state.tracks.length} track(s) for ${state.albumMeta.album || data.folder}`, 'success');

      // Auto search external metadata in background if album has title
      if (state.albumMeta.album) {
        searchExternal(state.albumMeta.album, state.albumMeta.artist);
      }
    } catch (err) {
      console.error('Failed to inspect path:', err);
      showToast(`Error inspecting path: ${err.message}`, 'error');
    }
  }

  function updateNavbarHeader() {
    const badge = document.getElementById('navAlbumName');
    const folderName = state.albumMeta.album || state.currentPath.split('/').pop() || 'No Folder Loaded';
    badge.textContent = folderName;
    badge.title = state.currentPath;

    document.getElementById('badgeTrackCount').textContent = state.tracks.length;
  }

  // ================= ARTWORK MANAGER =================
  function updateArtworkView() {
    const previewImg = document.getElementById('artPreviewImg');
    const placeholder = document.getElementById('artPlaceholder');
    const resBadge = document.getElementById('artBadgeRes');
    const modBadge = document.getElementById('artModifiedBadge');

    const imgSrc = state.cover.currentBase64 || state.cover.currentUrl || state.cover.originalUrl;
    if (imgSrc) {
      previewImg.src = imgSrc;
      previewImg.style.display = 'block';
      placeholder.style.display = 'none';

      const img = new Image();
      img.onload = function () {
        state.cover.width = img.naturalWidth;
        state.cover.height = img.naturalHeight;
        resBadge.textContent = `${img.naturalWidth} × ${img.naturalHeight} px`;
      };
      img.onerror = function () {
        resBadge.textContent = 'Preview';
      };
      img.src = imgSrc;
    } else {
      previewImg.style.display = 'none';
      placeholder.style.display = 'flex';
      resBadge.textContent = 'No Artwork';
    }

    modBadge.style.display = state.cover.isModified ? 'inline-block' : 'none';
  }

  function handleArtFile(file) {
    if (!file || !file.type.startsWith('image/')) {
      showToast('Please select a valid image file (JPEG or PNG)', 'error');
      return;
    }
    const reader = new FileReader();
    reader.onload = (e) => {
      state.cover.currentBase64 = e.target.result;
      state.cover.currentUrl = null;
      state.cover.isModified = true;
      updateArtworkView();
      showToast('Artwork replaced from file', 'success');
    };
    reader.readAsDataURL(file);
  }

  function resetArtwork() {
    state.cover.currentBase64 = null;
    state.cover.currentUrl = state.cover.originalUrl;
    state.cover.isModified = false;
    updateArtworkView();
    showToast('Reset artwork to disk version', 'info');
  }

  function selectProviderArt(url) {
    if (!url) return;
    state.cover.currentUrl = url;
    state.cover.currentBase64 = null;
    state.cover.isModified = true;
    updateArtworkView();
    showToast('Applied artwork from candidate provider', 'success');
  }

  // ================= COMMON ALBUM TAGS FORM =================
  function updateCommonAlbumForm() {
    document.getElementById('albumFieldTitle').value = state.albumMeta.album || '';
    document.getElementById('albumFieldArtist').value = state.albumMeta.album_artist || state.albumMeta.artist || '';
    document.getElementById('albumFieldYear').value = state.albumMeta.year || '';
    document.getElementById('albumFieldGenre').value = state.albumMeta.genre || '';
    document.getElementById('albumFieldProducers').value = state.albumMeta.producers.join(', ');
    document.getElementById('albumFieldComposers').value = state.albumMeta.composers.join(', ');
  }

  function syncCommonAlbumFormToState() {
    state.albumMeta.album = document.getElementById('albumFieldTitle').value.trim();
    state.albumMeta.album_artist = document.getElementById('albumFieldArtist').value.trim();
    state.albumMeta.year = document.getElementById('albumFieldYear').value.trim();
    state.albumMeta.genre = document.getElementById('albumFieldGenre').value.trim();
    
    const rawProds = document.getElementById('albumFieldProducers').value.trim();
    state.albumMeta.producers = rawProds ? rawProds.split(',').map(s => s.trim()).filter(Boolean) : [];

    const rawComps = document.getElementById('albumFieldComposers').value.trim();
    state.albumMeta.composers = rawComps ? rawComps.split(',').map(s => s.trim()).filter(Boolean) : [];
  }

  function applyCommonFieldsToAllTracks() {
    syncCommonAlbumFormToState();
    state.tracks.forEach((track) => {
      if (state.albumMeta.album) track.album = state.albumMeta.album;
      if (state.albumMeta.album_artist) track.album_artist = state.albumMeta.album_artist;
      if (state.albumMeta.year) track.year = state.albumMeta.year;
      if (state.albumMeta.genre) track.genre = state.albumMeta.genre;
      if (state.albumMeta.producers.length) track.producers = [...state.albumMeta.producers];
      if (state.albumMeta.composers.length) track.composers = [...state.albumMeta.composers];
    });
    renderSpreadsheetGrid();
    showToast('Applied album fields to all track rows', 'success');
  }

  // ================= GRANULAR PER-COLUMN HEADER BROADCAST =================
  function broadcastColumnValue(colIdx) {
    if (!state.tracks.length) {
      showToast('No tracks loaded to broadcast', 'error');
      return;
    }
    const colDef = COLUMNS[colIdx];
    if (!colDef || !colDef.editable) return;

    const row0 = state.tracks[0];
    const sourceVal = row0[colDef.key];

    // Deep clone value if list or string
    let broadcastVal = Array.isArray(sourceVal) ? [...sourceVal] : sourceVal;

    // Apply to Row 1 through Row N-1 for THIS column only
    for (let r = 1; r < state.tracks.length; r++) {
      if (Array.isArray(broadcastVal)) {
        state.tracks[r][colDef.key] = [...broadcastVal];
      } else {
        state.tracks[r][colDef.key] = broadcastVal;
      }
    }

    renderSpreadsheetGrid();
    const displayVal = Array.isArray(broadcastVal) ? broadcastVal.join(', ') : broadcastVal;
    showToast(`Broadcasted ${colDef.title} ("${displayVal || '[Empty]'}") to all ${state.tracks.length} tracks`, 'success');
  }

  // ================= SPREADSHEET GRID & EXCEL SELECTION =================
  function renderSpreadsheetGrid() {
    const tbody = document.getElementById('gridTbody');
    tbody.innerHTML = '';

    if (!state.tracks.length) {
      tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:30px; color:var(--text-dim);">No tracks loaded. Click 'Library' in top navigation to pick an album.</td></tr>`;
      return;
    }

    state.tracks.forEach((track, rIdx) => {
      const tr = document.createElement('tr');
      tr.dataset.row = rIdx;
      if (rIdx === state.selectedTrackIdx) {
        tr.classList.add('selected');
      }

      COLUMNS.forEach((col, cIdx) => {
        const td = document.createElement('td');
        td.dataset.row = rIdx;
        td.dataset.col = cIdx;

        const cellKey = `${rIdx}:${cIdx}`;

        // Active single cell focus
        if (state.activeCell.row === rIdx && state.activeCell.col === cIdx) {
          td.classList.add('cell-active');
        }

        // Multi-cell bounding box selection
        if (state.selectedCells.has(cellKey)) {
          td.classList.add('cell-selected');
        }

        // Modified / Dirty cell indicator
        const origTrack = state.originalTracks[rIdx];
        if (origTrack && col.editable) {
          const currentVal = formatFieldValue(track[col.key], col.type);
          const origVal = formatFieldValue(origTrack[col.key], col.type);
          if (currentVal !== origVal) {
            td.classList.add('cell-modified', 'dirty');
          }
        }

        if (col.editable) {
          td.classList.add('editable');
        }

        // Content rendering
        if (col.key === 'track_number') {
          td.classList.add('col-num');
          td.textContent = track.track_number !== undefined ? track.track_number : rIdx + 1;
        } else if (col.key === 'duration_seconds') {
          td.classList.add('col-duration');
          td.textContent = formatDuration(track.duration_seconds);
        } else if (col.key === 'lrc') {
          td.classList.add('col-lrc');
          const hasSynced = track.lyrics && track.lyrics.includes('[');
          td.innerHTML = hasSynced
            ? `<span class="lrc-badge synced">Synced</span>`
            : `<span class="lrc-badge missing">None</span>`;
        } else if (col.type === 'list') {
          const arr = Array.isArray(track[col.key]) ? track[col.key] : [];
          td.textContent = arr.join(', ');
          td.title = arr.join(', ');
        } else {
          td.textContent = track[col.key] || '';
          td.title = track[col.key] || '';
        }

        tr.appendChild(td);
      });

      tbody.appendChild(tr);
    });

    scrollActiveCellIntoView();
  }

  function formatFieldValue(val, type) {
    if (type === 'list') {
      return Array.isArray(val) ? val.join(', ') : '';
    }
    return val !== undefined && val !== null ? String(val).trim() : '';
  }

  function scrollActiveCellIntoView() {
    const activeEl = document.querySelector('.studio-grid td.cell-active');
    if (activeEl) {
      activeEl.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }

  function selectTrackRow(rIdx) {
    if (rIdx < 0 || rIdx >= state.tracks.length) return;
    state.selectedTrackIdx = rIdx;

    document.querySelectorAll('.studio-grid tbody tr').forEach((tr, idx) => {
      tr.classList.toggle('selected', idx === rIdx);
    });

    updateLyricsEditorView();
  }

  function setActiveCell(row, col, startEditing = false) {
    if (state.isEditing) {
      commitCellEdit();
    }

    const maxRow = state.tracks.length - 1;
    const maxCol = COLUMNS.length - 1;

    state.activeCell.row = Math.max(0, Math.min(row, maxRow));
    state.activeCell.col = Math.max(0, Math.min(col, maxCol));

    selectTrackRow(state.activeCell.row);

    document.querySelectorAll('.studio-grid td.cell-active').forEach(td => td.classList.remove('cell-active'));
    const targetTd = document.querySelector(`.studio-grid td[data-row="${state.activeCell.row}"][data-col="${state.activeCell.col}"]`);
    if (targetTd) {
      targetTd.classList.add('cell-active');
    }

    scrollActiveCellIntoView();

    if (startEditing) {
      startCellEdit();
    }
  }

  // Update rectangular range selection
  function updateRangeSelection(anchor, target) {
    state.selectedCells.clear();
    const minR = Math.min(anchor.row, target.row);
    const maxR = Math.max(anchor.row, target.row);
    const minC = Math.min(anchor.col, target.col);
    const maxC = Math.max(anchor.col, target.col);

    for (let r = minR; r <= maxR; r++) {
      for (let c = minC; c <= maxC; c++) {
        state.selectedCells.add(`${r}:${c}`);
      }
    }

    // Update visuals
    document.querySelectorAll('.studio-grid td.cell-selected').forEach(td => td.classList.remove('cell-selected'));
    state.selectedCells.forEach(key => {
      const [r, c] = key.split(':');
      const td = document.querySelector(`.studio-grid td[data-row="${r}"][data-col="${c}"]`);
      if (td) td.classList.add('cell-selected');
    });
  }

  function startCellEdit() {
    const colDef = COLUMNS[state.activeCell.col];
    if (!colDef || !colDef.editable) return;

    const td = document.querySelector(`.studio-grid td[data-row="${state.activeCell.row}"][data-col="${state.activeCell.col}"]`);
    if (!td || state.isEditing) return;

    state.isEditing = true;
    const track = state.tracks[state.activeCell.row];
    const initialVal = formatFieldValue(track[colDef.key], colDef.type);

    const input = document.createElement('input');
    input.type = colDef.type === 'number' ? 'number' : 'text';
    input.className = 'inline-cell-input';
    input.value = initialVal;

    td.appendChild(input);
    input.focus();
    input.select();

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        commitCellEdit();
        setActiveCell(state.activeCell.row + 1, state.activeCell.col, false);
      } else if (e.key === 'Tab') {
        e.preventDefault();
        commitCellEdit();
        if (e.shiftKey) {
          moveToPreviousEditableCell();
        } else {
          moveToNextEditableCell();
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        cancelCellEdit();
      }
    });

    input.addEventListener('blur', () => {
      if (state.isEditing) {
        commitCellEdit();
      }
    });
  }

  function commitCellEdit() {
    if (!state.isEditing) return;
    const input = document.querySelector('.inline-cell-input');
    if (input) {
      const colDef = COLUMNS[state.activeCell.col];
      const track = state.tracks[state.activeCell.row];
      const newVal = input.value.trim();

      if (colDef.type === 'number') {
        track[colDef.key] = parseInt(newVal, 10) || 1;
      } else if (colDef.type === 'list') {
        track[colDef.key] = newVal ? newVal.split(',').map(s => s.trim()).filter(Boolean) : [];
      } else {
        track[colDef.key] = newVal;
      }
      input.remove();
    }
    state.isEditing = false;
    renderSpreadsheetGrid();
  }

  function cancelCellEdit() {
    const input = document.querySelector('.inline-cell-input');
    if (input) {
      input.remove();
    }
    state.isEditing = false;
    renderSpreadsheetGrid();
  }

  function moveToNextEditableCell() {
    let r = state.activeCell.row;
    let c = state.activeCell.col + 1;
    while (r < state.tracks.length) {
      while (c < COLUMNS.length) {
        if (COLUMNS[c].editable) {
          setActiveCell(r, c, true);
          return;
        }
        c++;
      }
      r++;
      c = 0;
    }
  }

  function moveToPreviousEditableCell() {
    let r = state.activeCell.row;
    let c = state.activeCell.col - 1;
    while (r >= 0) {
      while (c >= 0) {
        if (COLUMNS[c].editable) {
          setActiveCell(r, c, true);
          return;
        }
        c--;
      }
      r--;
      c = COLUMNS.length - 1;
    }
  }

  function clearSelectedCells() {
    if (!state.selectedCells.size) return;
    let clearedCount = 0;

    state.selectedCells.forEach(key => {
      const [rStr, cStr] = key.split(':');
      const r = parseInt(rStr, 10);
      const c = parseInt(cStr, 10);
      const colDef = COLUMNS[c];
      if (colDef && colDef.editable && state.tracks[r]) {
        if (colDef.type === 'number') state.tracks[r][colDef.key] = 1;
        else if (colDef.type === 'list') state.tracks[r][colDef.key] = [];
        else state.tracks[r][colDef.key] = '';
        clearedCount++;
      }
    });

    renderSpreadsheetGrid();
    showToast(`Cleared ${clearedCount} cell(s)`, 'info');
  }

  function handleClipboardPaste(e) {
    if (['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
      return; // Regular text input handles paste naturally
    }

    const text = (e.clipboardData || window.clipboardData).getData('text');
    if (!text) return;
    e.preventDefault();

    const lines = text.split(/\r?\n/).filter((line, idx, arr) => {
      return !(idx === arr.length - 1 && line.trim() === '');
    });
    if (!lines.length) return;

    // Find top-left starting cell
    let startR = state.activeCell.row;
    let startC = state.activeCell.col;

    if (state.selectedCells.size > 0) {
      let minR = Infinity;
      let minC = Infinity;
      state.selectedCells.forEach(key => {
        const [r, c] = key.split(':').map(Number);
        if (r < minR) minR = r;
        if (c < minC) minC = c;
      });
      if (minR !== Infinity && minC !== Infinity) {
        startR = minR;
        startC = minC;
      }
    }

    let modifiedCount = 0;
    lines.forEach((line, dr) => {
      const rowVals = line.split('\t');
      rowVals.forEach((val, dc) => {
        const targetR = startR + dr;
        const targetC = startC + dc;
        if (targetR < state.tracks.length && targetC < COLUMNS.length) {
          const colDef = COLUMNS[targetC];
          if (colDef.editable) {
            const raw = val.trim();
            if (colDef.type === 'number') {
              state.tracks[targetR][colDef.key] = parseInt(raw, 10) || 1;
            } else if (colDef.type === 'list') {
              state.tracks[targetR][colDef.key] = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];
            } else {
              state.tracks[targetR][colDef.key] = raw;
            }
            modifiedCount++;
          }
        }
      });
    });

    renderSpreadsheetGrid();
    showToast(`Pasted ${modifiedCount} cell(s) from clipboard`, 'success');
  }

  function autoNumberTracks() {
    state.tracks.forEach((t, idx) => {
      t.track_number = idx + 1;
      t.total_tracks = state.tracks.length;
    });
    renderSpreadsheetGrid();
    showToast(`Re-sequenced tracks 1 to ${state.tracks.length}`, 'success');
  }

  function discardAllChanges() {
    state.albumMeta = JSON.parse(JSON.stringify(state.originalAlbumMeta));
    state.tracks = JSON.parse(JSON.stringify(state.originalTracks));
    resetArtwork();
    updateCommonAlbumForm();
    renderSpreadsheetGrid();
    updateLyricsEditorView();
    showToast('Discarded all unsaved edits', 'info');
  }

  // ================= EXTERNAL METADATA & BRANDED DIFF =================
  async function searchExternal(query, artist = '') {
    const cleanQ = query.trim();
    if (!cleanQ) return;
    try {
      document.getElementById('diffStatusNote').textContent = `Querying MusicBrainz, Deezer, Spotify, iTunes, Discogs, Genius, LRCLIB for "${cleanQ}"...`;
      const res = await apiRequest(`/api/studio/search-external?query=${encodeURIComponent(cleanQ)}&type=album&artist=${encodeURIComponent(artist.trim())}`);
      
      state.candidates = res.candidates || [];
      document.getElementById('badgeCandidateCount').textContent = state.candidates.length;
      document.getElementById('badgeCandidateCount').style.display = state.candidates.length ? 'inline-block' : 'none';

      renderCandidatesBar();
      populateProviderArtGrid();

      if (state.candidates.length) {
        selectCandidate(0);
        document.getElementById('diffStatusNote').textContent = `Found ${state.candidates.length} candidate(s). Click any to inspect diff.`;
      } else {
        document.getElementById('diffStatusNote').textContent = 'No external candidates found for this query.';
      }
    } catch (err) {
      console.error('Search external error:', err);
      document.getElementById('diffStatusNote').textContent = `Search failed: ${err.message}`;
    }
  }

  function renderCandidatesBar() {
    const bar = document.getElementById('diffCandidatesBar');
    bar.innerHTML = '';

    if (!state.candidates.length) {
      bar.innerHTML = `<div style="font-size:13px; color:var(--text-dim); padding:10px;">No candidates yet. Use the search bar above to look up metadata.</div>`;
      return;
    }

    state.candidates.forEach((cand, idx) => {
      const card = document.createElement('div');
      card.className = `candidate-card ${idx === state.selectedCandidateIdx ? 'active' : ''}`;
      
      const conf = Math.round(cand.confidence_score || 0);
      const isRec = cand.is_recommended;
      const prev = cand.preview || {};
      const brand = getBrandInfo(cand.source || cand.source_name);

      card.style.borderColor = idx === state.selectedCandidateIdx ? brand.color : '';

      card.innerHTML = `
        ${isRec ? '<span class="candidate-rec-tag">Recommended</span>' : ''}
        <div class="candidate-header">
          <span class="source-pill-badge ${brand.cls}">${brand.icon} ${escapeHtml(cand.source || brand.label)}</span>
          <span class="candidate-conf-badge">${conf}% Match</span>
        </div>
        <div class="candidate-title">${escapeHtml(prev.album || prev.title || 'Untitled')}</div>
        <div class="candidate-artist">${escapeHtml(prev.artist || 'Unknown')}</div>
        <div style="font-size:11px; color:var(--text-dim); display:flex; justify-content:space-between; margin-top:2px;">
          <span>${prev.track_count ? prev.track_count + ' tracks' : ''}</span>
          <span>${prev.year || ''}</span>
        </div>
      `;

      card.addEventListener('click', () => selectCandidate(idx));
      bar.appendChild(card);
    });
  }

  function populateProviderArtGrid() {
    const grid = document.getElementById('providerArtGrid');
    const section = document.getElementById('providerArtSection');
    grid.innerHTML = '';

    const artUrls = [];
    state.candidates.forEach(c => {
      const art = c.preview?.cover_url || c.album_data?.cover_url || c.track_data?.cover_url;
      if (art && !artUrls.some(a => a.url === art)) {
        artUrls.push({ url: art, source: c.source });
      }
    });

    if (artUrls.length) {
      section.style.display = 'block';
      artUrls.slice(0, 6).forEach(item => {
        const thumb = document.createElement('div');
        thumb.className = 'provider-art-thumb';
        const brand = getBrandInfo(item.source);
        thumb.innerHTML = `
          <img src="${item.url}" alt="Cover" loading="lazy">
          <span class="src-badge" style="background:${brand.color}; color:#fff;">${brand.label}</span>
        `;
        thumb.addEventListener('click', () => selectProviderArt(item.url));
        grid.appendChild(thumb);
      });
    } else {
      section.style.display = 'none';
    }
  }

  function selectCandidate(idx) {
    if (idx < 0 || idx >= state.candidates.length) return;
    state.selectedCandidateIdx = idx;

    renderCandidatesBar();
    renderDiffTable(state.candidates[idx]);
  }

  function renderDiffTable(cand) {
    const card = document.getElementById('diffTableCard');
    const tbody = document.getElementById('diffTableBody');
    const titleEl = document.getElementById('diffCandidateTitle');
    const srcEl = document.getElementById('diffCandidateSourceBadge');

    card.style.display = 'block';
    const albumData = cand.album_data || {};
    const prev = cand.preview || {};
    const brand = getBrandInfo(cand.source || cand.source_name);

    titleEl.textContent = prev.album || prev.title || 'Candidate Diff';
    srcEl.textContent = `${brand.icon} ${cand.source || brand.label}`;
    srcEl.className = `source-pill-badge ${brand.cls}`;

    tbody.innerHTML = '';

    const diffFields = [
      { key: 'album', name: 'Album Title', disk: state.albumMeta.album, cand: albumData.album || prev.album },
      { key: 'album_artist', name: 'Album Artist', disk: state.albumMeta.album_artist, cand: albumData.album_artist || prev.artist },
      { key: 'year', name: 'Year', disk: state.albumMeta.year, cand: albumData.year || prev.year },
      { key: 'genre', name: 'Genre', disk: state.albumMeta.genre, cand: albumData.genre || prev.genre },
      { key: 'producers', name: 'Producers', disk: state.albumMeta.producers.join(', '), cand: (albumData.producers || []).join(', ') },
      { key: 'composers', name: 'Composers', disk: state.albumMeta.composers.join(', '), cand: (albumData.composers || []).join(', ') },
    ];

    diffFields.forEach(f => {
      const diskVal = f.disk || '';
      const candVal = f.cand || '';
      const differs = diskVal !== candVal && candVal !== '';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="diff-field-name">${f.name}</td>
        <td class="diff-val-disk">${escapeHtml(diskVal) || '<span style="color:var(--text-dim);">[Empty]</span>'}</td>
        <td class="diff-val-provider ${differs ? 'differs' : ''}">${escapeHtml(candVal) || '<span style="color:var(--text-dim);">[None]</span>'}</td>
        <td class="diff-action">
          <button class="btn btn-secondary" style="font-size:11px; padding:3px 8px;" data-field="${f.key}">Accept</button>
        </td>
      `;

      tr.querySelector('button').addEventListener('click', () => {
        applySingleDiffField(f.key, candVal);
      });

      tbody.appendChild(tr);
    });
  }

  function applySingleDiffField(key, value) {
    if (key === 'album') state.albumMeta.album = value;
    else if (key === 'album_artist') state.albumMeta.album_artist = value;
    else if (key === 'year') state.albumMeta.year = value;
    else if (key === 'genre') state.albumMeta.genre = value;
    else if (key === 'producers') state.albumMeta.producers = value.split(',').map(s => s.trim()).filter(Boolean);
    else if (key === 'composers') state.albumMeta.composers = value.split(',').map(s => s.trim()).filter(Boolean);

    updateCommonAlbumForm();
    applyCommonFieldsToAllTracks();
    if (state.selectedCandidateIdx >= 0) {
      renderDiffTable(state.candidates[state.selectedCandidateIdx]);
    }
    showToast(`Accepted provider field: ${key}`, 'success');
  }

  function applyAllCandidateFields() {
    if (state.selectedCandidateIdx < 0) return;
    const cand = state.candidates[state.selectedCandidateIdx];
    const albumData = cand.album_data || {};
    const prev = cand.preview || {};

    if (albumData.album || prev.album) state.albumMeta.album = albumData.album || prev.album;
    if (albumData.album_artist || prev.artist) state.albumMeta.album_artist = albumData.album_artist || prev.artist;
    if (albumData.year || prev.year) state.albumMeta.year = albumData.year || prev.year;
    if (albumData.genre || prev.genre) state.albumMeta.genre = albumData.genre || prev.genre;
    if (albumData.producers) state.albumMeta.producers = [...albumData.producers];
    if (albumData.composers) state.albumMeta.composers = [...albumData.composers];

    if (Array.isArray(albumData.tracks) && albumData.tracks.length) {
      albumData.tracks.forEach((candTrk, idx) => {
        if (state.tracks[idx]) {
          if (candTrk.title) state.tracks[idx].title = candTrk.title;
          if (candTrk.artist) state.tracks[idx].artist = candTrk.artist;
          if (candTrk.composers?.length) state.tracks[idx].composers = [...candTrk.composers];
          if (candTrk.producers?.length) state.tracks[idx].producers = [...candTrk.producers];
        }
      });
    }

    const candArt = prev.cover_url || albumData.cover_url;
    if (candArt) {
      selectProviderArt(candArt);
    }

    updateCommonAlbumForm();
    applyCommonFieldsToAllTracks();
    renderDiffTable(cand);
    showToast('Applied all candidate fields to album and tracks', 'success');
  }

  // ================= SYNCED LYRICS EDITOR =================
  function updateLyricsEditorView() {
    const track = state.tracks[state.selectedTrackIdx];
    const titleEl = document.getElementById('lyricsCurrentTrack');
    const badgeEl = document.getElementById('lyricsStatusBadge');
    const textarea = document.getElementById('lyricsTextarea');
    const lineCountEl = document.getElementById('lyricsLineCount');

    if (!track) {
      titleEl.textContent = 'No track selected';
      badgeEl.textContent = 'None';
      badgeEl.className = 'lrc-badge missing';
      textarea.value = '';
      lineCountEl.textContent = '0 lines';
      return;
    }

    titleEl.textContent = `${track.track_number || ''}. ${track.title || track.filename}`;
    textarea.value = track.lyrics || '';

    const hasSynced = track.lyrics && track.lyrics.includes('[');
    if (hasSynced) {
      badgeEl.textContent = 'Synced LRC';
      badgeEl.className = 'lrc-badge synced';
    } else if (track.lyrics) {
      badgeEl.textContent = 'Plain text';
      badgeEl.className = 'lrc-badge';
    } else {
      badgeEl.textContent = 'No Lyrics';
      badgeEl.className = 'lrc-badge missing';
    }

    const lines = track.lyrics ? track.lyrics.split('\n').filter(Boolean).length : 0;
    lineCountEl.textContent = `${lines} line(s)`;
  }

  function handleLyricsTextareaInput() {
    const track = state.tracks[state.selectedTrackIdx];
    if (!track) return;
    const textarea = document.getElementById('lyricsTextarea');
    track.lyrics = textarea.value;
    track.has_lrc = track.lyrics.includes('[');

    const lines = track.lyrics ? track.lyrics.split('\n').filter(Boolean).length : 0;
    document.getElementById('lyricsLineCount').textContent = `${lines} line(s)`;

    const tdLrc = document.querySelector(`.studio-grid td[data-row="${state.selectedTrackIdx}"][data-col="9"]`);
    if (tdLrc) {
      tdLrc.innerHTML = track.has_lrc
        ? `<span class="lrc-badge synced">Synced</span>`
        : `<span class="lrc-badge missing">None</span>`;
    }
  }

  function shiftLyricsOffset(deltaMs) {
    const track = state.tracks[state.selectedTrackIdx];
    if (!track || !track.lyrics) return;

    const timestampRegex = /\[(\d{1,2}):(\d{2})(?:\.(\d{2,3}))?\]/g;
    let shiftCount = 0;

    const shiftedText = track.lyrics.replace(timestampRegex, (match, minStr, secStr, msStr) => {
      shiftCount++;
      const min = parseInt(minStr, 10);
      const sec = parseInt(secStr, 10);
      let ms = 0;
      if (msStr) {
        ms = msStr.length === 2 ? parseInt(msStr, 10) * 10 : parseInt(msStr, 10);
      }
      let totalMs = (min * 60 + sec) * 1000 + ms + deltaMs;
      if (totalMs < 0) totalMs = 0;

      const newMin = Math.floor(totalMs / 60000);
      const remSecMs = totalMs % 60000;
      const newSec = Math.floor(remSecMs / 1000);
      const newHundredths = Math.floor((remSecMs % 1000) / 10);

      const mm = String(newMin).padStart(2, '0');
      const ss = String(newSec).padStart(2, '0');
      const xx = String(newHundredths).padStart(2, '0');
      return `[${mm}:${ss}.${xx}]`;
    });

    track.lyrics = shiftedText;
    document.getElementById('lyricsTextarea').value = shiftedText;
    showToast(`Shifted ${shiftCount} timestamp(s) by ${deltaMs > 0 ? '+' : ''}${deltaMs}ms`, 'info');
  }

  async function fetchLyricsForActiveTrack() {
    const track = state.tracks[state.selectedTrackIdx];
    if (!track || !track.title) {
      showToast('Track title is required to search LRCLIB', 'error');
      return;
    }
    try {
      showToast(`Fetching lyrics for "${track.title}"...`, 'info');
      const res = await apiRequest(`/api/lyrics/fetch?track=${encodeURIComponent(track.title)}&artist=${encodeURIComponent(track.artist || '')}`);
      
      const lyrics = res.lyrics_synced || res.lyrics_unsynced || '';
      if (lyrics) {
        track.lyrics = lyrics;
        track.has_lrc = !!res.lyrics_synced;
        updateLyricsEditorView();
        renderSpreadsheetGrid();
        showToast('Successfully fetched lyrics from LRCLIB', 'success');
      } else {
        showToast('No lyrics found on LRCLIB for this track', 'info');
      }
    } catch (err) {
      console.error('LRCLIB fetch error:', err);
      showToast(`Lyrics fetch failed: ${err.message}`, 'error');
    }
  }

  // ================= COMMIT & SAVE PIPELINE =================
  async function commitChanges() {
    if (!state.currentPath) {
      showToast('No album folder or track is loaded', 'error');
      return;
    }

    syncCommonAlbumFormToState();
    if (state.isEditing) {
      commitCellEdit();
    }

    const btn = document.getElementById('btnCommitChanges');
    btn.disabled = true;
    btn.innerHTML = '<span>⏳</span> Saving...';

    try {
      const payload = {
        path: state.currentPath,
        album_fields: {
          album: state.albumMeta.album,
          album_artist: state.albumMeta.album_artist,
          year: state.albumMeta.year,
          genre: state.albumMeta.genre,
          producers: state.albumMeta.producers,
          composers: state.albumMeta.composers,
        },
        tracks: state.tracks.map(t => ({
          filename: t.filename,
          track_number: t.track_number,
          title: t.title,
          artist: t.artist,
          album_artist: t.album_artist,
          album: t.album,
          year: t.year,
          genre: t.genre,
          producers: t.producers,
          composers: t.composers,
          lyrics: t.lyrics,
        })),
        cover_base64: state.cover.currentBase64,
        cover_url: state.cover.currentUrl,
        rescan: true,
      };

      const res = await apiRequest('/api/studio/commit', 'POST', payload);
      showToast(`Saved! ${res.message || 'Tags synchronized'}`, 'success');

      state.originalAlbumMeta = JSON.parse(JSON.stringify(state.albumMeta));
      state.originalTracks = JSON.parse(JSON.stringify(state.tracks));
      state.cover.isModified = false;
      state.cover.currentBase64 = null;
      if (res.updated_tags?.cover_url) {
        state.cover.originalUrl = res.updated_tags.cover_url;
      }
      updateArtworkView();
      renderSpreadsheetGrid();
    } catch (err) {
      console.error('Failed to commit metadata:', err);
      showToast(`Save failed: ${err.message}`, 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<span>💾</span> Save Changes';
    }
  }

  // ================= EXPANDED LIBRARY BROWSER WITH ARTIST TREE =================
  function getLibraryContainer() {
    return document.getElementById('library-list') || document.querySelector('.library-albums-container') || document.getElementById('artistTreeContainer');
  }

  // Global accordion toggler
  window.toggleArtistGroup = function(btn) {
    const group = btn.closest('.artist-tree-group');
    if (!group) return;
    const grid = group.querySelector('.artist-albums-grid');
    const chevron = group.querySelector('.chevron-icon') || group.querySelector('.artist-toggle-icon');
    if (!grid) return;
    const isOpen = grid.style.display !== 'none';

    grid.style.display = isOpen ? 'none' : 'grid';
    group.classList.toggle('collapsed', isOpen);
    if (chevron) {
      chevron.textContent = isOpen ? '▶' : '▼';
    }
  };

  // Global loader to open album in studio editor
  window.loadAlbumIntoStudio = function(path) {
    if (!path) return;
    inspectPath(path);
  };

  function renderLibraryList(albums) {
    const container = getLibraryContainer();
    if (!container) return;

    if (!albums || albums.length === 0) {
      container.innerHTML = '<div class="empty-state">No albums found in library.</div>';
      return;
    }

    // Group by artist cleanly
    const artistMap = {};
    albums.forEach(rawAlb => {
      const albPath = rawAlb.path || rawAlb.folder || '';
      const albTitle = rawAlb.title || rawAlb.album || 'Unknown Album';
      const artist = rawAlb.artist || rawAlb.album_artist || 'Unknown Artist';
      const coverUrl = rawAlb.cover_url || (albPath ? getApiUrl(`/api/cover?path=${encodeURIComponent(albPath)}`) : '/static/img/cover-placeholder.png');

      const album = {
        ...rawAlb,
        path: albPath,
        folder: albPath,
        title: albTitle,
        album: albTitle,
        artist: artist,
        cover_url: coverUrl,
        track_count: rawAlb.track_count || 0,
        year: rawAlb.year || '',
      };

      if (!artistMap[artist]) artistMap[artist] = [];
      artistMap[artist].push(album);
    });

    const sortedArtists = Object.keys(artistMap).sort((a, b) => a.localeCompare(b));

    container.innerHTML = sortedArtists.map((artist) => {
      const artistAlbums = artistMap[artist];
      const albumCount = artistAlbums.length;
      
      return `
        <div class="artist-tree-group" data-artist="${escapeHtml(artist)}">
          <button type="button" class="artist-tree-header" onclick="toggleArtistGroup(this)">
            <div class="artist-header-left">
              <span class="chevron-icon">▶</span>
              <span class="artist-name">${escapeHtml(artist)}</span>
            </div>
            <span class="artist-count-pill">${albumCount} ${albumCount === 1 ? 'album' : 'albums'}</span>
          </button>
          <div class="artist-albums-grid" style="display: none;">
            ${artistAlbums.map(alb => `
              <div class="library-album-card" onclick="loadAlbumIntoStudio('${escapeHtml(alb.path)}')">
                <div class="album-card-cover">
                  <img src="${alb.cover_url || '/static/img/cover-placeholder.png'}" 
                       alt="${escapeHtml(alb.title)}" 
                       loading="lazy"
                       onerror="this.onerror=null; this.src='/static/img/cover-placeholder.png';" />
                </div>
                <div class="album-card-meta">
                  <div class="album-card-title" title="${escapeHtml(alb.title)}">${escapeHtml(alb.title)}</div>
                  <div class="album-card-sub">
                    ${alb.year ? `<span>${alb.year}</span> • ` : ''}<span>${alb.track_count || 0} tracks</span>
                  </div>
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      `;
    }).join('');
  }

  const renderArtistTree = renderLibraryList;

  async function loadLibraryTree() {
    const container = getLibraryContainer();
    const summary = document.getElementById('libraryStatsSummary');
    if (container) {
      container.innerHTML = `<div style="text-align:center; padding:30px; color:var(--text-dim);">Scanning Navidrome library...</div>`;
    }

    try {
      const res = await apiRequest('/api/library');
      state.libraryAlbums = (res.albums || []).map(alb => ({
        ...alb,
        path: alb.path || alb.folder || '',
        folder: alb.folder || alb.path || '',
        title: alb.title || alb.album || 'Unknown Album',
        album: alb.album || alb.title || 'Unknown Album',
        artist: alb.artist || alb.album_artist || 'Unknown Artist',
        cover_url: alb.cover_url || getApiUrl(`/api/cover?path=${encodeURIComponent(alb.path || alb.folder || '')}`),
      }));

      // Count unique artists and tracks
      const artistSet = new Set();
      let totalTracks = 0;
      state.libraryAlbums.forEach(alb => {
        artistSet.add(alb.artist);
        totalTracks += (alb.track_count || 0);
      });

      if (summary) {
        summary.textContent = `${state.libraryAlbums.length} albums across ${artistSet.size} artists (${totalTracks} tracks)`;
      }
      renderLibraryList(state.libraryAlbums);
    } catch (err) {
      if (container) {
        container.innerHTML = `<div style="text-align:center; padding:30px; color:var(--accent-rose);">Failed to load library: ${escapeHtml(err.message)}</div>`;
      }
    }
  }

  function filterLibraryTree() {
    const query = (document.getElementById('libViewFilterInput')?.value || '').toLowerCase().trim();
    if (!query) {
      renderLibraryList(state.libraryAlbums);
      return;
    }
    const filtered = state.libraryAlbums.filter(a =>
      (a.title || a.album || '').toLowerCase().includes(query) ||
      (a.artist || '').toLowerCase().includes(query)
    );
    renderLibraryList(filtered);
  }

  // ================= VIEW 2: DOWNLOADER & TASK POLLING =================
  async function triggerDownload() {
    const input = document.getElementById('downloaderUrlInput');
    const url = input.value.trim();
    if (!url) {
      showToast('Please paste a streaming or YouTube URL', 'error');
      return;
    }

    try {
      showToast('Starting background ingestion pipeline...', 'info');
      const res = await apiRequest('/api/download', 'POST', { url });
      input.value = '';
      showToast('Download task registered!', 'success');
      fetchDownloaderTasks();
      startTaskPolling();
    } catch (err) {
      showToast(`Ingestion failed: ${err.message}`, 'error');
    }
  }

  async function fetchDownloaderTasks() {
    try {
      const data = await apiRequest('/api/tasks');
      state.downloadTasks = data.tasks || [];
      renderDownloaderTasks(state.downloadTasks);

      const activeTask = state.downloadTasks.find(t => t.status === 'active');
      const stepper = document.getElementById('taskStepperCard');
      if (activeTask) {
        stepper.style.display = 'flex';
        document.getElementById('taskMsg').textContent = activeTask.message || 'Processing audio stream...';
        document.getElementById('taskStatusBadge').textContent = (activeTask.stage || 'DOWNLOADING').toUpperCase();
        document.getElementById('taskProgressBar').style.width = `${activeTask.progress || 10}%`;

        const st = (activeTask.stage || '').toLowerCase();
        document.getElementById('step-download').className = 'step-item ' + (st.includes('download') ? 'active' : (activeTask.progress > 30 ? 'done' : ''));
        document.getElementById('step-tag').className = 'step-item ' + (st.includes('tag') ? 'active' : (activeTask.progress > 60 ? 'done' : ''));
        document.getElementById('step-lyrics').className = 'step-item ' + (st.includes('lyric') ? 'active' : (activeTask.progress > 80 ? 'done' : ''));
        document.getElementById('step-scan').className = 'step-item ' + (st.includes('scan') ? 'active' : (activeTask.progress >= 95 ? 'done' : ''));
      } else {
        stepper.style.display = 'none';
      }
    } catch (err) {
      console.debug('Task poll error:', err);
    }
  }

  function renderDownloaderTasks(tasks) {
    const listEl = document.getElementById('downloaderTasksList');
    listEl.innerHTML = '';

    if (!tasks.length) {
      listEl.innerHTML = `<div style="color:var(--text-dim); padding:16px; font-size:13px; text-align:center;">No recent download tasks.</div>`;
      return;
    }

    tasks.slice(0, 10).forEach(t => {
      const card = document.createElement('div');
      card.className = 'task-item-card';

      let statusCls = 'status-downloading';
      if (t.status === 'completed') statusCls = 'status-completed';
      else if (t.status === 'failed') statusCls = 'status-failed';

      const title = t.title || (t.url ? t.url.replace(/^https?:\/\/(www\.)?/, '') : `Task #${t.id}`);
      const sub = t.artist ? `${t.artist} • ${t.message || t.stage || ''}` : (t.message || t.url || '');

      const coverHtml = t.cover_url
        ? `<img src="${getApiUrl(t.cover_url)}" class="task-thumb" alt="Cover">`
        : `<div class="task-thumb">🎵</div>`;

      card.innerHTML = `
        ${coverHtml}
        <div class="task-meta">
          <div class="task-title-line" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
          <div class="task-subtext" title="${escapeHtml(sub)}">${escapeHtml(sub)}</div>
        </div>
        <span class="status-badge ${statusCls}">${t.status || 'ACTIVE'}</span>
      `;

      listEl.appendChild(card);
    });
  }

  function startTaskPolling() {
    if (state.taskPollTimer) return;
    state.taskPollTimer = setInterval(() => {
      if (state.currentView === 'downloader') {
        fetchDownloaderTasks();
      }
    }, 2500);
  }

  function stopTaskPolling() {
    if (state.taskPollTimer) {
      clearInterval(state.taskPollTimer);
      state.taskPollTimer = null;
    }
  }

  // Requests Queue handling
  async function fetchMusicRequests() {
    try {
      const data = await apiRequest('/api/requests');
      state.musicRequests = data.requests || [];
      renderMusicRequests(state.musicRequests);
    } catch (err) {
      console.debug('Request fetch error:', err);
    }
  }

  function renderMusicRequests(requests) {
    const listEl = document.getElementById('downloaderRequestsList');
    listEl.innerHTML = '';

    if (!requests.length) {
      listEl.innerHTML = `<div style="color:var(--text-dim); padding:16px; font-size:13px; text-align:center;">No pending requests in queue.</div>`;
      return;
    }

    requests.forEach(req => {
      const card = document.createElement('div');
      card.className = 'request-item-card';

      const isPending = req.status === 'pending';
      const actionsHtml = isPending
        ? `<div style="display:flex; gap:6px;">
             <button class="btn btn-primary" style="font-size:11px; padding:3px 8px;" data-id="${req.id}" data-action="approve">✓ Ingest</button>
             <button class="btn btn-secondary" style="font-size:11px; padding:3px 8px;" data-id="${req.id}" data-action="reject">✕</button>
           </div>`
        : `<span class="status-badge ${req.status === 'completed' ? 'status-completed' : 'status-failed'}">${req.status}</span>`;

      card.innerHTML = `
        <div class="task-meta">
          <div class="task-title-line">${escapeHtml(req.query_or_url || 'Untitled Request')}</div>
          <div class="task-subtext">Requested by ${escapeHtml(req.user_name || 'User')}</div>
        </div>
        ${actionsHtml}
      `;

      if (isPending) {
        card.querySelectorAll('button').forEach(btn => {
          btn.addEventListener('click', () => handleRequestAction(btn.dataset.id, btn.dataset.action));
        });
      }

      listEl.appendChild(card);
    });
  }

  async function submitNewRequest() {
    const input = document.getElementById('requestQueryInput');
    const val = input.value.trim();
    if (!val) return;
    try {
      await apiRequest('/api/requests/submit', 'POST', { query_or_url: val });
      input.value = '';
      showToast('Music request queued successfully', 'success');
      fetchMusicRequests();
    } catch (err) {
      showToast(`Request failed: ${err.message}`, 'error');
    }
  }

  async function handleRequestAction(reqId, action) {
    try {
      await apiRequest('/api/requests/action', 'POST', { request_id: reqId, action });
      showToast(`Request ${action}d!`, 'success');
      fetchMusicRequests();
      fetchDownloaderTasks();
    } catch (err) {
      showToast(`Action failed: ${err.message}`, 'error');
    }
  }

  async function clearCompletedRequests() {
    try {
      await apiRequest('/api/requests/clear', 'POST', { status: 'completed_only' });
      showToast('Cleared resolved requests from history', 'info');
      fetchMusicRequests();
    } catch (err) {
      showToast(`Clear failed: ${err.message}`, 'error');
    }
  }

  // ================= MODALS & AUTH =================
  function openAuthModal() {
    const modal = document.getElementById('modalAuth');
    document.getElementById('authInputToken').value = getAuthToken();
    modal.classList.add('active');
  }

  function saveAuthToken() {
    const token = document.getElementById('authInputToken').value.trim();
    if (token) {
      localStorage.setItem('aura_auth_token', token);
      showToast('Saved authentication token', 'success');
    } else {
      localStorage.removeItem('aura_auth_token');
      showToast('Cleared saved token', 'info');
    }
    document.getElementById('modalAuth').classList.remove('active');
  }

  function toggleShortcutsModal() {
    document.getElementById('modalShortcuts').classList.toggle('active');
  }

  // ================= KEYBOARD NAVIGATION & EVENTS =================
  function initKeyboardNavigation() {
    window.addEventListener('keydown', (e) => {
      // 1. Global Shortcuts
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        commitChanges();
        return;
      }

      if (e.key === '?' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
        e.preventDefault();
        toggleShortcutsModal();
        return;
      }

      if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.active').forEach(m => m.classList.remove('active'));
      }

      // 2. Spreadsheet Grid Selection & Arrows (only in studio view)
      if (state.currentView !== 'studio') return;

      if (!state.isEditing && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
        // Clear cells on Delete / Backspace
        if (e.key === 'Delete' || e.key === 'Backspace') {
          e.preventDefault();
          clearSelectedCells();
          return;
        }

        // Shift + Arrows for rectangular range expansion
        if (e.shiftKey && ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
          e.preventDefault();
          if (e.key === 'ArrowUp') state.activeCell.row = Math.max(0, state.activeCell.row - 1);
          else if (e.key === 'ArrowDown') state.activeCell.row = Math.min(state.tracks.length - 1, state.activeCell.row + 1);
          else if (e.key === 'ArrowLeft') state.activeCell.col = Math.max(0, state.activeCell.col - 1);
          else if (e.key === 'ArrowRight') state.activeCell.col = Math.min(COLUMNS.length - 1, state.activeCell.col + 1);

          updateRangeSelection(state.selectionAnchor, state.activeCell);
          scrollActiveCellIntoView();
          return;
        }

        // Standard Single-Cell Navigation
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          setActiveCell(state.activeCell.row - 1, state.activeCell.col, false);
          state.selectionAnchor = { ...state.activeCell };
          state.selectedCells.clear();
          state.selectedCells.add(`${state.activeCell.row}:${state.activeCell.col}`);
          updateRangeSelection(state.selectionAnchor, state.activeCell);
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          setActiveCell(state.activeCell.row + 1, state.activeCell.col, false);
          state.selectionAnchor = { ...state.activeCell };
          state.selectedCells.clear();
          state.selectedCells.add(`${state.activeCell.row}:${state.activeCell.col}`);
          updateRangeSelection(state.selectionAnchor, state.activeCell);
        } else if (e.key === 'ArrowLeft') {
          e.preventDefault();
          setActiveCell(state.activeCell.row, state.activeCell.col - 1, false);
          state.selectionAnchor = { ...state.activeCell };
          state.selectedCells.clear();
          state.selectedCells.add(`${state.activeCell.row}:${state.activeCell.col}`);
          updateRangeSelection(state.selectionAnchor, state.activeCell);
        } else if (e.key === 'ArrowRight') {
          e.preventDefault();
          setActiveCell(state.activeCell.row, state.activeCell.col + 1, false);
          state.selectionAnchor = { ...state.activeCell };
          state.selectedCells.clear();
          state.selectedCells.add(`${state.activeCell.row}:${state.activeCell.col}`);
          updateRangeSelection(state.selectionAnchor, state.activeCell);
        } else if (e.key === 'Enter') {
          e.preventDefault();
          startCellEdit();
        } else if (e.key === 'Tab') {
          e.preventDefault();
          if (e.shiftKey) moveToPreviousEditableCell();
          else moveToNextEditableCell();
        }
      }
    });

    // Clipboard Paste Listener
    window.addEventListener('paste', handleClipboardPaste);

    // Grid Cell Clicks (Single & Shift-Click)
    const tbody = document.getElementById('gridTbody');
    tbody.addEventListener('click', (e) => {
      const td = e.target.closest('td');
      if (!td) return;
      const r = parseInt(td.dataset.row, 10);
      const c = parseInt(td.dataset.col, 10);
      if (isNaN(r) || isNaN(c)) return;

      if (e.shiftKey) {
        // Expand bounding box selection
        state.activeCell = { row: r, col: c };
        updateRangeSelection(state.selectionAnchor, state.activeCell);
      } else {
        // Single selection
        state.selectionAnchor = { row: r, col: c };
        state.selectedCells.clear();
        state.selectedCells.add(`${r}:${c}`);
        setActiveCell(r, c, false);
        updateRangeSelection(state.selectionAnchor, state.activeCell);
      }
    });

    tbody.addEventListener('dblclick', (e) => {
      const td = e.target.closest('td');
      if (!td) return;
      const r = parseInt(td.dataset.row, 10);
      const c = parseInt(td.dataset.col, 10);
      if (!isNaN(r) && !isNaN(c)) {
        setActiveCell(r, c, true);
      }
    });

    // Column Header Broadcast Buttons
    document.querySelectorAll('.col-broadcast-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const colIdx = parseInt(btn.dataset.col, 10);
        broadcastColumnValue(colIdx);
      });
    });
  }

  function initUiEvents() {
    // Mode Switcher Buttons
    document.querySelectorAll('.nav-view-btn').forEach(btn => {
      btn.addEventListener('click', () => switchView(btn.dataset.view));
    });

    // Top Nav buttons
    document.getElementById('btnCommitChanges').addEventListener('click', commitChanges);
    document.getElementById('btnShortcuts').addEventListener('click', toggleShortcutsModal);
    document.getElementById('btnAuthSettings').addEventListener('click', openAuthModal);

    // Search external metadata
    const searchInput = document.getElementById('navSearchInput');
    const searchBtn = document.getElementById('btnSearchExternal');
    searchBtn.addEventListener('click', () => searchExternal(searchInput.value));
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') searchExternal(searchInput.value);
    });

    // Studio workspace tabs
    const tabGrid = document.getElementById('tabBtnGrid');
    const tabDiff = document.getElementById('tabBtnDiff');
    const gridContainer = document.getElementById('gridContainer');
    const diffContainer = document.getElementById('diffContainer');

    tabGrid.addEventListener('click', () => {
      tabGrid.classList.add('active');
      tabDiff.classList.remove('active');
      gridContainer.style.display = 'block';
      diffContainer.style.display = 'none';
    });

    tabDiff.addEventListener('click', () => {
      tabDiff.classList.add('active');
      tabGrid.classList.remove('active');
      diffContainer.style.display = 'flex';
      gridContainer.style.display = 'none';
    });

    // Grid actions
    document.getElementById('btnAutoNumber').addEventListener('click', autoNumberTracks);
    document.getElementById('btnDiscardChanges').addEventListener('click', discardAllChanges);
    document.getElementById('btnApplyCommonToTracks').addEventListener('click', applyCommonFieldsToAllTracks);

    // Artwork actions
    const dropzone = document.getElementById('artDropzone');
    const fileInput = document.getElementById('artFileInput');
    document.getElementById('btnBrowseArt').addEventListener('click', () => fileInput.click());
    dropzone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', (e) => {
      if (e.target.files?.[0]) handleArtFile(e.target.files[0]);
    });

    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      if (e.dataTransfer.files?.[0]) handleArtFile(e.dataTransfer.files[0]);
    });

    document.getElementById('btnResetArt').addEventListener('click', resetArtwork);

    // Diff candidate apply all
    document.getElementById('btnApplyAllCandidateFields').addEventListener('click', applyAllCandidateFields);

    // Lyrics events
    document.getElementById('lyricsTextarea').addEventListener('input', handleLyricsTextareaInput);
    document.getElementById('btnOffsetMinus500').addEventListener('click', () => shiftLyricsOffset(-500));
    document.getElementById('btnOffsetMinus200').addEventListener('click', () => shiftLyricsOffset(-200));
    document.getElementById('btnOffsetPlus200').addEventListener('click', () => shiftLyricsOffset(200));
    document.getElementById('btnOffsetPlus500').addEventListener('click', () => shiftLyricsOffset(500));
    document.getElementById('btnFetchLrcLib').addEventListener('click', fetchLyricsForActiveTrack);

    // Downloader events
    document.getElementById('btnStartDownload').addEventListener('click', triggerDownload);
    document.getElementById('downloaderUrlInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') triggerDownload();
    });
    document.getElementById('btnRefreshTasks').addEventListener('click', fetchDownloaderTasks);
    document.getElementById('btnRefreshRequests').addEventListener('click', fetchMusicRequests);
    document.getElementById('btnSubmitRequest').addEventListener('click', submitNewRequest);
    document.getElementById('requestQueryInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitNewRequest();
    });
    document.getElementById('btnClearRequests').addEventListener('click', clearCompletedRequests);

    // Quality selector pills
    document.querySelectorAll('.quality-pill').forEach(pill => {
      pill.addEventListener('click', () => {
        document.querySelectorAll('.quality-pill').forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        state.selectedQuality = pill.dataset.quality;
      });
    });

    // Library view filter
    document.getElementById('libViewFilterInput').addEventListener('input', filterLibraryTree);

    // Modals
    document.getElementById('btnCloseShortcutsModal').addEventListener('click', toggleShortcutsModal);
    document.getElementById('btnDismissShortcutsModal').addEventListener('click', toggleShortcutsModal);

    document.getElementById('btnCloseAuthModal').addEventListener('click', () => document.getElementById('modalAuth').classList.remove('active'));
    document.getElementById('btnDismissAuthModal').addEventListener('click', () => document.getElementById('modalAuth').classList.remove('active'));
    document.getElementById('btnSaveAuthToken').addEventListener('click', saveAuthToken);

    // Back to dashboard link
    document.getElementById('btnDashboardLink').href = getApiUrl('/');
  }

  // ================= INITIALIZATION =================
  function init() {
    initKeyboardNavigation();
    initUiEvents();

    const params = new URLSearchParams(window.location.search);
    const targetPath = params.get('path');
    if (targetPath) {
      inspectPath(targetPath);
    } else {
      switchView('library');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
