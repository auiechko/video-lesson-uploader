# Тестування

Підтримувані версії Python: 3.11, 3.12 і 3.13. GitHub Actions запускає Linux
та Windows matrix; Windows 3.12/3.13 окремо захищають Telegram keyring/session
сценарії від платформних регресій.

## Повна локальна перевірка

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest
```

Активація PowerShell venv не потрібна.

## Основні набори

- `test_zoom_preflight.py` — незконвертовані, нестабільні, OneDrive та
  пошкоджені записи; повторна перевірка проблемних папок.
- `test_zoom_recordings.py` — назви Zoom-папок, кілька MP4 і часові сегменти.
- `test_zoom_matching.py` — допуски часу, конфлікти, overlap і split.
- `test_zoom_decisions.py` — повторне використання й анулювання ручних рішень.
- `test_workflow.py` — допустимі переходи й блокування прямого send.
- `test_reconciliation.py` — `PENDING` не стає невідомою доставкою, а
  підтверджені Telegram-повідомлення відновлюють `SENT`.
- `test_persistence.py` — скидання незавершеної історії зберігає `SENT` та
  не зачіпає інші batch.
- `test_zoom_batch.py` — один Calendar event, один album, один caption.
- `test_zoom_revalidation.py` — зміна MP4 після preview та перевірка MP4
  відновленого batch.
- `test_telegram_desktop_service.py` — Calendar revalidation, доступність чату
  й заборона upload до завершення перевірок.

Усі зовнішні інтеграції в unit/integration тестах замінені mock-об’єктами.
Реальні Telegram-повідомлення та Google Calendar записи тести не створюють.
