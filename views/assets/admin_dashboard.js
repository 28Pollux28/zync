(function() {
    let apiUrl = '';
    let challenges = [];
    let pollTimeoutIds = {}; // Track polling timeouts per challenge
    let teamDeployments = [];
    let errorDeployments = [];
    let teamFilterText = '';
    let errorDeploymentsInterval = null;
    let teamDeploymentsInterval = null;
    let errorCountdownInterval = null;
    let teamCountdownInterval = null;
    let errorSecondsRemaining = 10;
    let teamSecondsRemaining = 60;
    let networkErrorShown = false; // Track if network error alert is already shown

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
            await loadErrorDeployments();
            await loadTeamDeployments();

            // Start auto-refresh intervals
            startAutoRefresh();
        } catch (error) {
            showAlert('Error initializing dashboard: ' + error.message, 'danger');
        }
    }

    // Start auto-refresh intervals
    function startAutoRefresh() {
        // Clear any existing intervals
        if (errorDeploymentsInterval) clearInterval(errorDeploymentsInterval);
        if (teamDeploymentsInterval) clearInterval(teamDeploymentsInterval);
        if (errorCountdownInterval) clearInterval(errorCountdownInterval);
        if (teamCountdownInterval) clearInterval(teamCountdownInterval);

        // Reset countdown timers
        errorSecondsRemaining = 10;
        teamSecondsRemaining = 60;
        updateCountdownDisplay();

        // Countdown timer for error deployments (updates every second)
        errorCountdownInterval = setInterval(() => {
            errorSecondsRemaining--;
            if (errorSecondsRemaining <= 0) {
                errorSecondsRemaining = 10;
            }
            updateCountdownDisplay();
        }, 1000);

        // Countdown timer for team deployments (updates every second)
        teamCountdownInterval = setInterval(() => {
            teamSecondsRemaining--;
            if (teamSecondsRemaining <= 0) {
                teamSecondsRemaining = 60;
            }
            updateCountdownDisplay();
        }, 1000);

        // Refresh error deployments every 10 seconds
        errorDeploymentsInterval = setInterval(async () => {
            errorSecondsRemaining = 10;
            await loadErrorDeployments();
        }, 10000);

        // Refresh team deployments every 60 seconds
        teamDeploymentsInterval = setInterval(async () => {
            teamSecondsRemaining = 60;
            await loadTeamDeployments();
        }, 60000);
    }

    // Update countdown display
    function updateCountdownDisplay() {
        const errorCountdown = document.getElementById('error-countdown');
        const teamCountdown = document.getElementById('team-countdown');

        if (errorCountdown) {
            errorCountdown.textContent = errorSecondsRemaining;
        }
        if (teamCountdown) {
            teamCountdown.textContent = teamSecondsRemaining;
        }
    }

    // Stop auto-refresh intervals (useful for cleanup)
    function stopAutoRefresh() {
        if (errorDeploymentsInterval) {
            clearInterval(errorDeploymentsInterval);
            errorDeploymentsInterval = null;
        }
        if (teamDeploymentsInterval) {
            clearInterval(teamDeploymentsInterval);
            teamDeploymentsInterval = null;
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
            if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
                if (!networkErrorShown) {
                    showAlert('Unable to connect to the deployer. Please check if the Galvanize Instancer is running and accessible.', 'danger', true);
                    networkErrorShown = true;
                }
            } else {
                showAlert('Error loading challenges: ' + error.message, 'danger');
            }
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
            if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
                if (!networkErrorShown) {
                    showAlert('Unable to connect to the deployer. Please check if the Galvanize Instancer is running and accessible.', 'danger', true);
                    networkErrorShown = true;
                }
            } else {
                showAlert(`Error deploying ${chall.category}/${chall.challenge_name}: ${error.message}`, 'danger');
            }
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
            if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
                if (!networkErrorShown) {
                    showAlert('Unable to connect to the deployer. Please check if the Galvanize Instancer is running and accessible.', 'danger', true);
                    networkErrorShown = true;
                }
            } else {
                showAlert(`Error terminating ${chall.category}/${chall.challenge_name}: ${error.message}`, 'danger');
            }
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
            if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
                if (!networkErrorShown) {
                    showAlert('Unable to connect to the deployer. Please check if the Galvanize Instancer is running and accessible.', 'danger', true);
                    networkErrorShown = true;
                }
            } else {
                showAlert('Error terminating all: ' + error.message, 'danger');
            }
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
            if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
                if (!networkErrorShown) {
                    showAlert('Unable to connect to the deployer. Please check if the Galvanize Instancer is running and accessible.', 'danger', true);
                    networkErrorShown = true;
                }
            } else {
                showAlert('Error reloading challenges: ' + error.message, 'danger');
            }
        }
    }

    // Load error deployments
    async function loadErrorDeployments() {
        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/error-deployments`, {
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });
            if (!response.ok) {
                throw new Error('Failed to load error deployments');
            }

            errorDeployments = await response.json();
            renderErrorDeployments();
        } catch (error) {
            console.error('Error loading error deployments:', error);
            document.getElementById('error-deployments-spinner').style.display = 'none';
            document.getElementById('error-deployments-section').style.display = 'none';

            // Show alert if deployer is unreachable
            if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
                if (!networkErrorShown) {
                    showAlert('Unable to connect to the deployer. Please check if the Galvanize Instancer is running and accessible.', 'danger', true);
                    networkErrorShown = true;
                }
            } else {
                showAlert(`Error loading error deployments: ${error.message}`, 'warning');
            }
        }
    }

    // Render error deployments
    function renderErrorDeployments() {
        const section = document.getElementById('error-deployments-section');
        const spinner = document.getElementById('error-deployments-spinner');
        const content = document.getElementById('error-deployments-content');
        const empty = document.getElementById('error-deployments-empty');
        const tbody = document.getElementById('error-deployments-tbody');
        const countBadge = document.getElementById('error-count');

        spinner.style.display = 'none';

        if (errorDeployments.length === 0) {
            section.style.display = 'none';
            return;
        }

        section.style.display = 'block';
        countBadge.textContent = errorDeployments.length;

        // Store toggle states before updating
        const toggleStates = {};
        tbody.querySelectorAll('.error-toggle-btn').forEach(btn => {
            const errorId = btn.dataset.errorId;
            const icon = btn.querySelector('i');
            toggleStates[errorId] = icon && icon.classList.contains('fa-chevron-up');
        });

        // Get existing rows
        const existingRows = Array.from(tbody.querySelectorAll('tr'));
        const existingIds = new Set(existingRows.map(row => row.dataset.deploymentId));
        const newIds = new Set(errorDeployments.map(d => String(d.id)));

        // Remove rows that no longer exist
        existingRows.forEach(row => {
            if (!newIds.has(row.dataset.deploymentId)) {
                row.remove();
            }
        });

        // Update or add rows
        errorDeployments.forEach((deployment, index) => {
            const deploymentId = String(deployment.id);
            let row = tbody.querySelector(`tr[data-deployment-id="${deploymentId}"]`);

            const teamDisplay = deployment.team_id === '' ? '<em>Unique</em>' : escapeHtml(deployment.team_id);
            const createdAt = new Date(deployment.created_at).toLocaleString();

            // Truncate long error messages
            const errorMessage = deployment.error_message || '';
            const maxLength = 100;
            const isTruncated = errorMessage.length > maxLength;
            const truncatedError = isTruncated ? errorMessage.substring(0, maxLength) + '...' : errorMessage;
            const errorId = `error-msg-${deploymentId}`;

            const errorCell = isTruncated
                ? `<small class="text-danger">
                    <span id="${errorId}" class="error-message-text">${escapeHtml(truncatedError)}</span>
                    <button class="btn btn-link btn-sm p-0 ml-1 error-toggle-btn" data-error-id="${errorId}" data-full-message="${escapeHtml(errorMessage)}" data-truncated="${escapeHtml(truncatedError)}" title="Show full error">
                        <i class="fas fa-chevron-down"></i>
                    </button>
                </small>`
                : `<small class="text-danger">${escapeHtml(errorMessage)}</small>`;

            const rowHTML = `
                <td>${teamDisplay}</td>
                <td>${escapeHtml(deployment.category)}</td>
                <td>${escapeHtml(deployment.challenge_name)}</td>
                <td><span class="badge badge-${deployment.previous_status === 'starting' ? 'info' : 'warning'}">${deployment.previous_status}</span></td>
                <td>${errorCell}</td>
                <td><small>${createdAt}</small></td>
                <td>
                    <button class="btn btn-sm btn-warning retry-deploy-btn" data-id="${deployment.id}" data-action="deploy" title="Retry deployment">
                        <i class="fas fa-redo"></i> Retry
                    </button>
                    <button class="btn btn-sm btn-danger retry-terminate-btn" data-id="${deployment.id}" data-action="terminate" title="Terminate deployment">
                        <i class="fas fa-times"></i> Terminate
                    </button>
                    <button class="btn btn-sm btn-secondary retry-delete-btn" data-id="${deployment.id}" data-action="delete" title="Delete deployment record">
                        <i class="fas fa-trash"></i> Delete
                    </button>
                </td>
            `;

            if (row) {
                // Update existing row
                row.innerHTML = rowHTML;
            } else {
                // Create new row
                row = document.createElement('tr');
                row.dataset.deploymentId = deploymentId;
                row.innerHTML = rowHTML;

                // Insert at correct position to maintain order
                if (index < tbody.children.length) {
                    tbody.insertBefore(row, tbody.children[index]);
                } else {
                    tbody.appendChild(row);
                }
            }

            // Attach event listeners for this row
            row.querySelectorAll('.retry-deploy-btn, .retry-terminate-btn, .retry-delete-btn').forEach(btn => {
                btn.addEventListener('click', async function() {
                    const id = parseInt(this.dataset.id);
                    const action = this.dataset.action;

                    // Confirm delete action
                    if (action === 'delete') {
                        if (!confirm(`Are you sure you want to delete the deployment record for deployment ${id}? This action cannot be undone.`)) {
                            return;
                        }
                    }

                    await retryDeployment(id, action);
                });
            });

            // Attach event listener for error message toggle
            const toggleBtn = row.querySelector('.error-toggle-btn');
            if (toggleBtn) {
                const errorId = toggleBtn.dataset.errorId;

                // Restore toggle state if it was expanded
                if (toggleStates[errorId]) {
                    const fullMessage = toggleBtn.dataset.fullMessage;
                    const errorSpan = document.getElementById(errorId);
                    const icon = toggleBtn.querySelector('i');

                    if (errorSpan && icon) {
                        errorSpan.textContent = fullMessage;
                        icon.classList.remove('fa-chevron-down');
                        icon.classList.add('fa-chevron-up');
                        toggleBtn.title = 'Show less';
                    }
                }

                toggleBtn.addEventListener('click', function() {
                    const errorId = this.dataset.errorId;
                    const fullMessage = this.dataset.fullMessage;
                    const truncatedMessage = this.dataset.truncated;
                    const errorSpan = document.getElementById(errorId);
                    const icon = this.querySelector('i');

                    if (icon.classList.contains('fa-chevron-down')) {
                        // Expand
                        errorSpan.textContent = fullMessage;
                        icon.classList.remove('fa-chevron-down');
                        icon.classList.add('fa-chevron-up');
                        this.title = 'Show less';
                    } else {
                        // Collapse
                        errorSpan.textContent = truncatedMessage;
                        icon.classList.remove('fa-chevron-up');
                        icon.classList.add('fa-chevron-down');
                        this.title = 'Show full error';
                    }
                });
            }
        });

        content.style.display = 'block';
        empty.style.display = 'none';
    }

    // Retry a failed deployment
    async function retryDeployment(deploymentId, action) {
        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/retry-deployment`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    deployment_id: deploymentId,
                    action: action
                })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.message || 'Request failed');
            }

            // Show appropriate success message based on action
            const actionMessages = {
                'deploy': 'Retry deployment request submitted',
                'terminate': 'Terminate request submitted',
                'delete': 'Deployment record deleted'
            };
            const message = actionMessages[action] || 'Request submitted';
            showAlert(`${message} for deployment ${deploymentId}`, 'success');

            // Reload error deployments and team deployments
            setTimeout(async () => {
                await loadErrorDeployments();
                await loadTeamDeployments();
            }, 1000);
        } catch (error) {
            if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
                if (!networkErrorShown) {
                    showAlert('Unable to connect to the deployer. Please check if the Galvanize Instancer is running and accessible.', 'danger', true);
                    networkErrorShown = true;
                }
            } else {
                showAlert(`Error processing ${action} request: ${error.message}`, 'danger');
            }
        }
    }

    // Load team deployments
    async function loadTeamDeployments() {
        const spinner = document.getElementById('team-deployments-spinner');
        const content = document.getElementById('team-deployments-content');
        const empty = document.getElementById('team-deployments-empty');

        spinner.style.display = 'block';
        content.style.display = 'none';
        empty.style.display = 'none';

        try {
            const token = sessionStorage.getItem('jwt_admin');
            const response = await fetch(`${apiUrl}/admin/team-deployments`, {
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });
            if (!response.ok) {
                throw new Error('Failed to load team deployments');
            }

            teamDeployments = await response.json();
            renderTeamDeployments();
        } catch (error) {
            console.error('Error loading team deployments:', error);
            spinner.style.display = 'none';
            empty.style.display = 'block';

            // Show alert if deployer is unreachable
            if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
                if (!networkErrorShown) {
                    showAlert('Unable to connect to the deployer. Please check if the Galvanize Instancer is running and accessible.', 'danger', true);
                    networkErrorShown = true;
                }
            } else {
                showAlert(`Error loading team deployments: ${error.message}`, 'warning');
            }
        }
    }

    // Render team deployments
    function renderTeamDeployments() {
        const spinner = document.getElementById('team-deployments-spinner');
        const content = document.getElementById('team-deployments-content');
        const empty = document.getElementById('team-deployments-empty');
        const accordion = document.getElementById('team-deployments-accordion');
        const countBadge = document.getElementById('team-count');

        spinner.style.display = 'none';

        // Filter teams based on search
        const filteredTeams = teamDeployments.filter(team => {
            if (!teamFilterText) return true;
            return team.team_id.toLowerCase().includes(teamFilterText.toLowerCase());
        });

        if (filteredTeams.length === 0) {
            empty.style.display = 'block';
            content.style.display = 'none';
            countBadge.textContent = '0';
            return;
        }

        // Sort teams alphabetically (empty team_id for Unique Deployments comes first)
        filteredTeams.sort((a, b) => {
            if (a.team_id === '' && b.team_id === '') return 0;
            if (a.team_id === '') return -1;
            if (b.team_id === '') return 1;
            return a.team_id.localeCompare(b.team_id);
        });

        countBadge.textContent = filteredTeams.length;

        // Store current collapse states before updating
        const collapseStates = {};
        accordion.querySelectorAll('.collapse').forEach(collapse => {
            collapseStates[collapse.id] = collapse.classList.contains('show');
        });

        // Get existing cards
        const existingCards = Array.from(accordion.querySelectorAll('.card'));
        const existingTeamIds = new Set(existingCards.map(card => card.dataset.teamId));
        const newTeamIds = new Set(filteredTeams.map(t => t.team_id || 'unique'));

        // Remove cards that no longer exist
        existingCards.forEach(card => {
            if (!newTeamIds.has(card.dataset.teamId)) {
                card.remove();
            }
        });

        filteredTeams.forEach((team, index) => {
            const teamDisplay = team.team_id === '' ? 'Unique Deployments' : `Team: ${escapeHtml(team.team_id)}`;
            const teamKey = team.team_id === '' ? 'unique' : team.team_id;
            const collapseId = `collapse-team-${teamKey}`;

            let card = accordion.querySelector(`.card[data-team-id="${teamKey}"]`);
            const wasExpanded = collapseStates[collapseId] || false;

            let totalDeployments = team.deployments.length;
            let statusCounts = {running: 0, starting: 0, stopping: 0};
            team.deployments.forEach(d => {
                statusCounts[d.status] = (statusCounts[d.status] || 0) + 1;
            });

            const cardHTML = `
                <div class="card-header" id="heading-${teamKey}">
                    <h6 class="mb-0">
                        <button class="btn btn-link text-left w-100 d-flex justify-content-between align-items-center" 
                                type="button" data-toggle="collapse" data-target="#${collapseId}" 
                                aria-expanded="${wasExpanded}" aria-controls="${collapseId}">
                            <span>
                                <i class="fas fa-chevron-right"></i>
                                ${teamDisplay}
                                <span class="badge badge-secondary ml-2">${totalDeployments} deployment${totalDeployments !== 1 ? 's' : ''}</span>
                            </span>
                            <span>
                                ${statusCounts.running > 0 ? `<span class="badge badge-success mr-1">${statusCounts.running} running</span>` : ''}
                                ${statusCounts.starting > 0 ? `<span class="badge badge-info mr-1">${statusCounts.starting} starting</span>` : ''}
                                ${statusCounts.stopping > 0 ? `<span class="badge badge-warning mr-1">${statusCounts.stopping} stopping</span>` : ''}
                            </span>
                        </button>
                    </h6>
                </div>
                <div id="${collapseId}" class="collapse${wasExpanded ? ' show' : ''}" aria-labelledby="heading-${teamKey}">
                    <div class="card-body">
                        <table class="table table-sm table-hover">
                            <thead>
                                <tr>
                                    <th>Category</th>
                                    <th>Challenge</th>
                                    <th>Status</th>
                                    <th>Connection Info</th>
                                    <th>Deployed Since</th>
                                    <th>Duration</th>
                                    <th>Expires At</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${team.deployments.map(d => {
                                    const deployedSince = new Date(d.deployed_since).toLocaleString();
                                    const duration = formatDuration(d.deployed_duration_seconds);
                                    const expiresAt = d.expires_at ? new Date(d.expires_at).toLocaleString() : 'Never';
                                    
                                    return `
                                        <tr>
                                            <td>${escapeHtml(d.category)}</td>
                                            <td>${escapeHtml(d.challenge_name)}</td>
                                            <td><span class="badge badge-${getStatusBadge(d.status)}">${d.status}</span></td>
                                            <td><small><code>${escapeHtml(d.connection_info || 'N/A')}</code></small></td>
                                            <td><small>${deployedSince}</small></td>
                                            <td><small>${duration}</small></td>
                                            <td><small>${expiresAt}</small></td>
                                        </tr>
                                    `;
                                }).join('')}
                            </tbody>
                        </table>
                    </div>
                </div>
            `;

            if (card) {
                // Update existing card
                card.innerHTML = cardHTML;
            } else {
                // Create new card
                card = document.createElement('div');
                card.className = 'card mb-2';
                card.dataset.teamId = teamKey;
                card.innerHTML = cardHTML;

                // Insert at correct position to maintain order
                if (index < accordion.children.length) {
                    accordion.insertBefore(card, accordion.children[index]);
                } else {
                    accordion.appendChild(card);
                }
            }
        });

        content.style.display = 'block';
        empty.style.display = 'none';
    }

    // Format duration in seconds to human readable
    function formatDuration(seconds) {
        if (seconds < 60) return `${seconds}s`;
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
        const hours = Math.floor(minutes / 60);
        return `${hours}h ${minutes % 60}m`;
    }

    // Utility functions
    function showAlert(message, type, persist = false) {
        const container = document.getElementById('alert-container');
        const alert = document.createElement('div');
        alert.className = `alert alert-${type} alert-dismissible fade show`;
        alert.innerHTML = `
            ${message}
            <button type="button" class="close" data-dismiss="alert">&times;</button>
        `;
        container.appendChild(alert);

        // If this is a network error alert, reset the flag when dismissed
        if (message.includes('Unable to connect to the deployer')) {
            alert.addEventListener('closed.bs.alert', () => {
                networkErrorShown = false;
            });
            // Also listen for manual close button clicks
            const closeBtn = alert.querySelector('.close');
            if (closeBtn) {
                closeBtn.addEventListener('click', () => {
                    networkErrorShown = false;
                });
            }
        }

        if (!persist) {
            setTimeout(() => {
                alert.remove();
            }, 5000);
        }
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
    document.getElementById('refresh-btn').addEventListener('click', async () => {
        await loadChallenges();
        errorSecondsRemaining = 10;
        await loadErrorDeployments();
        teamSecondsRemaining = 60;
        await loadTeamDeployments();
    });
    document.getElementById('refresh-team-deployments-btn').addEventListener('click', async () => {
        teamSecondsRemaining = 60;
        await loadTeamDeployments();
    });

    // Team filter input
    document.getElementById('team-filter-input').addEventListener('input', function(e) {
        teamFilterText = e.target.value;
        renderTeamDeployments();
    });

    // Initialize on page load
    init();
})();
