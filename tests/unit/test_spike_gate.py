"""Тесты строгого gate-вердикта спайка (без сети)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.spike_ozon import GATE_THRESHOLD, load_segments, summarize

START = datetime(2026, 11, 22, tzinfo=UTC)


def _write_run(
    tmp_path: Path,
    *,
    records: list[dict[str, Any]],
    hours: float = 24.0,
    completed: bool = True,
    skus: list[str] | None = None,
    threshold: float = GATE_THRESHOLD,
) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    skus = skus if skus is not None else [str(i) for i in range(50)]
    planned_end = START + timedelta(hours=hours)
    manifest = {
        "version": 1,
        "run_id": "test-run",
        "source": "composer",
        "hours": hours,
        "min_interval_seconds": 2.0,
        "threshold": threshold,
        "started_at": START.isoformat(),
        "planned_end_at": planned_end.isoformat(),
        "skus": skus,
        "sku_count": len(skus),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with (run_dir / "gate_log.jsonl").open("w", encoding="utf-8") as log:
        for rec in records:
            log.write(json.dumps({"run_id": "test-run", **rec}) + "\n")
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": "test-run",
                "finished_at": planned_end.isoformat(),
                "completed": completed,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def _good_records() -> list[dict[str, Any]]:
    # 50 SKU, 50 попыток, шаг 1728 с: покрытие 24 ч без разрыва > лимита.
    records = []
    for i in range(50):
        records.append(
            {
                "ts": (START + timedelta(seconds=1728 * i)).isoformat(),
                "sku": str(i),
                "cls": "success",
                "status": 200,
            }
        )
    return records


def test_gate_passes_with_full_coverage_and_24h(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, records=_good_records())
    report = summarize(run_dir)
    assert report["gate_passed"] is True
    assert report["reasons"] == []
    assert report["success_rate"] == 1.0


def test_gate_fails_on_short_window(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, records=_good_records(), hours=0.01)
    report = summarize(run_dir)
    assert report["gate_passed"] is False
    assert "gate_window_too_short" in report["reasons"]


def test_gate_fails_when_not_completed(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, records=_good_records(), completed=False)
    report = summarize(run_dir)
    assert report["gate_passed"] is False
    assert "not_completed" in report["reasons"]


def test_gate_fails_on_missing_sku_success(tmp_path: Path) -> None:
    records = _good_records()
    records[0]["cls"] = "challenge"
    run_dir = _write_run(tmp_path, records=records)
    report = summarize(run_dir)
    assert report["gate_passed"] is False
    assert "success_coverage" in report["reasons"]


def test_gate_fails_on_low_success_rate(tmp_path: Path) -> None:
    records = _good_records()
    for rec in records[:10]:
        rec["cls"] = "challenge"
    run_dir = _write_run(tmp_path, records=records)
    report = summarize(run_dir)
    assert report["gate_passed"] is False
    assert "success_rate" in report["reasons"]


def test_gate_fails_on_coverage_gap(tmp_path: Path) -> None:
    records = _good_records()
    records[10]["ts"] = (START + timedelta(hours=10)).isoformat()
    run_dir = _write_run(tmp_path, records=records)
    report = summarize(run_dir)
    assert report["gate_passed"] is False
    assert "coverage_gap" in report["reasons"]


def test_gate_fails_on_foreign_records(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, records=_good_records())
    with (run_dir / "gate_log.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"run_id": "other", "sku": "0", "cls": "success"}) + "\n")
    report = summarize(run_dir)
    assert report["gate_passed"] is False
    assert "foreign_records" in report["reasons"]


def test_load_segments_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "skus.txt"
    path.write_text("foo-1\nbar-1\n", encoding="utf-8")
    try:
        load_segments(path)
    except ValueError as exc:
        assert "дубликаты" in str(exc)
    else:
        raise AssertionError("expected ValueError")
