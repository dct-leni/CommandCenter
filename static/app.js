/**
 * CommandCenter — Frontend Application Logic
 * Handles API communication, UI rendering, and user interactions.
 */

// ──────────────────────────────────────────────
//  State
// ──────────────────────────────────────────────

const state = {
    config: null,
    converterFiles: [],
    streamerFolders: [],
    streamStatus: null,
    systemStatus: null,
    folderBrowserTarget: null, // 'converter' or 'streamer'
    folderBrowserPath: '',
    expandedFolders: new Set(),
    pollingInterval: null,
    liveStreams: [],
    draggedSlotFile: null,
};

// ──────────────────────────────────────────────
//  API Client
// ──────────────────────────────────────────────

async function api(method, path, body = null) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);

    try {
        const res = await fetch(`/api${path}`, opts);
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || `HTTP ${res.status}`);
        }
        return data;
    } catch (err) {
        console.error(`API ${method} ${path} failed:`, err);
        throw err;
    }
}

function showLoadingOverlay(title = 'Processing File Transfer...', message = 'Moving large video file(s). Please wait while the operation completes.') {
    const overlay = document.getElementById('loading-overlay');
    const titleEl = document.getElementById('loading-overlay-title');
    const msgEl = document.getElementById('loading-overlay-message');
    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message;
    if (overlay) overlay.style.display = 'flex';
}

function hideLoadingOverlay() {
    const overlay = document.getElementById('loading-overlay');
    if (overlay) overlay.style.display = 'none';
}

window.showLoadingOverlay = showLoadingOverlay;
window.hideLoadingOverlay = hideLoadingOverlay;

async function rescanFolders(converter = true, streamer = true) {
    if (converter && state.config?.converter?.source_folder) {
        await scanConverterFolder(state.config.converter.source_folder);
    }
    if (streamer && state.config?.streamer?.content_folder) {
        if (state.streamStatus?.is_running) {
            try {
                const data = await api('GET', '/streamer/folders');
                await setStreamerFoldersAndRefresh(data.folders);
                const startBtn = document.getElementById('stream-start-btn');
                if (startBtn) startBtn.disabled = false;
            } catch (e) {
                await scanStreamerFolder(state.config.streamer.content_folder);
            }
        } else {
            await scanStreamerFolder(state.config.streamer.content_folder);
        }
    }
    await fetchLiveStreams();
}

async function updateConfigSetting(updates, rescan = false) {
    try {
        const data = await api('PUT', '/config', updates);
        if (data && typeof data === 'object') {
            state.config = data;
        }
        if (rescan) await rescanFolders();
        else renderStreamerFolders();
        showToast('Settings saved', 'success');
    } catch (err) {
        console.error(err);
    }
}

function formatMetaBadge(meta) {
    if (!meta || meta.error) return '';

    let bitrateStr = '';
    if (meta.video_bitrate || meta.audio_bitrate) {
        const vBit = meta.video_bitrate ? `V: ${formatBitrate(meta.video_bitrate)}` : '';
        const aBit = meta.audio_bitrate ? `A: ${formatBitrate(meta.audio_bitrate)}` : '';
        bitrateStr = [vBit, aBit].filter(Boolean).join(' · ');
    } else if (meta.bitrate) {
        bitrateStr = formatBitrate(meta.bitrate);
    }

    const parts = [
        meta.duration ? formatDuration(meta.duration) : '',
        meta.width && meta.height ? `${meta.width}×${meta.height}` : '',
        meta.codec && meta.codec !== 'unknown' ? meta.codec.toUpperCase() : '',
        meta.fps ? `${Math.round(meta.fps)}fps` : '',
        bitrateStr
    ].filter(Boolean);
    return parts.join(' · ');
}

function getThumbHtml(src, hasThumb = true, className = 'slot-file-thumb', placeholderIcon = 'fa-film') {
    const placeholder = `<div class="${className}-placeholder"><i class="fa-solid ${placeholderIcon}"></i></div>`;
    if (hasThumb === false) return placeholder;
    const safePlaceholder = placeholder.replace(/"/g, '&quot;').replace(/'/g, "\\'");
    return `<img class="${className}" src="${src}" alt="" loading="lazy" onerror="this.outerHTML='${safePlaceholder}'">`;
}

// ──────────────────────────────────────────────
//  Initialization
// ──────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
    // Load config (retrying in case of server restart)
    let configLoaded = false;
    for (let attempt = 1; attempt <= 6; attempt++) {
        try {
            state.config = await api('GET', '/config');
            applyConfig();
            configLoaded = true;
            break;
        } catch (e) {
            console.warn(`Attempt ${attempt} to load config failed:`, e);
            if (attempt < 6) {
                await new Promise(resolve => setTimeout(resolve, 1000));
            }
        }
    }
    if (!configLoaded) {
        showToast('Failed to load config', 'error');
    }

    // Check system status
    await checkSystemStatus();

    // Wire up event handlers
    document.getElementById('converter-browse-btn').addEventListener('click', () => openFolderBrowser('converter'));
    document.getElementById('streamer-browse-btn').addEventListener('click', () => openFolderBrowser('streamer'));
    document.getElementById('convert-all-btn').addEventListener('click', () => {
        const converting = state.converterFiles.filter(f => f.status === 'converting').length;
        const queued = state.converterFiles.filter(f => f.status === 'queued').length;
        if (converting > 0 || queued > 0) {
            stopConversion();
        } else {
            convertAll();
        }
    });
    document.getElementById('stream-start-btn').addEventListener('click', () => {
        if (state.streamStatus && state.streamStatus.is_running) {
            stopStreaming();
        } else {
            startStreaming();
        }
    });
    document.getElementById('modal-close-btn').addEventListener('click', closeFolderBrowser);
    document.getElementById('modal-select-btn').addEventListener('click', selectFolder);
    document.getElementById('file-picker-close').addEventListener('click', () => {
        document.getElementById('file-picker-modal').style.display = 'none';
    });

    // Close modals on overlay click
    document.getElementById('folder-modal').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeFolderBrowser();
    });
    document.getElementById('file-picker-modal').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) document.getElementById('file-picker-modal').style.display = 'none';
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeFolderBrowser();
            document.getElementById('file-picker-modal').style.display = 'none';
        }
    });

    // Start polling
    startPolling();

    // Enable drag & drop file upload on the converter panel
    initConverterDropZone();

    // If config has saved folders, auto-scan
    await rescanFolders();

    document.getElementById('stream-protocol').addEventListener('change', (e) => {
        if (state.config?.streamer) state.config.streamer.protocol = e.target.value;
        updateConfigSetting({ streamer: { protocol: e.target.value } });
    });
    document.getElementById('port-start').addEventListener('change', (e) => {
        const val = parseInt(e.target.value);
        if (state.config?.streamer && !isNaN(val)) state.config.streamer.port_range_start = val;
        updateConfigSetting({ streamer: { port_range_start: val } }, true);
    });
    document.getElementById('port-end').addEventListener('change', (e) => {
        const val = parseInt(e.target.value);
        if (state.config?.streamer && !isNaN(val)) state.config.streamer.port_range_end = val;
        updateConfigSetting({ streamer: { port_range_end: val } }, true);
    });
});

