# HTTPS новых клиентов LoyalUP

При создании клиента через админку, leads или другой путь сохраняется
`public.clients_domain`. Host-service проверяет эту таблицу раз в минуту и
расширяет **действующий** сертификат `levelupapp.ru`, сохраняя все его SAN,
включая `www`/`vkapp` и имена, отсутствующие в таблице тенантов. Django не
получает доступ к certbot, root, docker socket или приватным ключам.

Новый одноуровневый `*.levelupapp.ru` включается только после того, как **все**
его A/AAAA адреса указывают на разрешённые IP сервера. `api-ya.levelupapp.ru`
явно исключён: обслуживается другим сервером. Внешние и вложенные домены
нуждаются в отдельной настройке. DNS создаётся отдельно; сервис ждёт его
готовности, а не меняет DNS. Чтение БД идёт напрямую через отдельный read-only
psql сеанс, без Django/Celery и без записи бизнес-данных.

Обычно сертификат появляется в течение минуты после готовности DNS. Между
успешными выпусками выдерживается 15 минут, чтобы объединять близкие создания;
после ошибки — час. Общий предел — 40 попыток за скользящие 7 суток. Это
внутренний запас по квоте, а не гарантия доступности квоты CA для других систем.
При 100 SAN сервис останавливает расширение и требует отдельного сертификата.
Он не удаляет старые имена. Удаление клиента тоже не сужает сертификат.

Перед каждым выпуском проверяется nginx и сохраняется root-only копия nginx,
lineage live/archive/renewal в `/var/lib/loyalup-tenant-ssl/backups/`. Затем:
`certbot certonly --nginx --non-interactive --cert-name levelupapp.ru --expand`
с полным списком старых и новых имён; проверка всех SAN; `nginx -t` и
`systemctl reload nginx`. Неудачный reload повторяется без нового заказа CA.
Штатное продление выполняет существующий `certbot.timer` с nginx installer.

## Установка из проверенного git-коммита (root на production host)

Проверить отсутствие tracked drift и актуальность IP в example config.
Копии приложения/БД не перезапускать: меняются только host-service и timer.

```sh
install -d -m 0755 /usr/local/libexec
install -o root -g root -m 0755 ops/tenant_ssl.py /usr/local/libexec/loyalup-tenant-ssl.py
# При обновлении сохранять рабочий конфиг; example используется только при первой установке.
test -f /etc/loyalup-tenant-ssl.json || install -o root -g root -m 0600 ops/tenant_ssl.example.json /etc/loyalup-tenant-ssl.json
install -o root -g root -m 0644 ops/systemd/loyalup-tenant-ssl.service /etc/systemd/system/
install -o root -g root -m 0644 ops/systemd/loyalup-tenant-ssl.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/loyalup-tenant-ssl.service /etc/systemd/system/loyalup-tenant-ssl.timer
python3 /usr/local/libexec/loyalup-tenant-ssl.py --check
systemctl daemon-reload
systemctl start loyalup-tenant-ssl.service
systemctl enable --now loyalup-tenant-ssl.timer
```

## Проверка и диагностика

```sh
python3 -m unittest discover -s ops -p 'test_tenant_ssl.py' -v
python3 /usr/local/libexec/loyalup-tenant-ssl.py --check
systemctl list-timers loyalup-tenant-ssl.timer
journalctl -u loyalup-tenant-ssl.service --since '1 hour ago'
curl -I https://NEW.levelupapp.ru/admin/
```

`--check` только читает данные/сертификат и выводит план. `waiting_dns` — DNS ещё
не готов; `deferred` — интервал/бюджет; ошибки сертификата и nginx видны в journal.
Для проверки не создавать настоящего клиента: offline-тесты имитируют события
создания, DNS, ошибки CA и nginx и не ходят в сеть/БД.

Отключение автоматизации: `systemctl disable --now loyalup-tenant-ssl.timer`.
Уже выпущенный сертификат и certbot renewal остаются действующими.

## Исправление 04.10.2026

Добавлен `asap-novocheb.levelupapp.ru`: сохранены все 15 прежних имён,
итого 16 SAN; срок нового сертификата — 02.01.2027. Staging dry-run,
nginx test/reload и внешний HTTPS без обхода TLS-проверки прошли.
Закрытая исходная копия: `/root/loyalup-ssl-backups/20261004T180640Z`.
