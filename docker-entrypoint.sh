#!/bin/sh
# Pick the process from EMPIRE_SERVICE: api | publisher | mcp | recurring | rotate.
set -eu
case "${EMPIRE_SERVICE:-}" in
  api) exec tasks-api ;;
  publisher) exec empire-publish --loop "${PUBLISH_INTERVAL:-5}" ;;
  mcp) exec empire-mcp-http ;;
  recurring) exec empire-recurring ;;
  rotate) exec empire-token-rotate-hosted ;;
  *) echo "EMPIRE_SERVICE must be 'api', 'publisher', 'mcp', 'recurring', or 'rotate'" >&2; exit 64 ;;
esac
