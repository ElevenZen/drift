"""Setup script for Drift.

This file serves as a backward-compatible shim for legacy build tools and
older versions of pip (such as pip < 21.3) that require a setup.py file.
All primary package metadata and build configuration are declaratively
defined in pyproject.toml per PEP 517 and PEP 621 standards.
"""

from setuptools import setup

setup()
