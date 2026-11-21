# Changelog

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