function applyConfig() {
    if (!state.config) return;
    const { converter, streamer, server } = state.config;

    if (converter?.source_folder) {
        document.getElementById('converter-folder').value = converter.source_folder;
    }
    if (streamer?.content_folder) {
        document.getElementById('streamer-folder').value = streamer.content_folder;
    }
    if (streamer?.port_range_start) {
        document.getElementById('port-start').value = streamer.port_range_start;
    }
    if (streamer?.port_range_end) {
        document.getElementById('port-end').value = streamer.port_range_end;
    }
    if (streamer?.protocol) {
        document.getElementById('stream-protocol').value = streamer.protocol;
    }
}

// ──────────────────────────────────────────────
//  System Status
// ──────────────────────────────────────────────

async function checkSystemStatus() {
    try {
        state.systemStatus = await api('GET', '/system/status');
        const ffmpegDot = document.getElementById('ffmpeg-dot');
        const mediamtxDot = document.getElementById('mediamtx-dot');
        const codecLabel = document.getElementById('codec-status-label');

        ffmpegDot.className = `status-dot ${state.systemStatus.ffmpeg ? 'ok' : 'error'}`;
        mediamtxDot.className = `status-dot ${state.systemStatus.mediamtx ? 'ok' : 'error'}`;
        if (codecLabel && state.systemStatus.best_encoder) {
            codecLabel.textContent = `Codec: ${state.systemStatus.best_encoder}`;
        }
    } catch (e) {
        console.error('System status check failed:', e);
    }
}

// ──────────────────────────────────────────────
//  Polling
// ──────────────────────────────────────────────

let pollCount = 0;
function startPolling() {
    if (state.pollingInterval) clearInterval(state.pollingInterval);
    state.pollingInterval = setInterval(async () => {
        await pollConverterStatus();
        await pollStreamStatus();

        pollCount++;
        if (pollCount % 5 === 0) {
            await checkSystemStatus();
        }
    }, 2000);
}

async function pollConverterStatus() {
    if (state.converterFiles.length === 0) return;

    try {
        const data = await api('GET', '/converter/status');
        state.converterFiles = data.files;
        renderConverterFiles();
    } catch (e) {
        // Silent fail on poll
    }
}

async function pollStreamStatus() {
    try {
        const data = await api('GET', '/streamer/status');
        state.streamStatus = data;
        updateStreamUI();
        const lsData = await api('GET', '/streamer/live_streams');
        state.liveStreams = lsData.live_streams || [];
        renderLiveStreams();
    } catch (e) {
        // Silent fail on poll
    }
}

// ──────────────────────────────────────────────
//  Converter
// ──────────────────────────────────────────────

async function scanConverterFolder(path) {
    try {
        const data = await api('POST', '/converter/scan', { path });
        state.converterFiles = data.files;
        renderConverterFiles();
        updateConverterCounter();
        document.getElementById('convert-all-btn').disabled = false;
    } catch (e) {
        showToast(`Failed to scan folder: ${e.message}`, 'error');
    }
}

function renderConverterFiles() {
    const container = document.getElementById('converter-file-list');

    if (state.converterFiles.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon"><i class="fa-regular fa-folder-open"></i></div>
                <p>No video files found in this folder</p>
            </div>
        `;
        return;
    }

    container.innerHTML = state.converterFiles.map(file => {
        let statusHtml = '';
        let actionHtml = '';

        switch (file.status) {
            case 'done':
                statusHtml = '<span class="status-badge done">Done</span>';
                break;
            case 'converting':
                const pct = Math.round(file.progress * 100);
                statusHtml = `
                    <span class="status-badge converting">${pct}%</span>
                    <div class="progress-bar"><div class="progress-bar-fill" style="width:${pct}%"></div></div>
                `;
                break;
            case 'queued':
                statusHtml = '<span class="status-badge queued">Queued</span>';
                break;
            case 'error':
                statusHtml = `<span class="status-badge error" title="${escapeHtml(file.error)}">Error</span>`;
                break;
            case 'pending':
            default:
                actionHtml = `<button class="btn btn-sm btn-primary" onclick="convertFile('${escapeHtml(file.filename)}')">Convert</button>`;
                break;
        }

        const thumbHtml = getThumbHtml(`/api/converter/thumbnail/${encodeURIComponent(file.filename)}`, file.has_thumbnail, 'file-thumb');
        const isDraggable = (file.extension === '.ts' || file.status === 'done');
        const dragAttr = isDraggable ? 'draggable="true"' : '';
        const dragEvent = isDraggable ? `ondragstart="handleDragStart(event, '${escapeAttr(file.filename)}')"` : '';

        const metaBadge = formatMetaBadge(file.metadata);
        const metaDetails = [
            formatSize(file.size),
            file.extension.toUpperCase().replace('.', ''),
            metaBadge
        ].filter(Boolean).join(' • ');

        let notesList = [];
        if (file.audio_note) notesList.push(`<i class="fa-solid fa-volume-high"></i> ${escapeHtml(file.audio_note)}`);
        if (file.scaled_note) notesList.push(`<i class="fa-solid fa-ruler"></i> ${escapeHtml(file.scaled_note)}`);
        const notesHtml = notesList.length ? `<div class="file-notes" style="font-size: 11px; color: var(--text-muted); margin-top: 3px;">${notesList.join(' • ')}</div>` : '';

        return `
            <div class="file-item" ${dragAttr} ${dragEvent}>
                ${thumbHtml}
                <div class="file-info">
                    <div class="file-name" title="${escapeHtml(file.filename)}">${escapeHtml(file.filename)}</div>
                    <div class="file-meta">${metaDetails}</div>
                    ${notesHtml}
                </div>
                <div class="file-actions">
                    ${statusHtml}
                    ${actionHtml}
                </div>
            </div>
        `;
    }).join('');

    updateConverterCounter();
}

function updateConverterCounter() {
    const total = state.converterFiles.length;
    const done = state.converterFiles.filter(f => f.status === 'done').length;
    const converting = state.converterFiles.filter(f => f.status === 'converting').length;
    const queued = state.converterFiles.filter(f => f.status === 'queued').length;
    const counter = document.getElementById('converter-counter');

    if (total === 0) {
        counter.textContent = 'No files';
    } else if (converting > 0 || queued > 0) {
        let msg = `${done}/${total} done`;
        if (converting > 0) msg += ` · ${converting} converting`;
        if (queued > 0) msg += ` · ${queued} queued`;
        counter.textContent = msg;
    } else {
        counter.textContent = `${done}/${total} done`;
    }

    // Toggle Convert All / Stop Convert button states dynamically
    const convertAllBtn = document.getElementById('convert-all-btn');
    const pending = state.converterFiles.filter(f => f.status === 'pending').length;

    if (converting > 0 || queued > 0) {
        convertAllBtn.textContent = 'Stop Convert';
        convertAllBtn.className = 'btn btn-danger';
        convertAllBtn.disabled = false;
    } else {
        convertAllBtn.textContent = 'Convert All';
        convertAllBtn.className = 'btn btn-primary';
        convertAllBtn.disabled = pending === 0;
    }
}

async function convertFile(filename) {
    try {
        await api('POST', '/converter/convert', { filename });
        showToast(`Converting: ${filename}`, 'success');
        if (state.config?.converter?.source_folder) {
            await scanConverterFolder(state.config.converter.source_folder);
        }
    } catch (e) {
        showToast(`Failed to start conversion: ${e.message}`, 'error');
    }
}

async function convertAll() {
    try {
        const data = await api('POST', '/converter/convert', {});
        showToast(`Started converting ${data.count} files`, 'success');
        if (state.config?.converter?.source_folder) {
            await scanConverterFolder(state.config.converter.source_folder);
        }
    } catch (e) {
        showToast(`Failed to start conversion: ${e.message}`, 'error');
    }
}

async function stopConversion() {
    try {
        showToast('Stopping conversion and clearing queue...', 'info');
        await api('POST', '/converter/stop');
        showToast('Conversions stopped successfully', 'success');
        if (state.config?.converter?.source_folder) {
            await scanConverterFolder(state.config.converter.source_folder);
        }
    } catch (e) {
        showToast(`Failed to stop conversion: ${e.message}`, 'error');
    }
}

// ──────────────────────────────────────────────
//  Streamer
// ──────────────────────────────────────────────

async function setStreamerFoldersAndRefresh(folders) {
    const folderDetailsPromises = folders.map(async (folder) => {
        if (state.expandedFolders.has(folder.name)) {
            try {
                const detail = await api('GET', `/streamer/folder/${encodeURIComponent(folder.name)}`);
                return { ...folder, ...detail };
            } catch (e) {
                console.error(`Failed to refresh expanded folder detail for ${folder.name}:`, e);
            }
        }
        // Fallback to existing detail if available
        const existing = (state.streamerFolders || []).find(f => f.name === folder.name);
        if (existing && existing.slots) {
            return { ...folder, ...existing };
        }
        return folder;
    });

    state.streamerFolders = await Promise.all(folderDetailsPromises);
    renderStreamerFolders();
}

async function scanStreamerFolder(path) {
    try {
        const data = await api('POST', '/streamer/scan', { path });
        await setStreamerFoldersAndRefresh(data.folders);
        document.getElementById('stream-start-btn').disabled = false;
    } catch (e) {
        showToast(`Failed to scan folder: ${e.message}`, 'error');
    }
}

function renderStreamerFolders() {
    const container = document.getElementById('streamer-folder-list');

    if (state.streamerFolders.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon"><i class="fa-solid fa-satellite-dish"></i></div>
                <p>No date-range folders found (expected format: DDMM_DDMM)</p>
                <div style="margin-top: 15px; display: flex; justify-content: center; gap: 10px;">
                    <button class="btn btn-secondary" onclick="openCreateFolderModal()">+ New Folder</button>
                    <button class="btn btn-secondary" onclick="openCreateLiveStreamModal()">+ New Stream</button>
                </div>
            </div>
        `;
        return;
    }

    const cardsHtml = state.streamerFolders.map(folder => {
        const isExpanded = state.expandedFolders.has(folder.name);
        const isActive = folder.is_active;

        return `
            <div class="folder-card ${isActive ? 'active' : ''}" id="folder-${folder.name}"
                 ondragover="handleDragOver(event)"
                 ondragleave="handleDragLeave(event)"
                 ondrop="handleDrop(event, '${escapeAttr(folder.name)}')">
                <div class="folder-card-header" onclick="toggleFolder('${folder.name}')">
                    <span class="folder-icon"><i class="fa-regular fa-folder"></i></span>
                    <span class="folder-card-title">${folder.name}</span>
                    ${isActive ? '<span class="folder-active-tag">Active</span>' : ''}
                    <span class="folder-date-range">${folder.display_range}</span>
                    <span class="folder-file-count">${folder.file_count} files</span>
                    <button class="folder-edit-btn" onclick="openModifyFolderModal(event, '${escapeAttr(folder.name)}')" title="Modify Date Range"><i class="fa-solid fa-pen"></i></button>
                    ${!isActive ? `<button class="folder-delete-btn" onclick="deleteFolder(event, '${escapeAttr(folder.name)}')" title="Remove Folder (Moves videos back to Input)"><i class="fa-solid fa-trash"></i></button>` : ''}
                    <i class="fa-solid fa-chevron-right folder-chevron ${isExpanded ? 'open' : ''}"></i>
                </div>
                <div class="folder-card-body ${isExpanded ? 'open' : ''}" id="folder-body-${folder.name}">
                    ${isExpanded ? renderFolderFiles(folder) : ''}
                </div>
            </div>
        `;
    }).join('');

    const newFolderBtnHtml = `
        <div style="display: flex; justify-content: center; gap: 10px; margin: 20px 0;">
            <button class="btn btn-secondary" onclick="openCreateFolderModal()" style="padding: 8px 16px;">
                + New Folder
            </button>
            <button class="btn btn-secondary" onclick="openCreateLiveStreamModal()" style="padding: 8px 16px;">
                + New Stream
            </button>
        </div>
    `;

    container.innerHTML = cardsHtml + newFolderBtnHtml;
}

