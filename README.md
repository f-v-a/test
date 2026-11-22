# README.md — Telegram-бот «Трекер цен на маркетплейсах» (Ozon-first)

> Этот файл — единственный источник правды о продукте для AI-агента и разработчиков.
> Работай строго по майлстоунам (раздел 13). Не расширяй scope без явного указания.
> Если требование неоднозначно — выбери простейшее решение и запиши его в `DECISIONS.md`.
> Если код расходится с этим файлом — обнови файл в том же PR.
> **Docker в проекте не используется. Первый маркетплейс — Ozon.**

---

## 1. Продукт

**Одна строка:** пользователь кидает боту ссылку на товар Ozon (позже — Wildberries),
бот следит за ценой и мгновенно сообщает о снижении или достижении целевой цены.

**Аудитория:** активные покупатели маркетплейсов; реселлеры; люди, ждущие скидку
на конкретную дорогую вещь.

**Ценность:** не нужно вручную открывать приложение и проверять цену — бот сделает
это сам и разбудит уведомлением в нужный момент.

**Бизнес-модель:** freemium + подписка через Telegram Stars (валюта `XTR`).
Цифровые товары внутри Telegram продаются только за Stars — внешние платежи не использовать.

### Тарифы (значения — стартовые, задаются через env)

| Параметр            | FREE          | PLUS            | PRO              |
|---------------------|---------------|-----------------|------------------|
| Товаров на отслежке | 3             | 20              | 100              |
| Интервал проверки*  | 6 часов       | 1 час           | 15 минут         |
| Целевая цена        | да            | да              | да               |
| Глубина истории     | 7 дней        | 90 дней         | 365 дней         |
| Цена                | 0             | 100 Stars/мес   | 270 Stars/мес    |

\* Интервалы — целевые, не SLA. При дефиците пропускной способности фактический
интервал может быть длиннее (см. раздел 14, п. 2). Задержка фиксируется метрикой.

---

## 2. Scope

### Входит в MVP (M0–M3)
- Маркетплейс: **только Ozon**.
- Добавление товара по ссылке или ID; список товаров; удаление; пауза.
- Целевая цена на товар + уведомление о любом снижении.
- Периодические проверки, история цен, уведомления о снижении / цели / рестоке.
- Подписки Stars с автопродлением, лимиты тарифов, пейволл.

### NON-GOALS — агент НЕ делает это без отдельного задания
- Wildberries (до M4), Яндекс Маркет / AliExpress (позже).
- Веб-дашборд, мобильное приложение.
- **Docker и любые контейнеры** — локально и в проде нативные процессы.
- Автопокупка, оформление заказов, авторизация в аккаунтах маркетплейсов.
- Парсинг отзывов, вопросов, рейтингов продавцов.
- ML-прогнозы цен, «предсказание скидок».
- Алерты по цене с Ozon Картой (в MVP — справочно, см. раздел 5).
- Мультиязычность (весь UI — русский; строки централизованы для будущего i18n).
- Реферальная программа, промокоды, A/B-тесты.

---

## 3. Пользовательские сценарии

### Команды
- `/start` — онбординг + главное меню (inline-кнопки).
- `/add` — подсказка «пришлите ссылку или ID товара».
- `/list` — мои товары (карточками, с кнопками).
- `/tariff` — тарифы и оплата.
- `/settings` — тихие часы, часовой пояс (M4; в MVP — дефолты).
- `/help` — справка.

### Flow: добавление товара
1. Пользователь присылает `https://www.ozon.ru/product/krossovki-muzhskie-123456789/`
   (или короткую ссылку `ozon.ru/t/XXXX`, или голый ID `123456789`).
2. Бот извлекает SKU, запрашивает провайдер, показывает карточку:
   название, обычная цена, цена с Ozon Картой (если есть), наличие, фото.
