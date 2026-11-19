# Benchmark results

Written by `make bench` from a run of `.github/workflows/bench.yml`. See
[README.md](README.md) for what each method does.

- Table `bench_orders`: 15,000,000 rows, read in full into Spark's noop sink.
- 16 partitions. Each method ran 3 times, interleaved with the others; the times are medians.
- Spark 4.3.0 in local mode with 4 cores, Java 21.0.13, Python 3.14.4.
- PostgreSQL 18.7 (Debian 18.7-1.pgdg13+2) in a service container on the same runner (ubuntu26 20261116.202.1, 4 CPUs, 16 GB).

## `customer_id` (bigint)

| Method | Tasks | Rows per task (min / median / max) | Largest task | Planning query | Max task time | Job time |
|--------|------:|-----------------------------------:|-------------:|---------------:|--------------:|---------:|
| default bounds | 16 | 59,272 / 115,569 / 12,444,535 | 83% | 0.1 s | 17.0 s | 17.0 s |
| hash-mod | 17 | 149,331 / 869,180 / 1,514,943 | 10% | 0.0 s | 6.5 s | 21.9 s |
| quantile ranges | 17 | 149,331 / 928,167 / 1,154,564 | 8% | 2.7 s | 4.4 s | 17.6 s |

Quantile ranges against hash-mod: +32% max task time, +20% job time (positive is faster; the kill criterion is about 20%).

## `created_at` (timestamp)

| Method | Tasks | Rows per task (min / median / max) | Largest task | Planning query | Max task time | Job time |
|--------|------:|-----------------------------------:|-------------:|---------------:|--------------:|---------:|
| default bounds | 16 | 361,686 / 367,593 / 9,329,785 | 62% | 0.1 s | 11.3 s | 19.7 s |
| hash-mod | 17 | 150,776 / 927,860 / 930,708 | 6% | 0.0 s | 6.7 s | 27.1 s |
| quantile ranges | 17 | 150,776 / 928,076 / 928,077 | 6% | 4.7 s | 4.2 s | 15.3 s |

Quantile ranges against hash-mod: +37% max task time, +43% job time (positive is faster; the kill criterion is about 20%).
