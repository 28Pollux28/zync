# Changelog

## vX.X.X (YYYY-MM-DD)
 - Add challenge manager (`/admin/zync_challenges`), replacing ctfd-management's `challenges.sh`: ingest, sync and delete the challenges of Galvanize's repositories in CTFd, for one challenge, a selection or a whole repository, with dry run; clone, update and remove repositories, build and remove images through Galvanize's admin API
 - Link CTFd challenges to their definition file (new table `zync_challenge_sources`), and run manager actions as background jobs with live logs, one at a time across workers (new tables `zync_jobs`, `zync_lock`)
 - Requires a Galvanize version with the repository API and the `manage` token scope; no new dependency, and no access to the server, Docker or git from CTFd. Zync relies on Galvanize for everything about the repositories: which challenges have something to build (`build.buildable`, refused builds), where declared files are (`files`), the repository commit; builds and image actions name challenges by definition path
 - Sync refuses requirements that would form a cycle (A requires B, B requires A), counting the prerequisites already in CTFd: the challenges whose requirements change and would close a cycle keep their current ones (the message names the cycle and what they keep), checked again until none is left, since a refused challenge's current requirements may close another cycle; a cycle already in CTFd is reported. A dry run reports the same. A job on a repository Galvanize does not have fails (delete still removes the CTFd challenges of a repository removed from Galvanize). The challenge listing includes each definition's resolved `files` and `commit`; when Galvanize cannot answer, the listing shows the error instead of every challenge as gone from the repository (a 404 that is not Galvanize's answer, from the proxy in front of a stopped Galvanize, counts as Galvanize not answering, not as a missing repository, file or operation)
 - The page shows a spinner on running tasks (refreshing the task list while one runs), an indeterminate progress bar while Galvanize clones or updates a repository (it reports no progress), and when Refresh last succeeded or why it failed. Rebuild sends and counts only the challenges with something to build; image states show unreachable hosts and failed checks apart, and an image whose removal failed as still there

## v0.5.2 (2026-03-05)
 - Add Powered by in footer 

## v0.5.1 (2026-02-20)
 - Modify bump version script to automatically tag and commit to master

## v0.5.0 (2026-02-19)
 - Add environment variable support for deployer configuration (ZYNC_DEPLOYER_URL, ZYNC_JWT_SECRET)
 - Update README with comprehensive feature documentation and environment variable usage
 - Change background on timebox (unreadable on core theme)

## v0.4.0 (2026-02-18)
 - Add error deployments monitoring section with auto-refresh (10s) in admin dashboard
 - Add team deployments overview section with auto-refresh (60s) in admin dashboard
 - Add countdown timers showing time until next auto-refresh
 - Add retry, terminate, and delete actions for failed deployments
 - Add team filtering functionality in team deployments view
 - Add expandable/collapsible error messages for better readability
 - Add visual status badges and deployment duration tracking
 - Allow network error alerts to reshow after being dismissed in admin dashboard

## v0.3.0 (2026-02-17)
 - Add reload challenge index to admin dashboard

## v0.1.1 (2025-02-07)
 - Add admin dashboard
 - Handle unique challenges in challenge view for players

## v0.1.0 (2025-02-04)
 - Initial release
 - Added feedback on zync config setup
