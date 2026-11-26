(function () {
    const API = '/admin/zync_challenges/api';
    const LABELS = {
        // Zync jobs (CTFd)
        ingest: 'Ingest', sync: 'Sync', all: 'Rebuild + ingest', delete: 'Delete from CTFd',
        // Galvanize operations
        clone: 'Clone', pull: 'Update repository', remove: 'Remove repository', build: 'Rebuild', images: 'Images',
    };
    const STATUS_BADGES = {
        queued: 'secondary', running: 'primary', success: 'success',
        warning: 'warning', failed: 'danger', interrupted: 'dark', cancelling: 'secondary', cancelled: 'dark',
    };
    const RUNNING = ['queued', 'running', 'cancelling'];
    const DOWNLOADS = { clone: 'Cloning', pull: 'Updating' }; // Galvanize operations fetching a repository

    let repos = null; // until the first overview
    let reposError = null;
    let orphanRepos = [];
    let currentRepo = null;
    let challenges = [];
    let imageStates = {}; // image -> {host: state}, from the last "Check images"
    let selected = new Set();
    let scope = 'all';
    let shown = null; // {kind: 'job'|'op', id}
    let offset = 0;
    let pollTimer = null;
    let wasRunning = false;
    let afterTask = null; // callback when the shown task finishes
    let tasksTimer = null;
    let runningTasks = new Set(); // "kind:id" of the tasks running at the last listing
    let downloads = []; // clones and pulls running

    const $ = (id) => document.getElementById(id);

    function esc(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    async function api(path, options = {}) {
        const opts = Object.assign({ credentials: 'same-origin', headers: {} }, options);
        if (opts.body !== undefined) {
            opts.method = opts.method || 'POST';
            opts.headers['Content-Type'] = 'application/json';
            opts.headers['CSRF-Token'] = window.init.csrfNonce;
            opts.body = JSON.stringify(opts.body);
        }
        const resp = await fetch(API + path, opts);
        let data = null;
        try { data = await resp.json(); } catch (e) { /* not JSON */ }
        if (!resp.ok || !data || data.success === false) {
            throw new Error((data && data.error) || `HTTP ${resp.status}`);
        }
        return data.data;
    }

    function alertBox(message, type = 'danger') {
        const div = document.createElement('div');
        div.className = `alert alert-${type} alert-dismissible fade show`;
        div.innerHTML = `${esc(message)}<button type="button" class="close" data-dismiss="alert"><span>&times;</span></button>`;
        $('zc-alerts').appendChild(div);
        if (type === 'success' || type === 'info') setTimeout(() => div.remove(), 6000);
    }

    const badge = (text, type) => `<span class="badge badge-${type}">${esc(text)}</span>`;
    const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : '');
    const isRunning = (status) => RUNNING.includes(status);
    const spinner = '<span class="spinner-border spinner-border-sm text-primary align-middle mr-1" role="status"><span class="sr-only">Running</span></span>';
    const statusBadge = (status) => (isRunning(status) ? spinner : '') + badge(status, STATUS_BADGES[status] || 'secondary');
    // Galvanize reports no progress for a clone or a pull: the bar only shows that it runs
    const progressBar = '<div class="progress zc-progress"><div class="progress-bar progress-bar-striped progress-bar-animated w-100" role="progressbar"></div></div>';

    // Replaces the content of el only when it changed: the panels are
    // rendered again at every poll, and new spinners and progress bars would
    // restart their animation each time
    function setHTML(el, html) {
        if (el.zcHTML === html) return;
        el.zcHTML = html;
        el.innerHTML = html;
    }

    // ── Repositories ────────────────────────────────────────────────────────

    // Each loader returns an error message, or null
    async function loadOverview() {
        let data;
        try {
            data = await api('/overview');
        } catch (e) {
            alertBox('Cannot load the overview: ' + e.message);
            return e.message;
        }
        repos = data.repos || [];
        reposError = data.galvanize.error || null;
        orphanRepos = data.orphan_repos || [];
        $('zc-galvanize-url').textContent = data.galvanize.url ? '— ' + data.galvanize.url : '';
        const sources = data.clone_sources || [];
        $('zc-clone-sources').innerHTML = sources.length
            ? 'Allowed sources: ' + sources.map((s) => `<code>${esc(s)}</code>`).join(', ')
            : '<span class="text-danger">Cloning is disabled: configure repositories.sources in Galvanize.</span>';
        $('zc-clone-form').querySelector('button[type=submit]').disabled = !sources.length;
        renderRepos();
        if (data.active_job && !shown) showTask('job', data.active_job.id);
        const known = repos.map((r) => r.name).concat(orphanRepos);
        if (currentRepo !== null && !known.includes(currentRepo)) {
            currentRepo = null;
            $('zc-challenges-card').style.display = 'none';
        }
        if (currentRepo === null && known.length === 1) selectRepo(known[0]);
        return reposError;
    }

    function renderRepos() {
        if (repos === null) return;
        const error = reposError;
        const rows = repos.map((r) => {
            let git = '<span class="text-muted">plain folder</span>';
            if (r.is_git) {
                git = `<div class="small"><code>${esc(r.remote || 'no remote')}</code></div>
                       <div class="small text-muted">${esc(r.ref || r.branch || 'detached')} @ ${esc(r.commit || '?')}
                       ${r.commit_message ? '— ' + esc(r.commit_message) : ''} ${r.dirty ? badge('local changes', 'warning') : ''}</div>`;
            }
            if (r.error) git += `<div class="small text-danger">${esc(r.error)}</div>`;
            return `<tr class="${r.name === currentRepo ? 'table-active' : ''}">
                <td><a href="#" data-open="${esc(r.name)}" class="font-weight-bold">${esc(r.name)}</a></td>
                <td>${git}</td>
                <td class="text-center">${esc(r.challenges)}</td>
                <td class="text-right text-nowrap">
                    ${r.is_git ? `<button class="btn btn-sm btn-outline-info" data-repo-action="pull" data-repo="${esc(r.name)}" title="Fetch and reset to the remote"
                        ${downloads.some((d) => d.repo === r.name) ? 'disabled' : ''}><i class="fas fa-cloud-download-alt"></i> Update</button>` : ''}
                    <button class="btn btn-sm btn-outline-danger" data-repo-action="remove" data-repo="${esc(r.name)}" title="Delete the folder from Galvanize"><i class="fas fa-trash"></i></button>
                </td></tr>`;
        }).concat(orphanRepos.map((name) => `<tr class="${name === currentRepo ? 'table-active' : ''}">
                <td><a href="#" data-open="${esc(name)}" class="font-weight-bold">${esc(name)}</a></td>
                <td><span class="text-warning small">No longer in Galvanize: its challenges are still in CTFd</span></td>
                <td class="text-center">—</td>
                <td></td></tr>`));

        if (error || !rows.length) {
            $('zc-repos-table').style.display = rows.length ? '' : 'none';
            $('zc-repos-empty').style.display = '';
            $('zc-repos-empty').textContent = error || 'No challenge repository in Galvanize: clone one.';
        } else {
            $('zc-repos-empty').style.display = 'none';
            $('zc-repos-table').style.display = '';
        }
        setHTML($('zc-repos-tbody'), rows.join(''));
        setHTML($('zc-downloads'), downloads.map((d) => `<div class="small mb-2">${spinner}${DOWNLOADS[d.action]}
            <code>${esc(d.repo)}</code>...${progressBar}</div>`).join(''));
    }

    // ── Challenges ──────────────────────────────────────────────────────────

    async function selectRepo(name) {
        currentRepo = name;
        selected = new Set();
        $('zc-repo-title').textContent = name;
        $('zc-challenges-card').style.display = '';
        renderRepos();
        await loadChallenges();
    }

    async function loadChallenges() {
        if (currentRepo === null) return null;
        $('zc-challenges-spinner').style.display = '';
        try {
            const data = await api('/challenges?repo=' + encodeURIComponent(currentRepo));
            challenges = data.challenges;
            const keys = new Set(challenges.map((c) => c.key));
            selected = new Set([...selected].filter((k) => keys.has(k)));
            const cats = [...new Set(challenges.map((c) => c.category))].sort();
            const current = $('zc-filter-category').value;
            $('zc-filter-category').innerHTML = '<option value="">All categories</option>'
                + cats.map((c) => `<option value="${esc(c)}" ${c === current ? 'selected' : ''}>${esc(c)}</option>`).join('');
            const skipped = data.skipped || [];
            $('zc-skipped').style.display = skipped.length ? '' : 'none';
            $('zc-skipped').innerHTML = `<strong>${skipped.length} definition(s) Galvanize could not read:</strong>`
                + skipped.map((s) => `<div><code>${esc(s.path)}</code>: ${esc(s.reason)}</div>`).join('');
            renderChallenges();
            return null;
        } catch (e) {
            alertBox('Cannot load the challenges: ' + e.message);
            return e.message;
        } finally {
            $('zc-challenges-spinner').style.display = 'none';
        }
    }

    function filteredChallenges() {
        const cat = $('zc-filter-category').value;
        const text = $('zc-filter-text').value.trim().toLowerCase();
        return challenges.filter((c) => (!cat || c.category === cat)
            && (!text || c.name.toLowerCase().includes(text) || c.path.toLowerCase().includes(text)));
    }

    // Badges for an image's state per host, as Galvanize's last image action reported it
    function imageStateBadges(hosts) {
        const groups = { present: [], missing: [], kept: [], unreachable: [], error: [] };
        const group = {
            present: 'present', failed: 'kept', // failed: a removal failed, the image is still there
            absent: 'missing', removed: 'missing', unreachable: 'unreachable',
        };
        Object.entries(hosts).forEach(([host, state]) => groups[group[state] || 'error'].push(host));
        const parts = [];
        if (!groups.missing.length && !groups.unreachable.length && !groups.error.length) parts.push(badge('on every host', 'success'));
        if (groups.missing.length) parts.push(badge('missing on ' + groups.missing.join(', '), 'warning'));
        if (groups.kept.length) parts.push(badge('not removed on ' + groups.kept.join(', '), 'info'));
        if (groups.unreachable.length) parts.push(badge('unreachable: ' + groups.unreachable.join(', '), 'dark'));
        if (groups.error.length) parts.push(badge('error on ' + groups.error.join(', '), 'danger'));
        return parts.join(' ');
    }

    function imageCell(c) {
        const b = c.build || {};
        if (c.orphan) return '';
        if (!c.deployable) return '<span class="text-muted small">—</span>';
        const images = b.images || [];
        let html = images.map((img) => {
            const hosts = imageStates[img];
            return `<div class="small"><code>${esc(img)}</code> ${hosts ? imageStateBadges(hosts) : ''}</div>`;
        }).join('');
        if (b.refused) html = `<div>${badge('build refused', 'danger')}</div>` + html + `<div class="small text-danger">${esc(b.reason)}</div>`;
        else if (b.kind === 'none') html += `<div class="small text-muted">${esc(b.reason || 'nothing to build')}</div>`;
        else html = `<div class="small text-muted">${b.kind === 'compose' ? 'compose build' : 'docker build'}</div>` + html;
        return html;
    }

    function ctfdCell(c) {
        if (!c.ctfd) return badge('not ingested', 'light');
        const state = c.ctfd.state === 'visible' ? 'success' : 'secondary';
        return `<a href="/admin/challenges/${c.ctfd.id}" target="_blank">#${c.ctfd.id}</a> ${badge(c.ctfd.state, state)}
            ${c.orphan ? badge('definition gone', 'warning') : ''}
            <div class="small text-muted">${esc(c.ctfd.value)} pts</div>`;
    }

    function renderChallenges() {
        const rows = filteredChallenges();
        $('zc-challenge-count').textContent = challenges.length;
        $('zc-filtered-count').textContent = rows.length;
        $('zc-selected-count').textContent = selected.size;
        $('zc-select-all').checked = rows.length > 0 && rows.every((c) => selected.has(c.key));

        $('zc-challenges-tbody').innerHTML = rows.map((c) => {
            const reqs = c.requirements.length
                ? `<div class="small text-muted"><i class="fas fa-lock"></i> ${esc(c.requirements.join(', '))}</div>` : '';
            const actions = [];
            if ((c.build || {}).buildable) actions.push(`<button class="btn btn-sm btn-outline-info" data-row-action="build" data-key="${esc(c.key)}" title="Rebuild"><i class="fas fa-hammer"></i></button>`);
            if (c.ctfd && !c.orphan) actions.push(`<button class="btn btn-sm btn-outline-primary" data-row-action="sync" data-key="${esc(c.key)}" title="Sync"><i class="fas fa-sync-alt"></i></button>`);
            if (!c.ctfd) actions.push(`<button class="btn btn-sm btn-outline-success" data-row-action="ingest" data-key="${esc(c.key)}" title="Ingest"><i class="fas fa-file-import"></i></button>`);
            if (c.ctfd) actions.push(`<button class="btn btn-sm btn-outline-danger" data-row-action="delete" data-key="${esc(c.key)}" title="Delete from CTFd"><i class="fas fa-trash"></i></button>`);
            return `<tr>
                <td><input type="checkbox" data-key="${esc(c.key)}" ${selected.has(c.key) ? 'checked' : ''}></td>
                <td><div class="zc-name">${esc(c.name)} <span class="text-muted font-weight-normal">· ${esc(c.category)}</span></div>
                    <div class="zc-path">${esc(c.path)}</div>${reqs}</td>
                <td><span class="badge badge-info">${esc(c.type)}</span></td>
                <td>${imageCell(c)}</td>
                <td>${ctfdCell(c)}</td>
                <td class="text-right text-nowrap zc-row-actions">${actions.join(' ')}</td></tr>`;
        }).join('') || '<tr><td colspan="6" class="text-center text-muted">No challenge</td></tr>';
    }

    function scopeEntries() {
        if (scope === 'selected') return challenges.filter((c) => selected.has(c.key));
        if (scope === 'filtered') return filteredChallenges();
        return null; // whole repository
    }

    // ── Actions ─────────────────────────────────────────────────────────────

    async function startJob(body, confirmText) {
        if (confirmText && !window.confirm(confirmText)) return null;
        try {
            const job = await api('/jobs', { body });
            showTask('job', job.id);
            loadTasks();
            return job;
        } catch (e) {
            alertBox(e.message);
            return null;
        }
    }

    async function startOperation(action, body, confirmText) {
        if (confirmText && !window.confirm(confirmText)) return null;
        try {
            const op = await api('/galvanize/' + action, { body });
            showTask('op', op.id);
            loadTasks();
            return op;
        } catch (e) {
            alertBox(e.message);
            return null;
        }
    }

    function runAction(action, entries) {
        if (entries !== null && entries.length === 0) {
            alertBox('No challenge selected', 'warning');
            return;
        }
        const force = $('zc-opt-force').checked;
        const dryRun = $('zc-opt-dry').checked;
        const label = entries === null ? `every challenge of '${currentRepo}'` : `${entries.length} challenge(s)`;
        const refs = (list) => list.map((c) => ({ path: c.path }));

        // What a rebuild builds: Galvanize tells which challenges have something to build
        const pool = entries === null ? challenges : entries;
        const toBuild = pool.filter((c) => (c.build || {}).buildable);

        if (action === 'build') {
            if (!toBuild.length) {
                alertBox(`Nothing to rebuild: none of these ${pool.length} challenge(s) builds an image `
                    + '(pulled images, refused builds, or not deployed by Galvanize)', 'warning');
                return;
            }
            const others = pool.length - toBuild.length;
            startOperation('build', { repo: currentRepo, challenges: refs(toBuild), no_cache: force },
                `Rebuild the images of ${toBuild.length} challenge(s) on Galvanize's hosts?${force ? ' (no cache)' : ''}`
                + (others ? `\n\n${others} other challenge(s) have nothing to build: skipped.` : ''));
            return;
        }

        if (action === 'images-status' || action === 'images-remove') {
            const targets = entries === null ? null : entries.filter((c) => c.deployable);
            if (targets !== null && targets.length === 0) {
                alertBox('None of these challenges is deployed by Galvanize', 'warning');
                return;
            }
            const scopeBody = targets === null ? { repo: currentRepo } : { repo: currentRepo, challenges: refs(targets) };
            if (action === 'images-status') {
                startOperation('images', Object.assign({ action: 'status' }, scopeBody));
            } else {
                startOperation('images', Object.assign({ action: 'remove' }, scopeBody),
                    `Remove the built images of ${label} from Galvanize's hosts? Pulled images are kept.`);
            }
            return;
        }

        const body = { action, repo: currentRepo, keys: entries === null ? null : entries.map((c) => c.key), force, dry_run: dryRun };
        let confirmText = null;
        if (!dryRun && action === 'delete') {
            confirmText = `Delete ${label} from CTFd, with their solves, flags, hints and files?`
                + (force ? '\n\nForce: challenges required by other challenges are deleted too.' : '');
        } else if (entries === null && !dryRun) {
            confirmText = `${LABELS[action]}: ${label}${action === 'all' ? ` (${toBuild.length} to rebuild)` : ''}?`;
        }
        startJob(body, confirmText);
    }

    async function removeRepo(name) {
        if (!window.confirm(`Delete the folder '${name}' from Galvanize?`)) return;
        const alsoCtfd = window.confirm(`Also delete the challenges of '${name}' from CTFd?\n\nOK: delete them from CTFd first · Cancel: keep them in CTFd`);
        if (!alsoCtfd) {
            startOperation('remove', { repo: name });
            return;
        }
        const job = await startJob({ action: 'delete', repo: name, keys: null, force: $('zc-opt-force').checked, dry_run: false });
        if (!job) return;
        afterTask = (task) => {
            if (task.status === 'success') startOperation('remove', { repo: name });
            else alertBox(`Some challenges of '${name}' could not be deleted from CTFd: the folder is kept`, 'warning');
        };
    }

    // ── Task log ────────────────────────────────────────────────────────────

    function normalise(kind, t) {
        if (kind === 'job') {
            return {
                kind, id: t.id, action: t.action, repo: t.repo, status: t.status, created_at: t.created_at,
                user: t.created_by, dry: t.params && t.params.dry_run, summary: t.summary,
                lines: (t.log || '').split('\n').filter((l) => l.length).map((line) => {
                    const m = line.match(/^\[(DEBUG|INFO|SUCCESS|WARNING|ERROR)\] ?(.*)$/);
                    return m ? { level: m[1], message: m[2] } : { level: 'INFO', message: line };
                }),
                next: t.log_length,
            };
        }
        return {
            kind, id: t.id, action: t.kind, repo: t.target, status: t.status, created_at: t.created_at, user: 'Galvanize',
            summary: t.result, lines: (t.lines || []).map((l) => ({ level: l.level.toUpperCase(), message: l.message })),
            next: t.line_count,
        };
    }

    function showTask(kind, id) {
        shown = { kind, id };
        offset = 0;
        wasRunning = false;
        $('zc-log').innerHTML = '';
        $('zc-task-progress').style.display = 'none';
        if (pollTimer) clearTimeout(pollTimer);
        pollTask();
    }

    function appendLines(lines) {
        if (!lines.length) return;
        const box = $('zc-log');
        const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        box.insertAdjacentHTML('beforeend', lines.map((l) => `<div class="lvl-${l.level}">${
            l.level === 'INFO' || l.level === 'DEBUG' ? '' : `[${l.level}] `}${esc(l.message)}</div>`).join(''));
        if (atBottom) box.scrollTop = box.scrollHeight;
    }

    async function pollTask() {
        const current = shown;
        let task;
        try {
            const raw = current.kind === 'job'
                ? await api(`/jobs/${current.id}?offset=${offset}`)
                : await api(`/galvanize/operations/${encodeURIComponent(current.id)}?offset=${offset}`);
            task = normalise(current.kind, raw);
        } catch (e) {
            pollTimer = setTimeout(pollTask, 3000);
            return;
        }
        if (current !== shown) return;
        appendLines(task.lines);
        offset = task.next;
        $('zc-task-title').textContent = `${current.kind === 'job' ? 'Zync' : 'Galvanize'} · ${LABELS[task.action] || task.action}`
            + (task.repo ? ` · ${task.repo}` : '');
        setHTML($('zc-task-status'), statusBadge(task.status) + (task.dry ? ' ' + badge('dry run', 'info') : ''));
        $('zc-task-meta').textContent = (task.user ? task.user + ' · ' : '') + fmtDate(task.created_at);
        const running = isRunning(task.status);
        $('zc-task-cancel').style.display = running && task.status !== 'cancelling' ? '' : 'none';
        $('zc-task-progress').style.display = running && current.kind === 'op' && DOWNLOADS[task.action] ? '' : 'none';
        if (running) {
            wasRunning = true;
            pollTimer = setTimeout(pollTask, 1000);
        } else if (wasRunning || afterTask) {
            // Just finished (or finished before the first poll with a follow-up pending)
            wasRunning = false;
            if (current.kind === 'op' && task.action === 'images' && task.summary && task.summary.images) {
                Object.assign(imageStates, task.summary.images);
            }
            const callback = afterTask;
            afterTask = null;
            loadTasks();
            await loadOverview();
            await loadChallenges();
            if (callback) callback(task);
        }
        markShown();
    }

    // The list is polled while a task runs, so that its spinners, and the
    // page once a task that is not the shown one finishes, stay current
    async function loadTasks() {
        clearTimeout(tasksTimer);
        let jobs = [];
        let ops = [];
        try { jobs = await api('/jobs'); } catch (e) { /* reported elsewhere */ }
        try { ops = await api('/galvanize/operations'); } catch (e) { /* Galvanize unreachable */ }
        const all = jobs.map((j) => normalise('job', j)).concat(ops.map((o) => normalise('op', o)))
            .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        const tasks = all.slice(0, 30);
        setHTML($('zc-tasks'), tasks.map((t) => `
            <a class="list-group-item list-group-item-action d-flex justify-content-between align-items-center" data-task-kind="${t.kind}" data-task-id="${esc(t.id)}">
                <span>${t.kind === 'job' ? '<i class="fas fa-flag" title="Zync (CTFd)"></i>' : '<i class="fas fa-server" title="Galvanize"></i>'}
                    ${esc(LABELS[t.action] || t.action)} <span class="text-muted">${t.repo ? '· ' + esc(t.repo) : ''}</span></span>
                <span class="text-nowrap">${statusBadge(t.status)}</span>
            </a>`).join('') || '<div class="text-muted small">No task yet</div>');
        markShown();
        if (!shown && tasks.length) showTask(tasks[0].kind, tasks[0].id);

        const running = all.filter((t) => isRunning(t.status));
        const runningKeys = new Set(running.map((t) => `${t.kind}:${t.id}`));
        const shownKey = shown ? `${shown.kind}:${shown.id}` : null;
        const finished = [...runningTasks].some((k) => !runningKeys.has(k) && k !== shownKey);
        runningTasks = runningKeys;
        downloads = running.filter((t) => t.kind === 'op' && DOWNLOADS[t.action]);
        renderRepos();
        clearTimeout(tasksTimer);
        if (running.length) tasksTimer = setTimeout(loadTasks, 2000);
        if (finished) {
            await loadOverview();
            await loadChallenges();
        }
    }

    function markShown() {
        document.querySelectorAll('#zc-tasks [data-task-id]').forEach((el) => {
            el.classList.toggle('active', !!shown && el.dataset.taskKind === shown.kind && el.dataset.taskId === String(shown.id));
        });
    }

    // ── Events ──────────────────────────────────────────────────────────────

    document.addEventListener('click', (ev) => {
        const open = ev.target.closest('[data-open]');
        if (open) {
            ev.preventDefault();
            selectRepo(open.dataset.open);
            return;
        }
        const repoAction = ev.target.closest('[data-repo-action]');
        if (repoAction) {
            const name = repoAction.dataset.repo;
            if (repoAction.dataset.repoAction === 'pull') {
                startOperation('pull', { repo: name }, `Update '${name}' from its remote?\n\nLocal changes to tracked files are discarded.`);
            } else {
                removeRepo(name);
            }
            return;
        }
        const action = ev.target.closest('[data-action]');
        if (action) {
            runAction(action.dataset.action, scopeEntries());
            return;
        }
        const rowAction = ev.target.closest('[data-row-action]');
        if (rowAction) {
            runAction(rowAction.dataset.rowAction, challenges.filter((c) => c.key === rowAction.dataset.key));
            return;
        }
        const scopeBtn = ev.target.closest('[data-scope]');
        if (scopeBtn) {
            scope = scopeBtn.dataset.scope;
            document.querySelectorAll('[data-scope]').forEach((b) => b.classList.toggle('active', b === scopeBtn));
            return;
        }
        const taskItem = ev.target.closest('[data-task-id]');
        if (taskItem) {
            showTask(taskItem.dataset.taskKind, taskItem.dataset.taskKind === 'job' ? Number(taskItem.dataset.taskId) : taskItem.dataset.taskId);
        }
    });

    document.addEventListener('change', (ev) => {
        if (ev.target.matches('#zc-challenges-tbody input[data-key]')) {
            if (ev.target.checked) selected.add(ev.target.dataset.key);
            else selected.delete(ev.target.dataset.key);
            $('zc-selected-count').textContent = selected.size;
            if (selected.size && scope === 'all') $('zc-scope-selected').click();
        }
    });

    $('zc-select-all').addEventListener('change', (ev) => {
        filteredChallenges().forEach((c) => (ev.target.checked ? selected.add(c.key) : selected.delete(c.key)));
        if (selected.size && scope === 'all') $('zc-scope-selected').click();
        renderChallenges();
    });
    $('zc-filter-category').addEventListener('change', renderChallenges);
    $('zc-filter-text').addEventListener('input', renderChallenges);
    $('zc-show-debug').addEventListener('change', (ev) => $('zc-log').classList.toggle('hide-debug', !ev.target.checked));
    $('zc-task-cancel').addEventListener('click', async () => {
        if (!shown) return;
        // A Zync job stops between two challenges (and cancels the build it waits for)
        const path = shown.kind === 'job' ? `/jobs/${shown.id}/cancel` : `/galvanize/operations/${encodeURIComponent(shown.id)}/cancel`;
        try { await api(path, { body: {} }); } catch (e) { alertBox(e.message); }
    });
    $('zc-refresh').addEventListener('click', async () => {
        const btn = $('zc-refresh');
        const status = $('zc-refresh-status');
        btn.disabled = true;
        btn.querySelector('i').classList.add('fa-spin');
        status.className = 'small text-muted mr-2';
        status.textContent = 'Refreshing...';
        let error = null;
        try {
            error = await loadOverview();
            error = (await loadChallenges()) || error;
            await loadTasks();
        } finally {
            btn.disabled = false;
            btn.querySelector('i').classList.remove('fa-spin');
        }
        // Shown even when nothing changed, so that the click visibly did something
        status.className = `small mr-2 ${error ? 'text-danger' : 'text-success'}`;
        status.textContent = error ? `Refresh failed: ${error}` : `Refreshed at ${new Date().toLocaleTimeString()}`;
    });

    $('zc-clone-form').addEventListener('submit', (ev) => {
        ev.preventDefault();
        startOperation('clone', {
            url: $('zc-clone-url').value.trim(),
            ref: $('zc-clone-ref').value.trim() || null,
            name: $('zc-clone-name').value.trim() || null,
            force: $('zc-clone-force').checked,
        }, $('zc-clone-force').checked ? 'Replace the existing folder if there is one?' : null);
    });

    loadOverview();
    loadTasks();
})();