3. Кнопки: `[Следить]` `[Задать цель 🎯]` `[Отмена]`.
4. При превышении лимита тарифа — пейволл (см. ниже), товар не добавляется.
5. Невалидная ссылка / товар не найден — понятная ошибка с примером ссылки.
   Challenge антибота — НЕ «товар не найден»: показать «не удалось получить цену,
   попробуйте позже», товар не добавлять.

### Flow: список
Каждый товар — сообщение: `название — текущая цена (Δ за 7 дней) — цель`,
кнопки: `[🎯 Цель]` `[⏸ Пауза]` `[🗑 Удалить]`. Удаление — с подтверждением.

### Flow: оплата
1. `/tariff` → карточки PLUS/PRO → кнопка `[Оплатить N ⭐/мес]`.
2. `sendInvoice` в личку: `currency="XTR"`, `subscription_period=2592000` (30 дней),
   payload `sub:{tier}:{user_id}`.
3. `pre_checkout_query` → ответить в течение 10 секунд, валидировать payload.
4. `successful_payment` → активировать/продлить тариф, идемпотентно по
   `telegram_payment_charge_id`.
5. Автопродление делает Telegram; каждое списание приходит как `successful_payment`.

### Пейволл (текст-шаблон)
> 🔒 На бесплатном тарифе можно следить за 3 товарами.
> Оформите PLUS — до 20 товаров и проверка каждый час за 100 ⭐/мес.
> `[Оформить PLUS]` `[Сравнить тарифы]`

### Шаблоны уведомлений (хранить в `texts.py`)
- Падение: `📉 Цена упала!\n{title}\n{old} ₽ → {new} ₽ (−{pct}%)\n{card_line}{url}`
- Цель: `🎯 Цель достигнута!\n{title}\nЦена {new} ₽ ≤ вашей цели {target} ₽\n{card_line}{url}`
- Рестоке: `✅ Снова в наличии!\n{title} — {price} ₽\n{url}`
- Товар пропал: `⚠️ Товар недоступен\n{title}\nПроверки приостановлены. {url}`

`{card_line}` = `С Ozon Картой: {card} ₽\n`, если card-цена известна, иначе пустая строка.

---

## 4. Архитектура и стек (без Docker)

```
Telegram ⇄ [bot: aiogram 3] ──┐
                              ├─ PostgreSQL (данные, нативно)
[worker: arq cron] ───────────┤
      │                       └─ Redis (очередь, кэш, rate-limit, нативно)
      └─ fetch → providers (Ozon, ...)
```

Два процесса из одного репо: `bot` (хендлеры, платежи) и `worker` (проверки цен,
рассылка уведомлений, cron-обслуживание). Общаются через БД и Redis.

| Компонент | Выбор | Правила |
|---|---|---|
| Язык | Python 3.12 | только async-код |
| Bot framework | aiogram 3.x | Router на домен, тонкие хендлеры |
| ORM | SQLAlchemy 2 (async) + asyncpg | никаких запросов из хендлеров, только через repo/service |
| Миграции | alembic | обязательны при любом изменении схемы |
| Очередь/cron | arq (Redis) | cron-джоба выбора due-товаров каждые 60 с |
| HTTP | httpx; для Ozon — curl_cffi (TLS-имперсонация) | таймаут 10–20 с, retry/backoff в провайдере |
| Headless (опция) | Playwright | только если spike выберет эту стратегию |
| Конфиг | pydantic-settings | все секреты через env |
| Логи | structlog (JSON) | user_id/product_id в контексте |
| Тесты | pytest + pytest-asyncio + respx | сеть в CI запрещена, только фикстуры |
| Линт/типы | ruff + mypy | CI блокирует при ошибках |
| Инфра | venv + systemd | без контейнеров; PostgreSQL и Redis — нативные сервисы |

### Локальный запуск
```
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# PostgreSQL и Redis установить нативно (apt/brew) или использовать managed
cp .env.example .env   # заполнить
alembic upgrade head
make bot      # терминал 1: python -m src.bot
make worker   # терминал 2: python -m src.worker
```

