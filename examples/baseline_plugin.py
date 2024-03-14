"""
Generate patching baselines from sites

requires `besapi`, install with command `pip install besapi`

Example Usage:
python baseline_plugin.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

References:
- https://github.com/jgstew/besapi/blob/master/examples/rest_cmd_args.py
- https://github.com/jgstew/besapi/blob/master/examples/baseline_by_relevance.py
- https://github.com/jgstew/tools/blob/master/Python/locate_self.py
"""

import argparse
import datetime
import logging
import logging.handlers
import os
import platform
import sys

import ruamel.yaml

import besapi

__version__ = "0.0.1"
verbose = 0
bes_conn = None
invoke_folder = None
config_yaml = None


def get_invoke_folder():
    """Get the folder the script was invoked from

    References:
    - https://github.com/jgstew/tools/blob/master/Python/locate_self.py
    """
    # using logging here won't actually log it to the file:

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        if verbose:
            print("running in a PyInstaller bundle")
        invoke_folder = os.path.abspath(os.path.dirname(sys.executable))
    else:
        if verbose:
            print("running in a normal Python process")
        invoke_folder = os.path.abspath(os.path.dirname(__file__))

    if verbose:
        print(f"invoke_folder = {invoke_folder}")

    return invoke_folder


def get_config(path="baseline_plugin.config.yaml"):
    """load config from yaml file"""

    if not (os.path.isfile(path) and os.access(path, os.R_OK)):
        path = os.path.join(invoke_folder, path)

    logging.info("loading config from: `%s`", path)

    if not (os.path.isfile(path) and os.access(path, os.R_OK)):
        raise FileNotFoundError(path)

    with open(path, "r", encoding="utf-8") as stream:
        yaml = ruamel.yaml.YAML(typ="safe", pure=True)
        config_yaml = yaml.load(stream)

    if verbose > 1:
        logging.debug(config_yaml["bigfix"])

    return config_yaml


def test_file_exists(path):
    """return true if file exists"""

    if not (os.path.isfile(path) and os.access(path, os.R_OK)):
        path = os.path.join(invoke_folder, path)

    logging.info("testing if exists: `%s`", path)

    if os.path.isfile(path) and os.access(path, os.R_OK) and os.access(path, os.W_OK):
        return path

    return False


