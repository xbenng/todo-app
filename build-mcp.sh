#!/usr/bin/env bash
set -euo pipefail
MCP_DIR="$(cd "$(dirname "$0")" && pwd)/mcp-servers"
mkdir -p "$MCP_DIR"

# Pinned versions
SLACK_VERSION="1.2.3"
IMAP_REF="ed133aa7163989231c5ad502cf81cb636c198b5d"
SMARTSHEET_REF="d262e91668ec2b7af5ddc789d417f64361339008"
CALDAV_REF="b63f3d488efbe9a75a7fc3480ce67eecfce3ab6c"

# --- Slack (Go binary via npm) ---
echo "Installing slack @ v${SLACK_VERSION}..."
rm -rf "$MCP_DIR/slack"
mkdir -p "$MCP_DIR/slack"
(cd "$MCP_DIR/slack" && npm init -y --silent >/dev/null 2>&1 && npm install --silent "slack-mcp-server@${SLACK_VERSION}")

# --- IMAP & Smartsheet (Node/TypeScript: clone, build, keep dist + prod deps) ---
for entry in \
  "imap|https://github.com/nikolausm/imap-mcp-server.git|$IMAP_REF" \
  "smartsheet|https://github.com/xbenng/smartsheet-mcp-server.git|$SMARTSHEET_REF"
do
  IFS='|' read -r name url ref <<< "$entry"
  echo "Building $name @ ${ref:0:10}..."
  tmp=$(mktemp -d)
  git clone --quiet "$url" "$tmp"
  (cd "$tmp" && git checkout --quiet "$ref" && npm ci --silent && npm run build)
  rm -rf "$MCP_DIR/$name"
  mkdir -p "$MCP_DIR/$name"
  cp -r "$tmp/dist" "$MCP_DIR/$name/dist"
  cp "$tmp/package.json" "$MCP_DIR/$name/"
  cp "$tmp/package-lock.json" "$MCP_DIR/$name/" 2>/dev/null || true
  # Strip duplicate shebangs from dist JS files (esbuild banner + source can produce two)
  for jsfile in "$MCP_DIR/$name"/dist/*.js; do
    if [ "$(head -2 "$jsfile" | grep -c '^#!')" = "2" ]; then
      tail -n +2 "$jsfile" > "$jsfile.tmp" && mv "$jsfile.tmp" "$jsfile"
    fi
  done
  # Install production deps only in the output dir
  (cd "$MCP_DIR/$name" && npm ci --omit=dev --silent 2>/dev/null || npm install --omit=dev --silent)
  rm -rf "$tmp"
done

# --- Patch IMAP to support OAuth2 accessToken ---
IMAP_DIST="$MCP_DIR/imap/dist/index.js"
if [ -f "$IMAP_DIST" ]; then
  # 1. Guard decrypt() for undefined passwords (OAuth accounts have no password)
  sed -i.bak 's/decrypt(text) {/decrypt(text) { if (!text) return text;/' "$IMAP_DIST"
  # 2. Support accessToken in connect auth
  sed -i.bak 's/auth: {[[:space:]]*user: account.user,[[:space:]]*pass: account.password[[:space:]]*}/auth: account.accessToken ? { user: account.user, accessToken: account.accessToken } : { user: account.user, pass: account.password }/g' "$IMAP_DIST" 2>/dev/null || true
  rm -f "$IMAP_DIST.bak"
fi

# --- CalDAV (Python): clone at pinned ref and pip install ---
echo "Building caldav @ ${CALDAV_REF:0:10}..."
rm -rf "$MCP_DIR/caldav"
git clone --quiet https://github.com/xbenng/caldav-mcp.git "$MCP_DIR/caldav"
(cd "$MCP_DIR/caldav" && git checkout --quiet "$CALDAV_REF")
rm -rf "$MCP_DIR/caldav/.git"
pip install --quiet "$MCP_DIR/caldav"

echo "MCP servers built and installed in $MCP_DIR"
