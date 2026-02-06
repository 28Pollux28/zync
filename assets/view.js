CTFd._internal.challenge.data = undefined

CTFd._internal.challenge.renderer = null;

CTFd._internal.challenge.preRender = function () {
}

CTFd._internal.challenge.render = function (markdown) {

    return CTFd._internal.challenge.renderer.render(markdown)
}

String.prototype.format = function () {
    const args = arguments;
    return this.replace(/{([0-9]+)}/g, function (match, index) {
        return typeof args[index] == 'undefined' ? match : args[index];
    });
};


function zyncStopAllActivity() {
    const s = CTFd._internal.challenge._zyncState;

    // stop countdown
    if (s.timeCountdownInterval) {
        clearInterval(s.timeCountdownInterval);
        s.timeCountdownInterval = null;
    }

    // stop scheduled polling
    if (s.pollTimeoutId) {
        clearTimeout(s.pollTimeoutId);
        s.pollTimeoutId = null;
    }

    // abort in-flight status request
    if (s.pollAbortController) {
        try { s.pollAbortController.abort(); } catch (e) {}
        s.pollAbortController = null;
    }
}

function zyncSchedulePoll(delayMs, skipLoadingState = true) {
    const s = CTFd._internal.challenge._zyncState;
    if (!s.modalOpen) return; // don't schedule anything if modal is closed

    if (s.pollTimeoutId) clearTimeout(s.pollTimeoutId);

    s.pollTimeoutId = setTimeout(() => {
        // re-check at execution time too
        if (!s.modalOpen) return;
        getDeploymentStatus(skipLoadingState);
    }, delayMs);
}

CTFd._internal.challenge.postRender = function () {
    const s = CTFd._internal.challenge._zyncState;

    const modalEl = document.querySelector("#challenge-window");
    if (modalEl) {
        // Mark open + start initial load
        s.modalOpen = true;

        // Important: ensure we don't stack listeners if postRender runs multiple times
        if (!modalEl._zyncListenersBound) {
            modalEl.addEventListener("hidden.bs.modal", () => {
                s.modalOpen = false;
                zyncStopAllActivity();
            });

            modalEl.addEventListener("shown.bs.modal", () => {
                s.modalOpen = true;
                // optional: refresh immediately when reopened
                getDeploymentStatus();
            });

            modalEl._zyncListenersBound = true;
        }
    } else {
        // If we can't find modal element, at least allow polling when rendered
        s.modalOpen = true;
    }

};


CTFd._internal.challenge.submit = function (preview) {
    var challenge_id = parseInt(CTFd.lib.$('#challenge-id').val())
    var submission = CTFd.lib.$('#challenge-input').val()

    var body = {
        'challenge_id': challenge_id,
        'submission': submission,
    }
    var params = {}
    if (preview) {
        params['preview'] = true
    }

    return CTFd.api.post_challenge_attempt(params, body).then(function (response) {
        if (response.status === 429) {
            // User was ratelimited but process response
            return response
        }
        if (response.status === 403) {
            // User is not logged in or CTF is paused.
            return response
        }
        return response
    })
};

function setDeploymentInfo(htmlContent) {
    const challengeId = CTFd._internal.challenge.data.id;
    CTFd.lib.$(`#zync_deploy_${challengeId}`).html(htmlContent);
}

CTFd._internal.challenge._zyncState = CTFd._internal.challenge._zyncState || {
    timeCountdownInterval: null,
    pollTimeoutId: null,
    pollAbortController: null,
    modalOpen: false,
};

