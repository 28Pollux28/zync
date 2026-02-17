(function() {
    let apiUrl = '';
    let challenges = [];
    let pollTimeoutIds = {}; // Track polling timeouts per challenge

    // Initialize admin token and load challenges
    async function init() {
        try {
            const response = await fetch('/admin/zync_token');
            if (!response.ok) {
                throw new Error('Failed to get admin token');
            }
            const data = await response.json();
            sessionStorage.setItem('jwt_admin', data.token);
            apiUrl = data.api_url;

            await loadChallenges();
        } catch (error) {
            showAlert('Error initializing dashboard: ' + error.message, 'danger');
        }
    }

    // Stop all polling for a specific challenge
    function stopPolling(challengeKey) {
        if (pollTimeoutIds[challengeKey]) {
            clearTimeout(pollTimeoutIds[challengeKey]);
            delete pollTimeoutIds[challengeKey];
        }
    }

    // Schedule a status poll for a specific challenge
    function schedulePoll(chall, delayMs) {
        const key = `${chall.category}_${chall.challenge_id}`;
        stopPolling(key);

        pollTimeoutIds[key] = setTimeout(async () => {
            await updateChallengeStatus(chall);
        }, delayMs);
    }

    // Update status for a single challenge
    async function updateChallengeStatus(chall) {
        const key = `${chall.category}_${chall.challenge_id}`;
        try {
            // Get or create status token for this challenge
            let statusToken = sessionStorage.getItem(`jwt_admin_${chall.category}_${chall.challenge_id}`);

            if (!statusToken) {
                const tokenResp = await fetch('/admin/zync_status_token', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'CSRF-Token': window.init.csrfNonce
                    },
                    body: JSON.stringify({
                        category: chall.category,
                        challenge_name: chall.challenge_name
                    })
                });

                if (!tokenResp.ok) {
                    chall.status = 'error';
                    chall.connection_info = '';
                    updateChallengeRow(chall);
                    return;
                }

                const tokenData = await tokenResp.json();
                statusToken = tokenData.token;
                chall.challenge_id = tokenData.challenge_id;
                sessionStorage.setItem(`jwt_admin_${chall.category}_${chall.challenge_id}`, statusToken);
            }

            // Get status using the challenge-specific token
            const statusResp = await fetch(`${apiUrl}/status`, {
                headers: {
                    'Authorization': `Bearer ${statusToken}`
                }
            });

            if (statusResp.ok) {
                const statusData = await statusResp.json();
                chall.status = statusData.status;
                chall.connection_info = statusData.connection_info || '';

                // Schedule next poll if status is transitional
                if (statusData.status === 'starting' || statusData.status === 'stopping') {
                    schedulePoll(chall, 2000);
                }
            } else if (statusResp.status === 404) {
                chall.status = 'not_deployed';
                chall.connection_info = '';
                stopPolling(key);
            } else {
                chall.status = 'unknown';
                chall.connection_info = '';
            }

            updateChallengeRow(chall);
        } catch (e) {
            chall.status = 'error';
            chall.connection_info = '';
            updateChallengeRow(chall);
        }
    }

    // Update a single challenge row in the table
    function updateChallengeRow(chall) {
        const index = challenges.findIndex(c =>
            c.category === chall.category && c.challenge_name === chall.challenge_name
        );

        if (index === -1) return;

        challenges[index] = chall;

        const row = document.querySelector(`tr[data-challenge-index="${index}"]`);
        if (!row) return;

        const isTransitioning = (chall.status === 'starting' || chall.status === 'stopping');

        // Update status badge
        const statusBadge = row.querySelector('.status-badge');
        if (statusBadge) {
            statusBadge.className = `badge badge-${getStatusBadge(chall.status)} status-badge`;
            statusBadge.textContent = chall.status;
        }

        // Update connection info
        const connectionInfo = row.querySelector('.connection-info');
        if (connectionInfo) {
            connectionInfo.textContent = chall.connection_info || 'N/A';
        }

        // Handle checkbox - disable and uncheck if transitioning
        const checkbox = row.querySelector('.chall-checkbox');
        if (checkbox) {
            if (isTransitioning) {
                checkbox.disabled = true;
                checkbox.checked = false;
            } else {
                checkbox.disabled = false;
            }
        }

        // Update actions cell - show spinner or buttons
        const actionsCell = row.querySelector('.actions-cell');
        if (actionsCell) {
            if (isTransitioning) {
                actionsCell.innerHTML = '<div class="text-center"><i class="fas fa-circle-notch fa-spin fa-1x"></i></div>';
            } else {
                actionsCell.innerHTML = `
                    <button class="btn btn-sm btn-success deploy-one-btn" data-index="${index}" ${chall.status === 'running' ? 'disabled' : ''}>
                        <i class="fas fa-play"></i> Deploy
                    </button>
                    <button class="btn btn-sm btn-danger terminate-one-btn" data-index="${index}" ${chall.status !== 'running' ? 'disabled' : ''}>
                        <i class="fas fa-stop"></i> Terminate
                    </button>
                `;
                // Re-attach event listeners for the new buttons
                const deployBtn = actionsCell.querySelector('.deploy-one-btn');
                const terminateBtn = actionsCell.querySelector('.terminate-one-btn');

                if (deployBtn) {
                    deployBtn.addEventListener('click', function() {
                        const idx = parseInt(this.dataset.index);
                        deployChallenge(challenges[idx]);
                    });
                }

                if (terminateBtn) {
                    terminateBtn.addEventListener('click', function() {
                        const idx = parseInt(this.dataset.index);
                        terminateChallenge(challenges[idx]);
                    });
                }
            }
        }

        // Update button states (for deploy/terminate selected buttons)
        updateButtonStates();
    }

    // Load challenges from API
    async function loadChallenges() {
        showSpinner(true);
        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/list-unique-challs`, {
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });

            if (!response.ok) {
                throw new Error('Failed to load challenges');
            }

            challenges = await response.json();
            challenges.sort((a, b) => a.category.localeCompare(b.category) || a.challenge_name.localeCompare(b.challenge_name));

            // Get status for each challenge
            await Promise.all(challenges.map(async (chall) => {
                try {
                    // Request a status token for this specific challenge
                    const tokenResp = await fetch('/admin/zync_status_token', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'CSRF-Token': window.init.csrfNonce
                        },
                        body: JSON.stringify({
                            category: chall.category,
                            challenge_name: chall.challenge_name
                        })
                    });

                    if (!tokenResp.ok) {
                        chall.status = 'error';
                        chall.connection_info = '';
                        return;
                    }

                    const tokenData = await tokenResp.json();
                    const statusToken = tokenData.token;
                    chall.challenge_id = tokenData.challenge_id;

                    // Store the token in sessionStorage
                    sessionStorage.setItem(`jwt_admin_${chall.category}_${chall.challenge_id}`, statusToken);

                    // Get status using the challenge-specific token
                    const statusResp = await fetch(`${apiUrl}/status`, {
                        headers: {
                            'Authorization': `Bearer ${statusToken}`
                        }
                    });

                    if (statusResp.ok) {
                        const statusData = await statusResp.json();
                        chall.status = statusData.status;
                        chall.connection_info = statusData.connection_info || '';

                        // Schedule polling if status is transitional
                        if (statusData.status === 'starting' || statusData.status === 'stopping') {
                            schedulePoll(chall, 0);
                        }
                    } else if (statusResp.status === 404) {
                        chall.status = 'not_deployed';
                        chall.connection_info = '';
                    } else {
                        chall.status = 'unknown';
                        chall.connection_info = '';
                    }
                } catch (e) {
                    chall.status = 'error';
                    chall.connection_info = '';
                }
                renderTable();
            }));
        } catch (error) {
            showAlert('Error loading challenges: ' + error.message, 'danger');
        } finally {
            showSpinner(false);
        }
    }

    // Render challenges table
    function renderTable() {
        const tbody = document.getElementById('challenges-tbody');
        tbody.innerHTML = '';

        challenges.forEach((chall, index) => {
            const row = document.createElement('tr');
            row.setAttribute('data-challenge-index', index);
            let actionsCell = chall.status === 'running' || chall.status === 'not_deployed' || chall.status === 'error' || chall.status === 'unknown' ? `
                <td class="actions-cell">
                    <button class="btn btn-sm btn-success deploy-one-btn" data-index="${index}" ${chall.status === 'running' || chall.status === 'starting' ? 'disabled' : ''}>
                        <i class="fas fa-play"></i> Deploy
                    </button>
                    <button class="btn btn-sm btn-danger terminate-one-btn" data-index="${index}" ${chall.status !== 'running' && chall.status !== 'starting' ? 'disabled' : ''}>
                        <i class="fas fa-stop"></i> Terminate
                    </button>
                </td>` :
                `<td class="actions-cell"><div class="text-center"><i class="fas fa-circle-notch fa-spin fa-1x"></i></div></td>`;

            row.innerHTML = `
                <td><input type="checkbox" class="chall-checkbox" data-index="${index}"></td>
                <td>${escapeHtml(chall.category)}</td>
                <td>${escapeHtml(chall.challenge_name)}</td>
                <td><span class="badge badge-${getStatusBadge(chall.status)} status-badge">${chall.status}</span></td>
                <td><code class="connection-info">${escapeHtml(chall.connection_info || 'N/A')}</code></td>
                ${actionsCell}
            `;
            tbody.appendChild(row);
        });

        document.getElementById('challenges-table').style.display = 'table';
        attachEventListeners();
    }

    // Attach event listeners
    function attachEventListeners() {
        // Select all checkbox
        document.getElementById('select-all').addEventListener('change', function(e) {
            const checkboxes = document.querySelectorAll('.chall-checkbox');
            checkboxes.forEach(cb => cb.checked = e.target.checked);
            updateButtonStates();
        });

        // Individual checkboxes
        document.querySelectorAll('.chall-checkbox').forEach(cb => {
            cb.addEventListener('change', updateButtonStates);
        });

        // Deploy one
        document.querySelectorAll('.deploy-one-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                const index = parseInt(this.dataset.index);
                deployChallenge(challenges[index]);
            });
        });

        // Terminate one
        document.querySelectorAll('.terminate-one-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                const index = parseInt(this.dataset.index);
                terminateChallenge(challenges[index]);
            });
        });
    }

    // Update button states based on selections
    function updateButtonStates() {
        const selected = document.querySelectorAll('.chall-checkbox:checked');
        document.getElementById('deploy-selected-btn').disabled = selected.length === 0;
        document.getElementById('terminate-selected-btn').disabled = selected.length === 0;
    }

    // Deploy a single challenge
    async function deployChallenge(chall) {
        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/deploy`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    category: chall.category,
                    challenge_name: chall.challenge_name
                })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.message || 'Deployment failed');
            }

            showAlert(`Deploying ${chall.category}/${chall.challenge_name}`, 'success');

            // Update status immediately and start polling
            chall.status = 'starting';
            updateChallengeRow(chall);
            schedulePoll(chall, 2000);
        } catch (error) {
            showAlert(`Error deploying ${chall.category}/${chall.challenge_name}: ${error.message}`, 'danger');
        }
    }

    // Terminate a single challenge
    async function terminateChallenge(chall) {
        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/terminate`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    category: chall.category,
                    challenge_name: chall.challenge_name
                })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.message || 'Termination failed');
            }

            showAlert(`Terminating ${chall.category}/${chall.challenge_name}`, 'success');

            // Update status immediately and start polling
            chall.status = 'stopping';
            updateChallengeRow(chall);
            schedulePoll(chall, 2000);
        } catch (error) {
            showAlert(`Error terminating ${chall.category}/${chall.challenge_name}: ${error.message}`, 'danger');
        }
    }

    // Deploy selected challenges
    async function deploySelected() {
        const selected = Array.from(document.querySelectorAll('.chall-checkbox:checked'))
            .map(cb => challenges[parseInt(cb.dataset.index)]);

        for (const chall of selected) {
            await deployChallenge(chall);
        }
    }

    // Terminate selected challenges
    async function terminateSelected() {
        const selected = Array.from(document.querySelectorAll('.chall-checkbox:checked'))
            .map(cb => challenges[parseInt(cb.dataset.index)]);

        for (const chall of selected) {
            await terminateChallenge(chall);
        }
    }

    // Deploy all challenges
    async function deployAll() {
        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/deploy-all`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.message || 'Bulk deployment failed');
            }

            const result = await response.json();
            showAlert(`Deploying all ${result.challenges_count} challenges`, 'success');

            // Start polling for all challenges
            challenges.forEach(chall => {
                chall.status = 'starting';
                updateChallengeRow(chall);
                schedulePoll(chall, 2000);
            });
        } catch (error) {
            showAlert('Error deploying all: ' + error.message, 'danger');
        }
    }

    // Terminate all challenges
    async function terminateAll() {
        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/terminate-all`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.message || 'Bulk termination failed');
            }

            const result = await response.json();
            showAlert(`Terminating all ${result.challenges_count} challenges`, 'success');

            // Start polling for all challenges
            challenges.forEach(chall => {
                chall.status = 'stopping';
                updateChallengeRow(chall);
                schedulePoll(chall, 2000);
            });
        } catch (error) {
            showAlert('Error terminating all: ' + error.message, 'danger');
        }
    }

    // Reload challenges from Galvanize
    async function reloadChallengesFromGalvanize() {
        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/reload-challs`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.message || 'Failed to reload challenges');
            }

            showAlert('Challenges reloaded successfully in Galvanize', 'success');

            // Reload the challenges list
            await loadChallenges();
        } catch (error) {
            showAlert('Error reloading challenges: ' + error.message, 'danger');
        }
    }

    // Utility functions
    function showAlert(message, type) {
        const container = document.getElementById('alert-container');
        const alert = document.createElement('div');
        alert.className = `alert alert-${type} alert-dismissible fade show`;
        alert.innerHTML = `
            ${message}
            <button type="button" class="close" data-dismiss="alert">&times;</button>
        `;
        container.appendChild(alert);

        setTimeout(() => {
            alert.remove();
        }, 5000);
    }

    function showSpinner(show) {
        document.getElementById('loading-spinner').style.display = show ? 'block' : 'none';
        document.getElementById('challenges-table').style.display = show ? 'none' : 'table';
    }

    function getStatusBadge(status) {
        switch(status) {
            case 'running': return 'success';
            case 'starting': return 'info';
            case 'stopping': return 'warning';
            case 'not_deployed': return 'secondary';
            case 'error': return 'danger';
            default: return 'secondary';
        }
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Button event listeners
    document.getElementById('deploy-selected-btn').addEventListener('click', deploySelected);
    document.getElementById('terminate-selected-btn').addEventListener('click', terminateSelected);
    document.getElementById('deploy-all-btn').addEventListener('click', deployAll);
    document.getElementById('terminate-all-btn').addEventListener('click', terminateAll);
    document.getElementById('reload-challs-btn').addEventListener('click', reloadChallengesFromGalvanize);
    document.getElementById('refresh-btn').addEventListener('click', loadChallenges);

    // Initialize on page load
    init();
})();
