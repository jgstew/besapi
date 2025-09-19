#!/usr/bin/env python
"""Test besapi with pytest.

This was converted from tests/tests.py which was used before this pytest was added.
"""

import json
import os
import random
import subprocess
import sys

import pytest

# mypy: disable-error-code="arg-type"

if not os.getenv("TEST_PIP"):
    # add module folder to import paths for testing local src
    sys.path.append(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    )
    # reverse the order so we make sure to get the local src module
    sys.path.reverse()

import besapi
import besapi.plugin_utilities

if os.name == "nt":
    import besapi.plugin_utilities_win


def test_besapi_version():
    """Test that the besapi version is not None."""
    assert besapi.besapi.__version__ is not None


def test_rand_password():
    """Test that the generated random password has the correct length."""
    assert 15 == len(besapi.besapi.rand_password(15))


def test_sanitize_txt():
    """Test that the sanitize_txt function works correctly."""
    assert ("test--string", "test") == besapi.besapi.sanitize_txt(
        r"test/\string", "test%"
    )


def test_replace_text_between():
    """Test that the replace_text_between function works correctly."""
    assert "http://localhost:52311/file.example" == besapi.besapi.replace_text_between(
        "http://example:52311/file.example", "://", ":52311", "localhost"
    )


def test_validate_site_path():
    """Test the validate_site_path function with various inputs."""
    assert "master" in besapi.besapi.BESConnection.validate_site_path(
        "", "master", False
    )
    assert "custom/" in besapi.besapi.BESConnection.validate_site_path(
        "", "custom/Example", False
    )
    assert "operator/" in besapi.besapi.BESConnection.validate_site_path(
        "", "operator/Example", False
    )


def test_validate_xml_bes_file():
    """Test the validate_xml_bes_file function with good and bad files."""
    assert besapi.besapi.validate_xml_bes_file("tests/good/RelaySelectTask.bes") is True
    assert (
        besapi.besapi.validate_xml_bes_file("tests/good/ComputerGroupsExample.bes")
        is True
    )
    assert (
        besapi.besapi.validate_xml_bes_file("tests/bad/RelaySelectTask_BAD.bes")
        is False
    )
    assert (
        besapi.besapi.validate_xml_bes_file("tests/bad/ComputerGroups_BAD.bes") is False
    )


def test_failing_validate_site_path():
    """Test that validate_site_path raises ValueError for invalid inputs."""

    with pytest.raises(ValueError):
        besapi.besapi.BESConnection.validate_site_path("", "bad/Example", False)

    with pytest.raises(ValueError):
        besapi.besapi.BESConnection.validate_site_path("", "bad/master", False)

    with pytest.raises(ValueError):
        besapi.besapi.BESConnection.validate_site_path("", "", False, True)

    with pytest.raises(ValueError):
        besapi.besapi.BESConnection.validate_site_path("", None, False, True)


class RequestResult:
    text = "this is just a test"
    headers: list = []


def test_rest_result():
    """Test the RESTResult class."""
    request_result = RequestResult()
    rest_result = besapi.besapi.RESTResult(request_result)

    assert rest_result.besdict is not None
    assert rest_result.besjson is not None
    assert b"<BES>Example</BES>" in rest_result.xmlparse_text("<BES>Example</BES>")
    assert rest_result.text == "this is just a test"


def test_parse_bes_modtime():
    """Test the parse_bes_modtime function."""
    assert (
        2017 == besapi.besapi.parse_bes_modtime("Tue, 05 Sep 2017 23:31:48 +0000").year
    )


def test_get_action_combined_relevance():
    """Test the get_action_combined_relevance function."""
    assert (
        "( ( True ) AND ( windows of operating system ) ) AND ( False )"
        == besapi.besapi.get_action_combined_relevance(
            ["True", "windows of operating system", "False"]
        )
    )


def test_get_target_xml():
    """Test the get_target_xml function with various inputs."""
    assert "<CustomRelevance>False</CustomRelevance>" == besapi.besapi.get_target_xml()
    assert "<AllComputers>true</AllComputers>" == besapi.besapi.get_target_xml(
        "<AllComputers>"
    )
    assert "<ComputerID>1</ComputerID>" == besapi.besapi.get_target_xml(1)
    assert (
        "<CustomRelevance><![CDATA[not windows of operating system]]></CustomRelevance>"
        == besapi.besapi.get_target_xml("not windows of operating system")
    )
    assert (
        "<ComputerID>1</ComputerID><ComputerID>2</ComputerID>"
        == besapi.besapi.get_target_xml([1, 2])
    )
    assert (
        "<ComputerName>Computer 1</ComputerName><ComputerName>Another Computer</ComputerName>"
        == besapi.besapi.get_target_xml(["Computer 1", "Another Computer"])
    )


