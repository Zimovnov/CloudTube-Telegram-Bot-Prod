# Legal Status

## Current Product State

В активном коде бот работает бесплатно для всех пользователей:

- нет Premium, подписок, тарифов и платежного UX;
- нет месячных лимитов запросов;
- есть единый технический максимум длительности медиа — 3 часа;
- есть metadata-редактирование `mp3`;
- монетизация планируется через стороннюю рекламу.

Payment/Robokassa/Stars-код и SQL-таблицы могут оставаться как legacy-след, но новый runtime не должен запускать платежный контур.

## Personal Data

Проект обрабатывает:

- Telegram user id, language code and user settings;
- role/admin profile state;
- admin audit events;
- metadata session data, including temporary file copies and title/artist;
- logs after sanitizer;
- ad frequency state and aggregate impressions.

Если рекламная аналитика расширяется до кликов, UTM, сегментов или персонализации, нужно обновить legal docs before release.

## Advertising

Рекламные сообщения должны быть явно отделены от сервисных сообщений. В v1 бот формирует:

- label `Реклама`;
- рекламный текст;
- рекламодателя;
- `erid`;
- кнопку-ссылку.

Перед реальным размещением рекламы оператору нужно убедиться, что рекламодатель, договоры, erid и отчетность через ОРД/ЕРИР оформлены корректно. Это не заменяет консультацию юриста.

## External Services

Остаются юридически чувствительными:

- Telegram как платформа обмена сообщениями;
- YouTube/SoundCloud как источники контента;
- optional fallback upload to `gofile/file.io`;
- advertiser websites opened by users through ad buttons;
- hosting/VPS/Redis provider.

## User Documents

Legal-шаблоны должны описывать:

- бесплатный характер доступа;
- технические ограничения, включая 3 часа;
- ответственность пользователя за правомерность скачивания;
- рекламу и переходы на сайты рекламодателей;
- обработку персональных данных;
- временное хранение metadata working files;
- fallback transfer to external file hosting, если этот сценарий остается.

## Open Legal Tasks

1. Заполнить реквизиты оператора в legal drafts.
2. Опубликовать постоянные URL для оферты, политики и согласия.
3. Проверить уведомление оператора персональных данных.
4. Настроить процесс маркировки и отчетности интернет-рекламы до коммерческого размещения.
