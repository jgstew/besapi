"""
Create an action from fixlet/task xml bes file and monitor it's results for ~300 seconds

requires `besapi`, install with command `pip install besapi`
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

__version__ = "1.0.1"
verbose = 0
bes_conn = None
invoke_folder = None


def get_invoke_folder(verbose=0):
    """Get the folder the script was invoked from"""
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
    """Get the filename the script was invoked from"""
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


def get_action_combined_relevance(relevances):
    """take array of ordered relevance clauses and return relevance string for action"""

    relevance = ""

    if not relevances:
        return "False"
    if len(relevances) == 0:
        return "False"
    if len(relevances) == 1:
        return relevances[0]
    if len(relevances) > 0:
        for clause in relevances:
            if len(relevance) == 0:
                relevance = clause
            else:
                relevance = "( " + relevance + " ) AND ( " + clause + " )"

    return relevance


def main():
    """Execution starts here"""
    print("main()")

    print("NOTE: this script requires besapi v3.3.3+ due to besapi.plugin_utilities")

    parser = besapi.plugin_utilities.setup_plugin_argparse()

    # add additonal arg specific to this script:
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
    global bes_conn, verbose, invoke_folder
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

    logging.info("---------- Starting New Session -----------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("Python version: %s", platform.sys.version)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("this plugin's version: %s", __version__)

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    tree = lxml.etree.parse(args.file)
    title = tree.xpath("//BES/*[self::Task or self::Fixlet]/Title/text()")[0]

    logging.debug("Title: %s", title)

    actionscript = tree.xpath(
        "//BES/*[self::Task or self::Fixlet]/DefaultAction/ActionScript/text()"
    )[0]

    logging.debug("ActionScript: %s", actionscript)

    try:
        success_criteria = tree.xpath(
            "//BES/*[self::Task or self::Fixlet]/DefaultAction/SuccessCriteria/@Option"
        )[0]
    except IndexError:
        # TODO: check if task or fixlet first?
        success_criteria = "RunToCompletion"

    if success_criteria == "CustomRelevance":
        # TODO: add handling for CustomRelevance case?
        logging.error("SuccessCriteria = %s is not handled!", success_criteria)
        sys.exit(1)

    logging.debug("success_criteria: %s", success_criteria)

    relevance_clauses = tree.xpath(
        "//BES/*[self::Task or self::Fixlet]/Relevance/text()"
    )

    logging.debug("Relevances: %s", relevance_clauses)

    relevance_clauses_combined = get_action_combined_relevance(relevance_clauses)

    logging.debug("Relevance Combined: %s", relevance_clauses_combined)

    action_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
	<SingleAction>
		<Title>{title}</Title>
		<Relevance><![CDATA[{relevance_clauses_combined}]]></Relevance>
		<ActionScript MIMEType="application/x-Fixlet-Windows-Shell"><![CDATA[// Start:
{actionscript}
// End]]></ActionScript>
		<SuccessCriteria Option="{success_criteria}"></SuccessCriteria>
		<Target>
            <AllComputers>true</AllComputers>
        </Target>
	</SingleAction>
</BES>
"""

    logging.debug("Action XML:\n%s", action_xml)

    action_result = bes_conn.post(bes_conn.url("actions"), data=action_xml)

    logging.info("Action Result:/n%s", action_result)

    action_id = action_result.besobj.Action.ID

    logging.info("Action ID: %s", action_id)

    logging.info("Monitoring action results:")

    previous_result = ""
    i = 0
    try:
        # loop ~300 second for results
        while i < 30:
            print("... waiting for results ... Ctrl+C to quit loop")

            time.sleep(10)

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
        print("\nloop interuppted")

    logging.info("---------- END -----------")


if __name__ == "__main__":
    main()
