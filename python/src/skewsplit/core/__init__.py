"""The pure planning core.

Everything in this package uses the standard library only and does no I/O.
Adapters such as the PostgreSQL reader turn database statistics into the core's
input shape; the core turns that shape into predicates. See
docs/adr/0001-monorepo-and-pure-core.md.
"""

__all__: list[str] = []