Makefile: `install`, `migrate`, `bot`, `worker`, `test`, `lint`.
Прод: `deploy/bot.service`, `deploy/worker.service` (systemd), `deploy/backup.timer`
(ежедневный pg_dump). CI (GitHub Actions): ruff + mypy + unit-тесты на фикстурах —
без БД, без сети, без контейнеров.

---

## 5. Провайдеры маркетплейсов

Каждый маркетплейс — адаптер за единым интерфейсом. Бизнес-логика не знает
о деталях fetch/парсинга/антибота.

```python
class PriceProvider(Protocol):
    marketplace: Marketplace  # enum: OZON, WB

    def extract_sku(self, text: str) -> str | None:
        """Ссылка или ID -> SKU. None, если не распознано."""

    async def fetch(self, skus: list[str]) -> dict[str, ProductSnapshot]:
        """Возвращает найденные SKU. Отсутствующие просто не входят в ответ.
        Challenge/блокировка — исключение ProviderUnavailable, НЕ пустой ответ."""


@dataclass(frozen=True)
class ProductSnapshot:
    sku: str
    title: str
    price_kop: int  # обычная цена, ЦЕЛЫЕ копейки. Никаких float.
    card_price_kop: int | None  # цена с Ozon Картой (WB: None)
    in_stock: bool
    image_url: str | None
```

### Ozon (M1, основной)

⚠️ **Главный технический риск проекта — антибот Ozon.** Обычный httpx-запрос
получает 403/challenge. Официального API цен чужих товаров НЕТ (Seller API —
только собственные товары продавца; для трекера не подходит, не использовать).

**M1 начинается со spike (1–2 дня). Лестница стратегий:**
1. **HTTP с TLS-имперсонацией**: `curl_cffi` (`impersonate="chrome"`), браузерные
   заголовки, cookie-jar. Эндпоинт-кандидат (⚠️ UNOFFICIAL, проверить живым запросом):
   `https://api.ozon.ru/composer-api.bx/page/json/v2?url=/product/{slug}-{id}/`
   → JSON с `widgetStates`.
2. **Headless-браузер**: Playwright (+ stealth-настройки) как отдельный fetcher.
   Тяжелее по ресурсам, обычно стабильнее против challenge.
3. **Платный parser-API**: быстрый путь к MVP, себестоимость на запрос.
   Подключать только по явному решению владельца (влияет на тарифы).

**Gate spike (обязателен):** ≥ 90 % успешных fetch на 50 тестовых SKU в течение
24 часов при интервале ≥ 2 с. Gate не пройден → **СТОП**, запись в `DECISIONS.md`,
выбор: другая стратегия / платный API / смена первого маркетплейса.
Не строить M2+ на нестабильном fetch.

Раннер: `scripts/spike_ozon.py`. Логи/манифесты — в `spike_out/` (ignored).
```bash
python scripts/spike_ozon.py --probe --no-proxy         # разовая проверка источников без прокси
python scripts/spike_ozon.py --source composer --hours 24 --no-proxy
python scripts/spike_ozon.py --resume                   # продолжить тот же run
python scripts/spike_ozon.py --report --source composer
```
Источники gate: `composer`, `www-composer`, `entrypoint`, `api-entrypoint`,
`mobile-composer`, `mobile-entrypoint`. `--probe` дополнительно смотрит
`product`/`mobile-product`, но они не являются gate-источниками. Флаг `--no-proxy`
запрещает прокси и игнорирует `OZON_PROXY_URL`; без явного прокси env
`HTTP(S)_PROXY` также отключаются. Smoke (`--hours < 24`) никогда не получает
`gate_passed=true`.

Строгий вердикт требует: 50 SKU, окно ≥ 24 ч, завершённый run, ≥ 90 % успешных
попыток, хотя бы один success по каждому SKU, отсутствие разрывов покрытия больше
`backoff_cap + interval + timeout`. Манифест фиксирует run/source/SKU-лист;
смешивание разных run в одном логу запрещается.

