"""CI downloads the JDBC driver once per run, and the Spark legs reuse it."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def job(text: str, name: str) -> str:
    """Return the text of one job of a workflow."""
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\n)*)", text, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def target(name: str) -> str:
    """Return the recipe of one Makefile target."""
    match = re.search(rf"^{re.escape(name)}:.*\n((?:\t.*\n)*)", MAKEFILE, re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def test_only_make_pgjdbc_downloads_the_driver() -> None:
    assert MAKEFILE.count("$(PGJDBC_URL)") == 1
    assert "$(PGJDBC_URL)" in target("pgjdbc")
    for name in ("bench", "spark-e2e"):
        assert "\t$(MAKE) --no-print-directory pgjdbc\n" in target(name), name


def test_make_pgjdbc_checks_a_download_before_it_takes_the_jars_name() -> None:
    recipe = target("pgjdbc")
    part = '"$(PGJDBC_SHA256)  $(PGJDBC_JAR).part" | sha256sum --check --strict'
    assert recipe.index("-o $(PGJDBC_JAR).part") < recipe.index(part)
    assert recipe.index(part) < recipe.index("mv $(PGJDBC_JAR).part $(PGJDBC_JAR)")
    assert recipe.endswith(
        '\techo "$(PGJDBC_SHA256)  $(PGJDBC_JAR)" | sha256sum --check --strict\n'
    )
    assert 'if [[ "$(PGJDBC_DOWNLOAD)" != yes ]]; then' in recipe


def test_one_job_downloads_the_driver_for_every_spark_leg() -> None:
    pgjdbc, spark = job(CI, "pgjdbc"), job(CI, "spark-e2e")
    assert "    if: needs.changes.outputs.spark-e2e == 'true'\n" in pgjdbc
    assert "      - run: make pgjdbc\n" in pgjdbc
    upload = re.search(r"upload-artifact@.*\n        with:\n((?:          .*\n)+)", pgjdbc)
    assert upload is not None
    assert upload.group(1) == (
        "          name: pgjdbc\n"
        "          path: .pgjdbc/\n"
        "          include-hidden-files: true\n"
        "          if-no-files-found: error\n"
        "          retention-days: 1\n"
    )
    assert "    needs: [changes, pgjdbc]\n" in spark
    download = "download-artifact@"
    assert spark.index(download) < spark.index("make spark-e2e")
    assert "          name: pgjdbc\n          path: .pgjdbc\n" in spark
    assert re.search(r"^      - run: make spark-e2e .* PGJDBC_DOWNLOAD=no$", spark, re.MULTILINE)
