#!/usr/bin/env python
"""
file to configure python build and packaging for pip
"""

try:
    from setuptools import setup
except (ImportError, ModuleNotFoundError):
    from distutils.core import setup

setup(
    name="besapi",
    version="0.7",
    author="Matt Hansen",
    author_email="hansen.m@psu.edu",
    description="Library for working with the BigFix REST API",
    license="BSD",
    keywords="bigfix ibm iem tem rest",
    url="https://github.com/CLCMacTeam/besapi",
    long_description=(
        "python-besapi is a Python library designed to "
        "interact with the BES (BigFix) REST API."
    ),
    packages=["besapi", "bescli"],
    package_data={
        "besapi": ["schemas/*.xsd"],
    },
    install_requires=["requests", "lxml", "cmd2"],
    include_package_data=True,
    package_dir={"": "src"},
)
