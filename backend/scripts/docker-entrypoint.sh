#!/bin/sh
set -e

if [ -n "${NAVIGATE_PUBLIC_ORIGIN:-}" ]; then
  case ",${NAVIGATE_CORS_ORIGINS:-}," in
    *",${NAVIGATE_PUBLIC_ORIGIN},"*) ;;
    *)
      if [ -n "${NAVIGATE_CORS_ORIGINS:-}" ]; then
        NAVIGATE_CORS_ORIGINS="${NAVIGATE_CORS_ORIGINS},${NAVIGATE_PUBLIC_ORIGIN}"
      else
        NAVIGATE_CORS_ORIGINS="${NAVIGATE_PUBLIC_ORIGIN}"
      fi
      export NAVIGATE_CORS_ORIGINS
      ;;
  esac
fi

if [ "${NAVIGATE_SKIP_MIGRATIONS:-}" != "1" ]; then
  alembic upgrade head
fi
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
