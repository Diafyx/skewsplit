# CI calls these same targets, so no test command exists only in YAML.
#
#   make check      what a PR runs, minus PostgreSQL and Spark (needs only uv)
#   make check-all  every job that can run locally
#
# py-integration and spark-e2e need a PostgreSQL server or Spark with Java.
# They run in GitHub Actions only; locally they stop with a message.

SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := check
MAKEFLAGS += --no-builtin-rules

# The Python used for local runs and for the lint job. CI overrides it per leg.
PYTHON ?= 3.15
UV_RUN := uv run --frozen --python $(PYTHON)
REPORTS := reports
CORE := python/src/skewsplit/core
WORKFLOWS := $(wildcard .github/workflows/*.yml)
LOWEST_VENV := .venv-lowest

.PHONY: check check-all lint py-unit coverage packaging py-lowest py-integration spark-e2e clean

check: lint py-unit coverage packaging

check-all: check py-lowest

lint:
	$(UV_RUN) ruff check
	$(UV_RUN) ruff format --check
	$(UV_RUN) mypy
	$(UV_RUN) typos
ifneq ($(WORKFLOWS),)
	$(UV_RUN) actionlint
	$(UV_RUN) zizmor --offline .github/workflows
endif

# Unit and property tests with coverage data. Run from a clean data file so a
# stale run cannot hide missing lines.
py-unit:
	mkdir -p $(REPORTS)
	rm -f .coverage .coverage.*
	$(UV_RUN) coverage run -m pytest --junitxml=$(REPORTS)/junit-py-unit.xml

# The A13 gates: 100% line and branch coverage for the pure core, at least 95%
# for the whole package. CI merges the data from every leg before this step.
coverage:
	if compgen -G '.coverage.*' >/dev/null; then $(UV_RUN) coverage combine; fi
	$(UV_RUN) coverage report --include='$(CORE)/*' --fail-under=100
	$(UV_RUN) coverage report
	$(UV_RUN) coverage xml

# Build the sdist and wheel, check their metadata, then install the wheel into
# a clean environment and run it.
packaging:
	rm -rf dist .venv-smoke
	uv build --package skewsplit --out-dir dist
	$(UV_RUN) twine check --strict dist/*
	$(UV_RUN) python scripts/check_dist.py dist
	uv venv --quiet --python $(PYTHON) .venv-smoke
	uv pip install --quiet --python .venv-smoke dist/*.whl
	.venv-smoke/bin/skewsplit --version
	.venv-smoke/bin/python -c 'import skewsplit; print(skewsplit.__version__)'
	rm -rf .venv-smoke

# The oldest allowed versions of every direct dependency, in a separate
# environment so uv.lock is never touched.
py-lowest:
	rm -rf $(LOWEST_VENV)
	uv venv --quiet --python $(PYTHON) $(LOWEST_VENV)
	uv pip install --quiet --python $(LOWEST_VENV) --resolution lowest-direct \
		--group test -e python
	mkdir -p $(REPORTS)
	$(LOWEST_VENV)/bin/python -m pytest -p no:cacheprovider \
		--junitxml=$(REPORTS)/junit-py-lowest.xml

py-integration:
	@if [[ -z "$${SKEWSPLIT_PG_DSN:-}" ]]; then \
		echo "py-integration needs PostgreSQL (set SKEWSPLIT_PG_DSN). It runs in CI only."; \
		exit 1; \
	fi
	mkdir -p $(REPORTS)
	$(UV_RUN) coverage run -m pytest -m integration --junitxml=$(REPORTS)/junit-py-integration.xml

spark-e2e:
	@if [[ -z "$${SKEWSPLIT_PG_DSN:-}" || -z "$${JAVA_HOME:-}" ]]; then \
		echo "spark-e2e needs PostgreSQL and Java (set SKEWSPLIT_PG_DSN and JAVA_HOME). It runs in CI only."; \
		exit 1; \
	fi
	mkdir -p $(REPORTS)
	$(UV_RUN) pytest -m spark --junitxml=$(REPORTS)/junit-spark-e2e.xml

clean:
	rm -rf dist $(REPORTS) .coverage .coverage.* .venv-smoke $(LOWEST_VENV) \
		.mypy_cache .pytest_cache .ruff_cache .hypothesis .benchmarks
