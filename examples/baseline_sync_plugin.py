"""
This will sync baselines that are not in sync.

requires `besapi`, install with command `pip install besapi`

LIMITATION: This does not work with baselines in the actionsite
- Only works on baselines in custom sites

Example Usage:
python baseline_sync_plugin.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

Example Usage with config file:
python baseline_sync_plugin.py

This can also be run as a BigFix Server Plugin Service.

References:
- https://developer.bigfix.com/rest-api/api/admin.html
- https://github.com/jgstew/besapi/blob/master/examples/rest_cmd_args.py
- https://github.com/jgstew/tools/blob/master/Python/locate_self.py
"""

import logging
import ntpath
import os
import platform
import sys

import besapi
import besapi.plugin_utilities

__version__ = "1.1.2"
verbose = 0
bes_conn = None
invoke_folder = None


def get_invoke_folder(verbose=0):
    """Get the folder the script was invoked from."""
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


def get_invoke_file_name(verbose=0):
    """Get the filename the script was invoked from."""
    # using logging here won't actually log it to the file:

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        if verbose:
            print("running in a PyInstaller bundle")
        invoke_file_path = sys.executable
    else:
        if verbose:
            print("running in a normal Python process")
        invoke_file_path = __file__

    if verbose:
        print(f"invoke_file_path = {invoke_file_path}")

    # get just the file name, return without file extension:
    return os.path.splitext(ntpath.basename(invoke_file_path))[0]


def baseline_sync(baseline_id, site_path):
    """Sync a baseline."""
    logging.info("Syncing baseline: %s/%s", site_path, baseline_id)

    # get baseline sync xml:
    results = bes_conn.get(f"baseline/{site_path}/{baseline_id}/sync")

    baseline_xml_sync = results.text

    results = bes_conn.put(
        f"baseline/{site_path}/{baseline_id}", data=baseline_xml_sync
    )

    logging.debug("Sync results: %s", results.text)

    logging.info("Baseline %s/%s synced successfully", site_path, baseline_id)


def process_baseline(baseline_id, site_path):
    """Process a single baseline."""
    logging.info("Processing baseline: %s/%s", site_path, baseline_id)

    # get baseline xml:
    results = bes_conn.get(f"baseline/{site_path}/{baseline_id}")

    baseline_xml = results.text

    if 'SyncStatus="source fixlet differs"' in baseline_xml:
        logging.info("Baseline %s/%s is out of sync", site_path, baseline_id)
        return baseline_sync(baseline_id, site_path)
    else:
        logging.info("Baseline %s/%s is in sync", site_path, baseline_id)


def process_site(site_path):
    """Process a single site."""
    logging.info("Processing site: %s", site_path)

    # get site name from end of path:
    # if site_path does not have / then use site_path as site_name
    site_name = site_path.split("/")[-1]

    # get baselines in site:
    session_relevance = f"""ids of fixlets whose(baseline flag of it) of bes custom sites whose(name of it = "{site_name}")"""

    logging.debug("Getting baselines in site: %s", site_name)
    results = bes_conn.session_relevance_json(session_relevance)

    logging.info("Found %i baselines in site: %s", len(results["result"]), site_name)

    for baseline_id in results["result"]:
        process_baseline(baseline_id, site_path)


def main():
    """Execution starts here."""
    print("main() start")

    parser = besapi.plugin_utilities.setup_plugin_argparse()

    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    # allow set global scoped vars
    global bes_conn, verbose, invoke_folder
    verbose = args.verbose

    # get folder the script was invoked from:
    invoke_folder = get_invoke_folder(verbose)

    log_file_path = os.path.join(invoke_folder, get_invoke_file_name(verbose) + ".log")

    logging_config = besapi.plugin_utilities.get_plugin_logging_config(
        log_file_path, verbose, args.console
    )

    logging.basicConfig(**logging_config)

    logging.info("---------- Starting New Session -----------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("%s's version: %s", get_invoke_file_name(verbose), __version__)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("Python version: %s", platform.sys.version)
    logging.warning(
        "Results may be incorrect if not run as a MO or an account without scope of all computers"
    )

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    session_relevance = """names of bes custom sites whose(exists fixlets whose(baseline flag of it) of it)"""

    logging.info("Getting custom sites with baselines")
    results = bes_conn.session_relevance_json(session_relevance)

    logging.info("Processing %i custom sites with baselines", len(results["result"]))

    for site in results["result"]:
        try:
            process_site("custom/" + site)
        except PermissionError:
            logging.error(
                "Error processing site %s: Permission Denied, skipping site.", site
            )
            continue

    logging.info("---------- Ending Session -----------")


if __name__ == "__main__":
    main()