def test_bescli():
    """Test the BESCLInterface class and its methods."""
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
            int(bigfix_cli.bes_conn.session_relevance_string("number of bes computers"))
            > 0
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
        assert upload_result.besobj.FileUpload.Available == 1
        assert upload_result.besdict["FileUpload"]["Available"] == "1"
        assert upload_result.besjson is not None
        upload_result_json = json.loads(upload_result.besjson)
        assert upload_result_json["FileUpload"]["Available"] == "1"

        assert "test_besapi_upload.txt</URL>" in str(upload_result)
        upload_prefetch = bigfix_cli.bes_conn.parse_upload_result_to_prefetch(
            upload_result
        )
        # print(upload_prefetch)
        assert "prefetch test_besapi_upload.txt sha1:" in upload_prefetch

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
        assert bes_conn.login()


def test_plugin_utilities_win_get_besconn_root_windows_registry():
    """Test getting a BESConnection from the Windows Registry."""
    if "BES_ROOT_SERVER" not in os.environ:
        pytest.skip("Skipping Windows Registry test, BES_ROOT_SERVER not set.")
    if "BES_USER_NAME" not in os.environ:
        pytest.skip("Skipping Windows Registry test, BES_USER_NAME not set.")
    if "BES_PASSWORD" not in os.environ:
        pytest.skip("Skipping Windows Registry test, BES_PASSWORD not set.")

    if not os.name == "nt":
        pytest.skip("Skipping Windows Registry test on non-Windows system.")

    # only run this test if besapi > v3.9.1:
    if besapi.besapi.__version__ <= "3.9.1":
        pytest.skip("Skipping test for besapi <= 3.9.1")

    # get env vars for testing:
    root_server = os.getenv("BES_ROOT_SERVER")
    root_user = os.getenv("BES_USER_NAME")
    root_user_password = os.getenv("BES_PASSWORD")

    encrypted_str = besapi.plugin_utilities_win.win_dpapi_encrypt_str(
        root_user_password
    )

    import winreg

    # write user and encrypted password to registry for testing:
    # HKEY_LOCAL_MACHINE\SOFTWARE\Wow6432Node\BigFix\Enterprise Server\MFSConfig
    subkey = r"SOFTWARE\Wow6432Node\BigFix\Enterprise Server\MFSConfig"
    hive = winreg.HKEY_LOCAL_MACHINE  # type: ignore[attr-defined]

    key = winreg.CreateKey(hive, subkey)
    winreg.SetValueEx(
        key, "RESTURL", 0, winreg.REG_SZ, "https://" + root_server + ":52311/api"
    )
    winreg.SetValueEx(key, "RESTUsername", 0, winreg.REG_SZ, root_user)
    winreg.SetValueEx(key, "RESTPassword", 0, winreg.REG_SZ, "{obf}" + encrypted_str)
    winreg.CloseKey(key)
    bes_conn = besapi.plugin_utilities_win.get_besconn_root_windows_registry()
    assert bes_conn is not None


def test_bes_conn_json():
    """Test the BESConnection class with JSON output."""

    bes_conn = besapi.plugin_utilities.get_besapi_connection(None)

    if bes_conn and bes_conn.login():
        print("testing session_relevance_json")
        result = bes_conn.session_relevance_json("number of all bes sites")
        assert result is not None
        assert int(result["result"][0]) > 0
        result = bes_conn.session_relevance_json(
            """("[%22" & it & "%22]") of concatenation "%22, %22" of names of all bes sites"""
        )
        assert result is not None
        string_first_result_json = result["result"][0]
        print(string_first_result_json)
        assert '", "' in string_first_result_json
        assert '["' in string_first_result_json
        assert '"BES Support"' in string_first_result_json

        print("testing session_relevance_json_array")
        result = bes_conn.session_relevance_json_array("number of all bes sites")
        print(result)
        assert result is not None
        assert int(result[0]) > 0
        print("testing session_relevance_json_string")
        result = bes_conn.session_relevance_json_string("number of all bes sites")
        print(result)
        assert result is not None
        assert int(result) > 0
        print("testing session_relevance_json_string tuple")
        result = bes_conn.session_relevance_json_string(
            '(ids of it, names of it, "TestString") of all bes sites'
        )
        print(result)
        assert result is not None
        assert "TestString" in result
        assert "BES Support" in result
    else:
        pytest.skip("Skipping BESConnection test, no config file or login failed.")


