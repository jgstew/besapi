#!/usr/bin/env python
"""File to configure python build and packaging for pip."""

try:
    from setuptools import setup
except (ImportError, ModuleNotFoundError):
    from distutils.core import setup  # type: ignore[no-redef,import-not-found]

setup(
    name="besapi",
    # version= moved to setup.cfg
    author="Matt Hansen, James Stewart",
    author_email="hansen.m@psu.edu, james@jgstew.com",
    description="Library for working with the BigFix REST API",
    license="MIT",
    keywords="bigfix iem tem rest api",
    url="https://github.com/jgstew/besapi",
    # long_description= moved to setup.cfg
    packages=["besapi", "bescli"],
    package_data={"besapi": ["schemas/*.xsd"]},
    install_requires=["requests", "lxml", "cmd2", "setuptools"],
    include_package_data=True,
    package_dir={"": "src"},
)
