# Main Power — ai.main-power.org (MVP)

Платформа сообщества Main Power (Франкфурт): регистрация, профиль из 4 вопросов Hub, семантический Need/Offer-матчинг, ассистентка Aiko (публичная и персональная), ивенты (синхронизация с main-power.org, запись, создание участниками, Stripe за фиче-флагом), обмен контактами только по opt-in, закрытый чат через Telegram, каталог услуг (чат-боты и кибербезопасность от Andrii-IT) и суперадминка для заказчика.

UI полностью на немецком. Стек: Flask 3, SQLAlchemy, PostgreSQL (локально SQLite), Anthropic API, серверные шаблоны без JS-фреймворка.

## Быстрый старт (локально, без ключей)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # для локали: FLASK_ENV=development, COOKIE_SECURE=0, DATABASE_URL закомментировать
flask init-db && flask seed   # события тянутся с main-power.org/api/events, + 12 демо-участников
flask run                     # http://localhost:5000
pytest -q                     # 6 smoke-тестов, работают офлайн
```

Без ключей всё работает: Aiko отвечает из базы знаний (демо-режим), эмбеддинги считаются локально (хеширование), письма пишутся в лог.

| Вход | Логин | Пароль |
|---|---|---|
| Суперадмин | admin@main-power.local | admin-passwort-bitte-aendern |
| Демо-участник | julia.wagner@demo.main-power.local (и ещё 11) | demo-passwort-123 |

Альтернатива: `docker compose up --build`, затем `docker compose exec app flask init-db && docker compose exec app flask seed` → http://localhost:8000

## Ключи и интеграции

- **ANTHROPIC_API_KEY** — включает Aiko и LLM-обоснования матчей. Модель задаётся через `ANTHROPIC_MODEL`.
- **VOYAGE_API_KEY** (рекомендуется) или **OPENAI_API_KEY** — нормальные мультиязычные эмбеддинги. После смены провайдера: `flask reembed` (или кнопка в админке → Einstellungen).
- **Telegram**:
  1. @BotFather → создать бота, получить токен и username. В `/setprivacy` можно оставить enabled.
  2. Создать супергруппу, добавить бота админом с правами «Пригласительные ссылки» и «Блокировка участников». ID группы (`-100…`) взять, например, через @RawDataBot.
  3. Заполнить `TELEGRAM_*` в `.env`.
  4. Прод: `flask telegram-set-webhook` (нужен HTTPS `BASE_URL`). Локально: `flask telegram-poll`.
  - Логика: привязка аккаунта через deep-link → персональная одноразовая инвайт-ссылка (1 человек, 24 ч) → бот выкидывает из группы всех, кто не привязан к активному аккаунту, и тех, кого заблокировали/удалили. В личке бота отвечает Aiko с контекстом профиля.
- **Stripe** (`STRIPE_ENABLED=1`): Checkout для платных ивентов (Hub 25 €). Webhook: `https://ai.main-power.org/webhooks/stripe`, событие `checkout.session.completed`. Когда флаг выключен, платная запись получает статус `reserved` (оплата на месте).
- **SMTP** — письма: приветствие, сброс пароля, запрос контакта, ответ на запрос, заявка на услугу (уходит на `provider_email` услуги).

## Деплой на Hetzner (как у остальных проектов)

```bash
sudo -u postgres createuser mainpower -P && sudo -u postgres createdb -O mainpower mainpower
git clone … /srv/mainpower && cd /srv/mainpower
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
set -a; . ./.env; set +a
.venv/bin/flask init-db && .venv/bin/flask seed --no-demo
sudo cp deploy/mainpower.service /etc/systemd/system/ && sudo systemctl enable --now mainpower
sudo cp deploy/nginx.conf /etc/nginx/sites-available/ai.main-power.org   # + certbot
crontab deploy/crontab.txt   # синхронизация событий раз в час
```

Порт gunicorn по умолчанию 8010 — поправь, если занят другим проектом. Plausible добавляется одной строкой `<script defer data-domain=… src=…>` в `app/templates/base.html` плюс домен Plausible в CSP (`script-src`, `connect-src`) в `app/__init__.py`.

## Чеклист перед go-live

