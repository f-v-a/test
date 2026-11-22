VENV := .venv
PY := $(VENV)/bin/python

.PHONY: install migrate bot worker test lint

install:
	python3.12 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

migrate:
	$(VENV)/bin/alembic upgrade head

bot:
	$(PY) -m src.bot

worker:
	$(PY) -m src.worker

test:
	$(PY) -m pytest

lint:
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy src tests scripts/spike_ozon.py