function renderFolderFiles(folder) {
    const slots = folder.slots || [];
    const existingSlotsMap = new Map(slots.map(s => [s.port, s]));
    const startPort = (state.config && state.config.streamer && state.config.streamer.port_range_start) ? state.config.streamer.port_range_start : 1935;
    const endPort = (state.config && state.config.streamer && state.config.streamer.port_range_end) ? state.config.streamer.port_range_end : 1944;

    const allPortsSet = new Set();
    for (let p = startPort; p <= endPort; p++) {
        allPortsSet.add(p);
    }
    for (const slot of slots) {
        allPortsSet.add(slot.port);
    }

    const allPorts = Array.from(allPortsSet).sort((a, b) => a - b);

    return allPorts.map(port => {
        const slot = existingSlotsMap.get(port);
        if (!slot) {
            return `
                <div class="slot-card slot-new-card" id="slot-${folder.name}-${port}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleSlotDrop(event, '${escapeAttr(folder.name)}', ${port})">
                    <div class="slot-header" style="border-bottom:none; justify-content:space-between;">
                        <div style="display:flex; align-items:center; gap:8px;">
                            <span class="slot-port-badge" style="border-style:dashed; color:var(--text-muted);">+ New Port :${port}</span>
                            <span style="font-size:12px; color:var(--text-muted);">Drag & drop video here or click "+ Add File" to create port :${port}</span>
                        </div>
                        <button class="slot-add-btn" onclick="openAddFileModal('${escapeAttr(folder.name)}', ${port})">+ Add File</button>
                    </div>
                </div>
            `;
        }

        const files = slot.files || [];
        const filesDetail = slot.files_detail || [];

        // Find live stream for this port
        const liveStream = state.streamStatus && state.streamStatus.active_streams
            ? state.streamStatus.active_streams.find(s => s.port === port)
            : null;

        const isLive = liveStream && liveStream.status === 'live';
        const pct = liveStream ? Math.round((liveStream.progress || 0) * 100) : 0;
        const currentFile = liveStream ? liveStream.filename : null;

        const portBadge = isLive
            ? `<span class="slot-port-badge live"><span class="live-dot"></span>:${port}</span>`
            : `<span class="slot-port-badge">:${port}</span>`;

        // Stream URL link always visible (RTMP/HLS)
        const protocol = (state.config && state.config.streamer && state.config.streamer.protocol) ? state.config.streamer.protocol.toUpperCase() : 'RTMP';
        const extIp = (state.streamStatus && state.streamStatus.external_ip && state.streamStatus.external_ip !== '127.0.0.1')
            ? state.streamStatus.external_ip
            : ((state.folderDetail && state.folderDetail.external_ip && state.folderDetail.external_ip !== '127.0.0.1')
                ? state.folderDetail.external_ip
                : (window.location.hostname && window.location.hostname !== '127.0.0.1' && window.location.hostname !== 'localhost'
                    ? window.location.hostname
                    : '127.0.0.1'));
        let displayUrl = '';
        if (liveStream && (liveStream.status === 'live' || liveStream.status === 'starting') && (liveStream.stream_url || liveStream.rtmp_url)) {
            displayUrl = liveStream.stream_url || liveStream.rtmp_url;
            if (displayUrl.includes('127.0.0.1') || displayUrl.includes('localhost')) {
                displayUrl = displayUrl.replace(/127\.0\.0\.1|localhost/g, extIp);
            }
        } else if (protocol === 'HLS') {
            displayUrl = `http://${extIp}:${port}/stream/index.m3u8`;
        } else {
            displayUrl = `rtmp://${extIp}:${port}/stream`;
        }
        const urlHtml = `<div class="slot-url" onclick="copyToClipboard('${escapeAttr(displayUrl)}'); event.stopPropagation();" title="Click to copy playback link (${protocol})">
            <i class="fa-solid fa-link"></i> ${escapeHtml(displayUrl)} <span style="opacity:0.75; font-size:10px;">[${protocol}]</span>
        </div>`;

        // Progress bar
        const progressHtml = isLive ? `
            <div class="slot-progress-bar">
                <div class="slot-progress-fill" style="width:${pct}%"></div>
            </div>
        ` : '';

        // File entries
        const fileRows = files.map((fname, fi) => {
            const detail = filesDetail[fi] || {};
            const isCurrent = isLive && fname === currentFile;
            const metaBadge = formatMetaBadge(detail.metadata);
            const thumbSrc = `/api/streamer/folder/${encodeURIComponent(folder.name)}/thumbnail/${encodeURIComponent(fname)}`;
            const thumbHtml = getThumbHtml(thumbSrc, detail.has_thumbnail, 'slot-file-thumb');

            return `
                <div class="slot-file-entry ${isCurrent ? 'is-playing' : ''}" draggable="true" ondragstart="handleSlotFileDragStart(event, '${escapeAttr(folder.name)}', ${port}, '${escapeAttr(fname)}')" ondragend="handleSlotFileDragEnd(event)">
                    ${thumbHtml}
                    <div class="slot-file-info">
                        <span class="slot-file-name" title="${escapeHtml(fname)}">${escapeHtml(fname)}</span>
                        ${metaBadge ? `<span class="slot-file-meta">${metaBadge}</span>` : ''}
                    </div>
                    ${isCurrent ? '<span class="slot-playing-badge"><i class="fa-solid fa-play"></i> Playing</span>' : ''}
                    <div class="slot-reorder-buttons" style="display:flex; gap:2px; flex-shrink:0;">
                        <button class="slot-reorder-btn" onclick="moveFileInSlot(event, '${escapeAttr(folder.name)}', ${port}, ${fi}, -1)" title="Move Up" ${fi === 0 ? 'disabled' : ''}><i class="fa-solid fa-caret-up"></i></button>
                        <button class="slot-reorder-btn" onclick="moveFileInSlot(event, '${escapeAttr(folder.name)}', ${port}, ${fi}, 1)" title="Move Down" ${fi === files.length - 1 ? 'disabled' : ''}><i class="fa-solid fa-caret-down"></i></button>
                    </div>
                    <button class="slot-remove-btn" onclick="removeFileFromSlot(event, '${escapeAttr(folder.name)}', ${port}, '${escapeAttr(fname)}')" title="Remove"><i class="fa-solid fa-xmark"></i></button>
                </div>
            `;
        }).join('');

        return `
            <div class="slot-card" id="slot-${folder.name}-${port}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleSlotDrop(event, '${escapeAttr(folder.name)}', ${port})">
                <div class="slot-header">
                    ${portBadge}
                    <span class="slot-file-count">${files.length} file${files.length !== 1 ? 's' : ''}</span>
                    ${urlHtml}
                    <button class="slot-add-btn" onclick="openAddFileModal('${escapeAttr(folder.name)}', ${port})">+ Add File</button>
                </div>
                ${progressHtml}
                <div class="slot-file-list">
                    ${fileRows || '<div class="slot-empty">No files. Click + Add File to assign videos.</div>'}
                </div>
            </div>
        `;
    }).join('');
}