function formatTimeLeft(expirationTime) {
    if (!expirationTime) return null;
    const exp = new Date(expirationTime);
    const now = new Date();
    const diffMs = exp - now;
    if (diffMs <= 0) return "0m 00s";
    const totalSeconds = Math.floor(diffMs / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const secondsPadded = String(seconds).padStart(2, "0");
    if (hours > 0) {
        return `${hours}h ${minutes}m ${secondsPadded}s`;
    }
    return `${minutes}m ${secondsPadded}s`;
}

function displayConnectionInfo(statusData) {
    const connection_info = statusData.connection_info || "";
    const expiration_time = statusData.expiration_time;
    const extensions_left = statusData.extensions_left;
    const extension_time = statusData.extension_time || "30m";
    const unique = statusData.unique;

    // If connection_info is a URL, make it a clickable link
    const connectionInfo = (connection_info.indexOf('http') === 0) ? `<a href="${connection_info}" target="_blank">${connection_info}</a>` : `<code>${connection_info}</code>`;

    let timeBoxHtml = "";
    if (expiration_time) {
        const timeLeft = formatTimeLeft(expiration_time);
        timeBoxHtml = `<div class="mt-2 p-2 border rounded bg-light"><strong>Temps restant&nbsp;:</strong> <span id="time_remaining">${timeLeft}</span></div>`;
    }

    let extendBoxHtml = "";
    if (extensions_left !== undefined && extensions_left !== 0) {
        const extendLabel = extensions_left === -1
            ? `Ajouter du temps (+${extension_time})`
            : `Ajouter du temps (+${extension_time}) — ${extensions_left} restant${extensions_left > 1 ? "s" : ""}`;
        extendBoxHtml = `<div class="mt-2"><a onclick="extendInstance()" class='btn btn-outline-primary border zync-extend-btn'><small><i class="fas fa-clock me-1"></i>${extendLabel}</small></a></div>`;
    }

    let deleteBoxHtml = "";
    if (unique === false) {
        deleteBoxHtml = `<div class="mt-2"><a onclick="confirm_delete_deployment()" data-bs-theme='dark' class='btn btn-danger border border-white'><small style='color:white;'><i class="fas fa-trash me-1"></i>Supprimer l'instance</small></a></div>`
    }

    setDeploymentInfo(
        '<div>Instance disponible à&nbsp;:<br />' + connectionInfo + '</div>' +
        timeBoxHtml +
        extendBoxHtml +
        deleteBoxHtml
    );

    // Start countdown if we have expiration time
    if (expiration_time) {
        if (CTFd._internal.challenge._zyncState.timeCountdownInterval) clearInterval(CTFd._internal.challenge._zyncState.timeCountdownInterval);
        CTFd._internal.challenge._zyncState.timeCountdownInterval = setInterval(() => {
            const el = document.getElementById("time_remaining");
            if (el) {
                const timeLeft = formatTimeLeft(expiration_time);
                el.textContent = timeLeft;
                if (timeLeft === "0m 00s" && CTFd._internal.challenge._zyncState.timeCountdownInterval) {
                    clearInterval(CTFd._internal.challenge._zyncState.timeCountdownInterval);
                    CTFd._internal.challenge._zyncState.timeCountdownInterval = null;
                    setTimeout(() => {
                        getDeploymentStatus();
                    },600);
                }
            }
        }, 1000);
    }
}

function setSpinningWheel() {
    setDeploymentInfo('<div class="text-center"><i class="fas fa-circle-notch fa-spin fa-1x"></i></div>');
}

function setDeployingMessage() {
    setDeploymentInfo('<div class="text-center"><i class="fas fa-circle-notch fa-spin fa-1x"></i></div><br/><div class="text-center">Déploiement en cours, veuillez patienter...</div>');
}

function setUndeployingMessage() {
    setDeploymentInfo('<div class="text-center"><i class="fas fa-circle-notch fa-spin fa-1x"></i></div><br/><div class="text-center">Suppression de l\'instance en cours, veuillez patienter...</div>');
}

function resetDeployButton() {
    if (CTFd._internal.challenge._zyncState.timeCountdownInterval) {
        clearInterval(CTFd._internal.challenge._zyncState.timeCountdownInterval);
        CTFd._internal.challenge._zyncState.timeCountdownInterval = null;
    }
    setDeploymentInfo(`<span><a onclick="deploy()" class='btn btn-dark border border-white'><small style='color:white;'><i class="fas fa-play me-1"></i>Déployer l'instance</small></a></span>`);
}

function showErrorMessage(title, body) {
    const content =
        '<div>' +
        '<h5>' + title + '</h5>' +
        '<p>' + body + '</p>' +
        '</div>';

    setDeploymentInfo(content);
}

async function getDeployerURL() {
    return await CTFd.fetch("/api/v1/deploy/url")
        .then(response => {
            if (!response.ok) throw new Error("Erreur lors de la récupération de l'URL du déployeur");
            return response.json();
        })
        .then(deployerUrl => {
            if (!deployerUrl.deployer_url) throw new Error("URL du déployeur manquante");
            localStorage.setItem("deployer_url", deployerUrl.deployer_url);
            return true;
        })
        .catch(error => {
            showErrorMessage("Attention&nbsp;!", "Impossible d'obtenir l'URL du déployeur.");
            console.error(error);
            return false;
        });
}

async function getToken(challengeId) {
    try {
        const response = await CTFd.fetch("/api/v1/deploy/token", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ challenge_id: challengeId }),
        });

        if (!response.ok) {
            throw new Error("Failed to fetch token");
        }

        const { token } = await response.json();

        if (!token) {
            throw new Error("Missing token in response");
        }

        sessionStorage.setItem("jwt_" + challengeId, token);
        return true;
    } catch (err) {
        showErrorMessage(
            "Attention&nbsp;!",
            "Impossible d'obtenir un jeton d'authentification pour le déploiement."
        );
        return false;
    }
}

