# Сборщик роликов студии (19.10.2026)

Отдельный процесс рядом с HyperFrames, Chrome и FFmpeg (в контейнере `web`
их нет). Берёт версию ролика из очереди LoyalUP, собирает MP4 в четырёх
форматах (9:16, 1:1, 16:9, короткий 6 с) и отправляет обратно.

- `compose.py` — сценарий → `index.html` HyperFrames (без сети и случайности);
- `worker.py` — очередь: `claim` → материалы → рендер → обложка → `output` → `done`;
- `fonts/Manrope.ttf` — шрифт с кириллицей (OFL, лицензия рядом);
- `studio-worker.service` — служба пользователя systemd.

Настройка — переменные окружения в начале `worker.py`. Токен: файл
`~/.config/loyalup-workstation/studio-worker.token` (0600), то же значение —
`STUDIO_WORKER_TOKEN` в `.env` LoyalUP. Пустой токен на стороне LoyalUP =
очередь выключена (503).

Проверка без сервера: `python3 worker.py --local job.json OUT_DIR` — в
`job.json` у материалов `url` = локальный путь к файлу.

Рендер на VPS без GPU: 9:16 на 9 секунд — около минуты, формат за форматом,
`--workers 1` (память 8 ГБ общая с остальной работой).
