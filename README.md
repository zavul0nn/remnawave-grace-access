# Remnawave Grace Access

Worker для выдачи аварийного временного VPN-доступа пользователям с истекшей или ограниченной подпиской.

Основной сценарий: пользователь забыл продлить VPN, доступ уже закончился или уперся в лимит, а Telegram в его регионе недоступен без VPN. Worker автоматически дает короткое окно доступа, например 3 дня и 1 GiB, чтобы пользователь смог открыть Telegram, бота, канал или поддержку и продлить основную подписку.

Это не биллинг и не полноценная система продления. Это аварийный `grace access` для восстановления связи с пользователем.

## Режимы работы

Worker поддерживает два backend-а:

- `remnawave` - работа напрямую с Remnawave API через SDK.
- `bot` - работа через Bedolaga Bot API.

Backend выбирается переменной:

```env
API_BACKEND=remnawave
```

или:

```env
API_BACKEND=bot
```

## Что делает worker

Worker периодически сканирует пользователей или подписки и обрабатывает статусы:

- `EXPIRED` / `expired` - подписка истекла.
- `LIMITED` / `limited` - подписка ограничена, например из-за лимита трафика.

При обработке worker:

- выбирает служебный squad по статусу;
- выдает доступ на `EXTEND_DAYS` дней;
- добавляет или выставляет указанный squad;
- выдает лимит трафика из `TRAFFIC_LIMIT_BYTES`;
- записывает факт выдачи в SQLite, чтобы не раздавать доступ бесконтрольно.

По умолчанию временный доступ выдается на 3 дня с лимитом 1 GiB.

## Remnawave backend

В режиме `API_BACKEND=remnawave` worker сканирует пользователей Remnawave через SDK.

При обработке пользователя worker:

- заменяет все текущие internal squads пользователя на один служебный squad;
- переводит пользователя в `ACTIVE`;
- выставляет новый `expire_at` на `now_utc + EXTEND_DAYS`;
- ставит `traffic_limit_bytes=TRAFFIC_LIMIT_BYTES`;
- выставляет `traffic_limit_strategy=NO_RESET`;
- сбрасывает использованный трафик через `reset_user_traffic`.

Минимальная конфигурация для Remnawave:

```env
API_BACKEND=remnawave
REMNAWAVE_API_BASE=https://remnawave.example.com/api
REMNAWAVE_API_TOKEN=put-token-here
TARGET_EXPIRED_SQUAD_UUID=e9534880-836d-41bc-9dc4-a453056ad5d1
TARGET_LIMITED_SQUAD_UUID=06e88dc4-6b6e-4db0-8de0-28c68ac12025
```

Дополнительные параметры:

```env
REMNAWAVE_CADDY_TOKEN=
REMNAWAVE_SSL_IGNORE=false
```

## Bedolaga Bot API backend

В режиме `API_BACKEND=bot` worker работает через Bedolaga Bot API.

Используемые endpoints:

- `GET /subscriptions` - сканирование подписок.
- `POST /subscriptions/{id}/extend` - продление подписки на `EXTEND_DAYS` дней.
- `POST /subscriptions/{id}/squads` - добавление служебного squad.
- `DELETE /subscriptions/{id}/squads/{squad_uuid}` - удаление остальных squads.
- `POST /subscriptions/{id}/traffic` - добавление трафика.

Минимальная конфигурация для Bot API:

```env
API_BACKEND=bot
BOT_API_BASE=https://api.example.com
BOT_API_KEY=put-api-key-here
TARGET_EXPIRED_SQUAD_UUID=e9534880-836d-41bc-9dc4-a453056ad5d1
TARGET_LIMITED_SQUAD_UUID=06e88dc4-6b6e-4db0-8de0-28c68ac12025
PAGE_SIZE=200
```

Дополнительные параметры:

```env
BOT_API_SSL_IGNORE=false
```

В Bot API режиме `TRAFFIC_LIMIT_BYTES` конвертируется в целые GiB с округлением вверх. Например `1073741824` станет `1`, а `1500000000` станет `2`.

Если `TRAFFIC_LIMIT_BYTES=0`, worker не вызывает endpoint добавления трафика.

## Функциональный parity между backend-ами

`bot` backend не повторяет Remnawave backend на уровне одинаковых API-полей, потому что Bedolaga Bot API дает другой набор операций. Но он повторяет основную бизнес-логику worker-а.

В обоих режимах worker:

- обрабатывает подписки со статусами `EXPIRED` / `LIMITED`;
- выбирает служебный squad по причине блокировки;
- выдает временный доступ на `EXTEND_DAYS`;
- выдает лимит трафика, заданный через `TRAFFIC_LIMIT_BYTES`;
- хранит состояние в SQLite;
- не выдает новый доступ раньше истечения `EXTEND_DAYS`;
- не доливает трафик сразу, если пользователь снова стал `LIMITED` раньше срока;
- прекращает автопродление, если пользователь ушел из служебного squad;
- не подхватывает пользователей, которых вручную поместили в служебный squad;
- поддерживает `DRY_RUN`.

