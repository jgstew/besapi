"""
Create an action from a fixlet or task xml bes file
and monitor its results for ~300 seconds.

requires `besapi`, install with command `pip install besapi`

NOTE: this script requires besapi v4.1.4+ due to use of besapi.besapi.get_action_combined_relevance

Example Usage:
python3 examples/action_and_monitor.py -c -vv --file './examples/content/TestEcho-Universal.bes'

Inspect examples/action_and_monitor.log for results
"""

import logging
import ntpath
import os
import platform
import sys
import time

import lxml.etree

import besapi
import besapi.plugin_utilities

__version__ = "1.2.1"
verbose = 0
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


def action_from_bes_file(bes_conn, file_path, targets="<AllComputers>"):
    """Create action from bes file with fixlet or task or single action."""

    action_xml = besapi.besapi.action_xml_from_bes_file(file_path, targets)

    # the validation isn't working, but everything seems valid :(

    # if not besapi.besapi.validate_xsd(action_xml):
    #     err_msg = "Action XML is not valid!"
    #     logging.error(err_msg)
    #     raise ValueError(err_msg)

    action_result = bes_conn.post(bes_conn.url("actions"), data=action_xml)

    logging.info("Action Result:/n%s", action_result)

    action_id = action_result.besobj.Action.ID

    logging.info("Action ID: %s", action_id)

    return action_id


def action_monitor_results(bes_conn, action_id, iterations=30, sleep_time=15):
    """Monitor the results of an action if interactive."""
    previous_result = ""
    i = 0
    try:
        # loop ~300 second for results
        while i < iterations:
            print("... waiting for results ... Ctrl+C to quit loop")

            time.sleep(sleep_time)

            # get the actual results:
            # api/action/ACTION_ID/status?fields=ActionID,Status,DateIssued,DateStopped,StoppedBy,Computer(Status,State,StartTime)
            # NOTE: this might not return anything if no clients have returned results
            #       this can be checked again and again for more results:
            action_status_result = bes_conn.get(
                bes_conn.url(
                    f"action/{action_id}/status?fields=ActionID,Status,DateIssued,DateStopped,StoppedBy,Computer(Status,State,StartTime)"
                )
            )

            if previous_result != str(action_status_result):
                logging.info(action_status_result)
                previous_result = str(action_status_result)

            i += 1

            if action_status_result.besobj.ActionResults.Status == "Stopped":
                logging.info("Action is stopped, halting monitoring loop")
                break

            # if not running interactively:
            # https://stackoverflow.com/questions/2356399/tell-if-python-is-in-interactive-mode
            if not sys.__stdin__.isatty():
                logging.warning("not interactive, stopping loop")
                break
    except KeyboardInterrupt:
        print("\nloop interrupted by user")

    return previous_result


def action_and_monitor(bes_conn, file_path, targets="<AllComputers>"):
    """Take action from bes xml file
    monitor results of action.
    """

    action_id = action_from_bes_file(bes_conn, file_path, targets)

    logging.info("Start monitoring action results:")

    results_action = action_monitor_results(bes_conn, action_id)

    logging.info("End monitoring, Last Result:\n%s", results_action)


def main():
    """Execution starts here."""
    print("main()")

    print(
        "NOTE: this script requires besapi v4.1.4+ due to use of besapi.besapi.get_action_combined_relevance"
    )

    parser = besapi.plugin_utilities.setup_plugin_argparse()

    # add additional arg specific to this script:
    parser.add_argument(
        "-f",
        "--file",
        help="xml bes file to create an action from",
        required=False,
        type=str,
    )
    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    # allow set global scoped vars
    global verbose, invoke_folder
    verbose = args.verbose

    # get folder the script was invoked from:
    invoke_folder = get_invoke_folder()

    log_file_path = os.path.join(
        get_invoke_folder(verbose), get_invoke_file_name(verbose) + ".log"
    )

    print(log_file_path)

    logging_config = besapi.plugin_utilities.get_plugin_logging_config(
        log_file_path, verbose, args.console
    )

    logging.basicConfig(**logging_config)

    logging.log(99, "---------- Starting New Session -----------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("%s's version: %s", get_invoke_file_name(verbose), __version__)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("Python version: %s", platform.sys.version)

    try:
        bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

        # set targeting criteria to computer id int or "<AllComputers>" or array
        targets = 0

        action_and_monitor(bes_conn, args.file, targets)

        logging.log(99, "---------- END -----------")
    except Exception as err:
        logging.error("An error occurred: %s", err)
        sys.exit(1)


if __name__ == "__main__":
    main()