1. `flask remove-demo`, сменить пароль суперадмина (или создать заказчику аккаунт: `flask create-admin hallo@main-power.org`).
2. `SECRET_KEY`, `COOKIE_SECURE=1`, HTTPS.
3. Datenschutzhinweise (`/datenschutz`) — это черновик, помечен «rechtlich prüfen». Отдать юристу / DSB заказчика: хостер, Anthropic, Voyage/OpenAI, Stripe, Telegram, SMTP.
4. AVV (Auftragsverarbeitungsverträge) с Anthropic, Voyage/OpenAI, Stripe, хостером. Для Telegram AVV нет, поэтому чат опционален и включается только активным действием участника.
5. Проверить с заказчиком тексты согласий на регистрации (`auth/register.html`) и версию `CONSENT_VERSION`.
6. Rate-limit с несколькими воркерами → `RATELIMIT_STORAGE_URI=redis://…`.
7. Бэкапы PostgreSQL.

## Что где

```
app/
  models.py            модель данных (User, Profile, Consent, Event, Registration, Match, IntroRequest, …)
  seed.py              FAQ и тексты с main-power.org, услуги, демо-данные
  services/
    matching.py        эмбеддинги + взаимный скоринг + LLM-обоснования (кэш в matches)
    aiko.py            системные промпты, публичный и персональный режим, фолбэк без ключа
    telegram.py        бот, привязка, инвайты, «страж» группы, polling
    payments.py        Stripe Checkout + webhook
    events_sync.py     импорт из main-power.org/api/events (вырезает Meet-ссылки и PIN)
    gdpr.py            экспорт (ст. 15/20) и удаление (ст. 17)
  blueprints/          public, auth, member (/app), admin (/admin), webhooks
  templates/           Jinja, немецкий UI
  static/              css/app.css (дизайн-система), js/app.js, img/ (с main-power.org)
tests/test_smoke.py    сквозные тесты основных сценариев
```

Подробности для доработки в Claude Code — в `CLAUDE.md`.

## Несколько клубов (SaaS, ветка `white-label`)
Одна установка обслуживает несколько клубов. У каждого клуба свои участники, мероприятия, форматы, FAQ, брендинг, тексты, Impressum/Datenschutz и имя ассистента. Изоляция на уровне строк (`club_id`) и автоматический фильтр для каждого запроса (`app/tenancy.py`). Покрыта тестами `tests/test_multiclub.py`.

Какой клуб показать, определяется по домену:
1. домен из `Club.domains` (собственный домен клуба);
2. иначе `<slug>.<PLATFORM_DOMAIN>`, например `berlin.klubs.andrii-it.de`;
3. иначе клуб по умолчанию, если не выставлен `STRICT_HOSTS=1`.

```bash
flask init-db
```
Этот шаг идемпотентно переводит старую базу с одним клубом в клуб `mainpower`. Проверено на копии прод-БД.

```bash
flask create-club berlin "Founders Berlin" --admin-email chefin@example.de --admin-password '…' --domains klub.example.de
```
Команда создаёт клуб по нейтральному шаблону. Пароль вводится вручную, в репозиторий не записывается.

```bash
flask list-clubs
```

- Консоль оператора: `/plattform`. Вход по `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD` из `.env`; если переменные не заданы, консоль выключена. В ней можно создать клуб, поставить его на паузу, задать домены и тариф, посмотреть статистику и расход ИИ по клубам.
- Админка клуба (роль superadmin): **Klub & Branding** (название, слоган, логотип, цвет, фото, тексты главной, контакты, ссылки на Impressum/Datenschutz, имя ассистента, факты для ИИ, URL синхронизации мероприятий, шаблоны, экспорт/импорт JSON) и **Formate** (форматы встреч как данные).
- Деплой нового клуба:
  - собственный домен: DNS A/CNAME на VPS, Traefik-роутер `Host(...)` для домена, домен вписать в карточку клуба;
  - субдомен платформы: wildcard-DNS `*.PLATFORM_DOMAIN` и wildcard-сертификат (DNS-challenge Cloudflare).
- Общие на всю платформу (пока не настраиваются по клубам): Telegram-бот, SMTP, Stripe, ключ Anthropic.
- Юридически: оператор платформы — обработчик данных, клуб — ответственный. С каждым клубом нужен AVV (Art. 28 DSGVO), а Impressum и Datenschutz клуб заполняет сам.
