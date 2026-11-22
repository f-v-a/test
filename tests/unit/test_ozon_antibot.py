"""Тесты классификации антибота Ozon на реальных фикстурах (без сети).

Фикстуры и их происхождение — tests/fixtures/ozon/README.md.
Важно: `challenge.html`/`challenge_headless.html` прежней сессии фактически
являются block-страницами; классификатор обязан называть их block.
"""

import json
from pathlib import Path

import pytest

from src.providers.ozon_antibot import AntibotKind, describe, detect_antibot, is_mintable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ozon"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("antibot_challenge.json", AntibotKind.CHALLENGE),
        ("antibot_captcha.json", AntibotKind.CAPTCHA),
        ("antibot_block.json", AntibotKind.BLOCK),
        ("antibot_block_nmk.json", AntibotKind.BLOCK),
        ("composer_403.json", AntibotKind.BLOCK),
    ],
)
def test_json_incidents(name: str, expected: AntibotKind) -> None:
    assert detect_antibot(403, _fixture(name)) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("antibot_challenge.html", AntibotKind.CHALLENGE),
        ("antibot_captcha.html", AntibotKind.CAPTCHA),
        ("antibot_block.html", AntibotKind.BLOCK),
        # Старые файлы названы challenge, но содержат block-страницу.
        ("challenge.html", AntibotKind.BLOCK),
        ("challenge_headless.html", AntibotKind.BLOCK),
    ],
)
def test_html_interstitials(name: str, expected: AntibotKind) -> None:
    assert detect_antibot(403, _fixture(name)) is expected


def test_passed_seed_is_not_antibot() -> None:
    assert detect_antibot(200, _fixture("seed_passed.html")) is None


def test_success_product_is_not_antibot() -> None:
    assert detect_antibot(200, _fixture("product_success.json")) is None


def test_plain_404_is_not_antibot() -> None:
    # «Товар не найден» — ответ приложения, не антибота: решает вызывающий код.
    assert detect_antibot(404, "not found") is None


def test_403_html_without_markers_is_block() -> None:
    assert detect_antibot(403, "<html><body>denied</body></html>") is AntibotKind.BLOCK


def test_200_html_without_markers_is_not_antibot() -> None:
    assert detect_antibot(200, "<html><body>ok</body></html>") is None


def test_json_incident_markers_are_case_insensitive() -> None:
    body = '{"IncidentId": "fab_x", "ChallengeURL": "https://..."}'
    assert detect_antibot(403, body) is AntibotKind.CHALLENGE


def test_html_markers_searched_beyond_head() -> None:
    # Тело challenge-страницы до 300 КБ: маркер может быть глубже «головы».
    body = "<html><head>" + "x" * 5000 + "abt-challenge/script_abc.js</head></html>"
    assert detect_antibot(403, body) is AntibotKind.CHALLENGE


def test_captcha_wins_over_challenge_when_both_present() -> None:
    body = "<html>abt-challenge/script_a.js abt-challenge/captcha_b</html>"
    assert detect_antibot(403, body) is AntibotKind.CAPTCHA


def test_success_page_with_incident_word_in_widget_is_not_antibot() -> None:
    # JSON-маркер ищется только в «голове»; тело товара его не содержит.
    page = json.loads(_fixture("product_success.json"))
    assert detect_antibot(200, json.dumps(page, ensure_ascii=False)) is None


@pytest.mark.parametrize(
    ("kind", "mintable"),
    [
        (None, True),
        (AntibotKind.CHALLENGE, True),
        (AntibotKind.CAPTCHA, False),
        (AntibotKind.BLOCK, False),
    ],
)
def test_is_mintable(kind: AntibotKind | None, mintable: bool) -> None:
    assert is_mintable(kind) is mintable


@pytest.mark.parametrize(
    "kind", [None, AntibotKind.CHALLENGE, AntibotKind.CAPTCHA, AntibotKind.BLOCK]
)
def test_describe_nonempty(kind: AntibotKind | None) -> None:
    assert describe(kind).strip()