async function toggleFolder(name) {
    if (state.expandedFolders.has(name)) {
        state.expandedFolders.delete(name);
    } else {
        state.expandedFolders.add(name);

        // Fetch folder details (includes slot config) for expanded view
        try {
            const detail = await api('GET', `/streamer/folder/${encodeURIComponent(name)}`);
            const idx = state.streamerFolders.findIndex(f => f.name === name);
            if (idx !== -1) {
                state.streamerFolders[idx] = { ...state.streamerFolders[idx], ...detail };
            }


        } catch (e) {
            console.error('Failed to load folder details:', e);
        }
    }
    renderStreamerFolders();
}

// ──────────────────────────────────────────────
//  Slot Management
// ──────────────────────────────────────────────

let _filePickerState = { folder: null, port: null };

async function refreshFolderDetail(folderName) {
    try {
        const detail = await api('GET', `/streamer/folder/${encodeURIComponent(folderName)}`);
        const idx = state.streamerFolders.findIndex(f => f.name === folderName);
        if (idx !== -1) state.streamerFolders[idx] = { ...state.streamerFolders[idx], ...detail };
        renderStreamerFolders();
        if (state.config?.converter?.source_folder) {
            scanConverterFolder(state.config.converter.source_folder);
        }
    } catch (e) {
        console.error('Failed to refresh folder:', e);
    }
}

async function openAddFileModal(folderName, port) {
    _filePickerState = { folder: folderName, port };
    document.getElementById('file-picker-port').textContent = port;
    document.getElementById('file-picker-modal').style.display = 'flex';
    document.getElementById('file-picker-body').innerHTML = '<div class="modal-loading">Loading converted files...</div>';

    try {
        const data = await api('GET', '/converter/status');
        const doneFiles = (data.files || []).filter(f => f.status === 'done');

        const folderDetail = state.streamerFolders.find(f => f.name === folderName);
        const currentSlot = (folderDetail?.slots || []).find(s => s.port === port);
        const alreadyAdded = new Set(currentSlot ? currentSlot.files : []);

        if (doneFiles.length === 0) {
            document.getElementById('file-picker-body').innerHTML = '<div class="modal-loading">No converted .ts files found in converter.</div>';
            return;
        }

        document.getElementById('file-picker-body').innerHTML = doneFiles.map(f => {
            const added = alreadyAdded.has(f.ts_filename || f.filename);
            const fname = f.ts_filename || f.filename;
            const metaBadge = formatMetaBadge(f.metadata);
            const thumbSrc = `/api/converter/thumbnail/${encodeURIComponent(f.filename)}`;
            const thumbHtml = getThumbHtml(thumbSrc, true, 'slot-file-thumb');

            return `
                <div class="file-picker-item ${added ? 'added' : ''}" onclick="${added ? '' : `addFileToSlot('${escapeAttr(folderName)}', ${port}, '${escapeAttr(fname)}')`}">
                    ${thumbHtml}
                    <div class="slot-file-info">
                        <span class="slot-file-name">${escapeHtml(fname)}</span>
                        ${metaBadge ? `<span class="slot-file-meta">${metaBadge}</span>` : ''}
                    </div>
                    ${added ? '<span class="slot-playing-badge">Added</span>' : '<button class="btn btn-primary" style="font-size:0.85em;padding:4px 10px;">Add</button>'}
                </div>
            `;
        }).join('');
    } catch (e) {
        document.getElementById('file-picker-body').innerHTML = `<div class="modal-loading" style="color:var(--red)">Error: ${escapeHtml(e.message)}</div>`;
    }
}

