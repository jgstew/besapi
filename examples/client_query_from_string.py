"""
Example session relevance results from a string.

requires `besapi`, install with command `pip install besapi`
"""

import json
import logging
import ntpath
import os
import platform
import sys
import time

import besapi
import besapi.plugin_utilities

CLIENT_RELEVANCE = "(computer names, model name of main processor, (it as string) of (it / (1024 * 1024 * 1024)) of total amount of ram)"
__version__ = "1.0.1"
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


def main():
    """Execution starts here."""
    print("main()")

    print("NOTE: this script requires besapi v3.3.3+ due to besapi.plugin_utilities")

    parser = besapi.plugin_utilities.setup_plugin_argparse()

    # add additional arg specific to this script:
    parser.add_argument(
        "-q",
        "--query",
        help="client query relevance",
        required=False,
        type=str,
        default=CLIENT_RELEVANCE,
    )
    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    # allow set global scoped vars
    global bes_conn, verbose, invoke_folder
    verbose = args.verbose

    # get folder the script was invoked from:
    invoke_folder = get_invoke_folder()

    log_file_path = os.path.join(
        get_invoke_folder(verbose), get_invoke_file_name(verbose) + ".log"
    )

    print(log_file_path)

    if not verbose or verbose == 0:
        verbose = 1

    # always print to console:
    logging_config = besapi.plugin_utilities.get_plugin_logging_config(
        log_file_path, verbose, True
    )

    logging.basicConfig(**logging_config)

    logging.log(99, "---------- Starting New Session -----------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("Python version: %s", platform.sys.version)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("this plugin's version: %s", __version__)

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    # get the ~10 most recent computers to report into BigFix:
    session_relevance = 'tuple string items (integers in (0,9)) of concatenations ", " of (it as string) of ids of bes computers whose(now - last report time of it < 90 * minute)'

    data = {"output": "json", "relevance": session_relevance}

    # submitting session relevance query using POST to reduce problems:
    result = bes_conn.post(bes_conn.url("query"), data)

    json_result = json.loads(str(result))

    logging.debug("computer ids to target with query: %s", json_result["result"])

    # this is the client relevance we are going to get the results of:
    client_relevance = args.query

    # generate target XML substring from list of computer ids:
    target_xml = (
        "<ComputerID>"
        + "</ComputerID><ComputerID>".join(json_result["result"])
        + "</ComputerID>"
    )

    # python template for ClientQuery BESAPI XML:
    query_payload = f"""<BESAPI>
<ClientQuery>
    <ApplicabilityRelevance>true</ApplicabilityRelevance>
    <QueryText>{client_relevance}</QueryText>
    <Target>
        {target_xml}
    </Target>
</ClientQuery>
</BESAPI>"""

    logging.debug("query_payload: %s", query_payload)

    # send the client query: (need it's ID to get results)
    query_submit_result = bes_conn.post(bes_conn.url("clientquery"), data=query_payload)

    client_query_id = query_submit_result.besobj.ClientQuery.ID

    logging.debug("query_submit_result: %s", query_submit_result)

    logging.debug("client_query_id: %s", client_query_id)

    previous_result = ""
    i = 0
    try:
        # loop ~90 second for results
        while i < 9:
            print("... waiting for results ... Ctrl+C to quit loop")

            # TODO: loop this to keep getting more results until all return or any key pressed
            time.sleep(20)

            # get the actual results:
            # NOTE: this might not return anything if no clients have returned results
            #       this can be checked again and again for more results:
            query_result = bes_conn.get(
                bes_conn.url(f"clientqueryresults/{client_query_id}")
            )

            if previous_result != str(query_result):
                print(query_result)
                previous_result = str(query_result)

            i += 1

            # if not running interactively:
            # https://stackoverflow.com/questions/2356399/tell-if-python-is-in-interactive-mode
            if not sys.__stdin__.isatty():
                logging.info("not interactive, stopping loop")
                break
    except KeyboardInterrupt:
        logging.info("\nloop interrupted by user")

    # log only final result:
    logging.info(previous_result)

    logging.log(99, "---------- Ended Session -----------")


if __name__ == "__main__":
    main()
