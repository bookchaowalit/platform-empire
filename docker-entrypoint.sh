#!/bin/sh
# Pick the process from EMPIRE_SERVICE: api | publisher | mcp | recurring | rotate | backlog-sync | stripe-reconcile.
set -eu
case "${EMPIRE_SERVICE:-}" in
  api) exec tasks-api ;;
  publisher) exec empire-publish --loop "${PUBLISH_INTERVAL:-5}" ;;
  mcp) exec empire-mcp-http ;;
  recurring) exec empire-recurring ;;
  rotate) exec empire-token-rotate-hosted ;;
  backlog-sync) exec empire-backlog-sync ;;
  stripe-reconcile) exec empire-stripe-reconcile ;;
  *) echo "EMPIRE_SERVICE must be 'api', 'publisher', 'mcp', 'recurring', 'rotate', 'backlog-sync', or 'stripe-reconcile'" >&2; exit 64 ;;
esac
