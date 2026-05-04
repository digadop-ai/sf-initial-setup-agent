/* sf-initial-setup-agent — dashboard live updates. Vanilla JS, no framework. */

(function () {
    if (!window.SF_DASHBOARD) return;

    const startBtn   = document.getElementById('start-btn');
    const cancelBtn  = document.getElementById('cancel-btn');
    const statePill  = document.getElementById('state-pill');
    const tbody      = document.querySelector('#chunk-table tbody');
    const empty      = document.getElementById('chunk-empty');
    const log        = document.getElementById('event-log');

    /** chunk_id → <tr> */
    const rows = new Map();
    let ws = null;
    let backoff = 1000;

    function setState(state) {
        statePill.textContent = state;
        statePill.className = 'pill state-' + state;
        const running = state === 'running';
        startBtn.disabled = running;
        cancelBtn.disabled = !running;
    }

    function logLine(event) {
        const ts = event.ts || new Date().toISOString();
        const summary = JSON.stringify(event);
        log.textContent += `${ts}  ${summary}\n`;
        log.scrollTop = log.scrollHeight;
    }

    function rowFor(chunkId) {
        let tr = rows.get(chunkId);
        if (tr) return tr;
        tr = document.createElement('tr');
        tr.className = 'chunk-row state-running';
        tr.innerHTML = `
            <td><code>${chunkId}</code></td>
            <td class="cstate">running</td>
            <td class="cmembers">—</td>
            <td class="cfiles">—</td>
            <td class="celapsed">0s</td>
            <td class="cnotes"></td>
        `;
        tbody.appendChild(tr);
        rows.set(chunkId, tr);
        empty.style.display = 'none';
        return tr;
    }

    function updateChunk(event) {
        const id = event.chunk_id;
        if (!id) return;
        const tr = rowFor(id);
        const set = (sel, val) => {
            const cell = tr.querySelector(sel);
            if (cell && val !== undefined && val !== null) cell.textContent = val;
        };
        if (event.event === 'chunk_started') {
            set('.cmembers', event.members);
            set('.celapsed', '0s');
            set('.cstate', 'running');
            tr.className = 'chunk-row state-running';
        } else if (event.event === 'chunk_progress') {
            set('.celapsed', event.elapsed_s + 's');
        } else if (event.event === 'chunk_done') {
            set('.cstate', 'done');
            set('.cfiles', event.files);
            set('.celapsed', event.elapsed_s + 's');
            tr.className = 'chunk-row state-done';
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
        }
    }

    function handleEvent(event) {
        logLine(event);
        const e = event.event;

        if (e === 'chunk_started' || e === 'chunk_progress' ||
            e === 'chunk_done' || e === 'chunk_failed') {
            updateChunk(event);
        } else if (e === 'process_exited') {
            setState(event.state || (event.returncode === 0 ? 'done' : 'failed'));
        } else if (e === 'cancelled') {
            setState('cancelled');
        } else if (e === 'no_session') {
            setState('idle');
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
                logLine({ event: 'bad_json', raw: msg.data });
            }
        });
        ws.addEventListener('close', () => {
            setTimeout(connect, Math.min(backoff, 15000));
            backoff *= 2;
        });
        ws.addEventListener('error', () => { try { ws.close(); } catch (_) {} });
    }

    startBtn.addEventListener('click', async () => {
        startBtn.disabled = true;
        try {
            const res = await fetch('/start-retrieve', { method: 'POST' });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                alert('Could not start: ' + (err.detail || res.status));
                startBtn.disabled = false;
                return;
            }
            // Server-side state will flip; we'll see events via WS.
            // Reconnect to pick up the new session's history.
            try { ws.close(); } catch (_) {}
            // Reset table for fresh session
            rows.clear();
            tbody.innerHTML = '';
            empty.style.display = '';
            log.textContent = '';
            setState('running');
        } catch (e) {
            alert('Start failed: ' + e);
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