**Ссылки и SKU:**
- `ozon.ru/product/{slug}-{id}/` → ID — последний числовой сегмент:
  regex `/product/(?:[^/]*-)?(\d+)/?`
- legacy: `ozon.ru/context/detail/id/{id}/`
- короткие `ozon.ru/t/{code}` → резолвить редирект (тоже под rate-limit)
- голые цифры → ID товара

**Цены и поля (⚠️ всё подтвердить на spike, записать фикстуры):**
- В `widgetStates` искать ключ `webPrice-*` → значение — JSON-строка с полями
  `price` (обычная), `cardPrice` (с Ozon Картой), `originalPrice`.
- Значения цен — форматированные строки вида «1 234 ₽» с неразрывными пробелами
  ( / ) → `parse_price()` → int копейки. Юнит-тесты парсера обязательны.
- **Продуктовое решение MVP:** алерты и целевая цена — по ОБЫЧНОЙ цене (`price_kop`).
  `card_price_kop` храним и показываем справочно. Выбор «следить за ценой с картой» —
  M4+. Зафиксировать в `DECISIONS.md`.
- Наличие: виджет out-of-stock / флаг доступности — подтвердить на spike.
- Название/фото: `webProductHeading` / og-мета — подтвердить на spike.

**Режим запросов:**
- **Батчинга НЕТ: 1 SKU = 1 запрос** (в отличие от WB). Интерфейс `fetch(skus)`
  сохраняется, внутри — последовательный цикл с rate-limit.
- ≤ 0.5 req/s (1 запрос / 2 с) + джиттер; опциональный прокси `OZON_PROXY_URL`.
- 403 / challenge / 429 → backoff 2^n (кап 30 мин); детект challenge-страницы
  по сигнатуре (фикстура `challenge.html` обязательна); эскалация стратегии — по конфигу.
- **Challenge ≠ отсутствие товара.** `dead` — только явный 404 / «товар не найден».

### Wildberries (M4, второй)
- Публичный эндпоинт (⚠️ UNOFFICIAL, проверить при реализации):
  `https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={sku1;sku2;...}`
- Батч ≤ 100 SKU на запрос; ≤ 1 req/s; цена в `sizes[].price.total` (копейки),
  fallback `salePriceU`; наличие — по суммарным остаткам. Подтвердить фикстурами.
- Ссылки: `wildberries.ru/catalog/(\d+)/detail.aspx`, голый артикул.
- `dest` влияет на региональную цену — один дефолтный регион, ограничение зафиксировать.

### Отказоустойчивость (общая)
- `fail_count >= 10` → статус `unavailable`, уведомить подписчиков один раз,
  проверять раз в 6 ч. Успешный ответ → сброс счётчика, статус `active`.
- Метрика `provider_success_rate` по каждому маркетплейсу с первого дня;
  алерт админу при < 90 % за час.

---

## 6. Модель данных

Все деньги — `INTEGER` в копейках. Все timestamps — `TIMESTAMPTZ`, UTC.

```
users            id, tg_id UNIQUE, username, first_name, tz (default Europe/Moscow),
                 is_blocked_bot, created_at
subscriptions    id, user_id FK, tier ENUM(free/plus/pro), started_at, expires_at,
                 auto_renew, status
products         id, marketplace ENUM, sku, url, title, image_url,
                 last_price_kop, prev_price_kop, card_price_kop NULL, in_stock,
                 status ENUM(active/unavailable/dead), fail_count,
                 last_checked_at, next_check_at,
                 UNIQUE(marketplace, sku)
tracks           id, user_id FK, product_id FK, target_price_kop NULL,
                 notify_any_drop bool DEFAULT true, notify_restock bool DEFAULT true,
                 is_active bool, created_at, UNIQUE(user_id, product_id)
price_history    id, product_id FK, price_kop, card_price_kop NULL, in_stock, recorded_at,
                 INDEX(product_id, recorded_at DESC)
payments         id, user_id FK, telegram_charge_id UNIQUE, stars_amount,
                 tier, period_days, status, created_at
notifications_log id, user_id FK, product_id FK, kind ENUM(drop/target/restock/dead),
                 old_price_kop, new_price_kop, sent_at
events           id, user_id NULL, name, payload JSONB, created_at
```

