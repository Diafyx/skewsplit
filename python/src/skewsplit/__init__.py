"""Balanced JDBC partition predicates for Spark, planned from column statistics."""

from importlib.metadata import version

__all__ = ["__version__"]

__version__: str = version("skewsplit")