async function addFileToSlot(folderName, port, filename) {
    const folderDetail = state.streamerFolders.find(f => f.name === folderName);
    const slots = folderDetail ? (folderDetail.slots || []) : [];
    const slot = slots.find(s => s.port === port);
    const currentFiles = slot ? [...slot.files] : [];

    if (currentFiles.includes(filename)) {
        showToast('File already in this slot', 'info');
        return;
    }

    showLoadingOverlay('Adding Video to Slot...', `Moving '${filename}' to folder '${folderName}' for port :${port}...`);
    try {
        await api('PUT', `/streamer/folder/${encodeURIComponent(folderName)}/slot`, {
            port,
            files: [...currentFiles, filename],
        });
        await refreshFolderDetail(folderName);
        if (document.getElementById('file-picker-modal').style.display === 'flex') {
            openAddFileModal(folderName, port);
        }
    } catch (e) {
        showToast(`Failed to add file: ${e.message}`, 'error');
    } finally {
        hideLoadingOverlay();
    }
}

async function removeFileFromSlot(event, folderName, port, filename) {
    if (event && event.stopPropagation) event.stopPropagation();
    showLoadingOverlay('Removing Video from Slot...', `Moving '${filename}' back to converter input if unused...`);
    try {
        await api('DELETE', `/streamer/folder/${encodeURIComponent(folderName)}/slot/file`, { port, filename });
        showToast(`Removed ${filename}`, 'success');
        await refreshFolderDetail(folderName);
    } catch (e) {
        showToast(`Failed to remove file: ${e.message}`, 'error');
    } finally {
        hideLoadingOverlay();
    }
}

let _modifyFolderName = null;

function openCreateFolderModal() {
    document.getElementById('folder-create-name').value = '';
    document.getElementById('folder-create-modal').style.display = 'flex';
}

function closeCreateFolderModal() {
    document.getElementById('folder-create-modal').style.display = 'none';
}

async function submitCreateFolder() {
    const nameInput = document.getElementById('folder-create-name');
    const name = nameInput.value.trim();
    if (!name) {
        showToast('Please enter a folder name', 'error');
        return;
    }
    try {
        const data = await api('POST', '/streamer/folders', { name });
        if (data && data.folders) {
            await setStreamerFoldersAndRefresh(data.folders);
        } else {
            await rescanFolders(false, true);
        }
        closeCreateFolderModal();
        showToast(`Folder '${name}' created`, 'success');
    } catch (e) {
        showToast(`Failed to create folder: ${e.message}`, 'error');
    }
}

function openModifyFolderModal(event, folderName) {
    if (event) event.stopPropagation();
    const folderDetail = state.streamerFolders.find(f => f.name === folderName);
    if (state.streamStatus?.is_running && (folderName === state.streamStatus.current_folder || folderDetail?.is_active)) {
        showToast('Cannot modify active folder while streaming is running. Stop streaming first.', 'error');
        return;
    }
    _modifyFolderName = folderName;
    document.getElementById('folder-modify-old-name').textContent = folderName;
    document.getElementById('folder-modify-name').value = folderName;
    document.getElementById('folder-modify-modal').style.display = 'flex';
}

function closeModifyFolderModal() {
    document.getElementById('folder-modify-modal').style.display = 'none';
    _modifyFolderName = null;
}

async function submitModifyFolder() {
    if (!_modifyFolderName) return;
    const nameInput = document.getElementById('folder-modify-name');
    const new_name = nameInput.value.trim();
    if (!new_name) {
        showToast('Please enter a folder name', 'error');
        return;
    }
    try {
        const data = await api('PUT', `/streamer/folder/${encodeURIComponent(_modifyFolderName)}`, { new_name });
        if (data && data.folders) {
            if (state.expandedFolders.has(_modifyFolderName)) {
                state.expandedFolders.delete(_modifyFolderName);
                state.expandedFolders.add(new_name);
            }
            await setStreamerFoldersAndRefresh(data.folders);
        } else {
            await rescanFolders(false, true);
        }
        closeModifyFolderModal();
        showToast(`Folder updated to '${new_name}'`, 'success');
    } catch (e) {
        showToast(`Failed to modify folder: ${e.message}`, 'error');
    }
}





function updateStreamUI() {
    if (!state.streamStatus) return;

    const badge = document.getElementById('stream-status-badge');
    const startBtn = document.getElementById('stream-start-btn');
    const errorsEl = document.getElementById('stream-errors');

    const isRunning = state.streamStatus.is_running;
    document.getElementById('stream-protocol').disabled = isRunning;
    document.getElementById('port-start').disabled = isRunning;
    document.getElementById('port-end').disabled = isRunning;
    document.getElementById('streamer-browse-btn').disabled = isRunning;

    if (isRunning) {
        const liveCount = state.streamStatus.active_streams.filter(s => s.status === 'live').length;
        badge.textContent = `Live · ${liveCount} streams`;
        badge.className = 'stream-status-badge live';
        startBtn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-stop"></i></span> Stop';
        startBtn.className = 'btn btn-danger';
        startBtn.disabled = false;
    } else {
        badge.textContent = 'Idle';
        badge.className = 'stream-status-badge';
        startBtn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-play"></i></span> Start Streaming';
        startBtn.className = 'btn btn-accent';
        startBtn.disabled = !state.streamerFolders.length;
    }

    // Show errors
    if (state.streamStatus.errors && state.streamStatus.errors.length > 0) {
        errorsEl.style.display = 'block';
        errorsEl.innerHTML = state.streamStatus.errors.map(e => `<p><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(e)}</p>`).join('');
    } else {
        errorsEl.style.display = 'none';
    }

    // Re-render folder list to update port indicators
    if (state.streamerFolders.length > 0) {
        renderStreamerFolders();
    }
}

async function startStreaming() {
    const portStart = parseInt(document.getElementById('port-start').value);
    const portEnd = parseInt(document.getElementById('port-end').value);
    const protocol = document.getElementById('stream-protocol').value;

    if (isNaN(portStart) || isNaN(portEnd) || portStart > portEnd) {
        showToast('Invalid port range', 'error');
        return;
    }

    if (state.config && state.config.streamer) {
        state.config.streamer.protocol = protocol;
    }

    try {
        const data = await api('POST', '/streamer/start', {
            port_range_start: portStart,
            port_range_end: portEnd,
            protocol: protocol
        });

        if (data.status === 'error') {
            showToast(data.error, 'error');
        } else {
            showToast('Streaming starting...', 'info');
            setTimeout(async () => {
                let success = false;
                for (let attempt = 1; attempt <= 6; attempt++) {
                    try {
                        const [cfgData, statusData] = await Promise.all([
                            api('GET', '/config'),
                            api('GET', '/streamer/status')
                        ]);
                        state.config = cfgData;
                        state.streamStatus = statusData;
                        updateStreamUI();
                        if (state.streamerFolders.length === 0 && state.config?.streamer?.content_folder) {
                            await scanStreamerFolder(state.config.streamer.content_folder);
                        } else {
                            renderStreamerFolders();
                        }
                        showToast('Streaming started', 'success');
                        success = true;
                        break;
                    } catch (err) {
                        console.warn(`Status check attempt ${attempt} failed:`, err);
                        await new Promise(resolve => setTimeout(resolve, 1000));
                    }
                }
                if (!success) {
                    showToast('Failed to verify streaming status. Please refresh.', 'error');
                }
            }, 1000);
        }
    } catch (e) {
        showToast(`Failed to start streaming: ${e.message}`, 'error');
    }
}

