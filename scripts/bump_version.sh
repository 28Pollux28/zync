#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

INIT_FILE="$PROJECT_ROOT/__init__.py"
CHANGELOG_FILE="$PROJECT_ROOT/CHANGELOG.md"

# Function to display usage
usage() {
    echo "Usage: $0 <patch|minor|major>"
    echo ""
    echo "Arguments:"
    echo "  patch   - Bump the patch version (e.g., 0.1.2 -> 0.1.3)"
    echo "  minor   - Bump the minor version (e.g., 0.1.2 -> 0.2.0)"
    echo "  major   - Bump the major version (e.g., 0.1.2 -> 1.0.0)"
    exit 1
}

# Check if argument is provided
if [ $# -ne 1 ]; then
    usage
fi

BUMP_TYPE="$1"

# Validate bump type
if [[ "$BUMP_TYPE" != "patch" && "$BUMP_TYPE" != "minor" && "$BUMP_TYPE" != "major" ]]; then
    echo "Error: Invalid bump type '$BUMP_TYPE'"
    usage
fi

# Extract current version from __init__.py
CURRENT_VERSION=$(grep -oP '__version__ = "\K[0-9]+\.[0-9]+\.[0-9]+' "$INIT_FILE")

if [ -z "$CURRENT_VERSION" ]; then
    echo "Error: Could not extract current version from $INIT_FILE"
    exit 1
fi

echo "Current version: $CURRENT_VERSION"

# Split version into components
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"

# Bump version based on type
case "$BUMP_TYPE" in
    patch)
        PATCH=$((PATCH + 1))
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        ;;
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        ;;
esac

NEW_VERSION="$MAJOR.$MINOR.$PATCH"
echo "New version: $NEW_VERSION"

# Update __init__.py
sed -i "s/__version__ = \"$CURRENT_VERSION\"/__version__ = \"$NEW_VERSION\"/" "$INIT_FILE"
echo "Updated $INIT_FILE"

# Update CHANGELOG.md - rename the unreleased placeholder to the new version with today's date
TODAY=$(date +%Y-%m-%d)

if [ -f "$CHANGELOG_FILE" ]; then
    # Replace the unreleased placeholder header with the new version, and add a fresh placeholder above it
    sed -i "s/^## vX\.X\.X (YYYY-MM-DD)$/## vX.X.X (YYYY-MM-DD)\n\n## v${NEW_VERSION} (${TODAY})/" "$CHANGELOG_FILE"
    echo "Updated $CHANGELOG_FILE"
else
    echo "Warning: $CHANGELOG_FILE not found, skipping changelog update"
fi

# Check if we're on master branch
cd "$PROJECT_ROOT"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "master" ]; then
    echo "Error: Not on master branch (currently on $BRANCH)"
    echo "Version files updated but not committed/tagged/pushed"
    exit 1
fi

# Check if tag already exists
if git rev-parse "v$NEW_VERSION" >/dev/null 2>&1; then
    echo "Error: Tag v$NEW_VERSION already exists"
    echo "Version files updated but not committed/tagged/pushed"
    exit 1
fi

# Stage the modified files
echo "Staging changes..."
git add "$INIT_FILE" "$CHANGELOG_FILE"

# Commit the changes
echo "Committing changes..."
git commit -m "Bump version to $NEW_VERSION"

# Create and push the tag
echo "Creating tag v$NEW_VERSION..."
git tag -a "v$NEW_VERSION" -m "Release v$NEW_VERSION"

# Push commits and tags
echo "Pushing to origin..."
git push origin "$BRANCH"
git push origin "v$NEW_VERSION"

echo ""
echo "✓ Version bumped from $CURRENT_VERSION to $NEW_VERSION"
echo "✓ Changes committed and pushed"
echo "✓ Tag v$NEW_VERSION created and pushed"

