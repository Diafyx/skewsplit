# skewsplit

skewsplit reads a database column's distribution and returns balanced,
index-friendly range predicates for the `predicates` argument of Spark's JDBC
reader, together with a report of the remaining skew.

This package is at an early stage and has no planning features yet. See the
[repository](https://github.com/Diafyx/skewsplit) for the roadmap.