async function stopStreaming() {
    try {
        await api('POST', '/streamer/stop');
        showToast('Streaming stopped', 'success');
    } catch (e) {
        showToast(`Failed to stop streaming: ${e.message}`, 'error');
    }
}

// ──────────────────────────────────────────────
//  Folder Browser Modal
// ──────────────────────────────────────────────

function openFolderBrowser(target) {
    state.folderBrowserTarget = target;
    state.folderBrowserPath = '';
    document.getElementById('folder-modal').style.display = 'flex';
    document.getElementById('modal-select-btn').disabled = true;
    document.getElementById('modal-selected-path').textContent = 'No folder selected';
    browseTo('');
}

function closeFolderBrowser() {
    document.getElementById('folder-modal').style.display = 'none';
}

async function browseTo(path) {
    state.folderBrowserPath = path;
    const body = document.getElementById('modal-body');
    body.innerHTML = '<div class="modal-loading">Loading...</div>';

    try {
        const data = await api('GET', `/browse?path=${encodeURIComponent(path)}`);
        renderBreadcrumb(data.path, data.parent);
        renderBrowserEntries(data.entries, data.path);

        // Update selected path
        if (path) {
            document.getElementById('modal-selected-path').textContent = path;
            document.getElementById('modal-select-btn').disabled = false;
        }
    } catch (e) {
        body.innerHTML = `<div class="modal-loading" style="color:var(--red)">Error: ${escapeHtml(e.message)}</div>`;
    }
}

function renderBreadcrumb(currentPath, parentPath) {
    const breadcrumb = document.getElementById('modal-breadcrumb');

    if (!currentPath) {
        breadcrumb.innerHTML = '<span style="color:var(--text-secondary)">Computer</span>';
        return;
    }

    // Split path into parts
    const parts = currentPath.replace(/\\/g, '/').split('/').filter(Boolean);
    let accumulated = '';
    const items = [];

    items.push(`<span class="breadcrumb-item" onclick="browseTo('')"><i class="fa-solid fa-laptop"></i></span>`);

    parts.forEach((part, i) => {
        accumulated += part + '/';
        const clickPath = accumulated.replace(/\/$/, '');
        if (i < parts.length - 1) {
            items.push(`<span class="breadcrumb-sep">/</span>`);
            items.push(`<span class="breadcrumb-item" onclick="browseTo('${escapeAttr(clickPath)}')">${escapeHtml(part)}</span>`);
        } else {
            items.push(`<span class="breadcrumb-sep">/</span>`);
            items.push(`<span style="color:var(--text-primary)">${escapeHtml(part)}</span>`);
        }
    });

    breadcrumb.innerHTML = items.join('');
}

function renderBrowserEntries(entries, currentPath) {
    const body = document.getElementById('modal-body');

    // Filter to only directories
    const dirs = entries.filter(e => e.is_dir);

    if (dirs.length === 0) {
        body.innerHTML = '<div class="modal-loading">No subfolders</div>';
        return;
    }

    body.innerHTML = dirs.map(entry => {
        return `
            <div class="modal-dir-item" ondblclick="browseTo('${escapeAttr(entry.path)}')" onclick="selectBrowserItem(this, '${escapeAttr(entry.path)}')">
                <span class="modal-dir-icon"><i class="fa-regular fa-folder"></i></span>
                <span class="modal-dir-name">${escapeHtml(entry.name)}</span>
            </div>
        `;
    }).join('');
}

function selectBrowserItem(el, path) {
    // Remove previous selection
    document.querySelectorAll('.modal-dir-item.selected').forEach(item => item.classList.remove('selected'));
    el.classList.add('selected');
    state.folderBrowserPath = path;
    document.getElementById('modal-selected-path').textContent = path;
    document.getElementById('modal-select-btn').disabled = false;
}

async function selectFolder() {
    const path = state.folderBrowserPath;
    if (!path) return;

    closeFolderBrowser();

    if (state.folderBrowserTarget === 'converter') {
        document.getElementById('converter-folder').value = path;
        await scanConverterFolder(path);
    } else if (state.folderBrowserTarget === 'streamer') {
        document.getElementById('streamer-folder').value = path;
        await scanStreamerFolder(path);
    }
}

// ──────────────────────────────────────────────
//  Converter Drag & Drop Upload
// ──────────────────────────────────────────────

const VIDEO_EXTENSIONS = new Set(['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.ts']);

function initConverterDropZone() {
    const panel   = document.getElementById('panel-converter');
    const overlay = document.getElementById('converter-drop-overlay');
    if (!panel || !overlay) return;

    // ── Step 1: Stop the BROWSER from opening dragged files globally ──────
    // Without this, dropping anywhere on the page navigates to the file.
    document.addEventListener('dragover', (e) => e.preventDefault());
    document.addEventListener('drop',     (e) => e.preventDefault());

    // ── Step 2: Show/hide overlay when dragging over the converter panel ──
    let dragDepth = 0;

    panel.addEventListener('dragenter', (e) => {
        e.preventDefault();
        dragDepth++;
        const inner = overlay.querySelector('.drop-zone-inner');
        if (state.draggedSlotFile) {
            if (inner) inner.innerHTML = '<i class="fa-solid fa-rotate-left"></i><span>Drop to return video to Input folder</span>';
        } else {
            if (inner) inner.innerHTML = '<i class="fa-solid fa-cloud-arrow-up"></i><span>Drop video files here</span>';
        }
        overlay.classList.add('active');
    });

    panel.addEventListener('dragover', (e) => {
        e.preventDefault();
        if (state.draggedSlotFile) {
            e.dataTransfer.dropEffect = 'move';
        } else {
            e.dataTransfer.dropEffect = 'copy';
        }
    });

    panel.addEventListener('dragleave', (e) => {
        dragDepth--;
        if (dragDepth <= 0) {
            dragDepth = 0;
            overlay.classList.remove('active');
        }
    });

    panel.addEventListener('drop', async (e) => {
        e.preventDefault();
        dragDepth = 0;
        overlay.classList.remove('active');

        // Check if a streamer slot file was dropped back onto converter panel
        if (state.draggedSlotFile) {
            const slotItem = { ...state.draggedSlotFile };
            state.draggedSlotFile = null;
            await removeFileFromSlot(null, slotItem.folder, slotItem.port, slotItem.filename);
            return;
        }

        if (!e.dataTransfer.types.includes('Files')) return;

        const folderInput = document.getElementById('converter-folder');
        if (!folderInput || !folderInput.value.trim()) {
            showToast('Please select an input folder first before dropping files.', 'error');
            return;
        }

        const allFiles = Array.from(e.dataTransfer.files);
        const videoFiles = allFiles.filter(f => {
            const ext = '.' + f.name.split('.').pop().toLowerCase();
            return VIDEO_EXTENSIONS.has(ext);
        });
        const ignoredCount = allFiles.length - videoFiles.length;

        if (videoFiles.length === 0) {
            showToast('No supported video files found in the dropped items.', 'error');
            return;
        }
        if (ignoredCount > 0) {
            showToast(`${ignoredCount} non-video file(s) ignored.`, 'info');
        }

        await uploadFilesToConverter(videoFiles);
    });
}

