/* sf-initial-setup-agent — dashboard live updates. Vanilla JS, no framework. */

(function () {
    if (!window.SF_DASHBOARD) return;

    const startBtn      = document.getElementById('start-btn');
    const cancelBtn     = document.getElementById('cancel-btn');
    const statePill     = document.getElementById('state-pill');
    const tbody         = document.querySelector('#chunk-table tbody');
    const empty         = document.getElementById('chunk-empty');
    const log           = document.getElementById('event-log');
    const setupStatus   = document.getElementById('setup-status');
    const setupStatusTx = document.getElementById('setup-status-text');
    const banner         = document.getElementById('completion-banner');
    const bannerHead     = banner ? banner.querySelector('.completion-headline') : null;
    const bannerDetail   = banner ? banner.querySelector('.completion-detail') : null;
    const bannerClose    = banner ? banner.querySelector('.completion-close') : null;
    const bannerCountdown = banner ? banner.querySelector('.completion-countdown') : null;
    const bannerStayOpen = banner ? banner.querySelector('.completion-stayopen') : null;
    const bannerQuitNow  = banner ? banner.querySelector('.completion-quitnow') : null;

    const MAX_LOG_ROWS = 250;

    /** chunk_id → <tr> */
    const rows = new Map();
    let ws = null;
    let backoff = 1000;
    let chunksStarted = false;

    // ── Completion-event state ──────────────────────────────────────────
    const ORIGINAL_TITLE = document.title;
    let runStartMs = null;
    let chunksDoneCount = 0;
    let chunksFailedCount = 0;
    let filesRetrievedCount = 0;
    let titleFlashHandle = null;
    let shutdownAtEpochMs = null;
    let shutdownTickHandle = null;

    function setState(state) {
        statePill.textContent = state;
        statePill.className = 'pill state-' + state;
        const running = state === 'running';
        startBtn.disabled = running;
        cancelBtn.disabled = !running;
    }

    // ── Event formatting (raw → human-readable log row) ─────────────────

    function fmtInt(n) { return (n == null) ? '' : Number(n).toLocaleString(); }

    function numEl(n) { return `<span class="event-num">${fmtInt(n)}</span>`; }

    function codeEl(s) { return `<code>${escapeHtml(s)}</code>`; }

    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#39;');
    }

    /**
     * Map a raw event from the backend to a renderable log row.
     * Return null to suppress (e.g. chunk_started — already shown in the chunks table).
     * Returned shape: { level, icon, html }
     */
    function formatEvent(event) {
        const e = event.event;
        switch (e) {
            // ── Setup phase ──
            case 'api_version':
                return { level: 'info', icon: '•',
                    html: `Connected to org. Metadata API ${codeEl('v' + event.value)}.` };
            case 'enumerate_started':
                return { level: 'step', icon: '→',
                    html: `Listing metadata types from org…` };
            case 'metadata_types_listed':
                return { level: 'info', icon: '•',
                    html: `Found ${numEl(event.count)} metadata types via describeMetadata.` };
            case 'enumerate_progress':
                return null;  // shown live in setup-status banner
            case 'enumerate_done':
                return { level: 'success', icon: '✓',
                    html: `Enumerated ${numEl(event.member_count)} members across ${numEl(event.type_count)} types.` };
            case 'metadata_types_supplemental_listed':
                return { level: 'step', icon: '→',
                    html: `Reviewing org features: trying ${numEl(event.count)} additional types not in describeMetadata…` };
            case 'enumerate_supplemental_progress':
                return null;  // shown live in setup-status banner
            case 'metadata_types_supplemented':
                if (event.types_with_members > 0) {
                    return { level: 'success', icon: '✓',
                        html: `Reviewing org features complete: ${numEl(event.types_with_members)} additional type(s) had members (reviewed ${numEl(event.types_tried)}).` };
                }
                return { level: 'info', icon: '•',
                    html: `Reviewing org features complete: no additional types matched (reviewed ${numEl(event.types_tried)}).` };
            case 'manifests_written':
                return { level: 'step', icon: '→',
                    html: `Built ${numEl(event.count)} chunk manifest${event.count === 1 ? '' : 's'}. Starting parallel retrieve…` };

            // ── Per-type retrieval phase ──
            case 'chunk_started': {
                const label = event.type_label || event.chunk_id;
                return { level: 'step', icon: '→',
                    html: `Retrieving ${codeEl(label)}…` };
            }
            case 'chunk_progress':
                return null;  // table cell shows live elapsed; would be too noisy in the log
            case 'chunk_done': {
                const label = event.type_label || event.chunk_id;
                const wc = event.warnings_count || 0;
                if (wc > 0) {
                    // Chunk succeeded but Salesforce returned per-member warnings
                    // (e.g. ExperienceBundle template unsupported, managed entity
                    // not found). Surface as a `warn` row so it's not lost in the
                    // success stream. Detail follows in `chunk_warnings`.
                    return { level: 'warn', icon: '⚠',
                        html: `Retrieved ${codeEl(label)} — ${numEl(event.files)} file${event.files === 1 ? '' : 's'} in ${event.elapsed_s}s, with ${numEl(wc)} warning${wc === 1 ? '' : 's'}` };
                }
                return { level: 'success', icon: '✓',
                    html: `Retrieved ${codeEl(label)} — ${numEl(event.files)} file${event.files === 1 ? '' : 's'} in ${event.elapsed_s}s` };
            }
            case 'chunk_warnings': {
                // Surface a category breakdown. Samples are available in the
                // payload for deeper inspection; the log row is intentionally
                // terse — operators chase details via retrieve-summary.json
                // or the per-chunk log file.
                const cats = event.by_category || {};
                const breakdown = Object.entries(cats)
                    .map(([cat, n]) => `${numEl(n)} ${codeEl(cat)}`)
                    .join(', ');
                return { level: 'warn', icon: '⚠',
                    html: `${codeEl(event.chunk_id)}: ${breakdown || numEl(event.count) + ' warning(s)'}` };
            }
            case 'warn_experience_cloud_template': {
                // High-signal: name the affected sites explicitly so the operator
                // can verify their content was captured by sibling EC types.
                const sites = (event.affected_sites || []).map(codeEl).join(', ');
                return { level: 'warn', icon: '⚠',
                    html: `Experience Cloud: ${sites || '(unknown)'} — ${escapeHtml(event.note || '')}` };
            }
            case 'folder_items_enumerated': {
                const counts = event.counts || {};
                const summary = Object.entries(counts)
                    .filter(([, n]) => n > 0)
                    .map(([t, n]) => `${codeEl(t)} ${numEl(n)}`)
                    .join(', ');
                return { level: 'info', icon: '•',
                    html: `Folder-based items enumerated: ${summary || '(none)'}` };
            }
            case 'chunk_failed': {
                const label = event.type_label || event.chunk_id;
                return { level: 'error', icon: '✗',
                    html: `${codeEl(label)} failed: ${escapeHtml(event.error || 'unknown error')}` +
                          (event.log_path ? ` — log: ${codeEl(event.log_path)}` : '') };
            }

            // ── Post phase ──
            case 'sync_state_written':
                return { level: 'success', icon: '✓',
                    html: `Sync state written to ${codeEl(event.path)}.` };
            case 'sync_state_error':
                return { level: 'warn', icon: '⚠',
                    html: `Could not write sync state: ${escapeHtml(event.error)}` };
            case 'troubleshooter_starting':
                return { level: 'step', icon: '→',
                    html: `Troubleshooter starting (${numEl(event.failed_count)} failed chunk${event.failed_count === 1 ? '' : 's'})…` };
            case 'troubleshooter_skipped':
                return { level: 'info', icon: '•',
                    html: `Troubleshooter skipped — ${escapeHtml(event.reason || '')}` };
            case 'troubleshooter_exited':
                return { level: 'info', icon: '•',
                    html: `Troubleshooter finished (exit ${event.returncode}).` };

            // ── Lifecycle ──
            case 'cancelled':
                return { level: 'warn', icon: '■',
                    html: `Retrieve cancelled by user.` };
            case 'process_exited':
                if (event.state === 'done') {
                    return { level: 'success', icon: '✓',
                        html: `Retrieve finished cleanly.` };
                } else if (event.state === 'cancelled') {
                    return null;  // already logged via 'cancelled'
                }
                return { level: 'error', icon: '✗',
                    html: `Retrieve exited with state ${codeEl(event.state)} (returncode ${event.returncode}).` };

            // ── Warnings ──
            case 'warn_experience_cloud':
                return { level: 'warn', icon: '⚠',
                    html: escapeHtml(event.message || 'Experience Cloud warning') };
            case 'warn_enumeration_list_failed':
                // Issue #29: a describeMetadata-reported type failed to list.
                // The retrieve still attempts it via a wildcard member, but the
                // operator should treat this as a completeness risk.
                return { level: 'warn', icon: '⚠',
                    html: `Could not list ${codeEl(event.type)} members — retrieving via wildcard fallback. ` +
                          escapeHtml(event.error || '') };

            // ── Shutdown scheduling ──
            case 'shutdown_kept_alive':
                return { level: 'info', icon: '•',
                    html: `Server staying alive — dashboard tab is still open. Close the tab to release, or click <em>Quit now</em> to shut down immediately.` };
            case 'shutdown_scheduled':
                return { level: 'info', icon: '•',
                    html: `Server shutting down in ${numEl(Math.round(event.grace_s))}s. Click <em>Stay open</em> to defer.` };
            case 'shutdown_deferred':
                return { level: 'info', icon: '•',
                    html: `Server shutdown deferred — terminal will stay open for 10 more minutes.` };
            case 'shutdown_now':
                return { level: 'info', icon: '•',
                    html: `Server shutting down. Terminal window will close.` };

            // ── Fallback for unknown events / generic logs ──
            case 'no_session':
                return null;
            case 'log':
                if (!event.message) return null;
                return { level: 'info', icon: '•',
                    html: escapeHtml(event.message) };
            case 'stdout':
                return null;
            default:
                // Unknown event — show name + small JSON payload, but not full dump
                return { level: 'info', icon: '•',
                    html: `${codeEl(e || 'event')}` };
        }
    }

    function tsToLocal(iso) {
        if (!iso) return new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
        try {
            return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
        } catch (_) { return iso; }
    }

    function logLine(event) {
        const formatted = formatEvent(event);
        if (!formatted) return;
        const row = document.createElement('div');
        row.className = `event-row event-${formatted.level}`;
        row.innerHTML =
            `<time class="event-time">${escapeHtml(tsToLocal(event.ts))}</time>` +
            `<span class="event-icon" aria-hidden="true">${escapeHtml(formatted.icon || '•')}</span>` +
            `<span class="event-msg">${formatted.html}</span>`;
        log.appendChild(row);
        // Trim oldest rows so DOM doesn't grow unbounded over a long run.
        while (log.childElementCount > MAX_LOG_ROWS) log.removeChild(log.firstElementChild);
        log.scrollTop = log.scrollHeight;
    }

    function setSetupStatus(text) {
        if (text == null) {
            setupStatus.hidden = true;
            setupStatusTx.textContent = '';
        } else {
            setupStatusTx.textContent = text;
            setupStatus.hidden = false;
        }
    }

    function rowFor(chunkId, label) {
        let tr = rows.get(chunkId);
        if (tr) return tr;
        tr = document.createElement('tr');
        tr.className = 'chunk-row state-running';
        const display = label || chunkId;
        tr.innerHTML = `
            <td class="ctype"><strong>${escapeHtml(display)}</strong></td>
            <td class="cstate">running</td>
            <td class="cmembers">—</td>
            <td class="cfiles">—</td>
            <td class="celapsed">0s</td>
            <td class="cwarnings">—</td>
            <td class="cnotes"></td>
        `;
        tbody.appendChild(tr);
        rows.set(chunkId, tr);
        empty.style.display = 'none';
        return tr;
    }

    function renderWarnings(cell, count, byCategory, samples) {
        // Build an expandable <details> block: summary shows the count + ⚠,
        // expanded body lists category breakdown and per-warning samples
        // (the chunk_warnings event includes up to 20 samples; rest are in
        // the chunk log file).
        cell.textContent = '';
        if (!count || count <= 0) {
            cell.textContent = '—';
            return;
        }
        const details = document.createElement('details');
        details.className = 'warnings-detail';
        const summary = document.createElement('summary');
        summary.innerHTML = `<strong>${count}</strong> ⚠`;
        details.appendChild(summary);

        if (byCategory && Object.keys(byCategory).length) {
            const cats = document.createElement('div');
            cats.className = 'warning-categories';
            cats.textContent = Object.entries(byCategory)
                .map(([cat, n]) => `${cat}: ${n}`).join(' · ');
            details.appendChild(cats);
        }
        if (samples && samples.length) {
            const list = document.createElement('ul');
            list.className = 'warnings-list';
            samples.forEach(w => {
                const li = document.createElement('li');
                const cat = document.createElement('span');
                cat.className = 'warning-category';
                cat.textContent = w.category || 'warning';
                li.appendChild(cat);
                if (w.member) {
                    const mem = document.createElement('code');
                    mem.textContent = w.member;
                    li.appendChild(document.createTextNode(' '));
                    li.appendChild(mem);
                }
                if (w.problem) {
                    const p = document.createElement('div');
                    p.className = 'warning-problem';
                    p.textContent = w.problem;
                    li.appendChild(p);
                }
                list.appendChild(li);
            });
            details.appendChild(list);
            if (count > samples.length) {
                const more = document.createElement('div');
                more.className = 'warnings-more';
                more.textContent = `+${count - samples.length} more — see retrieve-summary.json or the per-chunk log file.`;
                details.appendChild(more);
            }
        }
        cell.appendChild(details);
    }

    function updateChunk(event) {
        const id = event.chunk_id;
        if (!id) return;
        const tr = rowFor(id, event.type_label);
        const set = (sel, val) => {
            const cell = tr.querySelector(sel);
            if (cell && val !== undefined && val !== null) cell.textContent = val;
        };
        if (event.event === 'chunk_started') {
            set('.cmembers', event.members);
            set('.celapsed', '0s');
            set('.cstate', 'running');
            tr.className = 'chunk-row state-running';
            if (runStartMs == null) runStartMs = Date.now();
        } else if (event.event === 'chunk_progress') {
            set('.celapsed', event.elapsed_s + 's');
        } else if (event.event === 'chunk_done') {
            set('.cstate', 'done');
            set('.cfiles', event.files);
            set('.celapsed', event.elapsed_s + 's');
            tr.className = 'chunk-row state-done';
            chunksDoneCount += 1;
            if (typeof event.files === 'number') filesRetrievedCount += event.files;
        } else if (event.event === 'chunk_failed') {
            set('.cstate', 'failed');
            set('.celapsed', event.elapsed_s + 's');
            const notes = tr.querySelector('.cnotes');
            if (notes) {
                const err = document.createElement('span');
                err.className = 'err';
                err.textContent = event.error || 'failed';
                notes.appendChild(err);
                if (event.log_path) {
                    const code = document.createElement('code');
                    code.textContent = ' ' + event.log_path;
                    notes.appendChild(code);
                }
            }
            tr.className = 'chunk-row state-failed';
            chunksFailedCount += 1;
        } else if (event.event === 'chunk_warnings') {
            // Populate the warnings cell with count + expandable detail.
            // Triggered AFTER chunk_done for chunks that returned warnings,
            // or AFTER chunk_failed for failed chunks whose silent-drop
            // detection produced structured warnings.
            const cell = tr.querySelector('.cwarnings');
            if (cell) {
                renderWarnings(cell, event.count || 0, event.by_category || {}, event.samples || []);
            }
        }
    }

    // ── Completion celebration ──────────────────────────────────────────

    function formatElapsed(ms) {
        if (!ms || ms < 0) return '';
        const totalSec = Math.round(ms / 1000);
        const m = Math.floor(totalSec / 60);
        const s = totalSec % 60;
        return m > 0 ? `${m}m ${s}s` : `${s}s`;
    }

    function playChime(state) {
        try {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return;
            const ctx = new Ctx();
            const now = ctx.currentTime;
            const tones = state === 'done'
                ? [{ f: 523.25, t: 0.00 }, { f: 659.25, t: 0.10 }, { f: 783.99, t: 0.20 }]
                : state === 'failed'
                ? [{ f: 415.30, t: 0.00 }, { f: 311.13, t: 0.18 }, { f: 233.08, t: 0.36 }]
                : [{ f: 392.00, t: 0.00 }, { f: 329.63, t: 0.18 }];
            const master = ctx.createGain();
            master.gain.value = 0.18;
            master.connect(ctx.destination);
            tones.forEach(({ f, t }) => {
                const osc = ctx.createOscillator();
                const env = ctx.createGain();
                osc.type = state === 'failed' ? 'sawtooth' : 'triangle';
                osc.frequency.value = f;
                env.gain.setValueAtTime(0, now + t);
                env.gain.linearRampToValueAtTime(1, now + t + 0.02);
                env.gain.exponentialRampToValueAtTime(0.001, now + t + 0.55);
                osc.connect(env).connect(master);
                osc.start(now + t);
                osc.stop(now + t + 0.6);
            });
            setTimeout(() => { try { ctx.close(); } catch (_) {} }, 1500);
        } catch (_) { /* audio blocked; banner + title flash still fire */ }
    }

    function startTitleFlash(prefix) {
        stopTitleFlash();
        let on = true;
        titleFlashHandle = setInterval(() => {
            document.title = on ? `${prefix} · ${ORIGINAL_TITLE}` : ORIGINAL_TITLE;
            on = !on;
        }, 900);
        const stopOnFocus = () => {
            stopTitleFlash();
            window.removeEventListener('focus', stopOnFocus);
            document.removeEventListener('visibilitychange', visStop);
        };
        const visStop = () => { if (!document.hidden) stopOnFocus(); };
        window.addEventListener('focus', stopOnFocus);
        document.addEventListener('visibilitychange', visStop);
    }

    function stopTitleFlash() {
        if (titleFlashHandle) { clearInterval(titleFlashHandle); titleFlashHandle = null; }
        document.title = ORIGINAL_TITLE;
    }

    function showCompletionBanner(state, headline, detail) {
        if (!banner) return;
        banner.classList.remove('state-done', 'state-failed', 'state-cancelled', 'pulsing');
        banner.classList.add('state-' + state);
        if (state === 'done') banner.classList.add('pulsing');
        bannerHead.textContent = headline;
        bannerDetail.textContent = detail || '';
        banner.hidden = false;
        document.body.classList.add('has-completion-banner');
    }

    function hideCompletionBanner() {
        if (!banner) return;
        banner.hidden = true;
        document.body.classList.remove('has-completion-banner');
        stopShutdownCountdown();
    }

    function tickShutdownCountdown() {
        if (!bannerCountdown || shutdownAtEpochMs == null) return;
        const remainingMs = shutdownAtEpochMs - Date.now();
        if (remainingMs <= 0) {
            bannerCountdown.textContent = 'Closing now…';
            stopShutdownCountdown();
            return;
        }
        const sec = Math.ceil(remainingMs / 1000);
        bannerCountdown.textContent = `Terminal window will close in ${sec}s — click "Stay open" to defer.`;
        bannerCountdown.hidden = false;
    }

    function startShutdownCountdown(epochSeconds) {
        shutdownAtEpochMs = epochSeconds * 1000;
        if (bannerStayOpen) bannerStayOpen.hidden = false;
        if (bannerQuitNow) bannerQuitNow.hidden = false;
        tickShutdownCountdown();
        if (shutdownTickHandle) clearInterval(shutdownTickHandle);
        shutdownTickHandle = setInterval(tickShutdownCountdown, 500);
    }

    function showKeptAliveStatus() {
        if (!bannerCountdown) return;
        if (shutdownTickHandle) { clearInterval(shutdownTickHandle); shutdownTickHandle = null; }
        shutdownAtEpochMs = null;
        bannerCountdown.hidden = false;
        bannerCountdown.textContent = 'Dashboard tab is open — server staying alive. Close the tab to release, or click "Quit now".';
        if (bannerStayOpen) bannerStayOpen.hidden = true;
        if (bannerQuitNow) bannerQuitNow.hidden = false;
    }

    function stopShutdownCountdown() {
        if (shutdownTickHandle) { clearInterval(shutdownTickHandle); shutdownTickHandle = null; }
        shutdownAtEpochMs = null;
        if (bannerCountdown) { bannerCountdown.hidden = true; bannerCountdown.textContent = ''; }
        if (bannerStayOpen) bannerStayOpen.hidden = true;
        if (bannerQuitNow) bannerQuitNow.hidden = true;
    }

    if (bannerClose) bannerClose.addEventListener('click', hideCompletionBanner);
    if (bannerStayOpen) bannerStayOpen.addEventListener('click', async () => {
        bannerStayOpen.disabled = true;
        try {
            const res = await fetch('/defer-shutdown', { method: 'POST' });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                alert('Could not defer: ' + (err.detail || res.status));
            }
        } catch (e) {
            alert('Defer failed: ' + e);
        } finally {
            bannerStayOpen.disabled = false;
        }
    });
    if (bannerQuitNow) bannerQuitNow.addEventListener('click', async () => {
        if (!confirm('Quit now? The agent server will exit and the Terminal window will close.')) return;
        bannerQuitNow.disabled = true;
        try {
            const res = await fetch('/shutdown-now', { method: 'POST' });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                alert('Quit failed: ' + (err.detail || res.status));
                bannerQuitNow.disabled = false;
            }
        } catch (e) {
            alert('Quit failed: ' + e);
            bannerQuitNow.disabled = false;
        }
    });

    function onRetrieveTerminal(state) {
        const elapsed = runStartMs ? formatElapsed(Date.now() - runStartMs) : '';
        const totalChunks = chunksDoneCount + chunksFailedCount;
        let headline, detail;
        if (state === 'done') {
            headline = '✓ Retrieve complete';
            detail = `${fmtInt(filesRetrievedCount)} files across ${totalChunks} chunk${totalChunks === 1 ? '' : 's'}` +
                     (elapsed ? ` · ${elapsed}` : '');
        } else if (state === 'cancelled') {
            headline = 'Retrieve cancelled';
            detail = `${chunksDoneCount} of ${totalChunks} chunk${totalChunks === 1 ? '' : 's'} completed before cancel` +
                     (elapsed ? ` · ${elapsed}` : '');
        } else {
            headline = '✗ Retrieve finished with failures';
            detail = `${chunksFailedCount} of ${totalChunks} chunk${totalChunks === 1 ? '' : 's'} failed — see the summary`;
        }
        showCompletionBanner(state, headline, detail);
        playChime(state);
        if (document.hidden) {
            const prefix = state === 'done' ? '✓ DONE' : state === 'failed' ? '✗ FAILED' : '⏹ STOPPED';
            startTitleFlash(prefix);
        }
    }

    // ── Event routing ────────────────────────────────────────────────────

    function handleEvent(event) {
        const e = event.event;

        // Setup-phase status banner — transient "what's happening right now".
        if (e === 'api_version') {
            setSetupStatus(`Connected. Metadata API v${event.value}. Listing metadata types from org…`);
        } else if (e === 'enumerate_started') {
            setSetupStatus('Listing metadata types from org…');
        } else if (e === 'metadata_types_listed') {
            setSetupStatus(`Enumerating ${event.count} metadata types…`);
        } else if (e === 'enumerate_progress') {
            setSetupStatus(`Enumerating metadata types: ${event.completed} of ${event.total}…`);
        } else if (e === 'enumerate_done') {
            setSetupStatus(`Enumerated ${event.member_count.toLocaleString()} members across ${event.type_count} types. Reviewing org features…`);
        } else if (e === 'metadata_types_supplemental_listed') {
            setSetupStatus(`Reviewing org features: trying ${event.count} additional types…`);
        } else if (e === 'enumerate_supplemental_progress') {
            setSetupStatus(`Reviewing org features: ${event.completed} of ${event.total}…`);
        } else if (e === 'metadata_types_supplemented') {
            setSetupStatus(`Reviewing org features complete (${event.types_with_members} of ${event.types_tried} additional types had members). Building chunks…`);
        } else if (e === 'manifests_written') {
            setSetupStatus(`${event.count} chunk manifest(s) written. Starting parallel retrieve…`);
        }

        logLine(event);

        if (e === 'chunk_started' || e === 'chunk_progress' ||
            e === 'chunk_done' || e === 'chunk_failed' ||
            e === 'chunk_warnings') {
            if (e === 'chunk_started' && !chunksStarted) {
                chunksStarted = true;
                setSetupStatus(null);
            }
            updateChunk(event);
        } else if (e === 'process_exited') {
            setSetupStatus(null);
            const finalState = event.state || (event.returncode === 0 ? 'done' : 'failed');
            setState(finalState);
            onRetrieveTerminal(finalState);
        } else if (e === 'cancelled') {
            setSetupStatus(null);
            setState('cancelled');
            onRetrieveTerminal('cancelled');
        } else if (e === 'no_session') {
            setState('idle');
        } else if (e === 'shutdown_kept_alive') {
            showKeptAliveStatus();
        } else if (e === 'shutdown_scheduled') {
            startShutdownCountdown(event.shutdown_at_epoch);
        } else if (e === 'shutdown_deferred') {
            stopShutdownCountdown();
            if (bannerCountdown) {
                bannerCountdown.hidden = false;
                bannerCountdown.textContent = 'Terminal will stay open for 10 more minutes.';
            }
        } else if (e === 'shutdown_now') {
            if (bannerCountdown) {
                bannerCountdown.hidden = false;
                bannerCountdown.textContent = 'Closing now…';
            }
        }
    }

    function connect() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${proto}//${location.host}/ws/progress`);

        ws.addEventListener('open', () => { backoff = 1000; });
        ws.addEventListener('message', (msg) => {
            try {
                handleEvent(JSON.parse(msg.data));
            } catch (e) {
                logLine({ event: 'log', message: 'bad JSON from server', ts: new Date().toISOString() });
            }
        });
        ws.addEventListener('close', () => {
            setTimeout(connect, Math.min(backoff, 15000));
            backoff *= 2;
        });
        ws.addEventListener('error', () => { try { ws.close(); } catch (_) {} });
    }

    startBtn.addEventListener('click', async () => {
        // FIRST: lock the button + flip the entire UI to "running" state so the
        // user sees instant feedback. The fetch comes AFTER. If the fetch fails,
        // we undo the visual state below.
        startBtn.disabled = true;
        try { ws.close(); } catch (_) {}
        rows.clear();
        tbody.innerHTML = '';
        empty.style.display = '';
        log.replaceChildren();
        chunksStarted = false;
        runStartMs = null;
        chunksDoneCount = 0;
        chunksFailedCount = 0;
        filesRetrievedCount = 0;
        stopTitleFlash();
        hideCompletionBanner();
        setSetupStatus('Starting…');
        setState('running');

        try {
            const res = await fetch('/start-retrieve', { method: 'POST' });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                alert('Could not start: ' + (err.detail || res.status));
                // Roll back the optimistic UI to idle.
                setSetupStatus(null);
                setState('idle');
                startBtn.disabled = false;
                return;
            }
            // Server-side state will flip; we'll see events via WS reconnect.
        } catch (e) {
            alert('Start failed: ' + e);
            setSetupStatus(null);
            setState('idle');
            startBtn.disabled = false;
        }
    });

    cancelBtn.addEventListener('click', async () => {
        if (!confirm('Cancel the running retrieve? Chunks already complete on the server side will keep their files; in-flight chunks may leave partial state.')) return;
        cancelBtn.disabled = true;
        try {
            const res = await fetch('/cancel-retrieve', { method: 'POST' });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                alert('Cancel failed: ' + (err.detail || res.status));
            }
        } catch (e) {
            alert('Cancel failed: ' + e);
        }
    });

    connect();
})();
