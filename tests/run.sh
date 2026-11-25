#!/bin/sh
# Run Zync's tests inside the CTFd image, with CTFd's own test helpers.
#   tests/run.sh [pytest args]       (from a Docker host, any folder)
# CTFD_IMAGE selects the image (default: ctfd/ctfd:3.8.9, the version tested).
# ZYNC_TEST_DB=mariadb runs them against a throwaway MariaDB (MARIADB_IMAGE,
# default mariadb:11.8, the stack's) instead of SQLite.
#
# The tests are copied out of the plugin folder before they run: imported
# from inside it, pytest would import the plugin package (and CTFd's API)
# before any CTFd application exists, which CTFd does not support.
set -eu
cd "$(dirname "$0")/.."

db_url=sqlite:////tmp/zync-tests.db
network_args=""
if [ "${ZYNC_TEST_DB:-sqlite}" = mariadb ]; then
    name="zync-tests-mariadb-$$"
    docker network create "$name" >/dev/null
    cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; docker network rm "$name" >/dev/null 2>&1 || true; }
    trap cleanup EXIT INT TERM
    docker run -d --rm --name "$name" --network "$name" -e MARIADB_ROOT_PASSWORD=zync-tests \
        "${MARIADB_IMAGE:-mariadb:11.8}" >/dev/null
    i=0
    until docker exec "$name" mariadb-admin ping -uroot -pzync-tests --silent >/dev/null 2>&1; do
        i=$((i + 1))
        [ "$i" -lt 60 ] || { echo "MariaDB did not start" >&2; exit 1; }
        sleep 1
    done
    db_url="mysql+pymysql://root:zync-tests@$name/zync_tests"
    network_args="--network $name"
fi

# shellcheck disable=SC2086  # network_args is empty or two words
docker run --rm -u root -e PYTHONDONTWRITEBYTECODE=1 -e TESTING_DATABASE_URL="$db_url" $network_args \
    -v "$PWD:/opt/CTFd/CTFd/plugins/zync:ro" -w /opt/CTFd \
    --entrypoint sh "${CTFD_IMAGE:-ctfd/ctfd:3.8.9}" -c '
        pip install -q --disable-pip-version-check -r CTFd/plugins/zync/requirements.txt pytest==8.4.2 >/dev/null &&
        mkdir -p /tmp/zync-tests && cp CTFd/plugins/zync/tests/*.py /tmp/zync-tests/ &&
        python -m pytest -p no:cacheprovider -p no:randomly --rootdir /tmp/zync-tests -q /tmp/zync-tests "$@"' sh "$@"
