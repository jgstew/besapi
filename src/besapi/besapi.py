#!/usr/bin/env python
"""
This module besapi provides a simple interface to the BigFix REST API.

MIT License
Copyright (c) 2014 Matt Hansen
Copyright (c) 2021 James Stewart
Maintained by James Stewart since 2021

Library for communicating with the BES (BigFix) REST API.
"""

import configparser
import datetime
import getpass
import hashlib
import importlib.resources
import json
import logging
import os
import random
import site
import string
import sys
import urllib.parse

import lxml.etree
import lxml.objectify
import requests
import urllib3.poolmanager

__version__ = "4.0.2"

besapi_logger = logging.getLogger("besapi")

# pylint: disable=consider-using-f-string


def rand_password(length=20):
    """Get a random password."""

    all_safe_chars = string.ascii_letters + string.digits + "!#()*+,-.:;<=>?[]^_|~"

    # https://medium.com/analytics-vidhya/create-a-random-password-generator-using-python-2fea485e9da9
    password = "".join(random.sample(all_safe_chars, length))
    return password


def sanitize_txt(*args):
    """Clean arbitrary text for safe file system usage."""
    valid_chars = f"-_.() {string.ascii_letters}{string.digits}"

    sani_args = []
    for arg in args:
        sani_args.append(
            "".join(
                c
                for c in str(arg).replace("/", "-").replace("\\", "-").replace(" ", "_")
                if c in valid_chars
            )
            .encode("ascii", "ignore")
            .decode()
        )

    return tuple(sani_args)


def elem2dict(node):
    """
    Convert an lxml.etree node tree into a dict.

    https://gist.github.com/jacobian/795571?permalink_comment_id=2981870#gistcomment-2981870
    """
    result = {}

    for element in node.iterchildren():
        # Remove namespace prefix
        key = element.tag.split("}")[1] if "}" in element.tag else element.tag

        # Process element as tree element if the inner XML contains non-whitespace content
        if element.text and element.text.strip():
            value = element.text
        else:
            value = elem2dict(element)
        if key in result:
            if type(result[key]) is list:
                result[key].append(value)
            else:
                tempvalue = result[key].copy()
                result[key] = [tempvalue, value]
        else:
            result[key] = value
    return result


# https://stackoverflow.com/questions/16159969/replace-all-text-between-2-strings-python
def replace_text_between(
    original_text, first_delimiter, second_delimiter, replacement_text
):
    """Replace text between delimiters.

    Each delimiter should only appear once.
    """
    leading_text = original_text.split(first_delimiter)[0]
    trailing_text = original_text.split(second_delimiter)[1]

    return (
        leading_text
        + first_delimiter
        + replacement_text
        + second_delimiter
        + trailing_text
    )


# https://github.com/jgstew/generate_bes_from_template/blob/bcc6c79632dd375c2861608ded3ae5872801a669/src/generate_bes_from_template/generate_bes_from_template.py#L87-L92
def parse_bes_modtime(string_datetime):
    """Parse datetime string to object."""
    # ("%a, %d %b %Y %H:%M:%S %z")
    return datetime.datetime.strptime(string_datetime, "%a, %d %b %Y %H:%M:%S %z")


def get_action_combined_relevance(relevances: list[str]):
    """Take array of ordered relevance clauses and return relevance string for
    action.
    """

    relevance_combined = ""

    if not relevances:
        return "False"
    if len(relevances) == 0:
        return "False"
    if len(relevances) == 1:
        return relevances[0]
    if len(relevances) > 0:
        for clause in relevances:
            if len(relevance_combined) == 0:
                relevance_combined = clause
            else:
                relevance_combined = (
                    "( " + relevance_combined + " ) AND ( " + clause + " )"
                )

    return relevance_combined


def get_target_xml(targets=None):
    """Get target xml based upon input.

    Input can be a single string:
        - starts with "<AllComputers>" if all computers should be targeted
        - Otherwise will be interpreted as custom relevance

    Input can be a single int:
        - Single Computer ID Target

    Input can be an array:
        - Array of Strings: ComputerName
        - Array of Integers: ComputerID
    """
    if targets is None or not targets:
        besapi_logger.warning("No valid targeting found, will target no computers.")
        # default if invalid:
        return "<CustomRelevance>False</CustomRelevance>"

    # if targets is int:
    if isinstance(targets, int):
        if targets == 0:
            raise ValueError(
                "Int 0 is not valid Computer ID, set targets to an array of strings of computer names or an array of ints of computer ids or custom relevance string or <AllComputers>"
            )
        return f"<ComputerID>{targets}</ComputerID>"

    # if targets is str:
    if isinstance(targets, str):
        # if targets string starts with "<AllComputers>":
        if targets.startswith("<AllComputers>"):
            if "false" in targets.lower():
                # In my testing, <AllComputers>false</AllComputers> does not work correctly
                return "<CustomRelevance>False</CustomRelevance>"
                # return "<AllComputers>false</AllComputers>"
            return "<AllComputers>true</AllComputers>"
        # treat as custom relevance:
        return f"<CustomRelevance><![CDATA[{targets}]]></CustomRelevance>"

    # if targets is array:
    if isinstance(targets, list):
        element_type = type(targets[0])
        if element_type is int:
            # array of computer ids
            return (
                "<ComputerID>"
                + "</ComputerID><ComputerID>".join(map(str, targets))
                + "</ComputerID>"
            )
        if element_type is str:
            # array of computer names
            return (
                "<ComputerName>"
                + "</ComputerName><ComputerName>".join(targets)
                + "</ComputerName>"
            )

    besapi_logger.warning("No valid targeting found, will target no computers.")

    # default if invalid:
    return "<CustomRelevance>False</CustomRelevance>"


