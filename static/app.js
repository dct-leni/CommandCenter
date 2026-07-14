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

// ──────────────────────────────────────────────
//  Initialization
// ──────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
    // Load config
    try {
        state.config = await api('GET', '/config');
        applyConfig();
    } catch (e) {
        showToast('Failed to load config', 'error');
    }

    // Check system status
    await checkSystemStatus();

    // Wire up event handlers
    document.getElementById('converter-browse-btn').addEventListener('click', () => openFolderBrowser('converter'));
    document.getElementById('streamer-browse-btn').addEventListener('click', () => openFolderBrowser('streamer'));
    document.getElementById('convert-all-btn').addEventListener('click', convertAll);
    document.getElementById('stream-start-btn').addEventListener('click', startStreaming);
    document.getElementById('stream-stop-btn').addEventListener('click', stopStreaming);
    document.getElementById('modal-close-btn').addEventListener('click', closeFolderBrowser);
    document.getElementById('modal-select-btn').addEventListener('click', selectFolder);

    // Close modal on overlay click
    document.getElementById('folder-modal').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeFolderBrowser();
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeFolderBrowser();
    });

    // Start polling
    startPolling();

    // If config has saved folders, auto-scan
    if (state.config?.converter?.source_folder) {
        document.getElementById('converter-folder').value = state.config.converter.source_folder;
        await scanConverterFolder(state.config.converter.source_folder);
    }
    if (state.config?.streamer?.content_folder) {
        document.getElementById('streamer-folder').value = state.config.streamer.content_folder;
        await scanStreamerFolder(state.config.streamer.content_folder);
    }
    document.getElementById('stream-protocol').addEventListener('change', (e) => {
        api('PUT', '/config', { streamer: { protocol: e.target.value } })
            .then(() => showToast('Protocol saved', 'success')).catch(console.error);
    });
    document.getElementById('port-start').addEventListener('change', (e) => {
        api('PUT', '/config', { streamer: { port_range_start: parseInt(e.target.value) } }).catch(console.error);
    });
    document.getElementById('port-end').addEventListener('change', (e) => {
        api('PUT', '/config', { streamer: { port_range_end: parseInt(e.target.value) } }).catch(console.error);
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

        ffmpegDot.className = `status-dot ${state.systemStatus.ffmpeg ? 'ok' : 'error'}`;
        mediamtxDot.className = `status-dot ${state.systemStatus.mediamtx ? 'ok' : 'error'}`;
    } catch (e) {
        console.error('System status check failed:', e);
    }
}

// ──────────────────────────────────────────────
//  Polling
// ──────────────────────────────────────────────

