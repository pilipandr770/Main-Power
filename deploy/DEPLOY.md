# Деплой Klub-Plattform (Docker)

## Локально
```
docker compose up --build        # http://localhost:8000, Postgres + демо-данные
```
Демо-данные выключаются: `SEED_DEMO=0`. Ключ ИИ и остальное — в `.env` (см. `.env.example`).

## Hostinger VPS (рекомендуется)
1. VPS с Ubuntu 22.04/24.04 и Docker (в Hostinger есть шаблон «Ubuntu + Docker»).
2. DNS: A-запись домена платформы (`klub.example.org`) → IP VPS (в Cloudflare можно оставить «серое облако» или включить прокси).
3. На сервере:
```
git clone <repo> /srv/mainpower && cd /srv/mainpower
cp .env.example .env      # заполнить: SECRET_KEY, POSTGRES_PASSWORD, DOMAIN, ADMIN_EMAIL/PASSWORD, ANTHROPIC_API_KEY
docker compose -f docker-compose.prod.yml up -d --build
```
   Caddy сам получает сертификат Let's Encrypt (порты 80/443 открыты). Сервис `sync` раз в час тянет события из календаря-источника каждого клуба (если задан).
4. Первый запуск с `SEED=1` создаёт контент и суперадмина. Потом можно поставить `SEED=0`.
5. Бэкап БД: `docker compose -f docker-compose.prod.yml exec db pg_dump -U mainpower mainpower > backup.sql`
6. Обновление: `git pull && docker compose -f docker-compose.prod.yml up -d --build`

## Cloudflare
Cloudflare не запускает Flask/Docker-приложения на обычном тарифе (Cloudflare Containers пока в бете). Практичный вариант —
**VPS + Cloudflare как DNS/защита**:
- Прокси включён (оранжевое облако): SSL/TLS-режим **Full (strict)**, в `.env` `PROXY_HOPS=2` (иначе rate-limit видит IP Cloudflare).
- Без открытых портов: **Cloudflare Tunnel**. Создать туннель в Zero Trust, public hostname → `http://app:8000`, токен в `.env`
  (`CLOUDFLARE_TUNNEL_TOKEN`), запуск `docker compose -f docker-compose.prod.yml --profile tunnel up -d`; Caddy тогда можно не использовать.

## ИИ, токены, стоимость
- Модель по умолчанию `claude-haiku-4-5-20251001` (дёшево). Меняется через `ANTHROPIC_MODEL`; цены для оценки — `LLM_PRICE_IN/OUT` (USD за 1 млн токенов).
- Админка → Übersicht → «KI-Verbrauch»: токены сегодня / 30 дней / всего, оценка стоимости, разбивка по назначению.
- Публичный чат ограничен 12 запросами в минуту и 80 в час на IP. Для жёсткого лимита расходов задать лимит в консоли Anthropic.
- Правила EU AI Act/DSGVO (`AI_ACT_RULES` в `app/services/llm.py`) добавляются в начало system-промпта **каждого** вызова ИИ и приоритетнее любых
  админских «Zusätzliche Anweisungen».

## Aktueller Stand: mainpower.andrii-it.de (Hostinger-VPS srv1425385)
- Auf dem VPS läuft bereits Traefik (host-network, Ports 80/443). Die Plattform nutzt deshalb `docker-compose.traefik.yml`:
  App nur auf `127.0.0.1:8010`, Route per Datei `/opt/traefik-dynamic/mainpower.yml` (Vorlage `deploy/traefik-mainpower.yml`), Zertifikat `letsencrypt`.
- Code: `/srv/mainpower` (git pull, dann `docker compose -f docker-compose.traefik.yml up -d --build`). Secrets: `/srv/mainpower/.env` (chmod 600).
- DNS: A `mainpower` → 187.124.6.120, Cloudflare-Proxy an, `PROXY_HOPS=2`.
- SSH: eigener Schlüssel `~/.ssh/mainpower_deploy` (in hPanel als `mainpower-claude-deploy` hinterlegt) — nach dem Projekt in hPanel löschen.

## Gesetzes-Suche (internes Nachbarprojekt `laws_pipeline`)
- Läuft als eigenes Docker-Compose-Projekt unter `/opt/advokat/laws_pipeline` auf demselben VPS: `qdrant` (Vektordatenbank,
  204.978 Paragraphen aus dem kompletten deutschen Bundesrecht, gesetze-im-internet.de) + `laws-api` (FastAPI-Suche,
  `GET /search`). Beide **ohne öffentlichen Port**, nur im Docker-Netzwerk `laws_net` erreichbar (in deren `docker-compose.yml`
  so eingerichtet). Ein dritter Dienst `laws-worker` hält die Daten per Zeitplan aktuell (täglich RSS-Check, wöchentlich voller
  Refresh) — bewusst NICHT dauerhaft gestartet, um unnötigen Traffic/Kosten zu vermeiden; bei Bedarf manuell:
  `cd /opt/advokat/laws_pipeline && docker compose up -d laws-worker`.
- Die Plattform (`app`-Service) hängt zusätzlich am externen Netzwerk `laws_net` (siehe `docker-compose.traefik.yml`) und ruft
  intern `http://laws-api:8000` auf (`LAWS_API_URL` in `.env`). **Reihenfolge bei Neuaufsetzen:** zuerst
  `laws_pipeline` hochfahren (legt das Netzwerk `laws_net` an), erst danach `docker compose -f docker-compose.traefik.yml up -d`
  für die Plattform — sonst schlägt der Start wegen des fehlenden externen Netzwerks fehl.
- Die Plattform zitiert die Gesetzestexte unverändert (keine KI-Erfindung) und lässt Aiko nur erläutern, was der Text bedeutet —
  nie eine Handlungsempfehlung oder Rechtsberatung (siehe `app/services/laws.py`). Bekannte Einschränkung der Datenbasis:
  vereinzelt Duplikate und thematisch daneben liegende Treffer bei generischen Suchbegriffen; Aiko markiert das im Bericht
  ehrlich als „nicht einschlägig“, statt zu raten.
