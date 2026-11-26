# Changelog

## [0.2.1](https://github.com/Diafyx/skewsplit/compare/python-v0.2.0...python-v0.2.1) (2026-11-26)


### Bug Fixes

* **core:** print inf in the skew report for a planning time too large for a float ([0d6d649](https://github.com/Diafyx/skewsplit/commit/0d6d649ad3968a7d2b2881e3dddb19af41fad7c7))

## [0.2.0](https://github.com/Diafyx/skewsplit/compare/python-v0.1.0...python-v0.2.0) (2026-11-22)


### Features

* **core:** plan hash predicates for columns without a usable order ([#79](https://github.com/Diafyx/skewsplit/issues/79)) ([6da21ef](https://github.com/Diafyx/skewsplit/commit/6da21efc72365782daea5357b0f4b3f87f8da12f)), closes [#27](https://github.com/Diafyx/skewsplit/issues/27)
* **postgres:** choose the strategy automatically and say why ([#80](https://github.com/Diafyx/skewsplit/issues/80)) ([8797f5b](https://github.com/Diafyx/skewsplit/commit/8797f5b17a0e4cf4ac575ef970b78dc7bedda2ec)), closes [#28](https://github.com/Diafyx/skewsplit/issues/28)
* **postgres:** plan and verify a filtered read with where ([#83](https://github.com/Diafyx/skewsplit/issues/83)) ([e714669](https://github.com/Diafyx/skewsplit/commit/e7146699d980bc8ec4b8a0961fb7a4e2c17242bd)), closes [#30](https://github.com/Diafyx/skewsplit/issues/30)
* **postgres:** plan from a sample of the table with TABLESAMPLE ([#76](https://github.com/Diafyx/skewsplit/issues/76)) ([9b92c5d](https://github.com/Diafyx/skewsplit/commit/9b92c5dc8054c54d9c868ea032047b5838326f41)), closes [#25](https://github.com/Diafyx/skewsplit/issues/25)
* **postgres:** plan from exact quantiles of every row ([#78](https://github.com/Diafyx/skewsplit/issues/78)) ([748dcb5](https://github.com/Diafyx/skewsplit/commit/748dcb55a767942e7d5a382da721732bffd4383e))

## 0.1.0 (2026-11-21)


### Features

* **cli:** add the plan, verify and stats commands ([#66](https://github.com/Diafyx/skewsplit/issues/66)) ([69509f3](https://github.com/Diafyx/skewsplit/commit/69509f395bccd865e8096a6d46db672ef7d6d7f8)), closes [#20](https://github.com/Diafyx/skewsplit/issues/20)
* **core:** add value codecs for integer, numeric, date and timestamp types ([#53](https://github.com/Diafyx/skewsplit/issues/53)) ([2ea8138](https://github.com/Diafyx/skewsplit/commit/2ea8138f0a56e6de6f77d7c60577ea8cce62af2f)), closes [#11](https://github.com/Diafyx/skewsplit/issues/11)
* **core:** convert pg_stats statistics into the planner input ([#62](https://github.com/Diafyx/skewsplit/issues/62)) ([dce74a0](https://github.com/Diafyx/skewsplit/commit/dce74a00617340ece74b2fe00c9b452de584d880)), closes [#17](https://github.com/Diafyx/skewsplit/issues/17) [#61](https://github.com/Diafyx/skewsplit/issues/61)
* **core:** parse PostgreSQL array text from pg_stats ([#59](https://github.com/Diafyx/skewsplit/issues/59)) ([fe36fbc](https://github.com/Diafyx/skewsplit/commit/fe36fbc7e98b727fe03ac412de6bb3c1947fe3f1)), closes [#15](https://github.com/Diafyx/skewsplit/issues/15)
* **core:** plan split points from MCVs and histogram ([#55](https://github.com/Diafyx/skewsplit/issues/55)) ([2df4ba7](https://github.com/Diafyx/skewsplit/commit/2df4ba7591a0b7e25558b13b6caed46aa02da20d)), closes [#12](https://github.com/Diafyx/skewsplit/issues/12)
* **postgres:** find the column to split and add the py-integration job ([#60](https://github.com/Diafyx/skewsplit/issues/60)) ([42e8940](https://github.com/Diafyx/skewsplit/commit/42e89409835b71deae9a4056bf23c9097c450c5e)), closes [#16](https://github.com/Diafyx/skewsplit/issues/16)
* **postgres:** plan predicates from pg_stats statistics ([#63](https://github.com/Diafyx/skewsplit/issues/63)) ([10804d9](https://github.com/Diafyx/skewsplit/commit/10804d9307e0dec8163fc344e0d19b30d5139799)), closes [#17](https://github.com/Diafyx/skewsplit/issues/17)
* **postgres:** verify that every row matches exactly one predicate ([#64](https://github.com/Diafyx/skewsplit/issues/64)) ([cd09174](https://github.com/Diafyx/skewsplit/commit/cd09174e3ecaf769f9404a2e557f783434144f0f)), closes [#18](https://github.com/Diafyx/skewsplit/issues/18)
* **python:** add the public Python API ([#65](https://github.com/Diafyx/skewsplit/issues/65)) ([bcd6f70](https://github.com/Diafyx/skewsplit/commit/bcd6f701f1338fff3a3a7b5235084014d2f3fe75)), closes [#19](https://github.com/Diafyx/skewsplit/issues/19)
* **spark:** emit PySpark and Scala snippets and add the spark-e2e job ([#67](https://github.com/Diafyx/skewsplit/issues/67)) ([647ff1e](https://github.com/Diafyx/skewsplit/commit/647ff1e14a1fc5fe6ad03cb15be852743c62de58)), closes [#21](https://github.com/Diafyx/skewsplit/issues/21)


### Bug Fixes

* **ci:** run make mutation with the test tools installed ([#75](https://github.com/Diafyx/skewsplit/issues/75)) ([aa5a889](https://github.com/Diafyx/skewsplit/commit/aa5a88943cbff751d46b8643b6c237260d4ad214)), closes [#74](https://github.com/Diafyx/skewsplit/issues/74)


### Documentation

* **docs:** write the README's install, quick start, limitations and versions ([#70](https://github.com/Diafyx/skewsplit/issues/70)) ([916ffe4](https://github.com/Diafyx/skewsplit/commit/916ffe487a9064fa07a300e7de94fbc80aafe893)), closes [#23](https://github.com/Diafyx/skewsplit/issues/23)