Тариф пользователя = активная подписка (`expires_at > now()`), иначе FREE.
Товар общий: 1000 пользователей могут следить за одним SKU — строка в `products` одна,
fetch тоже один (критично для Ozon с его per-SKU запросами).

---

## 7. Worker: проверка цен

Cron каждые 60 секунд:
1. Выбрать товары с `next_check_at <= now()` и статусом, требующим проверки.
   Интервал товара = **минимальный интервал среди тарифов его активных подписчиков**
   (один PRO-подписчик ускоряет проверки товара для всех).
2. Сгруппировать по маркетплейсу. Ozon — последовательно per-SKU с rate-limit;
   WB (M4) — батчами. Кэш Redis (`price:{marketplace}:{sku}`, TTL 60 с) —
   защита от дублей в окне цикла.
3. Запросить провайдеры с rate-limit и backoff. `ProviderUnavailable` →
   инкремент `fail_count`, НЕ менять цену/наличие, перенести `next_check_at`.
4. Обновить `products`, записать `price_history`: при изменении цены/наличия
   ИЛИ если последняя запись старше 24 ч (heartbeat).
5. Поставить задачи на уведомления (раздел 8). Выставить `next_check_at`.
6. Ночной cron: очистка `price_history` глубже лимита тарифа; даунгрейд истёкших
   подписок (grace 24 ч) + уведомление; пересчёт `next_check_at`.

Worker — в единственном экземпляре; распределённый лок через Redis на джобу.

---

## 8. Правила уведомлений

- **Падение:** `new < old` и `(old − new) ≥ max(1000 коп, 1 % от old)`.
  Шлёт всем трекам с `notify_any_drop=true`. Только по `price_kop` (обычная цена);
  card-цена в MVP алертов не триггерит.
- **Цель:** `new ≤ target_price_kop`. Трек остаётся активным (кулдаун защищает от спама).
- **Рестоке:** `in_stock: false → true`, треки с `notify_restock=true`.
- **Кулдаун:** не чаще 1 уведомления данного `kind` по паре (user, product) за 6 ч —
  проверка по `notifications_log`.
- **Тихие часы:** 22:00–09:00 в tz пользователя. Отложенные алерты копятся и утром
  уходят одним дайджест-сообщением на пользователя.
- Несколько алертов одному юзеру в одном цикле — объединять в одно сообщение.
- `TelegramForbiddenError` (бот заблокирован) → `users.is_blocked_bot=true`,
  треки на паузу; `/start` реактивирует.
- Все отправленные алерты писать в `notifications_log` и `events`.

---

## 9. Платежи и подписки (Stars)

- Валюта только `XTR`; `provider_token` не нужен.
- Подписка: `sendInvoice(..., subscription_period=2592000)` в личный чат.
  Разовые покупки в MVP не продаём.
- `pre_checkout_query`: ответить ≤ 10 с; невалидный payload → отказ с текстом.
- `successful_payment`: идемпотентность по `telegram_payment_charge_id`
  (повторный апдейт не должен продлевать срок дважды). Продление:
  `expires_at = max(now(), expires_at) + 30 дней`.
- Возвраты: `refundStarPayment` — только админ-команда, логируется.
- Лимиты тарифов проверяются в service-слое (`TrackService.can_add`), не в хендлерах.
- Даунгрейд при истечении: треки сверх лимита FREE → `is_active=false`
  (самые старые продолжают работать), уведомить со списком приостановленных.
- Тесты платежей — против мок-апдейтов Telegram; реальные — в test-режиме Bot API.

