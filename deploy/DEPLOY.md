# Деплой Main Power (Docker)

## Локально
```
docker compose up --build        # http://localhost:8000, Postgres + демо-данные
```
Демо-данные выключаются: `SEED_DEMO=0`. Ключ ИИ и остальное — в `.env` (см. `.env.example`).

## Hostinger VPS (рекомендуется)
1. VPS с Ubuntu 22.04/24.04 и Docker (в Hostinger есть шаблон «Ubuntu + Docker»).
2. DNS: A-запись `ai.main-power.org` → IP VPS (в Cloudflare можно оставить «серое облако» или включить прокси).
3. На сервере:
```
git clone <repo> /srv/mainpower && cd /srv/mainpower
cp .env.example .env      # заполнить: SECRET_KEY, POSTGRES_PASSWORD, DOMAIN, ADMIN_EMAIL/PASSWORD, ANTHROPIC_API_KEY
docker compose -f docker-compose.prod.yml up -d --build
```
   Caddy сам получает сертификат Let's Encrypt (порты 80/443 открыты). Сервис `sync` раз в час тянет события с main-power.org.
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