async function ensureToken(challengeId) {
     if (!sessionStorage.getItem("jwt_"+challengeId)) {
        const tokenObtained = await getToken(challengeId);
        if (!tokenObtained) {
            return false;
        }
    }

    // check JWT validity
    const token = sessionStorage.getItem("jwt_"+challengeId);
    const tokenParts = token.split('.');
    const now = Math.ceil(Date.now() / 1000);
    if (tokenParts.length !== 3 || (JSON.parse(atob(tokenParts[1])).exp < now)) {
        const tokenObtained = await getToken(challengeId);
        if (!tokenObtained) {
            return false;
        }
    }
    return true;
}

async function getDeploymentStatus(skipLoadingState = false) {
    const s = CTFd._internal.challenge._zyncState;

    // If modal is closed, do nothing (prevents late timers from re-polling)
    if (!s.modalOpen) return;

    // Abort any previous in-flight status request before starting a new one
    if (s.pollAbortController) {
        try { s.pollAbortController.abort(); } catch (e) {}
    }
    s.pollAbortController = new AbortController();

    if (!skipLoadingState) {
        setSpinningWheel();
    }

    if (!localStorage.getItem("deployer_url")) {
        const deployerUrlObtained = await getDeployerURL();
        if (!deployerUrlObtained) {
            return;
        }
    }

    const challengeId = CTFd._internal.challenge.data.id;

    const tokenValid = await ensureToken(challengeId);
    if (!tokenValid) {
        showErrorMessage("Attention&nbsp;!", "Impossible d'obtenir un jeton d'authentification pour le déploiement.");
        return;
    }

    const token = sessionStorage.getItem("jwt_" + challengeId);

    try {
        const res = await fetch(localStorage.getItem("deployer_url") + "/status", {
            headers: {
                Authorization: "Bearer " + token,
            },
            signal: s.pollAbortController.signal,
        });
        switch (res.status) {
            case 200:
                const data = await res.json();
                const status = data.status; //running, stopping, starting, error
                switch (status) {
                    case "running":
                        displayConnectionInfo(data);
                        return;
                    case "stopping":
                        setUndeployingMessage();
                        zyncSchedulePoll(2000, true);
                        return;
                    case "starting":
                        setDeployingMessage();
                        zyncSchedulePoll(2000, true);
                        return;
                    case "error":
                        showErrorMessage("Erreur", data.connection_info || "Le déploiement a échoué.");
                        zyncSchedulePoll(10000, true);
                        return;
                }
                return;
            case 401:
                showErrorMessage("Attention&nbsp;!", "Authentification invalide");
                return;
            case 404:
                // check if response is empty or has json error message
                if (!res.headers.get("Content-Type")?.includes("application/json")) {
                    resetDeployButton();
                    return;
                }
                const data404 = await res.json();
                if (data404.message.includes("unique")) {
                    // Hide deploy button if challenge is not unique
                    document.querySelector(`#zync_deploy_${challengeId}`).style.display = "none";
                }
                return;
            case 500:
                const errData = await res.json();
                const errMsg = typeof errData.message === "string" ? errData.message : (errData.message || "Erreur serveur");
                showErrorMessage("Attention&nbsp;!", errMsg);
                return;
        }
    } catch (err) {
        console.log(err)
        if (err && (err.name === "AbortError")) return;
        showErrorMessage(
            "Attention&nbsp;!",
            "Impossible d'obtenir le statut du déploiement"
        );
    }
    return;
}

async function deploy() {
    const challengeId = CTFd._internal.challenge.data.id;
    const tokenValid = await ensureToken(challengeId);
    if (!tokenValid) {
        showErrorMessage("Attention&nbsp;!", "Impossible d'obtenir un jeton d'authentification pour le déploiement.");
        return;
    }

    let res;
    try {
        setDeployingMessage();
        const challengeCategory = CTFd._internal.challenge.data.category || "";
        res = await fetch(localStorage.getItem("deployer_url") + "/deploy", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + sessionStorage.getItem("jwt_" + CTFd._internal.challenge.data.id),
            },
            body: JSON.stringify({
                challenge_name: CTFd._internal.challenge.data.name,
                category: challengeCategory,
            }),

        })
    } catch (err) {
        showErrorMessage("Attention&nbsp;!", "Erreur réseau pendant le démarrage du déploiement.");
        return;
    }
    if (res.status !== 202) {
        switch (res.status) {
            case 409:
                // If a deployment is already in progress, poll for status
                setDeployingMessage();
                setTimeout(() => getDeploymentStatus(true), 2000);
                return;
            case 400:
                showErrorMessage("Attention&nbsp;!", "Challenge invalide.");
                return;
            case 401:
                showErrorMessage("Attention&nbsp;!", "Authentification invalide");
                return;
            case 500:
                const error = await res.json();
                const errorMessage = JSON.parse(error.message || "{}");
                showErrorMessage("Attention&nbsp;!", (errorMessage.message || "Erreur pendant le démarrage du déploiement.") + "<br>Merci de réessayer plus tard ou de contacter un administrateur.");
                return;
            default:
                showErrorMessage("Attention&nbsp;!", "Qu'est-ce que t'as essayé de faire pour arriver là 🤡??");
        }
        return;
    }
    // Poll every 2 seconds until deployment is complete
    setDeployingMessage();
    setTimeout(() => getDeploymentStatus(true), 2000);
}