def validate_xsd(doc):
    """Validate results using XML XSDs."""
    try:
        xmldoc = lxml.etree.fromstring(doc)
    except BaseException:  # pylint: disable=broad-except
        return False

    for xsd in ["BES.xsd", "BESAPI.xsd", "BESActionSettings.xsd"]:
        schema_path = importlib.resources.files(__package__) / f"schemas/{xsd}"
        with schema_path.open("r") as xsd_file:
            xmlschema_doc = lxml.etree.parse(xsd_file)

        # one schema may throw an error while another will validate
        try:
            xmlschema = lxml.etree.XMLSchema(xmlschema_doc)
        except lxml.etree.XMLSchemaParseError as err:
            # this should only error if the XSD itself is malformed
            besapi_logger.error("ERROR with `%s`: %s", xsd, err)
            raise err

        if xmlschema.validate(xmldoc):
            return True

    return False


def validate_xml_bes_file(file_path):
    """Take a file path as input,
    read as binary data,.

    validate against xml schema

    returns True for valid xml
    returns False for invalid xml (or if file is not xml)
    """
    with open(file_path, "rb") as file:
        file_data = file.read()

    return validate_xsd(file_data)


def get_bes_conn_using_env():
    """Get BESConnection using environment variables."""
    username = os.getenv("BES_USER_NAME")
    password = os.getenv("BES_PASSWORD")
    rootserver = os.getenv("BES_ROOT_SERVER")

    if username and password and rootserver:
        bes_conn = BESConnection(username, password, rootserver)
        if bes_conn:
            return bes_conn

    return None


def get_bes_conn_using_config_file(conf_file=None):
    """
    Read connection values from config file.

    return besapi connection
    """
    config_paths = [
        "/etc/besapi.conf",
        os.path.expanduser("~/besapi.conf"),
        os.path.expanduser("~/.besapi.conf"),
        "besapi.conf",
    ]
    # if conf_file specified, then only use that:
    if conf_file:
        config_paths = [conf_file]

    configparser_instance = configparser.ConfigParser()

    found_config_files = configparser_instance.read(config_paths)

    if found_config_files and configparser_instance:
        print("Attempting BESAPI Connection using config file:", found_config_files)
        try:
            BES_ROOT_SERVER = configparser_instance.get("besapi", "BES_ROOT_SERVER")
        except BaseException:  # pylint: disable=broad-except
            BES_ROOT_SERVER = None

        try:
            BES_USER_NAME = configparser_instance.get("besapi", "BES_USER_NAME")
        except BaseException:  # pylint: disable=broad-except
            BES_USER_NAME = None

        try:
            BES_PASSWORD = configparser_instance.get("besapi", "BES_PASSWORD")
        except BaseException:  # pylint: disable=broad-except
            BES_PASSWORD = None

        if BES_ROOT_SERVER and BES_USER_NAME and BES_PASSWORD:
            return BESConnection(BES_USER_NAME, BES_PASSWORD, BES_ROOT_SERVER)

    return None


def get_bes_conn_interactive(
    user=None, password=None, root_server=None, force_prompt=False
):
    """Get BESConnection using interactive prompts."""

    if not (force_prompt or sys.__stdin__.isatty()):
        logging.error("No TTY available for interactive login!")
        return None

    print(
        "Attempting BESAPI Connection using interactive prompts. Use Ctrl-C to cancel."
    )
    try:
        if not user:
            user = str(input("User [%s]: " % getpass.getuser())).strip()
        if not user:
            user = getpass.getuser()

        if not root_server:
            root_server = str(
                input("Root Server (ex. %s): " % "https://localhost:52311")
            ).strip()
        if not root_server or root_server == "":
            print("Root Server is required, try again!")
            return get_bes_conn_interactive(
                user=user,
                password=password,
                root_server=None,
                force_prompt=force_prompt,
            )

        if not password:
            password = str(
                getpass.getpass(f"Password for {user}@{root_server}: ")
            ).strip()

        if not password or password == "":
            print("Password is required, try again!")
            return get_bes_conn_interactive(
                user=user,
                password=None,
                root_server=root_server,
                force_prompt=force_prompt,
            )
    except (KeyboardInterrupt, EOFError):
        print("\nLogin cancelled.")
        return None

    try:
        return BESConnection(user, password, root_server)
    except requests.exceptions.HTTPError as err:
        print("Bad Password, Try again!")
        logging.debug(err)
        return get_bes_conn_interactive(
            user=user, password=None, root_server=root_server, force_prompt=force_prompt
        )
    except requests.exceptions.ConnectionError as err:
        print("Bad Root Server Specified, Try again!")
        logging.debug("Connection Error: %s", err)
        return get_bes_conn_interactive(
            user=user, password=password, root_server=None, force_prompt=force_prompt
        )
    except Exception as e:  # pylint: disable=broad-except
        logging.error("Error occurred while establishing BESConnection: %s", e)
        return None


