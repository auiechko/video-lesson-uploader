# Архітектура

Застосунок розділяє підготовку та надсилання. Telegram-клієнт не створюється,
доки batch не пройшов усі блокувальні ворота.

## Потік даних

1. `config.py` і `desktop_controller.py` зберігають профільні налаштування,
   включно з довільною кореневою папкою Zoom. Секрети зберігає `keyring`.
2. `zoom_preflight.py` сканує всі папки вибраного періоду й перевіряє
   `video*.mp4`: локальність, розмір, стабільність і результат `ffprobe`.
3. `google_calendar.py` читає Calendar, а `calendar_rules.py` класифікує
   `ПАУЗА`, `ВП`, `Перенос`, `(пробне)` та `(без запису)`.
4. `zoom_recordings.py` будує часові сегменти, а `zoom_matching.py` зіставляє
   їх із проведеними уроками та формує діагностику конфліктів.
5. `zoom_decisions.py` і `persistence.py` зберігають ручні рішення разом з
   ідентичністю файлу. Зміна шляху, розміру, тривалості, часткового hash або
   Calendar event анулює рішення.
6. `zoom_batch.py` групує всі MP4 одного `calendar_event_id` в один логічний
   урок і один Telegram media album.
7. `workflow.py` дозволяє лише визначені переходи станів. `desktop.py`
   відображає стан та активує тільки допустимі кнопки.
8. Перед upload `zoom_revalidation.py` повторно перевіряє MP4, Calendar
   звіряється зі snapshot, SQLite резервується, а Telegram-чат перевіряється
   на доступність.

## Незмінні правила

- Один Calendar event — один логічний урок.
- Скасування й паузи ніколи не беруть участі у matching.
- `(без запису)` створює text-only item.
- Невирішена проблема блокує весь batch.
- `SENT` не надсилається повторно.
- API hash не потрапляє у config, SQLite, log, session path або Git.

## Стан workflow

`PERIOD_SELECTED → PREFLIGHT_RUNNING → PREFLIGHT_PASSED → MATCHING_RUNNING →
BATCH_READY → REVALIDATION_RUNNING → SENDING → COMPLETED`.

На проблемних гілках використовуються `PREFLIGHT_BLOCKED`,
`RESOLUTION_REQUIRED`, `BATCH_REVALIDATION_REQUIRED`, `SEND_PAUSED` і
`SEND_FAILED`. Перехід із блокувального стану прямо у `SENDING` заборонений.