function startPolling() {
    if (state.pollingInterval) clearInterval(state.pollingInterval);
    state.pollingInterval = setInterval(async () => {
        await pollConverterStatus();
        await pollStreamStatus();
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
                <div class="empty-icon">📂</div>
                <p>No video files found in this folder</p>
            </div>
        `;
        return;
    }

    container.innerHTML = state.converterFiles.map(file => {
        const sizeStr = formatSize(file.size);
        const durationStr = file.metadata?.duration ? formatDuration(file.metadata.duration) : '';
        const metaParts = [sizeStr, durationStr, file.extension].filter(Boolean);

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
            case 'error':
                statusHtml = `<span class="status-badge error" title="${escapeHtml(file.error)}">Error</span>`;
                break;
            case 'pending':
            default:
                actionHtml = `<button class="btn btn-sm btn-primary" onclick="convertFile('${escapeHtml(file.filename)}')">Convert</button>`;
                break;
        }

        const thumbHtml = file.has_thumbnail
            ? `<img class="file-thumb" src="/api/converter/thumbnail/${encodeURIComponent(file.filename)}" alt="" loading="lazy">`
            : `<div class="file-thumb-placeholder">🎬</div>`;
        const isDraggable = (file.extension === '.ts' || file.status === 'done');
        const dragAttr = isDraggable ? 'draggable="true"' : '';
        const dragEvent = isDraggable ? `ondragstart="handleDragStart(event, '${escapeAttr(file.filename)}')"` : '';

        let metaDetails = [
            formatSize(file.size),
            file.extension.toUpperCase().replace('.', '')
        ];
        if (file.metadata && !file.metadata.error) {
            if (file.metadata.duration) {
                const mins = Math.floor(file.metadata.duration / 60);
                const secs = Math.floor(file.metadata.duration % 60);
                metaDetails.push(`${mins}:${secs.toString().padStart(2, '0')}`);
            }
            if (file.metadata.width && file.metadata.height) {
                metaDetails.push(`${file.metadata.width}x${file.metadata.height}`);
            }
            if (file.metadata.codec && file.metadata.codec !== 'unknown') {
                metaDetails.push(file.metadata.codec.toUpperCase());
            }
            if (file.metadata.fps) {
                metaDetails.push(`${Math.round(file.metadata.fps)}fps`);
            }
        }
        
        return `
            <div class="file-item" ${dragAttr} ${dragEvent}>
                ${thumbHtml}
                <div class="file-info">
                    <div class="file-name" title="${escapeHtml(file.filename)}">${escapeHtml(file.filename)}</div>
                    <div class="file-meta">
                        ${metaDetails.join(' • ')}
                    </div>
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
    const counter = document.getElementById('converter-counter');

    if (total === 0) {
        counter.textContent = 'No files';
    } else if (converting > 0) {
        counter.textContent = `${done}/${total} done · ${converting} converting`;
    } else {
        counter.textContent = `${done}/${total} done`;
    }

    // Disable convert all if none pending
    const pending = state.converterFiles.filter(f => f.status === 'pending').length;
    document.getElementById('convert-all-btn').disabled = pending === 0;
}

async function convertFile(filename) {
    try {
        await api('POST', '/converter/convert', { filename });
        showToast(`Converting: ${filename}`, 'success');
    } catch (e) {
        showToast(`Failed to start conversion: ${e.message}`, 'error');
    }
}

async function convertAll() {
    try {
        const data = await api('POST', '/converter/convert', {});
        showToast(`Started converting ${data.count} files`, 'success');
    } catch (e) {
        showToast(`Failed to start conversion: ${e.message}`, 'error');
    }
}

// ──────────────────────────────────────────────
//  Streamer
// ──────────────────────────────────────────────

async function scanStreamerFolder(path) {
    try {
        const data = await api('POST', '/streamer/scan', { path });
        state.streamerFolders = data.folders;
        renderStreamerFolders();
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
                <div class="empty-icon">📡</div>
                <p>No date-range folders found (expected format: DDMM_DDMM)</p>
            </div>
        `;
        return;
    }

    container.innerHTML = state.streamerFolders.map(folder => {
        const isExpanded = state.expandedFolders.has(folder.name);
        const isActive = folder.is_active;

        return `
            <div class="folder-card ${isActive ? 'active' : ''}" id="folder-${folder.name}"
                 ondragover="handleDragOver(event)"
                 ondragleave="handleDragLeave(event)"
                 ondrop="handleDrop(event, '${escapeAttr(folder.name)}')">
                <div class="folder-card-header" onclick="toggleFolder('${folder.name}')">
                    <span class="folder-icon">📂</span>
                    <span class="folder-card-title">${folder.name}</span>
                    ${isActive ? '<span class="folder-active-tag">Active</span>' : ''}
                    <span class="folder-date-range">${folder.display_range}</span>
                    <span class="folder-file-count">${folder.file_count} files</span>
                    <span class="folder-chevron ${isExpanded ? 'open' : ''}">▶</span>
                </div>
                <div class="folder-card-body ${isExpanded ? 'open' : ''}" id="folder-body-${folder.name}">
                    ${isExpanded ? renderFolderFiles(folder) : ''}
                </div>
            </div>
        `;
    }).join('');
}

function renderFolderFiles(folder) {
    const files = folder.files_detail || folder.files.map(name => ({ filename: name }));

    if (!files || files.length === 0) {
        return '<div class="modal-loading">No .ts files found</div>';
    }

    return files.map(fileObj => {
        const filename = fileObj.filename;
        let portHtml = '';
        let progressHtml = '';
        let metaHtml = '';
        
        // Metadata from folder details or stream status
        let meta = fileObj.metadata || null;
        let streamInfo = null;

        if (state.streamStatus && state.streamStatus.active_streams) {
            const stream = state.streamStatus.active_streams.find(s => s.filename === filename);
            if (stream) {
                streamInfo = stream;
                if (stream.metadata) meta = stream.metadata;
                
                if (stream.status === 'live') {
                    portHtml = `
                        <span class="folder-file-port">
                            <span class="live-dot"></span>
                            ${stream.port}
                        </span>
                    `;
                    const pct = Math.round((stream.progress || 0) * 100);
                    progressHtml = `
                        <div class="stream-progress-container" style="width: 100%; height: 4px; background: var(--bg-input); border-radius: 2px; margin-top: 4px; overflow: hidden;">
                            <div class="stream-progress-fill" style="width: ${pct}%; height: 100%; background: var(--blue); transition: width 0.5s linear;"></div>
                        </div>
                    `;
                } else {
                    portHtml = `<span class="folder-file-port">${stream.port} (${stream.status})</span>`;
                }
            }
        }
        
        let details = [];
        if (meta && !meta.error) {
            if (meta.duration) {
                const mins = Math.floor(meta.duration / 60);
                const secs = Math.floor(meta.duration % 60);
                details.push(`${mins}:${secs.toString().padStart(2, '0')}`);
            }
            if (meta.width && meta.height) details.push(`${meta.width}x${meta.height}`);
        }

        // Display Streaming URL if stream is active (fallback to rtmp_url if stream_url is missing)
        if (streamInfo) {
            const displayUrl = streamInfo.stream_url || streamInfo.rtmp_url;
            if (displayUrl) {
                details.push(`<span onclick="copyToClipboard('${escapeAttr(displayUrl)}'); event.stopPropagation();" style="cursor: pointer; color: var(--blue); text-decoration: underline;" title="Click to copy URL">🔗 ${escapeHtml(displayUrl)}</span>`);
            }
        }

        if (details.length > 0) {
            metaHtml = `<div class="file-meta" style="font-size: 0.85em; margin-top: 2px; display: flex; gap: 8px; align-items: center;">${details.join(' <span style="color:var(--border);">•</span> ')}</div>`;
        }

        return `
            <div class="folder-file-item" style="flex-wrap: wrap;">
                <div style="display: flex; align-items: center; width: 100%;">
                    <img class="folder-file-thumb" 
                         src="/api/streamer/folder/${encodeURIComponent(folder.name)}/thumbnail/${encodeURIComponent(filename)}" 
                         alt="" loading="lazy"
                         onerror="this.style.display='none'">
                    <div style="display: flex; flex-direction: column; flex: 1; min-width: 0;">
                        <span class="folder-file-name" title="${escapeHtml(filename)}">${escapeHtml(filename)}</span>
                        ${metaHtml}
                    </div>
                    ${portHtml}
                </div>
                ${progressHtml}
            </div>
        `;
    }).join('');
}

async function toggleFolder(name) {
    if (state.expandedFolders.has(name)) {
        state.expandedFolders.delete(name);
    } else {
        state.expandedFolders.add(name);

        // Fetch folder details for expanded view
        try {
            const detail = await api('GET', `/streamer/folder/${encodeURIComponent(name)}`);
            // Update folder in state with detailed data
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

function updateStreamUI() {
    if (!state.streamStatus) return;

    const badge = document.getElementById('stream-status-badge');
    const startBtn = document.getElementById('stream-start-btn');
    const stopBtn = document.getElementById('stream-stop-btn');
    const errorsEl = document.getElementById('stream-errors');

    if (state.streamStatus.is_running) {
        const liveCount = state.streamStatus.active_streams.filter(s => s.status === 'live').length;
        badge.textContent = `Live · ${liveCount} streams`;
        badge.className = 'stream-status-badge live';
        startBtn.disabled = true;
        stopBtn.disabled = false;
    } else {
        badge.textContent = 'Idle';
        badge.className = 'stream-status-badge';
        startBtn.disabled = !state.streamerFolders.length;
        stopBtn.disabled = true;
    }

    // Show errors
    if (state.streamStatus.errors && state.streamStatus.errors.length > 0) {
        errorsEl.style.display = 'block';
        errorsEl.innerHTML = state.streamStatus.errors.map(e => `<p>⚠ ${escapeHtml(e)}</p>`).join('');
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

    try {
        const data = await api('POST', '/streamer/start', {
            port_range_start: portStart,
            port_range_end: portEnd,
            protocol: protocol
        });

        if (data.status === 'error') {
            showToast(data.error, 'error');
        } else {
            showToast('Streaming started', 'success');
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

    // Root/Drive
    items.push(`<span class="breadcrumb-item" onclick="browseTo('')">💻</span>`);

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
                <span class="modal-dir-icon">📁</span>
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
    const card = e.currentTarget;
    card.classList.remove('drag-over');

    const filename = e.dataTransfer.getData('text/plain');
    if (!filename) return;

    try {
        const data = await api('POST', '/converter/move', {
            filename: filename,
            target_folder: targetFolder
        });
        showToast(data.message, 'success');
        
        // Immediately rescan to update UI
        if (state.config && state.config.converter && state.config.converter.source_folder) {
            scanConverterFolder(state.config.converter.source_folder);
        }
        if (state.config && state.config.streamer && state.config.streamer.content_folder) {
            scanStreamerFolder(state.config.streamer.content_folder);
        }
    } catch (err) {
        showToast(`Failed to move file: ${err.message}`, 'error');
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
window.copyToClipboard = copyToClipboard;