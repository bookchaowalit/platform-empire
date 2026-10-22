#!/bin/sh
# Pick the process from EMPIRE_SERVICE: api | publisher | mcp | recurring | rotate | backlog-sync.
set -eu
case "${EMPIRE_SERVICE:-}" in
  api) exec tasks-api ;;
  publisher) exec empire-publish --loop "${PUBLISH_INTERVAL:-5}" ;;
  mcp) exec empire-mcp-http ;;
  recurring) exec empire-recurring ;;
  rotate) exec empire-token-rotate-hosted ;;
  backlog-sync) exec empire-backlog-sync ;;
  *) echo "EMPIRE_SERVICE must be 'api', 'publisher', 'mcp', 'recurring', 'rotate', or 'backlog-sync'" >&2; exit 64 ;;
esac