def create_baseline_from_site(site):
    """create a patching baseline from a site name

    References:
    - https://github.com/jgstew/besapi/blob/master/examples/baseline_by_relevance.py"""

    site_name = site["name"]
    logging.info("Create patching baseline for site: %s", site_name)

    # Example:
    # fixlets of bes sites whose(exists (it as trimmed string as lowercase) whose(it = "Updates for Windows Applications Extended" as trimmed string as lowercase) of (display names of it; names of it))
    fixlets_rel = f'fixlets of bes sites whose(exists (it as trimmed string as lowercase) whose(it = "{site_name}" as trimmed string as lowercase) of (display names of it; names of it))'

    session_relevance = f"""(it as string) of (url of site of it, ids of it, content id of default action of it | "Action1") of it whose(exists default action of it AND globally visible flag of it AND name of it does not contain "(Superseded)" AND exists applicable computers whose(now - last report time of it < 60 * day) of it) of {fixlets_rel}"""

    result = bes_conn.session_relevance_array(session_relevance)

    num_items = len(result)

    if num_items > 1:
        logging.info("Number of items to add to baseline: %s", num_items)

        baseline_components = ""

        IncludeInRelevance = "true"

        fixlet_ids_str = "0"

        if num_items > 100:
            IncludeInRelevance = "false"

        for item in result:
            tuple_items = item.split(", ")
            fixlet_ids_str += " ; " + tuple_items[1]
            baseline_components += f"""
            <BaselineComponent IncludeInRelevance="{IncludeInRelevance}" SourceSiteURL="{tuple_items[0]}" SourceID="{tuple_items[1]}" ActionName="{tuple_items[2]}" />"""

        logging.debug(baseline_components)

        baseline_rel = "true"

        # create baseline relevance such that only relevant if 1+ fixlet is relevant
        if num_items > 100:
            baseline_rel = f"""exists relevant fixlets whose(id of it is contained by set of ({ fixlet_ids_str })) of sites whose("Fixlet Site" = type of it AND "{ site_name }" = name of it)"""

        # generate XML for baseline with template:
        baseline_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
        <Baseline>
            <Title>Patches from {site_name} - {datetime.datetime.today().strftime('%Y-%m-%d')}</Title>
            <Description />
            <Relevance>{baseline_rel}</Relevance>
            <BaselineComponentCollection>
            <BaselineComponentGroup>{baseline_components}
            </BaselineComponentGroup>
            </BaselineComponentCollection>
        </Baseline>
        </BES>"""

        logging.debug("Baseline XML:\n%s", baseline_xml)

        file_path = "tmp_baseline.bes"
        site_name = "Demo"
        site_path = f"custom/{site_name}"

        # Does not work through console import:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(baseline_xml)

        logging.info("Importing generated baseline...")
        import_result = bes_conn.import_bes_to_site(file_path, site_path)

        logging.info("Result: Import XML:\n%s", import_result)

        os.remove(file_path)


def process_baselines(config):
    """generate baselines"""

    for site in config:
        create_baseline_from_site(site)


def main():
    """Execution starts here"""
    print("main()")

    parser = argparse.ArgumentParser(
        description="Provde command line arguments for REST URL, username, and password"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        help="Set verbose output",
        required=False,
        action="count",
        default=0,
    )
    parser.add_argument(
        "-besserver", "--besserver", help="Specify the BES URL", required=False
    )
    parser.add_argument("-r", "--rest-url", help="Specify the REST URL", required=False)
    parser.add_argument("-u", "--user", help="Specify the username", required=False)
    parser.add_argument("-p", "--password", help="Specify the password", required=False)
    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    # allow set global scoped vars
    global bes_conn, verbose, config_yaml, invoke_folder
    verbose = args.verbose

    # get folder the script was invoked from:
    invoke_folder = get_invoke_folder()

    # set different log levels:
    log_level = logging.WARNING
    if verbose:
        log_level = logging.INFO
    if verbose > 1:
        log_level = logging.DEBUG

    # get path to put log file in:
    log_filename = os.path.join(invoke_folder, "baseline_plugin.log")

    handlers = [
        logging.handlers.RotatingFileHandler(
            log_filename, maxBytes=5 * 1024 * 1024, backupCount=1
        )
    ]

    # log output to console if arg provided:
    if verbose:
        handlers.append(logging.StreamHandler())

    # setup logging:
    logging.basicConfig(
        encoding="utf-8",
        level=log_level,
        format="%(asctime)s %(levelname)s:%(message)s",
        handlers=handlers,
    )
    logging.info("----- Starting New Session ------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("Python version: %s", platform.sys.version)
    logging.debug("BESAPI Module version: %s", besapi.__version__)
    logging.debug("this plugin's version: %s", __version__)

    # process args, setup connection:
    rest_url = args.rest_url

    # normalize url to https://HostOrIP:52311
    if rest_url and rest_url.endswith("/api"):
        rest_url = rest_url.replace("/api", "")

    try:
        bes_conn = besapi.besapi.BESConnection(args.user, args.password, rest_url)
        # bes_conn.login()
    except (
        AttributeError,
        ConnectionRefusedError,
        besapi.besapi.requests.exceptions.ConnectionError,
    ):
        try:
            # print(args.besserver)
            bes_conn = besapi.besapi.BESConnection(
                args.user, args.password, args.besserver
            )
        # handle case where args.besserver is None
        # AttributeError: 'NoneType' object has no attribute 'startswith'
        except AttributeError:
            bes_conn = besapi.besapi.get_bes_conn_using_config_file()

    # get config:
    config_yaml = get_config()

    trigger_path = config_yaml["bigfix"]["content"]["Baselines"]["automation"][
        "trigger_file_path"
    ]

    # check if file exists, if so, return path, else return false:
    trigger_path = test_file_exists(trigger_path)

    if trigger_path:
        process_baselines(
            config_yaml["bigfix"]["content"]["Baselines"]["automation"]["sites"]
        )
        # delete trigger file
        os.remove(trigger_path)
    else:
        logging.info("Trigger File Does Not Exists, skipping execution!")

    logging.info("----- Ending Session ------")


if __name__ == "__main__":
    main()
