# Фикстуры Ozon

Сеть в тестах запрещена (README §12), поэтому провайдер проверяется на
записанных ответах. Здесь — только реальные тела ответов, без правок.

## Происхождение

| Файл | Что это | Откуда |
|---|---|---|
| `product_success.json` | pdp товара 1574782712 в наличии (`webPrice.isAvailable=true`, есть `cardPrice`) | omkarcloud/ozon-scraper @ `a1edb97`, `ozon/fixtures/product_1574782712.json.gz` (MIT), распаковано без изменений |
| `product_out_of_stock.json` | pdp товара 3755501937 распродан (`webOutOfStock`, в `webPrice` нет `isAvailable`) | там же, `product_out_of_stock_3755501937.json.gz` |
| `product_unknown.json` | несуществующий SKU 1: pdp с виджетом `error` вместо карточки | там же, `product_unknown_1.json.gz` |
| `antibot_challenge.html` | JS proof-of-work интерстишл (`abt-challenge/script_*`) | там же, `ozon/fixtures/antibot_challenge.html` |
| `antibot_captcha.html` | слайдер-капча (`abt-challenge/captcha_*`) | там же, `antibot_captcha.html` |
| `antibot_block.html` | «Похоже, нет соединения» — жёсткая блокировка IP | там же, `antibot_block.html` |
| `antibot_challenge.json` | JSON-инцидент `fab_chlg_*` с `challengeURL` (так отвечает /api/) | там же, `antibot_challenge.json` |
| `antibot_captcha.json` | JSON-инцидент `fab_cp_*` с `captchaURL` | там же, `antibot_captcha.json` |
| `antibot_block.json` | JSON-инцидент `fab_*` с `blockURL` | там же, `antibot_block.json` |
| `antibot_block_nmk.json` | JSON-инцидент `fab_nmk_*`, записан живым запросом сида `summaryV2` из песочницы 2026-11-22 (HTTP 403) | эта сессия |
| `challenge.html`, `challenge_headless.html` | записаны предыдущей сессией спайка; **фактически это block-страница**, а не challenge (см. ниже) | предыдущая сессия |
| `composer_403.json` | JSON-инцидент `fab_*` на `api.ozon.ru/composer-api.bx` | предыдущая сессия |
| `seed_passed.html` | **синтетическая** страница успешного сида (`<pre>{}</pre>`): живьём из песочницы не получить, вариант block | эта сессия, помечена как синтетическая |

Лицензия исходных фикстур — MIT, Copyright (c) 2026 Chetan Jain
(omkarcloud/ozon-scraper). Использованы как тестовые данные с указанием
источника; код провайдера написан независимо.

## Важно про именование

`challenge.html` и `challenge_headless.html`, записанные ранее, содержат
`<title>Похоже, нет соединения</title>` и `abt-challenge/incidents/images/warn`
— по классификации антибота это **block**, а не challenge. Прежние записи
DECISIONS.md называли их challenge, из-за чего блокировка IP выглядела как
«решаемая браузером задача». Файлы оставлены для совместимости существующих
тестов; новые тесты используют корректно названные `antibot_*`.

## Чего здесь нет

Успешного ответа, записанного **из нашей инфраструктуры**: с egress песочницы
антибот отдаёт вариант block на всех проверенных путях и в реальном Chromium
(см. DECISIONS.md, записи о mint-пробниках). Формат полей подтверждён
фикстурами omkarcloud, но gate (≥ 90 % на 50 SKU / 24 ч) требует прогона с
российского egress — там же будет записана наша success-фикстура.
