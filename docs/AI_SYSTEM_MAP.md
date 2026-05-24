# AI System Map

## Product Model

Бот бесплатно принимает ссылки SoundCloud/YouTube, скачивает медиа через `yt-dlp`/`ffmpeg`, отправляет результат в Telegram или fallback-ссылку, предлагает metadata-редактирование `mp3` и монетизируется сторонней рекламой.

Пользовательских подписок, Premium, оплаты и месячных лимитов в активном runtime нет.

## Main Runtime

- `bot.py` собирает Telegram application, conversation handlers, settings, metadata, admin, ads and download handlers.
- `app/handlers/downloads.py` управляет ссылками, выбором формата, обрезкой, запуском worker, выдачей результата и metadata prompt.
- `app/policy.py` возвращает единую политику: безлимитные запросы и `MAX_MEDIA_DURATION_SECONDS`.
- `app/services/worker.py` делает download/convert/upload fallback.
- `app/handlers/metadata.py` и `app/metadata_store.py` ведут временные metadata-сессии.

## Ads

- `app/ads_store.py` хранит кампании в Redis или local fallback.
- Кампания содержит `ad_id`, `text`, `button_text`, `url`, `advertiser`, `erid`, `enabled`, `weight`, timestamps и `created_by`.
- Реклама не показывается автоматически после скачивания.
- Админ запускает разовую рекламную рассылку командой `/admin_ad_send <ad_id>`.
- Сообщение формируется кодом: `Реклама`, текст, рекламодатель, `erid`, кнопка-ссылка.
- Если активных кампаний нет или частотность не наступила, реклама silently no-op.

## Access And Admin

- `app/access.py` хранит профили, роли, permissions, admin nonce and audit events.
- Пользовательские планы остались только как legacy-поле профиля для совместимости, не влияют на UX/download policy.
- `app/handlers/admin.py` поддерживает RBAC, broadcast, role management and ad management.

## Data Stores

- Redis/local fallback:
  - user settings;
  - user profiles and roles;
  - update/job dedup;
  - admin nonce and audit;
  - metadata sessions;
  - ad campaigns and impression counters.
- Filesystem:
  - temporary downloads;
  - metadata working copies;
  - logs;
  - read-only `cookies.txt`.
- Legacy PostgreSQL payment schema may remain in old deployments but is not required by the current runtime.

## External Integrations

- Telegram Bot API.
- YouTube/SoundCloud through `yt-dlp`.
- `ffmpeg`.
- Optional external file hosting fallback in `app/services/worker.py`.
- Advertiser links configured by admins.

## Privacy/Legal Hotspots

- Telegram user id, language, settings and role are personal data.
- Metadata editing temporarily stores working media copies and title/artist values.
- External file fallback may transfer files to third-party hosting.
- Advertising introduces advertiser links, `erid`, impression counters and frequency state.
