# CI calls these same targets, so no test command exists only in YAML.
#
#   make check      what a PR runs, minus PostgreSQL and Spark (needs only uv)
#   make check-all  every job that can run locally
#
# py-integration, spark-e2e and bench need a PostgreSQL server or Spark with
# Java. They run in GitHub Actions only; locally they stop with a message.

SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := check
MAKEFLAGS += --no-builtin-rules

# The Python used for local runs and for the lint job. CI overrides it per leg.
PYTHON ?= 3.15
UV_RUN := uv run --frozen --python $(PYTHON)
# The repository scripts use the standard library only and need no project sync.
SCRIPT := uv run --no-project --python $(PYTHON) python
REPORTS := reports
CORE := python/src/skewsplit/core
LOWEST_VENV := .venv-lowest
# Generated Markdown, such as the changelogs release-please writes, is not
# linted: see .gitattributes.
MD_PATHSPEC := ':(attr:!linguist-generated)*.md'
MD_FILES = $(shell git ls-files $(MD_PATHSPEC))
# The branch that diff coverage, changed-file link checks and pr-size compare
# against. CI sets it to the pull request's base.
COMPARE ?= origin/main
# zizmor's online audits need GH_TOKEN; CI clears this to run them.
ZIZMOR_FLAGS ?= --offline

.PHONY: check check-all lint check-filters py-unit coverage packaging docs py-lowest \
	py-integration spark-e2e pgjdbc pr-size pr-hygiene check-fixtures conformance property mutation links \
	bench-core bench clean

check: lint py-unit coverage packaging docs

check-all: check py-lowest conformance

# ruff, mypy, spelling, workflow audits, the issue forms and Dependabot config
# against their schemas, and the golden fixtures. mypy also checks the >>>
# examples of the docstrings and the Markdown files, written out as modules.
lint: check-filters check-fixtures
	$(UV_RUN) ruff check
	$(UV_RUN) ruff format --check
	$(UV_RUN) mypy
	$(SCRIPT) scripts/extract_examples.py $(REPORTS)/examples python/src $(MD_FILES)
	$(UV_RUN) mypy $(REPORTS)/examples
	$(UV_RUN) typos
	$(UV_RUN) actionlint
	$(UV_RUN) zizmor $(ZIZMOR_FLAGS) .github
	$(UV_RUN) check-jsonschema --builtin-schema vendor.github-issue-forms \
		.github/ISSUE_TEMPLATE/bug_report.yml .github/ISSUE_TEMPLATE/feature_request.yml \
		.github/ISSUE_TEMPLATE/task.yml
	$(UV_RUN) check-jsonschema --builtin-schema vendor.github-issue-config \
		.github/ISSUE_TEMPLATE/config.yml
	$(UV_RUN) check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml

# Every tracked file must match a group in .github/filters.yml.
check-filters:
	$(SCRIPT) scripts/check_filters.py