# https://docs.python-requests.org/en/latest/user/advanced/#transport-adapters
class HTTPAdapterBlocksize(requests.adapters.HTTPAdapter):
    """Custom HTTPAdapter for requests to override blocksize
    for Uploading or Downloading large files.
    """

    def __init__(self, blocksize=1000000, **kwargs):
        self.blocksize = blocksize
        super().__init__(**kwargs)

    # override init_poolmanager from regular HTTPAdapter
    # https://stackoverflow.com/questions/22915295/python-requests-post-and-big-content/22915488#comment125583017_22915488
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        """Initializes a urllib3 PoolManager.

        This method should not be called from user code, and is only
        exposed for use when subclassing the
        :class:`HTTPAdapter <requests.adapters.HTTPAdapter>`.

        :param connections: The number of urllib3 connection pools to cache.
        :param maxsize: The maximum number of connections to save in the pool.
        :param block: Block when no free connections are available.
        :param pool_kwargs: Extra keyword arguments used to initialize the Pool Manager.
        """
        # save these values for pickling
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block

        try:
            self.poolmanager = urllib3.poolmanager.PoolManager(
                num_pools=connections,
                maxsize=maxsize,
                block=block,
                blocksize=self.blocksize,
                **pool_kwargs,
            )
        except Exception:  # pylint: disable=broad-except
            self.poolmanager = urllib3.poolmanager.PoolManager(
                num_pools=connections,
                maxsize=maxsize,
                block=block,
                **pool_kwargs,
            )


