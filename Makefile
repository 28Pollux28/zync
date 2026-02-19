.PHONY: help bump-patch bump-minor bump-major version tag

## help: Show this help message
help:
	@echo 'Usage:'
	@sed -n 's/^##//p' ${MAKEFILE_LIST} | column -t -s ':' | sed -e 's/^/ /'

## bump-patch: Bump patch version and commit/tag/push
bump-patch:
	./scripts/bump_version.sh patch

## bump-minor: Bump minor version and commit/tag/push
bump-minor:
	./scripts/bump_version.sh minor

## bump-major: Bump major version and commit/tag/push
bump-major:
	./scripts/bump_version.sh major

## version: Show current version
version:
	@grep -oP '__version__ = "\K[0-9]+\.[0-9]+\.[0-9]+' __init__.py

## tag: Create and push a git tag for the current version
tag:
	@VERSION=$$(grep -oP '__version__ = "\K[0-9]+\.[0-9]+\.[0-9]+' __init__.py); \
	BRANCH=$$(git rev-parse --abbrev-ref HEAD); \
	if [ "$$BRANCH" != "master" ]; then \
		echo "Error: Not on master branch (currently on $$BRANCH)"; \
		exit 1; \
	fi; \
	if git rev-parse "v$$VERSION" >/dev/null 2>&1; then \
		echo "Error: Tag v$$VERSION already exists"; \
		exit 1; \
	fi; \
	git tag -a "v$$VERSION" -m "Release v$$VERSION"; \
	git push origin "v$$VERSION"; \
	echo "Tagged and pushed v$$VERSION"