# The golden fixtures against their schema, then against the rules of the spec
# that need no planner. CI runs this in the lint job, on every pull request.
check-fixtures:
	$(UV_RUN) check-jsonschema --check-metaschema spec/schema/fixture.schema.json
	$(UV_RUN) check-jsonschema --schemafile spec/schema/fixture.schema.json spec/fixtures/*.json
	$(UV_RUN) check-jsonschema --check-metaschema spec/schema/pg-array.schema.json
	$(UV_RUN) check-jsonschema --schemafile spec/schema/pg-array.schema.json \
		spec/fixtures/pg-array/*.json
	$(UV_RUN) check-jsonschema --check-metaschema spec/schema/pg-stats.schema.json
	$(UV_RUN) check-jsonschema --schemafile spec/schema/pg-stats.schema.json \
		spec/fixtures/pg-stats/*.json
	$(SCRIPT) scripts/check_fixtures.py

# Unit and property tests with coverage data. Run from a clean data file so a
# stale run cannot hide missing lines. CI sets COVERAGE_FILE per leg.
py-unit:
	mkdir -p $(REPORTS)
	rm -f .coverage .coverage.*
	$(UV_RUN) coverage run -m pytest --junitxml=$(REPORTS)/junit-py-unit.xml

# The A13 gates: 100% line and branch coverage for the pure core, at least 95%
# for the whole package, and 100% of the lines changed since $(COMPARE).
# CI downloads the data from every py-unit leg first; combine merges it.
coverage:
	mkdir -p $(REPORTS)
	if compgen -G '.coverage.*' >/dev/null; then $(UV_RUN) coverage combine; fi
	$(UV_RUN) coverage report --include='$(CORE)/*' --fail-under=100
	$(UV_RUN) coverage report
	$(UV_RUN) coverage xml
	$(UV_RUN) diff-cover $(REPORTS)/coverage.xml --compare-branch=$(COMPARE) --fail-under=100

# Build the sdist and wheel, check their metadata and contents, then install
# the wheel into a clean environment and run it.
packaging:
	rm -rf dist .venv-smoke
	uv build --package skewsplit --out-dir dist
	$(UV_RUN) twine check --strict dist/*
	$(UV_RUN) check-wheel-contents dist/*.whl
	$(SCRIPT) scripts/check_dist.py dist
	uv venv --quiet --python $(PYTHON) .venv-smoke
	uv pip install --quiet --python .venv-smoke dist/*.whl
	.venv-smoke/bin/skewsplit --version
	.venv-smoke/bin/python -c 'import skewsplit; print(skewsplit.__version__)'
	rm -rf .venv-smoke

# Markdown lint, the >>> examples in every Markdown file (pytest exits 5 when a
# run finds no examples, which is fine), relative links everywhere, and
# external links in the Markdown files changed since $(COMPARE).
docs:
	mkdir -p $(REPORTS)
	$(UV_RUN) pymarkdown --config .pymarkdown.json scan $(MD_FILES)
	$(UV_RUN) pytest -m '' -p no:randomly --doctest-glob='*.md' \
		--junitxml=$(REPORTS)/junit-docs.xml $(MD_FILES) || [[ $$? -eq 5 ]]
	$(SCRIPT) scripts/check_links.py $(MD_FILES)
	changed=$$(git diff --name-only --diff-filter=d $(COMPARE)... -- $(MD_PATHSPEC)); \
	if [[ -n "$$changed" ]]; then $(SCRIPT) scripts/check_links.py --external $$changed; fi

# The golden fixtures and the spec invariants, with the ci Hypothesis profile
# (ten times the examples of a plain run). HYPOTHESIS_PROFILE=nightly or fuzz searches longer.
HYPOTHESIS_PROFILE ?= ci
conformance:
	mkdir -p $(REPORTS)
	HYPOTHESIS_PROFILE=$(HYPOTHESIS_PROFILE) $(UV_RUN) pytest -m conformance \
		--junitxml=$(REPORTS)/junit-conformance.xml

# The property tests in PROPERTY_TESTS with $(HYPOTHESIS_PROFILE). nightly.yml
# runs all of them with the nightly profile, and each parser's with fuzz.
PROPERTY_TESTS ?= python/tests/property
property:
	mkdir -p $(REPORTS)
	HYPOTHESIS_PROFILE=$(HYPOTHESIS_PROFILE) $(UV_RUN) pytest -m property \
		--junitxml=$(REPORTS)/junit-property.xml $(PROPERTY_TESTS)

# Mutation testing of the core with mutmut, which nightly.yml runs. mutmut runs
# from python/, where its config is (see python/pyproject.toml), and starts
# from a clean mutants/ so no result of an earlier run is reused. Fails when
# fewer than MUTATION_MIN percent of the tested mutants are killed.
MUTATION_MIN ?= 90
MUTMUT := cd python && $(UV_RUN) --group mutation mutmut
mutation:
	mkdir -p $(REPORTS)
	rm -rf python/mutants
	$(MUTMUT) run
	$(MUTMUT) export-cicd-stats
	cp python/mutants/mutmut-cicd-stats.json $(REPORTS)/mutation.json
	$(MUTMUT) results > $(CURDIR)/$(REPORTS)/mutation-survivors.txt
	$(SCRIPT) scripts/mutation_score.py --min $(MUTATION_MIN) $(REPORTS)/mutation.json

# Every link in every Markdown file, external ones included, which nightly.yml
# runs. The docs target requests only those in the files a change touches.
links:
	$(SCRIPT) scripts/check_links.py --external $(MD_FILES)

# Time the planner micro-benchmarks. CI reports them and never gates on them.
bench-core:
	mkdir -p $(REPORTS)
	$(UV_RUN) pytest -m '' -p no:randomly --benchmark-enable \
		--benchmark-json=$(REPORTS)/benchmark-core.json python/tests/unit/test_planner_benchmarks.py

# The PostgreSQL JDBC driver that bench and spark-e2e give Spark, from Maven
# Central and checked by hash. Dependabot cannot see it, so its version and
# hash are pinned here only.
#
# make pgjdbc puts it in PGJDBC_JAR. A jar already there is used after its hash
# is checked, so a run downloads it only when it is missing. A download is
# checked before it takes the jar's name. With PGJDBC_DOWNLOAD=no, as ci.yml's
# spark-e2e legs pass with the jar from its pgjdbc job, a missing jar is an
# error instead of a request to Maven Central, which has refused some of many
# parallel downloads. curl retries every error.
PGJDBC_VERSION := 42.7.14
PGJDBC_SHA256 := 73914527305a40cce504b0d3d90b23caf565136912d607ae8ae7c5895512332c
PGJDBC_URL := https://repo1.maven.org/maven2/org/postgresql/postgresql/$(PGJDBC_VERSION)/postgresql-$(PGJDBC_VERSION).jar
PGJDBC_JAR := .pgjdbc/postgresql-$(PGJDBC_VERSION).jar
PGJDBC_DOWNLOAD ?= yes
pgjdbc:
	mkdir -p $(dir $(PGJDBC_JAR))
	if [[ ! -f $(PGJDBC_JAR) ]]; then \
		if [[ "$(PGJDBC_DOWNLOAD)" != yes ]]; then echo "$(PGJDBC_JAR) is missing"; exit 1; fi; \
		curl -fsSL --retry 5 --retry-all-errors -o $(PGJDBC_JAR).part $(PGJDBC_URL); \
		echo "$(PGJDBC_SHA256)  $(PGJDBC_JAR).part" | sha256sum --check --strict; \
		mv $(PGJDBC_JAR).part $(PGJDBC_JAR); \
	fi
	echo "$(PGJDBC_SHA256)  $(PGJDBC_JAR)" | sha256sum --check --strict

# The Spark benchmark (bench/README.md): build the skewed table, read it in each
# way, and write $(REPORTS)/RESULTS.md. PySpark goes into its own environment
# from bench/requirements.txt, checked by hash, and Spark gets the driver above.
BENCH_ROWS ?= 15000000
BENCH_PARTITIONS ?= 16
BENCH_RUNS ?= 3
BENCH_PYTHON := 3.14
BENCH_VENV := .venv-bench
bench:
	@if [[ -z "$${PGHOST:-}" || -z "$${JAVA_HOME:-}" ]]; then \
		echo "bench needs PostgreSQL and Java (set PGHOST and JAVA_HOME). It runs in CI only."; \
		exit 1; \
	fi
	[[ "$(BENCH_ROWS)$(BENCH_PARTITIONS)$(BENCH_RUNS)" =~ ^[0-9]+$$ ]]
	mkdir -p $(REPORTS)
	rm -rf $(BENCH_VENV)
	uv venv --quiet --python $(BENCH_PYTHON) $(BENCH_VENV)
	uv pip install --quiet --python $(BENCH_VENV) --require-hashes -r bench/requirements.txt
	$(MAKE) --no-print-directory pgjdbc
	psql -v rows=$(BENCH_ROWS) -f bench/skewed_table.sql
	$(BENCH_VENV)/bin/python bench/run.py --jdbc-jar $(PGJDBC_JAR) \
		--partitions $(BENCH_PARTITIONS) --runs $(BENCH_RUNS) $(REPORTS)/bench.json
	$(SCRIPT) bench/bench_report.py $(REPORTS)/bench.json $(REPORTS)/RESULTS.md

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

# The Spark tests (python/tests/spark/) with one Spark line, SPARK, in an
# environment of their own: the locked project and test tools, then PySpark
# from python/tests/spark/pyspark-$(SPARK)/requirements.txt, checked by hash.
SPARK ?= 4.3
SPARK_VENV := .venv-spark
SPARK_REQUIREMENTS := python/tests/spark/pyspark-$(SPARK)/requirements.txt
# What uv pip installs PySpark from. canary.yml passes a release that is not
# pinned yet, such as SPARK_INSTALL=pyspark==4.4.0.dev1, which has no hash.
SPARK_INSTALL ?= --require-hashes -r $(SPARK_REQUIREMENTS)
spark-e2e:
	@if [[ -z "$${SKEWSPLIT_PG_DSN:-}" || -z "$${JAVA_HOME:-}" ]]; then \
		echo "spark-e2e needs PostgreSQL and Java (set SKEWSPLIT_PG_DSN and JAVA_HOME). It runs in CI only."; \
		exit 1; \
	fi
	mkdir -p $(REPORTS)
	rm -rf $(SPARK_VENV)
	UV_PROJECT_ENVIRONMENT=$(SPARK_VENV) uv sync --frozen --quiet --python $(PYTHON)
	uv pip install --quiet --python $(SPARK_VENV) $(SPARK_INSTALL)
	$(MAKE) --no-print-directory pgjdbc
	SKEWSPLIT_SPARK=$(SPARK) SKEWSPLIT_PGJDBC_JAR=$(abspath $(PGJDBC_JAR)) \
		$(SPARK_VENV)/bin/python -m pytest -m spark --junitxml=$(REPORTS)/junit-spark-e2e.xml

# The size label for the changes since $(COMPARE). PR_BODY names a file with
# the pull request description, which a size/XXL change must justify.
PR_BODY ?=
pr-size:
	$(SCRIPT) scripts/pr_size.py $(COMPARE) HEAD $(PR_BODY)

# The pull request rules, checked against the event that started the workflow.
# Needs GITHUB_EVENT_PATH, GITHUB_REPOSITORY and GH_TOKEN, so it runs in CI only.
pr-hygiene:
	$(SCRIPT) scripts/pr_hygiene.py

clean:
	rm -rf dist $(REPORTS) .coverage .coverage.* .venv-smoke $(LOWEST_VENV) $(BENCH_VENV) $(SPARK_VENV) .pgjdbc \
		.mypy_cache .pytest_cache .ruff_cache .hypothesis .benchmarks python/mutants
