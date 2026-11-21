# Security policy

## Reporting a vulnerability

Please do not open a public issue. Report it privately through GitHub's
[private vulnerability reporting](https://github.com/Diafyx/skewsplit/security/advisories/new)
for this repository. Only the maintainers can see the report.

Include what you found, how to reproduce it, and which versions are affected.
You will get an acknowledgement within 7 days. We will keep you updated while
we work on a fix, agree a disclosure date with you, and credit you in the
advisory unless you ask us not to.

## Which releases get fixes

Before 1.0, security fixes go into a new patch release of the latest minor
version only. After 1.0 this policy will cover more versions.

## Supported versions

<!-- supported-versions:start -->
| Component | Versions | Notes |
|-----------|----------|-------|
| Python | 3.9 to 3.15 | 3.9 and 3.10 stay supported for Amazon EMR and AWS Glue users |
| Spark | 3.5, 4.1, 4.2, 4.3 | 4.0 reaches end of life on 23 November 2026 and is not tested |
| Python for Spark | 3.9 to 3.11 for Spark 3.5; 3.10 to 3.14 for 4.1 and 4.2; 3.11 to 3.14 for 4.3 | The Pythons each PySpark line supports; no PySpark line supports 3.15 yet |
| Java (for Spark) | 17 for Spark 3.5; 17 and 21 for Spark 4.x | Spark 3.5 does not support Java 21 |
| PostgreSQL | 15 to 19 | 14 reached end of life on 12 November 2026; 19 was released on 19 November 2026 |
| Operating systems | Linux, macOS, Windows | Database and Spark tests run on Linux |

Support for a version is dropped only in a minor release, announced in the
changelog one release ahead.
<!-- supported-versions:end -->

## Things to know when you use skewsplit

- skewsplit connects to your database with the credentials you give it. It
  reads statistics views and, depending on the strategy, samples or scans the
  column you name. Give it a role that can read only what it needs.
- Its output contains real values from that column (split points and frequent
  values). Treat plans, reports and logs like the data they describe.
- The package has no runtime dependencies. Database drivers are optional
  extras that you install yourself.
