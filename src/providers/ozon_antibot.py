"""Классификация ответов антибота Ozon.

Перенесено из открытых реализаций omkarcloud/ozon-scraper (`recipes/ozon.py`,
`ozon/parsers.py::antibot_kind`) и eduard256/shopper-mcp
(`src/marketplaces/ozon/index.ts::detectBlock`).

Антибот Ozon стоит перед ВСЕМИ путями, включая /api/ JSON-шлюз, и выбирает
вариант ответа по репутации IP-выхода, а не по клиенту:

* ``challenge`` — JS proof-of-work (`abt-challenge/script_*.js`, SHA1,
  POST /abt/result). Реальный Chrome проходит его сам за ~5-7 с и
  перезагружает страницу с `?__rr=1&abt_att=1`. Только этот вариант годится
  для получения идентичности (`abt_data`).
* ``captcha`` — слайдер (`abt-challenge/captcha_*`), фингерпринт считается
  байткод-VM. Не решаем: выход отклоняется сразу.
* ``block`` — «Похоже, нет соединения» (`fab_*` / `fab_nmk_*`): жёсткая
  блокировка диапазона/IP. Браузер здесь не помогает — вариант не меняется
  от клиента, проверено живыми пробниками (см. DECISIONS.md).

/api/ пути отдают не HTML-интерстишл, а JSON-инцидент::

    {"incidentId": "fab_chlg_...", "challengeURL": "...", "timeoutSec": 60}

Поэтому JSON разбираем первым: `challengeURL` -> challenge,
`captchaURL` -> captcha, иначе block.
"""

from enum import StrEnum
from typing import Final

#: Сколько символов начала тела считаем «головой» ответа: маркеры JSON-инцидента
#: и открывающий тег HTML всегда в начале, тело challenge-страницы — до 300 КБ.
_HEAD_CHARS: Final = 3000

CHALLENGE_MARKERS: Final = ("abt-challenge/script_", "Antibot Challenge Page")
CAPTCHA_MARKERS: Final = ("abt-challenge/captcha_", "Antibot Captcha")
BLOCK_MARKERS: Final = ("abt-challenge/incidents/images/warn", "нет соединения")

_HTML_MARKERS: Final = ("<html", "<!doctype")
_JSON_INCIDENT_MARKER: Final = '"incidentid"'


class AntibotKind(StrEnum):
    """Вариант ответа антибота. Значения совпадают с классификацией omkarcloud."""

    CHALLENGE = "challenge"
    CAPTCHA = "captcha"
    BLOCK = "block"


def detect_antibot(status: int, body: str) -> AntibotKind | None:
    """Вариант антибота в ответе или None, если это ответ самого сайта.

    None означает «антибота не видно»: это может быть и успешный JSON, и
    обычная ошибка приложения — решать вызывающему коду.
    """
    head = body[:_HEAD_CHARS]
    lowered_head = head.lower()
    if _JSON_INCIDENT_MARKER in lowered_head:
        if "challengeurl" in lowered_head:
            return AntibotKind.CHALLENGE
        if "captchaurl" in lowered_head:
            return AntibotKind.CAPTCHA
        return AntibotKind.BLOCK
    if any(marker in lowered_head for marker in _HTML_MARKERS):
        # Порядок важен: слабая разметка block-страницы встречается и в других.
        if any(marker in body for marker in CAPTCHA_MARKERS):
            return AntibotKind.CAPTCHA
        if any(marker in body for marker in CHALLENGE_MARKERS):
            return AntibotKind.CHALLENGE
        if any(marker in body for marker in BLOCK_MARKERS):
            return AntibotKind.BLOCK
        if status in (403, 429):
            return AntibotKind.BLOCK
    return None


def is_mintable(kind: AntibotKind | None) -> bool:
    """Годится ли выход для получения идентичности браузером.

    Чистый выход (None) отвечает JSON сразу — его cookie тоже рабочая;
    challenge проходится браузером сам. Captcha и block — нет: запускать
    браузер бессмысленно и вредно (лишние запросы флагуют IP сильнее).
    """
    return kind is None or kind is AntibotKind.CHALLENGE


def describe(kind: AntibotKind | None) -> str:
    """Короткое человекочитаемое объяснение для логов и сообщений об ошибке."""
    if kind is None:
        return "антибот не обнаружен"
    if kind is AntibotKind.CHALLENGE:
        return "JS-challenge (проходится браузером)"
    if kind is AntibotKind.CAPTCHA:
        return "слайдер-капча (не решается, нужен другой выход)"
    return "жёсткая блокировка IP (браузер не помогает)"
