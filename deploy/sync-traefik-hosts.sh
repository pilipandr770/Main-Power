#!/bin/sh
# Hält die Traefik-Routen der Klub-Plattform aktuell (Cron, jede Minute):
#  * ein Wildcard-Router für <slug>.<PLATFORM_DOMAIN> (Zertifikat per Cloudflare-DNS-Challenge, Resolver "cfdns")
#  * je eigene Domain eines Klubs ein eigener Router (Zertifikat per HTTP-Challenge, Resolver "letsencrypt")
# Die Domains kommen aus der App (`flask list-hosts`). Schreibt nur die eigene Datei und nur bei Änderungen;
# ist die App nicht erreichbar, bleibt die bisherige Datei unverändert.
#   * * * * * /srv/mainpower/deploy/sync-traefik-hosts.sh >> /var/log/mainpower-traefik.log 2>&1
set -eu
APP_DIR="${APP_DIR:-/srv/mainpower}"
OUT="${OUT:-/opt/traefik-dynamic/mainpower-clubs.yml}"
COMPOSE="docker compose -f $APP_DIR/docker-compose.traefik.yml"
cd "$APP_DIR"

platform_domain=$(grep -E '^PLATFORM_DOMAIN=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " | tr 'A-Z' 'a-z' || true)
hosts=$($COMPOSE exec -T app flask list-hosts 2>/dev/null) || { echo "$(date -Is) App nicht erreichbar, Datei bleibt"; exit 0; }
# nur plausible Hostnamen durchlassen (kein YAML/Regel-Einschleusen)
hosts=$(printf '%s\n' "$hosts" | grep -E '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' | sort -u || true)

tmp=$(mktemp)
{
  echo "# Automatisch erzeugt von deploy/sync-traefik-hosts.sh – nicht von Hand bearbeiten."
  echo "http:"
  echo "  routers:"
  if printf '%s' "$platform_domain" | grep -qE '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'; then
    esc=$(printf '%s' "$platform_domain" | sed 's/\./\\./g')
    echo "    mainpower-wildcard:"
    printf '      rule: '"'"'HostRegexp(`^[a-z0-9-]+\\.%s$`)'"'"'\n' "$esc"
    echo "      priority: 1"
    echo "      entryPoints: [websecure]"
    echo "      service: mainpower"
    echo "      tls:"
    echo "        certResolver: cfdns"
    echo "        domains:"
    echo "          - main: \"*.${platform_domain}\""
  fi
  for h in $hosts; do
    id=$(printf '%s' "$h" | tr -c 'a-z0-9' '-')
    echo "    mainpower-club-${id}:"
    echo "      rule: \"Host(\`${h}\`)\""
    echo "      entryPoints: [websecure]"
    echo "      service: mainpower"
    echo "      tls:"
    echo "        certResolver: letsencrypt"
  done
} > "$tmp"
# ohne Router ist "routers:" leer – dann gar keine Datei-Routen
if [ "$(grep -c '^    mainpower-' "$tmp")" -eq 0 ]; then printf '# keine Klub-Routen\n' > "$tmp"; fi

if [ ! -f "$OUT" ] || ! cmp -s "$tmp" "$OUT"; then
  chmod 644 "$tmp"; mv "$tmp" "$OUT"
  echo "$(date -Is) Traefik-Routen aktualisiert: $(printf '%s' "$hosts" | tr '\n' ' ')"
else
  rm -f "$tmp"
fi