---

## 10. Структура репозитория

```
price-tracker-bot/
├── README.md               # этот файл
├── DECISIONS.md            # журнал принятых решений (агент ведёт сам)
├── pyproject.toml
├── Makefile
├── .env.example
├── alembic/
├── deploy/                 # bot.service, worker.service, backup.timer (systemd)
├── src/
│   ├── core/               # config.py, logging.py
│   ├── db/                 # models.py, repo/ (по домену)
│   ├── providers/          # base.py, ozon.py, (wildberries.py — M4)
│   ├── services/           # tracking.py, pricing.py, notify.py, billing.py
│   ├── bot/                # handlers/, keyboards/, middlewares/, texts.py, filters.py
│   └── worker/             # jobs.py, scheduler.py
└── tests/
    ├── fixtures/ozon/      # записанные JSON + challenge.html + 404
    ├── unit/
    └── integration/
```

## 11. Конфигурация (.env.example)

```
BOT_TOKEN=
DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/app
REDIS_URL=redis://localhost:6379/0
ADMIN_IDS=123456789
LOG_LEVEL=INFO
TZ_DEFAULT=Europe/Moscow
OZON_STRATEGY=http            # http | headless | api — выбирается по итогам spike
OZON_RPS=0.5
OZON_PROXY_URL=               # опционально
OZON_API_KEY=                 # только для strategy=api
FREE_TRACK_LIMIT=3
PLUS_TRACK_LIMIT=20
PRO_TRACK_LIMIT=100
PLUS_STARS=100
PRO_STARS=270
QUIET_FROM=22
QUIET_TO=9
FEATURE_WB=false
```

## 12. Конвенции для агента

- Async везде; никакого блокирующего I/O в bot-процессе (headless — отдельный пул/процесс).
- Полные type hints; `mypy` и `ruff` зелёные; форматирование `ruff format`.
- Деньги — int копейки; время — UTC; конвертация в tz только на отображении.
- Все пользовательские строки — в `bot/texts.py`, без хардкода в хендлерах.
- Хендлер → сервис → репозиторий. SQL и fetch из хендлеров запрещены.
- Любое изменение схемы = alembic-миграция в том же коммите.
- Тесты обязательны для: правил алертов, лимитов, биллинга, парсинга ссылок,
  `parse_price()`, детекта challenge, провайдеров (на фикстурах). Сеть в CI запрещена.
- **Никаких контейнеров** в скриптах, инструкциях и CI.
- Один майлстоун = отдельная ветка/PR; коммиты в стиле Conventional Commits.
- Неоднозначности и компромиссы фиксировать в `DECISIONS.md` (дата, решение, причина).
- Не добавлять зависимости без необходимости; не рефакторить чужой scope.

## 13. Майлстоуны и Definition of Done

### M0 — Каркас (0.5–1 день)
- [x] venv + нативные PostgreSQL/Redis; `make bot` и `make worker` стартуют; `/start` отвечает.
- [x] Конфиг, structlog, alembic init, Makefile, CI (ruff+mypy+pytest) зелёный. Без Docker.

### M1 — Spike Ozon + провайдер + треки (3–5 дней)

Текущий статус: **gate не пройден**, публичные источники из песочницы отвечают
`403`. Подготовлены чистые помощники Ozon и строгий gate-раннер; трекинг/алерты
не включены до подтверждённого стабильного fetch.

- [ ] Spike проведён, стратегия выбрана, **gate ≥ 90 % на 50 SKU / 24 ч пройден**,
      результат в DECISIONS.md. Не пройден — стоп, эскалация владельцу.
- [ ] Фикстуры записаны: успешный ответ, challenge, 404. Формат полей подтверждён.
- [ ] Добавление по полной ссылке, короткой ссылке, legacy-ссылке и по ID;
      карточка товара с двумя ценами и наличием.
