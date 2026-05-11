#!/usr/bin/env sh
set -eu

if [ "$#" -eq 0 ]; then
  set -- autodl-helper run daemon --config /app/config.yaml
fi

if [ "$1" = "autodl-helper" ]; then
  exec "$@"
fi

case "$1" in
  -*|init|login|list|run|service|ui|debug|config)
    exec autodl-helper "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