class BESConnection:
    """BigFix RESTAPI connection abstraction class."""

    def __init__(self, username, password, rootserver, verify=False):
        if not verify:
            # disable SSL warnings
            requests.packages.urllib3.disable_warnings()  # pylint: disable=no-member
        self.verify = verify
        self.last_connected = None

        self.username = username
        self.session = requests.Session()
        self.session.auth = (username, password)

        # # configure retries for requests
        # retry = requests.adapters.Retry(
        #     total=2,
        #     backoff_factor=0.1,
        #     # status_forcelist=[500, 502, 503, 504],
        # )

        # # mount the HTTPAdapter with the retry configuration
        # self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=retry))

        # store if connection user is main operator
        self.is_main_operator = None

        self.webreports_info_xml = None

        # use a sitepath context if none specified when required.
        self.site_path = "master"

        # if not provided, add on https://
        if not rootserver.startswith("http"):
            rootserver = "https://" + rootserver
        # if port not provided, add on the default :52311
        if not rootserver.count(":") == 2:
            rootserver = rootserver + ":52311"

        self.rootserver = rootserver
        try:
            # get root server port
            self.rootserver_port = int(rootserver.split("://", 1)[1].split(":", 1)[1])
        except BaseException:  # pylint: disable=broad-except
            # if error, assume default
            self.rootserver_port = 52311

        self.login()

    def __repr__(self):
        """Object representation."""
        # https://stackoverflow.com/a/2626364/861745
        return f"Object: besapi.BESConnection( username={self.username}, rootserver={self.rootserver} )"

    def __eq__(self, other):
        if (
            self.rootserver == other.rootserver
            and self.session.auth == other.session.auth
            and self.verify == other.verify
        ):
            return True
        return False

    def __del__(self):
        """Cleanup on deletion of instance."""
        self.logout()
        self.session.auth = None

    def __bool__(self):
        """Get true or false."""
        return self.login()

    def url(self, path):
        """Get absolute url."""
        if path.startswith(self.rootserver):
            url = path
        else:
            url = f"{self.rootserver}/api/{path}"

        return url

    def get(self, path="help", **kwargs):
        """HTTP GET request."""
        self.last_connected = datetime.datetime.now()
        return RESTResult(
            self.session.get(self.url(path), verify=self.verify, **kwargs)
        )

    def post(self, path, data, validate_xml=None, **kwargs):
        """HTTP POST request."""

        # if validate_xml is true, data must validate to xml schema
        # if validate_xml is false, no schema check will be made
        if validate_xml:
            if not validate_xsd(data):
                err_msg = "data being posted did not validate to XML schema. If expected, consider setting validate_xml to false."
                if validate_xml:
                    besapi_logger.error(err_msg)
                    raise ValueError(err_msg)

                # this is intended it validate_xml is None, but not used currently
                besapi_logger.warning(err_msg)

        self.last_connected = datetime.datetime.now()
        return RESTResult(
            self.session.post(self.url(path), data=data, verify=self.verify, **kwargs)
        )

    def put(self, path, data, validate_xml=None, **kwargs):
        """HTTP PUT request."""
        self.last_connected = datetime.datetime.now()

        # if validate_xml is true, data must validate to xml schema
        # if validate_xml is false, no schema check will be made
        if validate_xml:
            if not validate_xsd(data):
                err_msg = "data being put did not validate to XML schema. If expected, consider setting validate_xml to false."
                if validate_xml:
                    besapi_logger.error(err_msg)
                    raise ValueError(err_msg)

                # this is intended it validate_xml is None, but not used currently
                besapi_logger.warning(err_msg)

        return RESTResult(
            self.session.put(self.url(path), data=data, verify=self.verify, **kwargs)
        )

    def delete(self, path, **kwargs):
        """HTTP DELETE request."""
        self.last_connected = datetime.datetime.now()
        return RESTResult(
            self.session.delete(self.url(path), verify=self.verify, **kwargs)
        )

    def am_i_main_operator(self):
        """Check if the current user is the main operator user."""
        if self.is_main_operator is None:
            try:
                self.webreports_info_xml = self.get("webreports")
                self.is_main_operator = True
            except PermissionError:
                self.is_main_operator = False
            except Exception as err:  # pylint: disable=broad-except
                besapi_logger.error("Error checking if main operator: %s", err)
                self.is_main_operator = None

        if self.is_main_operator is not None:
            return self.is_main_operator

    def session_relevance_json(self, relevance, **kwargs):
        """Get Session Relevance Results in JSON.

        This will submit the relevance string as json instead of html form data.
        """
        session_relevance = urllib.parse.quote(relevance, safe=":+")
        rel_data = {"output": "json", "relevance": session_relevance}
        self.last_connected = datetime.datetime.now()
        result = RESTResult(
            self.session.post(
                self.url("query"),
                data=rel_data,
                verify=self.verify,
                **kwargs,
            )
        )
        return json.loads(result.text)

    def session_relevance_json_array(self, relevance, **kwargs):
        """Get Session Relevance Results in an array from the json return.

        This will submit the relevance string as json instead of html form data.
        """
        result = self.session_relevance_json(relevance, **kwargs)
        return result["result"]

    def session_relevance_json_string(self, relevance, **kwargs):
        """Get Session Relevance Results in a string from the json return.

        This will submit the relevance string as json instead of html form data.
        """
        # not sure if the following is needed to handle some cases:
        # relevance = "(it as string) of ( " + relevance + " )"
        rel_result_array = self.session_relevance_json_array(relevance, **kwargs)
        # Ensure each element is converted to a string
        return "\n".join(map(str, rel_result_array))

    def session_relevance_xml(self, relevance, **kwargs):
        """Get Session Relevance Results XML."""
        self.last_connected = datetime.datetime.now()
        return RESTResult(
            self.session.post(
                self.url("query"),
                data=f"relevance={urllib.parse.quote(relevance, safe=':+')}",
                verify=self.verify,
                **kwargs,
            )
        )

    def session_relevance_array(self, relevance, **kwargs):
        """Get Session Relevance Results array."""
        rel_result = self.session_relevance_xml(relevance, **kwargs)
        # print(rel_result)
        result = []
        try:
            for item in rel_result.besobj.Query.Result.Answer:
                result.append(item.text)
        except AttributeError as err:
            # print(err)
            if "no such child: Answer" in str(err):
                try:
                    result.append("ERROR: " + rel_result.besobj.Query.Error.text)
                except AttributeError as err2:
                    if "no such child: Error" in str(err2):
                        result.append("<Nothing> Nothing returned, but no error.")
                        besapi_logger.info("Query did not return any results")
                    else:
                        besapi_logger.error("%s\n%s", err2, rel_result.text)
                        result.append("ERROR: " + rel_result.text)
                        raise
            else:
                besapi_logger.error("%s\n%s", err, rel_result.text)
                result.append("ERROR: " + rel_result.text)
                raise
        return result

    def session_relevance_string(self, relevance, **kwargs):
        """Get Session Relevance Results string."""
        rel_result_array = self.session_relevance_array(
            "(it as string) of ( " + relevance + " )", **kwargs
        )
        return "\n".join(rel_result_array)

    def login(self, timeout=(3, 20)):
        """Do login."""
        if bool(self.last_connected):
            duration_obj = datetime.datetime.now() - self.last_connected
            duration_minutes = duration_obj / datetime.timedelta(minutes=1)
            besapi_logger.info(
                "Connection Time: `%s` - Duration: %d minutes",
                self.last_connected,
                duration_minutes,
            )
            # default timeout is 5 minutes
            # I'm not sure if this is required
            # or if 'requests' would handle this automatically anyway
            # if int(duration_minutes) > 3:
            #     besapi_logger.info("Refreshing Login to prevent timeout.")
            #     self.last_connected = None

        if not bool(self.last_connected):
            result_login = self.get("login", timeout=timeout)
            if not result_login.request.status_code == 200:
                result_login.request.raise_for_status()
            if result_login.request.status_code == 200:
                # set time of connection
                self.last_connected = datetime.datetime.now()

        # This doesn't work until urllib3 is at least ~v2:
        if self.last_connected:
            try:
                self.session.mount(self.url("upload"), HTTPAdapterBlocksize())
            except Exception:  # pylint: disable=broad-except
                pass

        return bool(self.last_connected)

    def logout(self):
        """Clear session and close it."""
        self.session.cookies.clear()
        self.session.close()

    def set_dashboard_variable_value(
        self, dashboard_name, var_name, var_value, private=False
    ):
        """Set the variable value from a dashboard datastore."""

        dash_var_xml = f"""<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BESAPI.xsd">
            <DashboardData>
                    <Dashboard>{dashboard_name}</Dashboard>
                    <Name>{var_name}</Name>
                    <IsPrivate>{str(private).lower()}</IsPrivate>
                    <Value>{var_value}</Value>
            </DashboardData>
    </BESAPI>
    """

        return self.post(
            f"dashboardvariable/{dashboard_name}/{var_name}", data=dash_var_xml
        )

    def get_dashboard_variable_value(self, dashboard_name, var_name):
        """Get the variable value from a dashboard datastore."""

        return str(
            self.get(
                f"dashboardvariable/{dashboard_name}/{var_name}"
            ).besobj.DashboardData.Value
        )

    def validate_site_path(self, site_path, check_site_exists=True, raise_error=False):
        """Make sure site_path is valid."""

        if site_path is None:
            if not raise_error:
                return None
            raise ValueError("Site Path is `None` - NoneType Error")
        if str(site_path).strip() == "":
            if not raise_error:
                return None
            raise ValueError("Site Path is empty!")

        # options for valid site prefix: (master must be last, special case)
        site_prefixes = ["external/", "custom/", "operator/", "master"]

        for prefix in site_prefixes:
            if prefix in site_path:
                if prefix == "master" and prefix != site_path:
                    # Invalid: This error should be raised regardless
                    raise ValueError(
                        f"Site path for master actionsite must be `master` not `{site_path}`"
                    )
                if not check_site_exists:
                    # don't check if site exists first
                    return site_path

                # check site exists first
                site_result = self.get(f"site/{site_path}")
                if site_result.request.status_code != 200:
                    besapi_logger.info("Site `%s` does not exist", site_path)
                    if not raise_error:
                        return None

                    raise ValueError(f"Site at path `{site_path}` does not exist!")

                # site_path is valid and exists:
                return site_path

        # Invalid: No valid prefix found
        raise ValueError(
            f"Site Path does not start with a valid prefix! {site_prefixes}"
        )

    def get_current_site_path(self, site_path=None):
        """If site_path is none, get current instance site_path,
        otherwise validate and return provided site_path.
        """

        # use instance site_path context if none provided:
        if site_path is None or str(site_path).strip() == "":
            site_path = self.site_path

        if site_path is None or str(site_path).strip() == "":
            besapi_logger.error("Site Path context not set and Site Path not provided!")
            raise ValueError("Site Path context not set and Site Path not provided!")

        # don't check for site's existence when doing basic get
        return self.validate_site_path(site_path, check_site_exists=False)

    def set_current_site_path(self, site_path):
        """Set current site path context."""

        if self.validate_site_path(site_path):
            self.site_path = site_path
            return self.site_path

        return None

    def import_bes_to_site(self, bes_file_path, site_path=None):
        """Import bes file to site."""

        if not os.access(bes_file_path, os.R_OK):
            besapi_logger.error("%s is not readable", bes_file_path)
            raise FileNotFoundError(f"{bes_file_path} is not readable")

        site_path = self.get_current_site_path(site_path)

        self.validate_site_path(site_path, False, True)

        with open(bes_file_path, "rb") as f:
            content = f.read()

            # validate BES File contents:
            if not validate_xsd(content):
                besapi_logger.error("%s is not valid", bes_file_path)
                return None

            # https://developer.bigfix.com/rest-api/api/import.html
            result = self.post(f"import/{site_path}", content)
            return result

    def create_site_from_file(self, bes_file_path, site_type="custom"):
        """Create new site."""
        xml_parsed = lxml.etree.parse(bes_file_path)
        new_site_name = xml_parsed.xpath("/BES/CustomSite/Name/text()")[0]

        result_site_path = self.validate_site_path(
            site_type + "/" + new_site_name, True, False
        )

        if result_site_path:
            besapi_logger.warning("Site `%s` already exists", result_site_path)
            return None

        result_site = self.post("sites", lxml.etree.tostring(xml_parsed))

        return result_site

    def get_user(self, user_name):
        """Get a user."""

        result_users = self.get(f"operator/{user_name}")

        if result_users and "Operator does not exist" not in str(result_users):
            return result_users

        besapi_logger.info("User `%s` Not Found!", user_name)

    def create_user_from_file(self, bes_file_path):
        """Create user from xml."""
        xml_parsed = lxml.etree.parse(bes_file_path)
        new_user_name = xml_parsed.xpath("/BESAPI/Operator/Name/text()")[0]
        result_user = self.get_user(new_user_name)

        if result_user:
            besapi_logger.warning("User `%s` Already Exists!", new_user_name)
            return result_user
        besapi_logger.info("Creating User `%s`", new_user_name)
        user_result = self.post("operators", lxml.etree.tostring(xml_parsed))
        besapi_logger.debug("user creation result:\n%s", user_result)

        return self.get_user(new_user_name)

    def get_computergroup(self, group_name, site_path=None):
        """Get computer group resource URI."""

        site_path = self.get_current_site_path(site_path)
        result_groups = self.get(f"computergroups/{site_path}")

        for group in result_groups.besobj.ComputerGroup:
            if group_name == str(group.Name):
                besapi_logger.info(
                    "Found Group With Resource: %s", group.attrib["Resource"]
                )
                return group

        besapi_logger.info("Group `%s` Not Found!", group_name)

    def create_group_from_file(self, bes_file_path, site_path=None):
        """Create a new group."""
        site_path = self.get_current_site_path(site_path)
        xml_parsed = lxml.etree.parse(bes_file_path)
        new_group_name = xml_parsed.xpath("/BES/ComputerGroup/Title/text()")[0]

        existing_group = self.get_computergroup(new_group_name, site_path)

        if existing_group is not None:
            besapi_logger.warning("Group `%s` Already Exists!", new_group_name)
            return existing_group

        # print(lxml.etree.tostring(xml_parsed))

        create_group_result = self.post(
            f"computergroups/{site_path}", lxml.etree.tostring(xml_parsed)
        )

        besapi_logger.debug("group creation result:\n%s", create_group_result)

        return self.get_computergroup(site_path, new_group_name)

    def get_upload(self, file_name, file_hash):
        """
        Check for a specific file upload reference.

        each upload is uniquely identified by sha1 and filename

        - https://developer.bigfix.com/rest-api/api/upload.html
        - https://github.com/jgstew/besapi/issues/3
        """
        if len(file_hash) != 40:
            raise ValueError("Invalid SHA1 Hash! Must be 40 characters!")

        if " " in file_hash or " " in file_name:
            raise ValueError("file name and hash cannot contain spaces")

        if len(file_name) > 0:
            result = self.get(self.url("upload/" + file_hash + "/" + file_name))
        else:
            raise ValueError("No file_name specified. Must be at least one character.")

        if "Upload not found" in result.text:
            besapi_logger.debug("WARNING: Upload not found!")
            return None

        return result

    def upload(self, file_path, file_name=None, file_hash=None):
        """
        Upload a single file.

        https://developer.bigfix.com/rest-api/api/upload.html
        """
        if not os.access(file_path, os.R_OK):
            besapi_logger.error(file_path, "is not readable")
            raise FileNotFoundError

        # if file_name not specified, then get it from tail of file_path
        if not file_name:
            file_name = os.path.basename(file_path)

        # files cannot contain spaces:
        if " " in file_name:
            besapi_logger.warning(
                "Replacing spaces with underscores in `%s`", file_name
            )
            file_name = file_name.replace(" ", "_")

        if not file_hash:
            besapi_logger.warning(
                "SHA1 hash of file to be uploaded not provided, calculating it."
            )
            sha1 = hashlib.sha1()
            with open(file_path, "rb") as f:
                while True:
                    # read 64k chunks
                    data = f.read(65536)
                    if not data:
                        break
                    sha1.update(data)
            file_hash = sha1.hexdigest()

        check_upload = None
        if file_hash:
            check_upload = self.get_upload(str(file_name), str(file_hash))

            if check_upload:
                besapi_logger.warning(
                    "Existing Matching Upload Found, Skipping Upload!"
                )
                # return same data as if we had uploaded
                return check_upload

        # Example Header::  Content-Disposition: attachment; filename="file.xml"
        headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}
        logging.warning(
            "Uploading `%s` to BigFix Server, this could take a while.", file_name
        )
        with open(file_path, "rb") as f:
            return self.post(self.url("upload"), data=f, headers=headers)

    def parse_upload_result_to_prefetch(
        self, result_upload, use_localhost=True, use_https=True
    ):
        """Take a rest response from an upload and parse into prefetch."""
        file_url = str(result_upload.besobj.FileUpload.URL)
        if use_https:
            file_url = file_url.replace("http://", "https://")
        # there are 3 different possibilities for the server FQDN
        # localhost
        # self.rootserver (without port number)
        # the returned value from the upload result
        if use_localhost:
            file_url = replace_text_between(
                file_url, "://", ":" + str(self.rootserver_port), "localhost"
            )

        # get tail of `Name` in FileUpload Result
        file_name = str(result_upload.besobj.FileUpload.Name).rsplit("/", 1)[-1]
        file_size = int(result_upload.besobj.FileUpload.Size)
        file_sha1 = result_upload.besobj.FileUpload.SHA1
        file_sha256 = result_upload.besobj.FileUpload.SHA256
        return f"prefetch {file_name} sha1:{file_sha1} size:{file_size} {file_url} sha256:{file_sha256}"

    def get_content_by_resource(self, resource_url):
        """Get a single content item by resource."""
        # Get Specific Content
        content = None
        try:
            content = self.get(resource_url.replace("http://", "https://"))
        except PermissionError as err:
            besapi_logger.error("Could not export item:")
            besapi_logger.error(err)

        # item_id = int(resource_url.split("/")[-1])
        # site_name = resource_url.split("/")[-2]
        # if site_name == "master":
        #     site_path = site_name
        # else:
        #     site_path = resource_url.split("/")[-3] + "/" + site_name
        return content

    def update_item_from_file(self, file_path, site_path=None):
        """Update an item by name and last modified."""
        site_path = self.get_current_site_path(site_path)
        bes_tree = lxml.etree.parse(file_path)

        with open(file_path, "rb") as f:
            content = f.read()
            if not validate_xsd(content):
                besapi_logger.error("%s is not valid", file_path)
                return None

        # get name of first child tag of BES
        # - https://stackoverflow.com/a/3601919/861745
        bes_type = str(bes_tree.xpath("name(/BES/*[1])"))
        bes_title = bes_tree.xpath("/BES/*[1]/Title/text()")[0]
        # get last modification time:
        bes_last_mod = bes_tree.xpath(
            '/BES/*[1]/MIMEField[Name[contains(text(), "x-fixlet-modification-time")]]/Value/text()'
        )[0]
        bes_last_mod_obj = parse_bes_modtime(bes_last_mod)

        print(bes_title)
        print(bes_type)
        print(bes_last_mod)
        print(bes_last_mod_obj)

        return "WORK IN PROGRESS: besapi.update_item_from_file()"

    def save_item_to_besfile(
        self,
        xml_string,
        export_folder="./",
        name_trim=100,
    ):
        """Save an xml string to bes file."""
        item_folder = export_folder
        if not os.path.exists(item_folder):
            os.makedirs(item_folder)

        content_obj = RESTResult.objectify_text(None, xml_string)
        # get first tag in XML that is the Type
        content_type_tag = list(content_obj.__dict__.keys())[0]
        item = content_obj[content_type_tag]
        item_path = item_folder + "/%s.bes" % sanitize_txt(
            item.Title.text[:name_trim],
        )
        item_path = item_path.replace("//", "/")
        with open(
            item_path,
            "wb",
        ) as bes_file:
            bes_file.write(xml_string.encode("utf-8"))
        return item_path

    def export_item_by_resource(
        self,
        content_resource,
        export_folder="./",
        name_trim=100,
        include_item_type_folder=False,
        include_item_id=False,
    ):
        """Export a single item by resource.

        example resources:

         - content_type/site_type/site/id
         - https://localhost:52311/api/content_type/site_type/site/id
        """

        # Get Specific Content
        content = self.get_content_by_resource(content_resource)
        if not content:
            besapi_logger.warning("Content not found")
            return None

        # get first tag in XML that is the Type
        content_type_tag = list(content.besobj.__dict__.keys())[0]
        item_id = int(content_resource.split("/")[-1])
        item = content.besobj[content_type_tag]
        # print(item.__dict__.keys())
        item_folder = export_folder
        if include_item_type_folder:
            item_folder = export_folder + "%s" % sanitize_txt(content_type_tag)
        # print(item_folder)
        if not os.path.exists(item_folder):
            os.makedirs(item_folder)
        item_path = item_folder + "/%s.bes" % sanitize_txt(
            item.Title.text[:name_trim],
        )
        if include_item_id:
            item_path = item_folder + "/%s-%s.bes" % sanitize_txt(
                item_id,
                item.Title.text[:name_trim],
            )
        item_path = item_path.replace("//", "/")
        with open(
            item_path,
            "wb",
        ) as bes_file:
            bes_file.write(content.text.encode("utf-8"))
        return item_path

    def export_site_contents(
        self,
        site_path=None,
        export_folder="./",
        name_trim=100,
        verbose=False,
        include_site_folder=True,
        include_item_ids=True,
    ):
        """Export contents of site.

        Originally here:

        - https://gist.github.com/jgstew/1b2da12af59b71c9f88a
        - https://bigfix.me/fixlet/details/21282
        """
        site_path = self.get_current_site_path(site_path)
        if verbose:
            print("export_site_contents()")
        # Iterate Over All Site Content
        content = self.get("site/" + site_path + "/content")
        if verbose:
            print(content)
        if content.request.status_code == 200:
            print(
                "Archiving %d items from %s..." % (content().countchildren(), site_path)
            )

            for item in content().iterchildren():
                if verbose:
                    print(
                        "{%s} (%s) [%s] %s - %s    "
                        % (
                            site_path,
                            item.tag,
                            item.ID,
                            item.Name.text,
                            item.attrib["LastModified"],
                        )
                    )

                # Get Specific Content
                content = self.get_content_by_resource(item.attrib["Resource"])

                if not content:
                    continue

                # Write Content to Disk
                item_folder = export_folder + "%s/%s" % sanitize_txt(
                    site_path, item.tag
                )
                if not include_site_folder:
                    item_folder = export_folder + "%s" % sanitize_txt(item.tag)
                if not os.path.exists(item_folder):
                    os.makedirs(item_folder)

                item_path = export_folder + "%s/%s/%s-%s.bes" % sanitize_txt(
                    site_path,
                    item.tag,
                    item.ID,
                    item.Name.text[:name_trim],
                )
                if not include_item_ids:
                    item_path = export_folder + "%s/%s/%s.bes" % sanitize_txt(
                        site_path,
                        item.tag,
                        item.Name.text[:name_trim],
                    )
                if not include_site_folder:
                    item_path = export_folder + "%s/%s-%s.bes" % sanitize_txt(
                        item.tag,
                        item.ID,
                        item.Name.text[:name_trim],
                    )
                    if not include_item_ids:
                        item_path = export_folder + "%s/%s.bes" % sanitize_txt(
                            item.tag,
                            item.Name.text[:name_trim],
                        )
                with open(
                    item_path,
                    "wb",
                ) as bes_file:
                    bes_file.write(content.text.encode("utf-8"))

    def export_all_sites(
        self, include_external=False, export_folder="./", name_trim=70, verbose=False
    ):
        """Export all bigfix sites to a folder."""
        results_sites = self.get("sites")
        if verbose:
            print(results_sites)
        if results_sites.request.status_code == 200:
            for item in results_sites().iterchildren():
                site_path = item.attrib["Resource"].split("/api/site/", 1)[1]
                if include_external or "external/" not in site_path:
                    print("Exporting Site:", site_path)
                    self.export_site_contents(
                        site_path, export_folder, name_trim, verbose
                    )

    __call__ = login
    # https://stackoverflow.com/q/40536821/861745
    __enter__ = login


