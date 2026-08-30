"""Setup script for Drift.

This file provides backward-compatible build configuration for environments
running legacy setuptools (setuptools < 61.0.0) or pip without PEP 517 build
isolation, while modern build frontends can read pyproject.toml directly.
"""

from pathlib import Path
from setuptools import setup, find_packages

README_PATH = Path(__file__).parent / "README.md"
long_description = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else ""

setup(
    name="drift",
    version="0.1.0",
    description="Decoupled Two-Stage Git-Backed Dotfiles Manager",
    long_description=long_description,
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    package_data={
        "drift": ["templates/*", "templates/*.toml"],
        "drift.cli": ["help_docs/*", "help_docs/*.md"],
    },
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "drift=drift.cli:main",
        ],
    },
    extras_require={
        "rich": [
            "typer>=0.9.0",
            "rich>=13.0.0",
        ],
        "dev": [
            "typer>=0.9.0",
            "rich>=13.0.0",
            "coverage>=7.0.0",
            "pytest>=7.0.0",
            "build>=0.10.0",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
)