async function uploadFilesToConverter(files) {
    const progressToast = showUploadToast(`Uploading ${files.length} file(s)…`);

    const formData = new FormData();
    for (const f of files) {
        formData.append('files', f);
    }

    try {
        const res = await fetch('/api/converter/upload', {
            method: 'POST',
            body: formData,
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || res.statusText);
        }

        const data = await res.json();
        progressToast.remove();

        const saved = data.saved?.length ?? 0;
        const skipped = data.skipped?.length ?? 0;

        if (saved > 0) {
            showToast(`✓ ${saved} file(s) uploaded successfully.`, 'success');
        }
        if (skipped > 0) {
            showToast(`${skipped} file(s) skipped (unsupported format).`, 'info');
        }

        // Update the UI with the returned file list directly
        if (data.files) {
            state.converterFiles = data.files;
            renderConverterFiles();
            updateConverterCounter();
        }

    } catch (e) {
        progressToast.remove();
        showToast(`Upload failed: ${e.message}`, 'error');
    }
}

function showUploadToast(message) {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = 'toast upload';
    toast.innerHTML = `<i class="fa-solid fa-spinner fa-spin" style="margin-right:8px;"></i>${message}`;
    container.appendChild(toast);
    return toast; // caller removes it manually on completion
}

// ──────────────────────────────────────────────
//  Toast Notifications
// ──────────────────────────────────────────────

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'toast-out 200ms ease forwards';
        setTimeout(() => toast.remove(), 200);
    }, 4000);
}

// ──────────────────────────────────────────────
//  Utilities
// ──────────────────────────────────────────────

function formatSize(bytes) {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
}

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    return `${m}:${String(s).padStart(2, '0')}`;
}

function formatBitrate(bitrate) {
    if (!bitrate || bitrate <= 0) return '';
    if (bitrate < 1000000) {
        return `${Math.round(bitrate / 1000)} kbps`;
    }
    return `${(bitrate / 1000000).toFixed(1)} Mbps`;
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escapeAttr(str) {
    if (!str) return '';
    return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text)
        .then(() => showToast('URL copied to clipboard!', 'success'))
        .catch(err => showToast('Failed to copy URL', 'error'));
}

// ──────────────────────────────────────────────
//  Drag and Drop
// ──────────────────────────────────────────────

function handleDragStart(e, filename) {
    e.dataTransfer.setData('text/plain', filename);
    e.dataTransfer.effectAllowed = 'move';
}

function handleSlotFileDragStart(e, folderName, port, filename) {
    state.draggedSlotFile = { folder: folderName, port: port, filename: filename };
    e.dataTransfer.setData('text/plain', filename);
    e.dataTransfer.effectAllowed = 'move';
}

function handleSlotFileDragEnd(e) {
    setTimeout(() => { state.draggedSlotFile = null; }, 100);
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const card = e.currentTarget;
    if (!card.classList.contains('drag-over')) {
        card.classList.add('drag-over');
    }
}

function handleDragLeave(e) {
    const card = e.currentTarget;
    card.classList.remove('drag-over');
}

async function handleDrop(e, targetFolder) {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    const filename = e.dataTransfer.getData('text/plain');
    if (!filename) return;

    showLoadingOverlay('Moving Video File...', `Moving '${filename}' to stream folder '${targetFolder}'...`);
    try {
        const data = await api('POST', '/converter/move', { filename, target_folder: targetFolder });
        showToast(data.message, 'success');
        await rescanFolders();
    } catch (err) {
        showToast(`Failed to move file: ${err.message}`, 'error');
    } finally {
        hideLoadingOverlay();
    }
}

async function handleSlotDrop(e, targetFolder, port) {
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.classList.remove('drag-over');
    const filename = e.dataTransfer.getData('text/plain');
    if (!filename) return;

    const convFile = state.converterFiles.find(f => f.filename === filename || f.ts_filename === filename);
    let targetFileName = filename;

    if (convFile && convFile.status === 'done') {
        targetFileName = convFile.ts_filename || convFile.filename;
        showLoadingOverlay('Moving & Assigning Video...', `Moving '${convFile.filename}' to folder '${targetFolder}'...`);
        try {
            await api('POST', '/converter/move', { filename: convFile.filename, target_folder: targetFolder });
            await rescanFolders(true, false);
        } catch (err) {
            if (!err.message.includes('not found')) showToast(`Move warning: ${err.message}`, 'info');
        } finally {
            hideLoadingOverlay();
        }
    }

    await addFileToSlot(targetFolder, port, targetFileName);
}

async function deleteFolder(event, folderName) {
    if (event) event.stopPropagation();
    if (!confirm(`Are you sure you want to delete the folder '${folderName}'? Any video files inside will be moved back to the converter's input folder.`)) {
        return;
    }
    showLoadingOverlay('Deleting Folder...', `Moving video files inside '${folderName}' back to input folder and removing directory...`);
    try {
        const data = await api('DELETE', `/streamer/folder/${encodeURIComponent(folderName)}`);
        if (data && data.folders) {
            state.expandedFolders.delete(folderName);
            await setStreamerFoldersAndRefresh(data.folders);
        } else {
            await rescanFolders(true, true);
        }
        // Rescan converter panel so moved files show up immediately
        if (state.config?.converter?.source_folder) {
            await scanConverterFolder(state.config.converter.source_folder);
        }
        showToast(`Folder '${folderName}' deleted, files returned to input`, 'success');
    } catch (e) {
        showToast(`Failed to delete folder: ${e.message}`, 'error');
    } finally {
        hideLoadingOverlay();
    }
}

async function moveFileInSlot(event, folderName, port, currentIndex, direction) {
    if (event) event.stopPropagation();
    const folderDetail = state.streamerFolders.find(f => f.name === folderName);
    if (!folderDetail) return;
    const slot = (folderDetail.slots || []).find(s => s.port === port);
    if (!slot) return;
    const files = [...slot.files];

    const targetIndex = currentIndex + direction;
    if (targetIndex < 0 || targetIndex >= files.length) return;

    // Swap the elements
    const temp = files[currentIndex];
    files[currentIndex] = files[targetIndex];
    files[targetIndex] = temp;

    try {
        await api('PUT', `/streamer/folder/${encodeURIComponent(folderName)}/slot`, {
            port,
            files: files,
        });
        await refreshFolderDetail(folderName);
    } catch (e) {
        showToast(`Failed to reorder files: ${e.message}`, 'error');
    }
}

// Make functions available globally for onclick handlers
window.convertFile = convertFile;
window.toggleFolder = toggleFolder;
window.browseTo = browseTo;
window.selectBrowserItem = selectBrowserItem;
window.handleDragStart = handleDragStart;
window.handleDragOver = handleDragOver;
window.handleDragLeave = handleDragLeave;
window.handleDrop = handleDrop;
window.handleSlotDrop = handleSlotDrop;
window.copyToClipboard = copyToClipboard;
window.openAddFileModal = openAddFileModal;
window.addFileToSlot = addFileToSlot;
window.removeFileFromSlot = removeFileFromSlot;
window.handleSlotFileDragStart = handleSlotFileDragStart;
window.handleSlotFileDragEnd = handleSlotFileDragEnd;
window.deleteFolder = deleteFolder;
window.moveFileInSlot = moveFileInSlot;

