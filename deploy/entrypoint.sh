#!/bin/sh
# Startet vor dem Webserver: Tabellen anlegen, optional Demo-/Startdaten einspielen.
set -e
if [ "$1" = "gunicorn" ]; then
  i=0
  until flask init-db >/dev/null 2>&1; do
    i=$((i+1)); [ "$i" -ge 30 ] && { echo "Datenbank nicht erreichbar" >&2; flask init-db; exit 1; }
    sleep 2
  done
  # SEED=1: Inhalte (FAQ, Formate, Leistungen, Termine) + Superadmin, nur wenn die DB noch leer ist
  # SEED_DEMO=1: zusätzlich Demo-Mitglieder (vor Go-live: flask remove-demo)
  if [ "${SEED:-0}" = "1" ]; then
    if [ "${SEED_DEMO:-0}" = "1" ]; then flask seed --demo; else flask seed --no-demo; fi
  fi
fi
exec "$@"
