"""
Create an action from a fixlet or task xml bes file
and monitor it's results for ~300 seconds

requires `besapi`, install with command `pip install besapi`

NOTE: this script requires besapi v3.3.3+ due to use of besapi.plugin_utilities

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
import typing

import lxml.etree

import besapi
import besapi.plugin_utilities

__version__ = "1.2.1"
verbose = 0
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


def get_action_combined_relevance(relevances: typing.List[str]):
    """take array of ordered relevance clauses and return relevance string for action"""

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


def get_target_xml(targets="<AllComputers>"):
    """get target xml based upon input

    Input can be a single string:
        - starts with "<AllComputers>" if all computers should be targeted
        - Otherwise will be interpreted as custom relevance

    Input can be a single int:
        - Single Computer ID Target

    Input can be an array:
        - Array of Strings: ComputerName
        - Array of Integers: ComputerID
    """

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

    logging.warning("No valid targeting found, will target no computers.")

    # default if invalid:
    return "<CustomRelevance>False</CustomRelevance>"


def action_from_bes_file(bes_conn, file_path, targets="<AllComputers>"):
    """create action from bes file with fixlet or task"""
    # default to empty string:
    custom_relevance_xml = ""

    tree = lxml.etree.parse(file_path)

    # //BES/*[self::Task or self::Fixlet]/*[@id='elid']/name()
    bes_type = str(tree.xpath("//BES/*[self::Task or self::Fixlet]")[0].tag)

    logging.debug("BES Type: %s", bes_type)

    title = tree.xpath(f"//BES/{bes_type}/Title/text()")[0]

    logging.debug("Title: %s", title)

    actionscript = tree.xpath(f"//BES/{bes_type}/DefaultAction/ActionScript/text()")[0]

    logging.debug("ActionScript: %s", actionscript)

    try:
        success_criteria = tree.xpath(
            f"//BES/{bes_type}/DefaultAction/SuccessCriteria/@Option"
        )[0]
    except IndexError:
        # set success criteria if missing: (default)
        success_criteria = "RunToCompletion"
        if bes_type == "Fixlet":
            # set success criteria if missing: (Fixlet)
            success_criteria = "OriginalRelevance"

    if success_criteria == "CustomRelevance":
        custom_relevance = tree.xpath(
            f"//BES/{bes_type}/DefaultAction/SuccessCriteria/text()"
        )[0]
        custom_relevance_xml = f"<![CDATA[{custom_relevance}]]>"

    logging.debug("success_criteria: %s", success_criteria)

    relevance_clauses = tree.xpath(f"//BES/{bes_type}/Relevance/text()")

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
		<SuccessCriteria Option="{success_criteria}">{custom_relevance_xml}</SuccessCriteria>
		<Target>
            { get_target_xml(targets) }
        </Target>
	</SingleAction>
</BES>
"""

    logging.debug("Action XML:\n%s", action_xml)

    action_result = bes_conn.post(bes_conn.url("actions"), data=action_xml)

    logging.info("Action Result:/n%s", action_result)

    action_id = action_result.besobj.Action.ID

    logging.info("Action ID: %s", action_id)

    return action_id


def action_monitor_results(bes_conn, action_id, iterations=30, sleep_time=15):
    """monitor the results of an action if interactive"""
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
        print("\nloop interuppted")

    return previous_result


def action_and_monitor(bes_conn, file_path, targets="<AllComputers>"):
    """Take action from bes xml file
    monitor results of action"""

    action_id = action_from_bes_file(bes_conn, file_path, targets)

    logging.info("Start monitoring action results:")

    results_action = action_monitor_results(bes_conn, action_id)

    logging.info("End monitoring, Last Result:\n%s", results_action)


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

    logging.info("---------- Starting New Session -----------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("%s's version: %s", get_invoke_file_name(verbose), __version__)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("Python version: %s", platform.sys.version)

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    # set targeting criteria to computer id int or "<AllComputers>" or array
    targets = 0

    action_and_monitor(bes_conn, args.file, targets)

    logging.info("---------- END -----------")


if __name__ == "__main__":
    main()
