"""
This will output members of computer groups to files.

requires `besapi`, install with command `pip install besapi`

Example Usage:
python computer_group_output.py -r https://localhost:52311/api -u API_USER --days 90 -p API_PASSWORD

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


def string_truncate(text, max_length=70):
    """Truncate a string to a maximum length and append ellipsis if truncated."""
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


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
    logging.log(99, "---------- Starting New Session -----------")
    logging.debug("invoke folder: %s", invoke_folder)
    logging.debug("%s's version: %s", get_invoke_file_name(verbose), __version__)
    logging.debug("BESAPI Module version: %s", besapi.besapi.__version__)
    logging.debug("Python version: %s", platform.sys.version)
    logging.warning(
        "Results may be incorrect if not run as a MO or an account without scope of all computers"
    )

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    member_join_str = ";;;"
    session_relevance = f"""(item 0 of it & " - " & item 1 of it, item 2 of it) of ( ( (if automatic flag of it then "Automatic" else NOTHING) ; (if manual flag of it then "Manual" else NOTHING) ; (if server based flag of it then "Server" else NOTHING) ), names of it, concatenations "{member_join_str}" of names of members of it ) of bes computer groups"""

    logging.info("Getting computer group membership information")
    results = bes_conn.session_relevance_json(session_relevance)

    logging.info("Writing computer group membership to files")

    for result in results["result"]:
        group_members_str = result[1].strip()
        group_name = result[0].strip()

        if group_members_str == "":
            logging.warning("Group '%s' has no members, skipping it.", group_name)
            continue

        # split group_members_str on member_join_str
        group_members = group_members_str.split(member_join_str)

        logging.debug("GroupName: %s  > %d members", group_name, len(group_members))
        logging.debug("GroupMembers: %s", string_truncate(group_members_str))

        # write group members to file
        with open(f"{group_name}.txt", "w", encoding="utf-8") as f:
            f.writelines("\n".join(group_members))

    logging.log(99, "---------- Ending Session -----------")


if __name__ == "__main__":
    main()
