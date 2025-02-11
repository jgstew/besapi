#!/usr/bin/env python
"""
Test besapi
"""

import argparse
import os
import random
import subprocess
import sys

# check for --test_pip arg
parser = argparse.ArgumentParser()
parser.add_argument(
    "--test_pip", help="to test package installed with pip", action="store_true"
)
args = parser.parse_args()

if not args.test_pip:
    # add module folder to import paths for testing local src
    sys.path.append(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    )
    # reverse the order so we make sure to get the local src module
    sys.path.reverse()

import besapi
import besapi.plugin_utilities

print("besapi version: " + str(besapi.besapi.__version__))

assert 15 == len(besapi.besapi.rand_password(15))

assert ("test--string", "test") == besapi.besapi.sanitize_txt(r"test/\string", "test%")

assert "http://localhost:52311/file.example" == besapi.besapi.replace_text_between(
    "http://example:52311/file.example", "://", ":52311", "localhost"
)

# test site_path string validation
assert "master" in besapi.besapi.BESConnection.validate_site_path("", "master", False)
assert "custom/" in besapi.besapi.BESConnection.validate_site_path(
    "", "custom/Example", False
)
assert "operator/" in besapi.besapi.BESConnection.validate_site_path(
    "", "operator/Example", False
)

# start failing tests:
raised_errors = 0

# failing test:
try:
    besapi.besapi.BESConnection.validate_site_path("", "bad/Example", False)
except ValueError:
    raised_errors += 1

# failing test:
try:
    besapi.besapi.BESConnection.validate_site_path("", "bad/master", False)
except ValueError:
    raised_errors += 1

# failing test:
try:
    besapi.besapi.BESConnection.validate_site_path("", "", False, True)
except ValueError:
    raised_errors += 1

# failing test:
try:
    besapi.besapi.BESConnection.validate_site_path("", None, False, True)
except ValueError:
    raised_errors += 1

assert raised_errors == 4
# end failing tests


class RequestResult(object):
    text = "this is just a test"
    headers = []


request_result = RequestResult()
rest_result = besapi.besapi.RESTResult(request_result)

print(rest_result.besdict)
print(rest_result.besjson)
assert b"<BES>Example</BES>" in rest_result.xmlparse_text("<BES>Example</BES>")

assert rest_result.text == "this is just a test"

import bescli

bigfix_cli = bescli.bescli.BESCLInterface()

# just make sure these don't throw errors:
bigfix_cli.do_ls()
bigfix_cli.do_clear()
bigfix_cli.do_ls()
bigfix_cli.do_logout()
bigfix_cli.do_error_count()
bigfix_cli.do_version()
bigfix_cli.do_conf()

# this should really only run if the config file is present:
if bigfix_cli.bes_conn:
    # session relevance tests require functioning web reports server
    assert (
        int(bigfix_cli.bes_conn.session_relevance_string("number of bes computers")) > 0
    )
    assert (
        "test session relevance string result"
        in bigfix_cli.bes_conn.session_relevance_string(
            '"test session relevance string result"'
        )
    )
    bigfix_cli.do_set_current_site("master")

    # set working directory to folder this file is in:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # set working directory to src folder in parent folder
    os.chdir("../src")

    # Test file upload:
    upload_result = bigfix_cli.bes_conn.upload(
        "./besapi/__init__.py", "test_besapi_upload.txt"
    )
    # print(upload_result)
    assert "test_besapi_upload.txt</URL>" in str(upload_result)
    print(bigfix_cli.bes_conn.parse_upload_result_to_prefetch(upload_result))

    dashboard_name = "_PyBESAPI_tests.py"
    var_name = "TestVarName"
    var_value = "TestVarValue " + str(random.randint(0, 9999))

    assert var_value in str(
        bigfix_cli.bes_conn.set_dashboard_variable_value(
            dashboard_name, var_name, var_value
        )
    )

    assert var_value in str(
        bigfix_cli.bes_conn.get_dashboard_variable_value(dashboard_name, var_name)
    )

    if os.name == "nt":
        subprocess.run(
            'CMD /C python -m besapi ls clear ls conf "query number of bes computers" version error_count exit',
            check=True,
        )
        bes_conn = besapi.besapi.get_bes_conn_using_config_file()
        print("login succeeded:", bes_conn.login())

# test plugin_utilities:
print(besapi.plugin_utilities.get_invoke_folder())
print(besapi.plugin_utilities.get_invoke_file_name())

parser = besapi.plugin_utilities.setup_plugin_argparse(plugin_args_required=False)
# allow unknown args to be parsed instead of throwing an error:
args, _unknown = parser.parse_known_args()

# test logging plugin_utilities:
import logging

logging_config = besapi.plugin_utilities.get_plugin_logging_config("./tests.log")

# this use of logging.basicConfig requires python >= 3.9
if sys.version_info >= (3, 9):
    logging.basicConfig(**logging_config)

    logging.warning("Just testing to see if logging is working!")

    assert os.path.isfile("./tests.log")

sys.exit(0)