// ──────────────────────────────────────────────
//  Live Streams (HTTP Relay)
// ──────────────────────────────────────────────

async function fetchLiveStreams() {
    try {
        const data = await api('GET', '/streamer/live_streams');
        state.liveStreams = data.live_streams || [];
        renderLiveStreams();
    } catch (e) {
        console.error('Failed to fetch live streams:', e);
    }
}

function renderLiveStreams() {
    const container = document.getElementById('live-streams-list');
    if (!container) return;

    if (!state.liveStreams || state.liveStreams.length === 0) {
        container.innerHTML = '';
        return;
    }

    const html = state.liveStreams.map(item => {
        const isRunning = item.status === 'running' || item.status === 'listening';
        let statusBadge = '';
        if (item.status === 'running') {
            statusBadge = `<span class="livestream-status running"><i class="fa-solid fa-play"></i> Watching</span>`;
        } else if (item.status === 'listening') {
            statusBadge = `<span class="livestream-status listening"><i class="fa-solid fa-spinner"></i> Sleeping</span>`;
        } else if (item.status === 'error') {
            statusBadge = `<span class="livestream-status error" style="max-width: 750px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: inline-flex; align-items: center;" title="${escapeAttr(item.error || 'Error')}"><i class="fa-solid fa-triangle-exclamation"></i> Error: ${escapeAttr(item.error || 'Unknown Error')}</span>`;
        } else {
            statusBadge = `<span class="livestream-status stopped"><i class="fa-solid fa-stop"></i> Stopped</span>`;
        }

        const thumbSrc = item.thumbnail_url || `/api/streamer/live_stream/${item.id}/thumbnail?v=0`;
        const thumbHtml = getThumbHtml(thumbSrc, item.has_thumbnail, 'livestream-thumb', 'fa-tower-broadcast');

        return `
            <div class="folder-card livestream-card ${isRunning ? 'active' : ''}" id="livestream-${item.id}">
                <div class="folder-card-header" style="cursor: default; display: flex; align-items: center; gap: 10px; padding: 10px 12px;">
                    ${thumbHtml}
                    <span class="folder-card-title" style="margin-left: 5px;">${escapeAttr(item.name)}</span>
                    ${statusBadge}
                    <span class="folder-date-range" style="font-family: 'JetBrains Mono', monospace; font-size: 12px; margin-left: auto;">Port: ${item.port}</span>
                    <span class="folder-file-count" style="max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-left: 20px;" title="${escapeAttr(item.url)}">${escapeAttr(item.url)}</span>
                    
                    <div class="livestream-actions" style="margin-left: 20px; display: flex; align-items: center; gap: 8px;">
                        ${isRunning ? `
                            <button class="btn btn-danger" onclick="stopLiveStream('${item.id}')" title="Stop Stream" style="padding: 4px 10px; font-size: 12px; height: 28px;">
                                <i class="fa-solid fa-stop"></i> Stop
                            </button>
                        ` : `
                            <button class="btn btn-emerald" onclick="startLiveStream('${item.id}')" title="Start Stream" style="padding: 4px 10px; font-size: 12px; height: 28px;">
                                <i class="fa-solid fa-play"></i> Start
                            </button>
                        `}
                        <button class="folder-edit-btn" onclick="openEditLiveStreamModal('${item.id}')" title="Edit Stream" style="height: 28px; width: 28px; display: flex; align-items: center; justify-content: center;"><i class="fa-solid fa-pen"></i></button>
                        <button class="folder-delete-btn" onclick="deleteLiveStream('${item.id}')" title="Delete Stream" style="height: 28px; width: 28px; display: flex; align-items: center; justify-content: center;"><i class="fa-solid fa-trash"></i></button>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    container.innerHTML = `
        <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin: 20px 0 10px 0; display: flex; align-items: center; gap: 8px; font-family: 'Chakra Petch', sans-serif; text-transform: uppercase; letter-spacing: 0.5px;">
            <i class="fa-solid fa-tower-broadcast" style="color: var(--accent);"></i> Live HTTP Relays
        </div>
        ${html}
    `;
}

function openCreateLiveStreamModal() {
    document.getElementById('livestream-modal-title').textContent = 'Create Live HTTP Stream';
    document.getElementById('livestream-id').value = '';
    document.getElementById('livestream-name').value = '';
    document.getElementById('livestream-url').value = '';
    document.getElementById('livestream-port').value = '1913';
    document.getElementById('livestream-save-btn').textContent = 'Create';
    document.getElementById('livestream-modal').style.display = 'flex';
}

function openEditLiveStreamModal(streamId) {
    const item = state.liveStreams.find(x => x.id === streamId);
    if (!item) return;
    document.getElementById('livestream-modal-title').textContent = 'Edit Live HTTP Stream';
    document.getElementById('livestream-id').value = item.id;
    document.getElementById('livestream-name').value = item.name || '';
    document.getElementById('livestream-url').value = item.url || '';
    document.getElementById('livestream-port').value = item.port || 1913;
    document.getElementById('livestream-save-btn').textContent = 'Save';
    document.getElementById('livestream-modal').style.display = 'flex';
}

function closeLiveStreamModal() {
    document.getElementById('livestream-modal').style.display = 'none';
}

async function submitLiveStream() {
    const streamId = document.getElementById('livestream-id').value;
    const name = document.getElementById('livestream-name').value.trim();
    const url = document.getElementById('livestream-url').value.trim();
    const port = parseInt(document.getElementById('livestream-port').value, 10);

    if (!name || !url || isNaN(port)) {
        showToast('Please enter valid Name, URL, and Port', 'error');
        return;
    }

    try {
        if (streamId) {
            await api('PUT', `/streamer/live_stream/${streamId}`, { name, url, port });
            showToast('Live stream updated', 'success');
        } else {
            await api('POST', '/streamer/live_stream', { name, url, port });
            showToast('Live stream created', 'success');
        }
        closeLiveStreamModal();
        await fetchLiveStreams();
    } catch (e) {
        showToast(`Error saving live stream: ${e.message}`, 'error');
    }
}

async function startLiveStream(streamId) {
    try {
        showToast('Starting live stream...', 'info');
        await api('POST', `/streamer/live_stream/${streamId}/start`);
        showToast('Live stream started', 'success');
        await fetchLiveStreams();
    } catch (e) {
        showToast(`Failed to start stream: ${e.message}`, 'error');
    }
}

async function stopLiveStream(streamId) {
    try {
        await api('POST', `/streamer/live_stream/${streamId}/stop`);
        showToast('Live stream stopped', 'success');
        await fetchLiveStreams();
    } catch (e) {
        showToast(`Failed to stop stream: ${e.message}`, 'error');
    }
}

async function deleteLiveStream(streamId) {
    if (!confirm('Are you sure you want to delete this live stream?')) return;
    try {
        await api('DELETE', `/streamer/live_stream/${streamId}`);
        showToast('Live stream deleted', 'success');
        await fetchLiveStreams();
    } catch (e) {
        showToast(`Failed to delete stream: ${e.message}`, 'error');
    }
}

window.openCreateLiveStreamModal = openCreateLiveStreamModal;
window.openEditLiveStreamModal = openEditLiveStreamModal;
window.closeLiveStreamModal = closeLiveStreamModal;
window.submitLiveStream = submitLiveStream;
window.startLiveStream = startLiveStream;
window.stopLiveStream = stopLiveStream;
window.deleteLiveStream = deleteLiveStream;