CTFd._internal.challenge._zyncState = CTFd._internal.challenge._zyncState || {
    extendInProgress: false,
};

async function extendInstance() {
    const challengeId = CTFd._internal.challenge.data.id;
    if (CTFd._internal.challenge._zyncState.extendInProgress) return;

    const tokenValid = await ensureToken(challengeId);
    if (!tokenValid) {
        showErrorMessage("Attention&nbsp;!", "Impossible d'obtenir un jeton d'authentification pour le déploiement.");
        return;
    }

    CTFd._internal.challenge._zyncState.extendInProgress = true;
    const extendBtn = document.querySelector(`#zync_deploy_${challengeId} .zync-extend-btn`);
    if (extendBtn) {
        extendBtn.style.pointerEvents = "none";
        extendBtn.classList.add("opacity-50");
        extendBtn.innerHTML = '<small><i class="fas fa-spinner fa-spin me-1"></i>En cours...</small>';
    }

    try {
        const res = await fetch(localStorage.getItem("deployer_url") + "/extend", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + sessionStorage.getItem("jwt_" + challengeId),
            },
        });

        switch (res.status) {
            case 200:
                const data = await res.json();
                CTFd._internal.challenge._zyncState.extendInProgress = false;
                displayConnectionInfo(data);
                return;
            case 400:
                CTFd._internal.challenge._zyncState.extendInProgress = false;
                showErrorMessage("Attention&nbsp;!", "Impossible d'ajouter du temps (trop tôt, limite atteinte, etc.).");
                getDeploymentStatus();
                return;
            case 401:
                CTFd._internal.challenge._zyncState.extendInProgress = false;
                showErrorMessage("Attention&nbsp;!", "Authentification invalide");
                return;
            case 404:
                CTFd._internal.challenge._zyncState.extendInProgress = false;
                resetDeployButton();
                return;
            case 500:
                CTFd._internal.challenge._zyncState.extendInProgress = false;
                const err = await res.json();
                showErrorMessage("Attention&nbsp;!", err.message || "Erreur lors de l'extension.");
                getDeploymentStatus();
                return;
            default:
                CTFd._internal.challenge._zyncState.extendInProgress = false;
                showErrorMessage("Attention&nbsp;!", "Erreur inattendue lors de l'extension.");
                getDeploymentStatus();
        }
    } catch (err) {
        CTFd._internal.challenge._zyncState.extendInProgress = false;
        showErrorMessage("Attention&nbsp;!", "Erreur réseau lors de l'ajout de temps.");
        getDeploymentStatus();
    }
}

function confirm_delete_deployment() {
    if (confirm("Voulez-vous vraiment supprimer ce déploiement ?")) {
        delete_deployment();
    }
}

async function delete_deployment() {
    const challengeId = CTFd._internal.challenge.data.id;
    const challengeName = CTFd._internal.challenge.data.name;
    const challengeCategory = CTFd._internal.challenge.data.category || "";

    const tokenValid = await ensureToken(challengeId);
    if (!tokenValid) {
        showErrorMessage("Attention&nbsp;!", "Impossible d'obtenir un jeton d'authentification pour le déploiement.");
        return;
    }

    setUndeployingMessage();
    try {
        const res = await fetch(localStorage.getItem("deployer_url") + "/terminate", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + sessionStorage.getItem("jwt_" + challengeId),
            },
            body: JSON.stringify({
                challenge_name: challengeName,
                category: challengeCategory,
            }),
        });

        if (res.ok) {
            setTimeout(() => getDeploymentStatus(true), 2000);
        } else {
            switch (res.status) {
                case 401:
                    showErrorMessage("Attention&nbsp;!", "Authentification invalide");
                    break;
                case 500:
                    const err = await res.json();
                    showErrorMessage("Attention&nbsp;!", err.message || "Erreur lors de la suppression.");
                    break;
                default:
                    showErrorMessage("Attention&nbsp;!", "Erreur lors de la suppression du déploiement.");
            }
        }
    } catch (err) {
        showErrorMessage("Attention&nbsp;!", "Erreur réseau pendant la suppression du déploiement.");
    }
}