class RESTResult:
    """BigFix REST API Result Abstraction Class."""

    def __init__(self, request):
        self.request = request
        self.text = request.text
        self.valid = None
        self._besxml = None
        self._besobj = None
        self._besdict = None
        self._besjson = None

        try:
            if self.request.status_code == 403:
                # Error most likely due to not having master operator privs
                # Could also be due to non-master operator not having specific privs
                raise PermissionError(
                    f"\n - HTTP Response Status Code: `403` Forbidden\n - ERROR: `{self.text}`\n - URL: `{self.request.url}`"
                )

            besapi_logger.debug(
                "HTTP Request Status Code `%d` from URL `%s`",
                self.request.status_code,
                self.request.url,
            )
        except AttributeError as err:
            besapi_logger.warning("Error (expected during tests) %s", err)

        if (
            "content-type" in request.headers
            and request.headers["content-type"] == "application/xml"
        ):
            self.valid = True
        elif type(request.text) is str and self.validate_xsd(
            request.text.encode("utf-8")
        ):
            self.valid = True
        else:
            if self.validate_xsd(request.text):
                self.valid = True
            else:
                besapi_logger.debug(
                    "INFO: REST API Result does not appear to be XML, this could be expected."
                )
                self.valid = False

    def __str__(self):
        if self.valid:
            # I think this is needed for python3 compatibility:
            try:
                return self.besxml.decode("utf-8")
            except BaseException:  # pylint: disable=broad-except
                return self.besxml
        else:
            return self.text

    def __call__(self):
        return self.besobj

    @property
    def besxml(self):
        """Property for parsed xml representation."""
        if self.valid and self._besxml is None:
            self._besxml = self.xmlparse_text(self.text)

        return self._besxml

    @property
    def besobj(self):
        """Property for xml object representation."""
        if self.valid and self._besobj is None:
            self._besobj = self.objectify_text(self.text)

        return self._besobj

    @property
    def besdict(self):
        """Property for python dict representation."""
        if self._besdict is None:
            if self.valid:
                self._besdict = elem2dict(lxml.etree.fromstring(self.besxml))
            else:
                self._besdict = {"text": str(self)}

        return self._besdict

    @property
    def besjson(self):
        """Property for json representation."""
        if self._besjson is None:
            self._besjson = json.dumps(self.besdict, indent=2)

        return self._besjson

    def validate_xsd(self, doc):
        """Validate results using XML XSDs."""
        # return self.valid if already set
        if self.valid is not None and isinstance(self.valid, bool):
            return self.valid
        return validate_xsd(doc)

    def xmlparse_text(self, text):
        """Parse response text as xml."""
        if type(text) is str:
            root_xml = lxml.etree.fromstring(text.encode("utf-8"))
        else:
            root_xml = text

        return lxml.etree.tostring(root_xml, encoding="utf-8", xml_declaration=True)

    def objectify_text(self, text):
        """Parse response text as objectified xml."""
        if type(text) is str:
            root_xml = text.encode("utf-8")
        else:
            root_xml = text

        return lxml.objectify.fromstring(root_xml)


def main():
    """If invoked directly, run bescli command loop."""
    # pylint: disable=import-outside-toplevel
    try:
        from bescli import bescli
    except ImportError:
        site.addsitedir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from bescli import bescli
    bescli.main()


if __name__ == "__main__":
    logging.basicConfig()
    main()
