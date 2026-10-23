PYTHON ?= python3
COMPOSE ?= docker compose

.PHONY: help install lint format typecheck test test-unit gates plan-gates contracts up down migrate seed check-parallel

help:
	@echo "targets: install lint format typecheck test gates plan-gates contracts up down migrate seed check-parallel"

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	ruff check app

format:
	ruff format app

typecheck:
	mypy app

test:
	pytest

test-unit:
	pytest app/tests/unit -q

plan-gates:
	$(PYTHON) tools/feature_sets.py validate
	$(PYTHON) tools/feature_sets.py permissions
	$(PYTHON) tools/feature_sets.py coverage
	$(PYTHON) tools/render_plan_tables.py --check
	$(PYTHON) tools/check_parallel_plan.py

check-parallel:
	$(PYTHON) tools/check_parallel_plan.py --s0

contracts:
	$(PYTHON) -m app.contracts.cli lint

gates: plan-gates lint typecheck test

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

migrate:
	$(COMPOSE) run --rm api alembic upgrade head

seed:
	$(COMPOSE) run --rm api $(PYTHON) -m app.seed.cli --tenant system