def test_bes_conn_upload_always():
    """Test the BESConnection class with JSON output."""
    file_name = "LICENSE.txt"
    file_path = "../" + file_name
    if not os.path.isfile(os.path.abspath(file_path)):
        # handle case where not running from src or tests folder.
        file_path = "./" + file_name
    assert os.path.isfile(os.path.abspath(file_path))

    bes_conn = besapi.plugin_utilities.get_besapi_connection(None)
    if bes_conn and bes_conn.login():
        # test upload
        # Example Header::  Content-Disposition: attachment; filename="file.xml"
        headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}
        with open(file_path, "rb") as f:
            result = bes_conn.post(bes_conn.url("upload"), data=f, headers=headers)
        print(result)
        assert result is not None
        assert result.besobj.FileUpload.Available == 1
        assert result.besdict["FileUpload"]["Available"] == "1"
    else:
        pytest.skip("Skipping BESConnection upload test, login failed.")


def test_plugin_utilities_logging():
    """Test the plugin_utilities module."""
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


def test_plugin_utilities_win_dpapi():
    """Test the Windows DPAPI encryption function, if on Windows."""
    if not os.name == "nt":
        pytest.skip("Skipping Windows Registry test on non-Windows system.")

    # only run this test if besapi > v3.8.3:
    if besapi.besapi.__version__ <= "3.8.3":
        pytest.skip("Skipping test for besapi <= 3.8.3")

    test_string = "This is just a test string " + str(random.randint(0, 9999))
    encrypted_str = besapi.plugin_utilities_win.win_dpapi_encrypt_str(test_string)
    print("Encrypted string:", encrypted_str)
    assert encrypted_str != ""
    assert encrypted_str != test_string
    decrypted_str = besapi.plugin_utilities_win.win_dpapi_decrypt_base64(encrypted_str)
    print("Decrypted string:", decrypted_str)
    assert decrypted_str == test_string


def test_plugin_utilities_win_win_registry_value_read():
    """Test reading a Windows registry value."""
    if not os.name == "nt":
        pytest.skip("Skipping Windows Registry test on non-Windows system.")

    # only run this test if besapi > v3.8.3:
    if besapi.besapi.__version__ <= "3.8.3":
        pytest.skip("Skipping test for besapi <= 3.8.3")

    import winreg

    registry_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    registry_value = "ProgramFilesDir"
    result = besapi.plugin_utilities_win.win_registry_value_read(
        winreg.HKEY_LOCAL_MACHINE, registry_key, registry_value
    )

    assert result is not None
    print("Registry value:", result)
    assert "Program Files" in result


def test_plugin_utilities_win_get_win_registry_rest_pass():
    """Test getting the Windows Registry REST password."""
    if not os.name == "nt":
        pytest.skip("Skipping Windows Registry test on non-Windows system.")

    # only run this test if besapi > v3.8.3:
    if besapi.besapi.__version__ <= "3.8.3":
        pytest.skip("Skipping test for besapi <= 3.8.3")

    import winreg

    test_string = "This is just a test string " + str(random.randint(0, 9999))
    encrypted_str = besapi.plugin_utilities_win.win_dpapi_encrypt_str(test_string)

    # write encrypted string to registry for testing:
    # HKEY_LOCAL_MACHINE\SOFTWARE\Wow6432Node\BigFix\Enterprise Server\MFSConfig
    subkey = r"SOFTWARE\Wow6432Node\BigFix\Enterprise Server\MFSConfig"

    key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, subkey)
    winreg.SetValueEx(key, "RESTPassword", 0, winreg.REG_SZ, "{obf}" + encrypted_str)
    winreg.CloseKey(key)

    result = besapi.plugin_utilities_win.get_win_registry_rest_pass()
    assert result is not None
    print("Windows Registry REST password:", result)
    assert result == test_string
