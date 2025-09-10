"""
Add a mime field to custom content returned by session relevance.

This example adds a mime field to custom fixlets, tasks, baselines, and analyses that
contain the slower WMI or descendant inspector calls in their relevance, and do not already have
the mime field.

Other candidates for eval mime field addition due to slow relevance:
- anything examining log files
  - (it as lowercase contains ".log%22" AND it as lowercase contains " lines ")
- anything examining large files
- things enumerating `active devices` or `smbios`
- things enumerating the PATH environment variable or other environment variables with many entries
  - ` substrings separated by (";";":") of values of (variables "PATH" of it`
- complicated xpaths of many files
- getting maximum or maxima of modification times of files
- ` of folders of folders `
- ` image files of processes `
- `(now - modification time of it) < `
- ` of active director`
- ` of folders "Logs" of folders "__Global" of `
- complicated package relevance: rpm or debian package or winrt package
- event log relevance: `exists matches (case insensitive regex "records? of[a-z0-9]* event log") of it`
- hashing: `exists matches (case insensitive regex "(md5|sha1|sha2?_?\d{3,4})s? +of +") of it`

Use this session relevance to find fixlets missing the mime field:
- https://bigfix.me/relevance/details/3023816
"""

import logging
import ntpath
import os
import platform
import sys
import urllib.parse

import lxml.etree

import besapi
import besapi.plugin_utilities

MIME_FIELD_NAME = "x-relevance-evaluation-period"
MIME_FIELD_VALUE = "06:00:00"  # 6 hours

# Must return fixlet / task / baseline / analysis objects:
session_relevance_multiple_fixlets = """custom bes fixlets whose(exists (it as lowercase) whose(it contains " wmi" OR it contains " descendant") of relevance of it AND not exists mime fields "x-relevance-evaluation-period" of it)"""
# custom bes fixlets whose(not exists mime fields "x-relevance-evaluation-period" of it) whose(exists (it as lowercase) whose(exists matches (case insensitive regex "records? of[a-z0-9]* event log") of it OR (it contains ".log%22" AND it contains " lines ") OR (it contains " substrings separated by (%22;%22;%22:%22) of values of") OR it contains " wmi" OR it contains " descendant") of relevance of it)

__version__ = "0.1.1"
verbose = 0
bes_conn = None
invoke_folder = None
sites_no_permissions = []


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


def fixlet_xml_add_mime(
    fixlet_xml: str, mime_field_name: str, mime_field_value: str
) -> str | None:
    """Update fixlet XML to add mime field."""
    new_mime = f"""<MIMEField>
        <Name>{mime_field_name}</Name>
        <Value>{mime_field_value}</Value>
    </MIMEField>"""

    # need to check if mime field already exists in case session relevance is behind
    if mime_field_name in str(fixlet_xml).lower():
        logging.warning("Skipping item, it already has mime field")
        return None

    root_xml = lxml.etree.fromstring(fixlet_xml)

    # get first MIMEField
    xml_first_mime = root_xml.find(".//*/MIMEField")

    xml_container = xml_first_mime.getparent()

    # new mime to set relevance eval to once an hour:
    new_mime_lxml = lxml.etree.XML(new_mime)

    # insert new mime BEFORE first MIME
    # https://stackoverflow.com/questions/7474972/append-element-after-another-element-using-lxml
    xml_container.insert(xml_container.index(xml_first_mime), new_mime_lxml)

    # validate against XSD
    besapi.besapi.validate_xsd(
        lxml.etree.tostring(root_xml, encoding="utf-8", xml_declaration=False)
    )

    return lxml.etree.tostring(root_xml, encoding="utf-8", xml_declaration=True).decode(
        "utf-8"
    )


def get_content_restresult(
    bes_conn: besapi.besapi.BESConnection, fixlet_site_name: str, fixlet_id: int
) -> besapi.besapi.RESTResult | None:
    """Get fixlet content by ID and site name.

    This works with fixlets, tasks, baselines, and analyses.
    Might work with other content types too.
    """
    # URL encode the site name to handle special characters
    fixlet_site_name = urllib.parse.quote(fixlet_site_name, safe="")

    site_path = "custom/"

    # site path must be empty string for ActionSite
    if fixlet_site_name == "ActionSite":
        site_path = ""
        # site name must be "master" for ActionSite
        fixlet_site_name = "master"

    fixlet_content = bes_conn.get_content_by_resource(
        f"fixlet/{site_path}{fixlet_site_name}/{fixlet_id}"
    )
    return fixlet_content


def put_updated_xml(
    bes_conn: besapi.besapi.BESConnection,
    fixlet_site_name: str,
    fixlet_id: int,
    updated_xml: str,
) -> besapi.besapi.RESTResult | None:
    """PUT updated XML back to RESTAPI resource to modify.

    This works with fixlets, tasks, baselines, and analyses.
    Might work with other content types too.
    """
    # URL encode the site name to handle special characters
    fixlet_site_name = urllib.parse.quote(fixlet_site_name, safe="")

    # this type works for fixlets, tasks, and baselines
    fixlet_type = "fixlet"

    if "<Analysis>" in updated_xml:
        fixlet_type = "analysis"

    site_path = "custom/"

    # site path must be empty string for ActionSite
    if fixlet_site_name == "ActionSite":
        site_path = ""
        # site name must be "master" for ActionSite
        fixlet_site_name = "master"

    try:
        # PUT changed XML back to RESTAPI resource to modify
        update_result = bes_conn.put(
            f"{fixlet_type}/{site_path}{fixlet_site_name}/{fixlet_id}",
            updated_xml,
            headers={"Content-Type": "application/xml"},
        )
        return update_result
    except PermissionError as exc:
        logging.error(
            "PermissionError updating fixlet %s/%d:%s", fixlet_site_name, fixlet_id, exc
        )
        sites_no_permissions.append(fixlet_site_name)

    return None


def main():
    """Execution starts here.

    This is designed to be run as a plugin, but can also be run as a standalone script.
    """
    print("fixlet_add_mime_field main()")

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
        "Might get permissions error if not run as a MO or an account with write access to all affected custom content"
    )

    bes_conn = besapi.plugin_utilities.get_besapi_connection(args)

    results = bes_conn.session_relevance_json_array(
        "(id of it, name of site of it) of " + session_relevance_multiple_fixlets
    )

    logging.debug(results)

    for result in results:
        fixlet_id = result[0]
        fixlet_site_name = result[1]
        fixlet_site_name_safe = urllib.parse.quote(fixlet_site_name, safe="")

        if fixlet_site_name_safe in sites_no_permissions:
            logging.warning(
                "Skipping item %d, no permissions to update content in site '%s'",
                fixlet_id,
                fixlet_site_name,
            )
            continue

        logging.debug(fixlet_id, fixlet_site_name)

        fixlet_content = get_content_restresult(bes_conn, fixlet_site_name, fixlet_id)

        updated_xml = fixlet_xml_add_mime(
            fixlet_content.besxml, MIME_FIELD_NAME, MIME_FIELD_VALUE
        )

        if updated_xml is None:
            # skip, already has mime field
            continue

        # print(updated_xml)

        update_result = put_updated_xml(
            bes_conn, fixlet_site_name, fixlet_id, updated_xml
        )

        if update_result is not None:
            logging.info("Updated fixlet %d in site %s", fixlet_id, fixlet_site_name)

    logging.info("---------- Ending Session -----------")


if __name__ == "__main__":
    main()
