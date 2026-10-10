/**
 * Aura Hub — Desktop Metadata Studio
 * State management, spreadsheet grid keyboard navigation, side-by-side diffing,
 * artwork management, and synced lyrics timestamp shifting.
 */

(function () {
  'use strict';

  // ================= STATE =================
  const state = {
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
  };

  // Editable column mappings: table column index -> field key
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
    // 1. URL search params
    const params = new URLSearchParams(window.location.search);
    if (params.get('token')) return params.get('token');
    if (params.get('auth_token')) return params.get('auth_token');

    // 2. Telegram WebApp initData if running in Telegram
    if (window.Telegram?.WebApp?.initData) {
      return window.Telegram.WebApp.initData;
    }

    // 3. Local storage token
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
      showToast('Authentication required. Click the key icon to provide your token.', 'error');
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
      throw new Error(data.detail || data.message || `Request failed with status ${res.status}`);
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

  // ================= DATA LOADING =================
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
    const folderName = state.albumMeta.album || state.currentPath.split('/').pop() || 'Untitled';
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

      // Load dimensions
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

  // ================= SPREADSHEET GRID & EDITING =================
  function renderSpreadsheetGrid() {
    const tbody = document.getElementById('gridTbody');
    tbody.innerHTML = '';

    if (!state.tracks.length) {
      tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:30px; color:var(--text-dim);">No tracks loaded. Click 'Browse Library' to pick an album.</td></tr>`;
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

        // Check active cell
        if (state.activeCell.row === rIdx && state.activeCell.col === cIdx) {
          td.classList.add('cell-active');
        }

        // Check if modified compared to original
        const origTrack = state.originalTracks[rIdx];
        if (origTrack && col.editable) {
          const currentVal = formatFieldValue(track[col.key], col.type);
          const origVal = formatFieldValue(origTrack[col.key], col.type);
          if (currentVal !== origVal) {
            td.classList.add('cell-modified');
          }
        }

        if (col.editable) {
          td.classList.add('editable');
        }

        // Render cell content
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

    // Update row highlighting
    document.querySelectorAll('.studio-grid tbody tr').forEach((tr, idx) => {
      tr.classList.toggle('selected', idx === rIdx);
    });

    // Update lyrics editor for newly selected track
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

    // Also select that track row
    selectTrackRow(state.activeCell.row);

    // Re-render highlight
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
        // Move to next row in same col
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

  // ================= EXTERNAL METADATA & DIFF INSPECTOR =================
  async function searchExternal(query, artist = '') {
    const cleanQ = query.trim();
    if (!cleanQ) return;
    try {
      document.getElementById('diffStatusNote').textContent = `Querying MusicBrainz, Deezer, iTunes, Spotify, LRCLIB for "${cleanQ}"...`;
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

      card.innerHTML = `
        ${isRec ? '<span class="candidate-rec-tag">Recommended</span>' : ''}
        <div class="candidate-header">
          <span class="candidate-source-badge">${escapeHtml(cand.source || 'Provider')}</span>
          <span class="candidate-conf-badge">${conf}% Match</span>
        </div>
        <div class="candidate-title">${escapeHtml(prev.album || prev.title || 'Untitled')}</div>
        <div class="candidate-artist">${escapeHtml(prev.artist || 'Unknown')}</div>
        <div style="font-size:11px; color:var(--text-dim); display:flex; justify-content:space-between;">
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
      if (art && !artUrls.includes(art)) {
        artUrls.push({ url: art, source: c.source });
      }
    });

    if (artUrls.length) {
      section.style.display = 'block';
      artUrls.slice(0, 6).forEach(item => {
        const thumb = document.createElement('div');
        thumb.className = 'provider-art-thumb';
        thumb.innerHTML = `
          <img src="${item.url}" alt="Cover">
          <span class="src-badge">${item.source}</span>
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

    titleEl.textContent = prev.album || prev.title || 'Candidate Diff';
    srcEl.textContent = cand.source || 'Provider';

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

    // If candidate has tracks list, align track titles by position
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

    // Candidate artwork if available
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

    // Update row badge in grid without re-rendering everything
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

    // Regex for [mm:ss.xx] or [mm:ss.xxx]
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

      // Update baseline snapshots
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

  // ================= LIBRARY BROWSER MODAL =================
  async function openLibraryModal() {
    const modal = document.getElementById('modalLibrary');
    const listEl = document.getElementById('libAlbumList');
    modal.classList.add('active');

    if (!state.libraryAlbums.length) {
      listEl.innerHTML = `<div style="text-align:center; padding:20px; color:var(--text-dim);">Loading library albums...</div>`;
      try {
        const res = await apiRequest('/api/library');
        state.libraryAlbums = res.albums || [];
        renderLibraryList(state.libraryAlbums);
      } catch (err) {
        listEl.innerHTML = `<div style="text-align:center; padding:20px; color:var(--accent-rose);">Failed to load library: ${escapeHtml(err.message)}</div>`;
      }
    } else {
      renderLibraryList(state.libraryAlbums);
    }
  }

  function renderLibraryList(albums) {
    const listEl = document.getElementById('libAlbumList');
    listEl.innerHTML = '';

    if (!albums.length) {
      listEl.innerHTML = `<div style="text-align:center; padding:20px; color:var(--text-dim);">No matching albums found.</div>`;
      return;
    }

    albums.forEach(alb => {
      const item = document.createElement('div');
      item.className = 'lib-album-item';
      
      const coverUrl = getApiUrl(`/api/cover?path=${encodeURIComponent(alb.folder)}`);
      item.innerHTML = `
        <img class="lib-thumb" src="${coverUrl}" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' viewBox=\\'0 0 24 24\\'><rect fill=\\'%231a1e2d\\' width=\\'24\\' height=\\'24\\'/></svg>'">
        <div class="lib-meta">
          <div class="lib-meta-title">${escapeHtml(alb.album || alb.folder)}</div>
          <div class="lib-meta-artist">${escapeHtml(alb.artist || 'Unknown Artist')}</div>
          <div class="lib-meta-counts">${alb.track_count || 0} tracks • ${alb.lrc_count || 0} LRC</div>
        </div>
      `;

      item.addEventListener('click', () => {
        document.getElementById('modalLibrary').classList.remove('active');
        inspectPath(alb.folder);
      });

      listEl.appendChild(item);
    });
  }

  function filterLibraryList() {
    const filter = document.getElementById('libFilterInput').value.toLowerCase().trim();
    if (!filter) {
      renderLibraryList(state.libraryAlbums);
      return;
    }
    const filtered = state.libraryAlbums.filter(a =>
      (a.album || '').toLowerCase().includes(filter) ||
      (a.artist || '').toLowerCase().includes(filter) ||
      (a.folder || '').toLowerCase().includes(filter)
    );
    renderLibraryList(filtered);
  }

  // ================= MODALS & UTILITIES =================
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
    const modal = document.getElementById('modalShortcuts');
    modal.classList.toggle('active');
  }

  // ================= KEYBOARD & EVENT LISTENERS =================
  function initKeyboardNavigation() {
    window.addEventListener('keydown', (e) => {
      // 1. Global shortcuts
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

      // 2. Modals dismiss on Escape
      if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.active').forEach(m => m.classList.remove('active'));
      }

      // 3. Grid navigation when not editing inline input
      if (!state.isEditing && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          setActiveCell(state.activeCell.row - 1, state.activeCell.col, false);
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          setActiveCell(state.activeCell.row + 1, state.activeCell.col, false);
        } else if (e.key === 'ArrowLeft') {
          e.preventDefault();
          setActiveCell(state.activeCell.row, state.activeCell.col - 1, false);
        } else if (e.key === 'ArrowRight') {
          e.preventDefault();
          setActiveCell(state.activeCell.row, state.activeCell.col + 1, false);
        } else if (e.key === 'Enter') {
          e.preventDefault();
          startCellEdit();
        } else if (e.key === 'Tab') {
          e.preventDefault();
          if (e.shiftKey) {
            moveToPreviousEditableCell();
          } else {
            moveToNextEditableCell();
          }
        }
      }
    });

    // Grid cell clicks
    const tbody = document.getElementById('gridTbody');
    tbody.addEventListener('click', (e) => {
      const td = e.target.closest('td');
      if (!td) return;
      const r = parseInt(td.dataset.row, 10);
      const c = parseInt(td.dataset.col, 10);
      if (!isNaN(r) && !isNaN(c)) {
        setActiveCell(r, c, false);
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
  }

  function initUiEvents() {
    // Top nav buttons
    document.getElementById('btnCommitChanges').addEventListener('click', commitChanges);
    document.getElementById('btnOpenLibrary').addEventListener('click', openLibraryModal);
    document.getElementById('btnShortcuts').addEventListener('click', toggleShortcutsModal);
    document.getElementById('btnAuthSettings').addEventListener('click', openAuthModal);

    // Search external metadata
    const searchInput = document.getElementById('navSearchInput');
    const searchBtn = document.getElementById('btnSearchExternal');
    searchBtn.addEventListener('click', () => searchExternal(searchInput.value));
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') searchExternal(searchInput.value);
    });

    // Workspace tabs
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

    // Modals
    document.getElementById('btnCloseLibraryModal').addEventListener('click', () => document.getElementById('modalLibrary').classList.remove('active'));
    document.getElementById('btnDismissLibraryModal').addEventListener('click', () => document.getElementById('modalLibrary').classList.remove('active'));
    document.getElementById('libFilterInput').addEventListener('input', filterLibraryList);

    document.getElementById('btnCloseShortcutsModal').addEventListener('click', () => document.getElementById('modalShortcuts').classList.remove('active'));
    document.getElementById('btnDismissShortcutsModal').addEventListener('click', () => document.getElementById('modalShortcuts').classList.remove('active'));

    document.getElementById('btnCloseAuthModal').addEventListener('click', () => document.getElementById('modalAuth').classList.remove('active'));
    document.getElementById('btnDismissAuthModal').addEventListener('click', () => document.getElementById('modalAuth').classList.remove('active'));
    document.getElementById('btnSaveAuthToken').addEventListener('click', saveAuthToken);

    // Back to hub button subpath resolution
    document.getElementById('btnDashboardLink').href = getApiUrl('/');
  }

  // ================= INITIALIZATION =================
  function init() {
    initKeyboardNavigation();
    initUiEvents();

    // Check URL parameters for path
    const params = new URLSearchParams(window.location.search);
    const targetPath = params.get('path');
    if (targetPath) {
      inspectPath(targetPath);
    } else {
      // Auto open library browser modal to let user pick album
      openLibraryModal();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
