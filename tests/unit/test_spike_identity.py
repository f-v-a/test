"""Тесты метода идентичности в gate-раннере (без сети, на стабах транспорта)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import spike_ozon
from scripts.spike_ozon import (
    GATE_SOURCES,
    SOURCES,
    IdentityFetcher,
    _should_backoff,
    probe_sources,
    summarize,
)
from src.providers.base import ProviderUnavailable
from src.providers.ozon_provider import product_url
from src.providers.ozon_transport import TransportResponse

START = datetime(2026, 11, 22, tzinfo=UTC)

SUCCESS_BODY = json.dumps(
    {"widgetStates": {"webPrice-1": json.dumps({"price": "1 234 ₽"})}},
    ensure_ascii=False,
)


class StubTransport:
    """Транспорт без сети: записывает URL, отдаёт ответ или бросает ошибку."""

    def __init__(
        self, response: TransportResponse | None = None, error: Exception | None = None
    ) -> None:
        self.response = response or TransportResponse(status=200, body=SUCCESS_BODY)
        self.error = error
        self.urls: list[str] = []
        self.closed = False

    async def get(self, url: str) -> TransportResponse:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.response

    async def aclose(self) -> None:
        self.closed = True


def _fetcher(tmp_path: Path, transport: StubTransport) -> IdentityFetcher:
    return IdentityFetcher(
        SOURCES["identity"],
        proxy=None,
        ca_bundle="",
        profile_dir=tmp_path / "profile",
        transport=transport,
    )


def test_identity_sources_are_gate_sources() -> None:
    assert SOURCES["identity"].kind == "identity"
    assert SOURCES["identity"].inpage is False
    assert SOURCES["identity-inpage"].inpage is True
    assert "identity" in GATE_SOURCES
    assert "identity-inpage" in GATE_SOURCES


def test_fetcher_requests_product_page_via_transport(tmp_path: Path) -> None:
    transport = StubTransport()
    fetcher = _fetcher(tmp_path, transport)
    try:
        status, body, exc = fetcher.request("krossovki-muzhskie-123456789")
    finally:
        fetcher.close()
    assert (status, exc) == (200, None)
    assert body == SUCCESS_BODY
    assert transport.urls == [product_url("123456789")]
    # Путь кодируется целиком, как у веб-клиента: свои ?/& доходят до Ozon нетронутыми.
    assert "oos_search%3Dfalse" in transport.urls[0]


def test_fetcher_maps_provider_unavailable_to_error(tmp_path: Path) -> None:
    transport = StubTransport(error=ProviderUnavailable("mint невозможен: block"))
    fetcher = _fetcher(tmp_path, transport)
    try:
        status, body, exc = fetcher.request("123456789")
    finally:
        fetcher.close()
    assert status == 0
    assert body == ""
    assert exc is not None
    assert exc.startswith("ProviderUnavailable")
    assert "mint невозможен" in exc


def test_fetcher_maps_unexpected_error_to_error(tmp_path: Path) -> None:
    transport = StubTransport(error=RuntimeError("boom"))
    fetcher = _fetcher(tmp_path, transport)
    try:
        status, _, exc = fetcher.request("123456789")
    finally:
        fetcher.close()
    assert status == 0
    assert exc is not None
    assert exc.startswith("RuntimeError")


def test_fetcher_close_closes_transport_and_stops_thread(tmp_path: Path) -> None:
    transport = StubTransport()
    fetcher = _fetcher(tmp_path, transport)
    fetcher.request("123456789")
    fetcher.close()
    assert transport.closed is True
    assert not fetcher._thread.is_alive()


def test_should_backoff_matrix() -> None:
    composer = SOURCES["composer"]
    identity = SOURCES["identity"]
    assert _should_backoff("challenge", None, composer) is True
    assert _should_backoff("error", "TimeoutError", composer) is False
    assert _should_backoff("success", None, identity) is False
    assert _should_backoff("challenge", None, identity) is True
    # Сбой mint/сети на методе идентичности тоже backoff: не долбить заблокированный выход.
    assert _should_backoff("error", "IdentityUnavailable: block", identity) is True


def _write_identity_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    skus = [str(i) for i in range(50)]
    planned_end = START + timedelta(hours=24)
    manifest = {
        "version": 1,
        "run_id": "identity-run",
        "source": "identity",
        "kind": "identity",
        "hours": 24.0,
        "min_interval_seconds": 2.0,
        "threshold": 0.90,
        "started_at": START.isoformat(),
        "planned_end_at": planned_end.isoformat(),
        "skus": skus,
        "sku_count": len(skus),
        "identity_inpage": False,
        "identity_mode": "browser-mint",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with (run_dir / "gate_log.jsonl").open("w", encoding="utf-8") as log:
        for i, sku in enumerate(skus):
            log.write(
                json.dumps(
                    {
                        "run_id": "identity-run",
                        "ts": (START + timedelta(seconds=1728 * i)).isoformat(),
                        "sku": sku,
                        "cls": "success",
                        "status": 200,
                    }
                )
                + "\n"
            )
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": "identity-run",
                "finished_at": planned_end.isoformat(),
                "completed": True,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_gate_passes_for_identity_run(tmp_path: Path) -> None:
    report = summarize(_write_identity_run(tmp_path))
    assert report["source"] == "identity"
    assert report["gate_passed"] is True
    assert report["reasons"] == []


def test_probe_covers_identity_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = StubTransport()
    monkeypatch.setattr(spike_ozon, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(spike_ozon, "_session", lambda *_args: object())
    monkeypatch.setattr(
        spike_ozon,
        "_request",
        lambda *_args: (403, '{"incidentId": "fab_test"}', None),
    )
    monkeypatch.setattr(
        spike_ozon,
        "_identity_fetcher",
        lambda *_args: _fetcher(tmp_path, transport),
    )
    probe_sources(["tovar-123456789"], None, tmp_path / "probe")
    results = json.loads((tmp_path / "probe" / "source_probe.json").read_text(encoding="utf-8"))[
        "results"
    ]
    by_source: dict[str, dict[str, Any]] = {rec["source"]: rec for rec in results}
    assert by_source["identity"]["cls"] == "success"
    assert by_source["identity-inpage"]["cls"] == "success"
    assert by_source["composer"]["cls"] == "challenge"
    assert by_source["product"]["cls"] is None
    assert transport.closed is True


def test_probe_records_identity_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = StubTransport(error=ProviderUnavailable("patchright не установлен"))
    monkeypatch.setattr(spike_ozon, "_sleep", lambda _seconds: None)
    monkeypatch.setattr(spike_ozon, "_session", lambda *_args: object())
    monkeypatch.setattr(
        spike_ozon, "_request", lambda *_args: (403, '{"incidentId": "fab_test"}', None)
    )
    monkeypatch.setattr(
        spike_ozon, "_identity_fetcher", lambda *_args: _fetcher(tmp_path, transport)
    )
    probe_sources(["tovar-123456789"], None, tmp_path / "probe")
    results = json.loads((tmp_path / "probe" / "source_probe.json").read_text(encoding="utf-8"))[
        "results"
    ]
    by_source = {rec["source"]: rec for rec in results}
    assert by_source["identity"]["cls"] == "error"
    assert "patchright не установлен" in by_source["identity"]["error"]