Отличия backend-ов технические:

- Remnawave backend напрямую выставляет `status`, `expire_at`, `traffic_limit_bytes`, `active_internal_squads` и сбрасывает used traffic.
- Bot API backend вызывает endpoints продления, добавления squad, удаления лишних squads и добавления трафика.
- В Bot API режиме `TRAFFIC_LIMIT_BYTES` конвертируется в GiB, потому что API принимает трафик в `gb`.
- Фактическая дата окончания в Bot API режиме зависит от поведения endpoint-а `/subscriptions/{id}/extend`.

## Логика squads

Для разных причин блокировки можно использовать разные squads:

```text
EXPIRED -> TARGET_EXPIRED_SQUAD_UUID
LIMITED -> TARGET_LIMITED_SQUAD_UUID
```

Это удобно, если для истекших и ограниченных пользователей нужны разные маршруты, ноды, правила или тексты в панели.

Если отдельные squads не нужны, можно задать один общий squad:

```env
TARGET_SQUAD_UUID=e9534880-836d-41bc-9dc4-a453056ad5d1
```

Он будет fallback для всех статусов из `TARGET_STATUSES`.

## Повторное продление

Worker хранит состояние в SQLite и не раздает доступ бесконтрольно.

Если пользователь после выдачи временного доступа остается в одном из служебных squads, worker через `EXTEND_DAYS` дней снова выдаст новый период.

Если пользователь израсходовал лимит раньше срока и снова получил статус `LIMITED`, worker не выдаст новый лимит сразу. Новый период будет только после истечения `EXTEND_DAYS` с прошлого продления.

Если пользователь сам ушел из служебного squad в другой squad, worker больше не продлевает его автоматически. Снова он будет обработан только когда подписка опять попадет в `EXPIRED` или `LIMITED`.

Если пользователь уже находится в служебном squad, но worker раньше не выдавал ему временный доступ и в SQLite нет записи, worker не будет его продлевать. Это защищает от случайного подхвата пользователей, которых туда перенесли вручную.

## Дата продления

В Remnawave режиме worker выставляет дату как:

```text
now_utc + EXTEND_DAYS
```

Старый `expire_at` не увеличивается и не переносится вперед, даже если он был в будущем.

В Bot API режиме worker вызывает endpoint продления подписки. Фактическая дата зависит от реализации Bot API. Если API возвращает `end_date`, worker сохраняет эту дату в SQLite.

## Настройка

Скопируйте пример env-файла:

```bash
cp .env.example .env
```

Основные параметры:

```env
API_BACKEND=remnawave
TARGET_STATUSES=EXPIRED,LIMITED
EXTEND_DAYS=3
TRAFFIC_LIMIT_BYTES=1073741824
SCAN_INTERVAL_SECONDS=60
PAGE_SIZE=500
STATE_DB_PATH=./data/state.sqlite3
DRY_RUN=false
LOG_LEVEL=INFO
```

Ограничения `PAGE_SIZE`:

- Remnawave backend: от `1` до `500`.
- Bot API backend: от `1` до `200`.

Права для Docker volume:

```env
PUID=1000
PGID=1000
```

На Linux-сервере значения можно посмотреть командой:

```bash
id
```

## Запуск

```bash
docker compose up -d --build
```

Логи:

```bash
docker compose logs -f worker
```

Остановка:

```bash
docker compose down
```

## Проверочный режим

Перед реальным запуском включите:

```env
DRY_RUN=true
```

В этом режиме worker пишет в лог, кого он бы обновил, но не вызывает изменяющие API endpoints и не записывает факт продления в SQLite.

Рекомендуемый порядок первого запуска:

1. Запустить с `DRY_RUN=true`.
2. Проверить логи и убедиться, что выбираются правильные пользователи или подписки.
3. Переключить `DRY_RUN=false`.
4. Проверить одного тестового пользователя в панели или боте.

## Docker и SQLite

При запуске entrypoint:

- создает папку для SQLite по `STATE_DB_PATH`;
- выставляет владельца `PUID:PGID`;
- выставляет права `775`;
- запускает Python-процесс не от root.

Файл состояния хранит, кому и когда worker уже выдавал временный доступ. Без этого файла worker не сможет корректно отличать ручные действия от своих прошлых обработок.

## Ограничения

Worker не принимает платежи и не продлевает основную подписку как биллинг. Он только дает короткий служебный доступ.

В Remnawave режиме worker заменяет список internal squads на один служебный squad.

В Bot API режиме worker добавляет служебный squad и удаляет остальные connected squads через API бота.

Перед production-запуском обязательно проверьте поведение на одном тестовом пользователе или подписке.

## Локальные проверки

```bash
python -m pytest -q
python -m compileall -q app tests
```