- [ ] `/list` с кнопками (цель/пауза/удалить), лимит FREE enforced.
- [ ] Юнит-тесты: extract_sku, parse_price, детект challenge; контракт-тесты провайдера.

### M2 — Проверки и алерты (2–3 дня)
- [ ] Cron выбирает due-товары по мин-интервалу тарифов; per-SKU rate-limit; backoff;
      challenge не меняет цену и не помечает dead.
- [ ] История пишется по политике change-or-heartbeat.
- [ ] Алерты drop/target/restock с кулдауном, тихими часами и утренним дайджестом.
- [ ] Юнит-тесты всех правил; интеграционный прогон 500 товаров на моках.

### M3 — Деньги (2–3 дня)
- [ ] Подписка Stars: инвойс → pre_checkout → successful_payment → активный тариф.
- [ ] Идемпотентность платежей; автопродление; даунгрейд с приостановкой треков.
- [ ] Пейволл при лимите; `/tariff`; тесты биллинга на мок-апдейтах.

### M4 — Wildberries и полировка (3–5 дней)
- [ ] WB-адаптер: батчи, фикстуры, контракт-тесты; FEATURE_WB=true.
- [ ] PNG-график истории цены по кнопке (matplotlib, вне event loop).
- [ ] `/settings`: тихие часы, часовой пояс.
- [ ] (Опционально, по решению владельца) алерты по цене с Ozon Картой.

### M5 — Эксплуатация и запуск (2–3 дня)
- [ ] Админ `/stats`: юзеры, треки, MRR в Stars, success rate провайдеров, latency алертов.
- [ ] Sentry; systemd-юниты на VPS; backup.timer (pg_dump); runbook в README.
- [ ] Событийная аналитика: start, product_added, alert_sent, paywall_shown, payment.

**MVP с первыми деньгами = M0–M3, ~8–12 рабочих дней.**

## 14. Риски и ограничения (читать перед работой)

1. **Антибот Ozon — главный риск проекта.** Может ужесточиться в любой момент.
   Митигация: gate в M1, лестница стратегий, изоляция в адаптере, контракт-тесты,
   мониторинг success rate с первого дня. Если стабильность недостижима —
   честный pivot: платный parser-API или WB первым. Не замалчивать проблему.
2. **Пропускная способность.** Ozon: 1 SKU = 1 запрос, 0.5 rps ≈ 43 тыс. запросов/сутки
   на один IP. PRO-интервал 15 мин = 96 проверок/товар/сутки → потолок ~400–450
   PRO-товаров на IP. Рычаги: прокси-пул, удлинение интервалов, платный API.
   Общий SKU на всех подписчиков — использовать обязательно.
3. **Две цены Ozon.** Алерты в MVP по обычной цене; card-цена справочно.
   Возможны вопросы пользователей — объяснить в `/help`, решение в DECISIONS.md.
4. **ToS / юрисдикция.** Только публичные данные, без авторизации, щадящий rate-limit.
   Антибот — явное противодействие площадки; оценить риски до публичного запуска.
5. **Экономика Stars.** Комиссии сторов зашиты в цену; при strategy=api себестоимость
   запроса заложить в тарифы до запуска продаж.
6. **Масштаб worker.** До ~5 тыс. товаров сFREE/PLUS-интервалами один worker справляется;
   PRO-heavy нагрузка упирается в п. 2, а не в CPU.

## 15. Reference links (проверять актуальность при реализации)

- Bot Payments (Stars): https://core.telegram.org/bots/payments-stars
- Bot API (subscriptions, sendInvoice): https://core.telegram.org/bots/api
- aiogram 3 docs: https://docs.aiogram.dev
- curl_cffi (TLS-имперсонация): https://github.com/lexiforest/curl_cffi
- Playwright Python: https://playwright.dev/python/
- Ozon composer endpoint: см. раздел 5 (unofficial, верифицировать на spike)
- WB card endpoint: см. раздел 5 (unofficial, верифицировать при M4)