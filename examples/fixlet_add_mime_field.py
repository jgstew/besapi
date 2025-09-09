"""
Add mime field to custom content.

Use this session relevance to find fixlets missing the mime field:
- https://bigfix.me/relevance/details/3023816
"""

import lxml.etree

import besapi

# Must return fixlet or task objects:
session_relevance_multiple_fixlets = """custom bes fixlets whose(exists (it as lowercase) whose(it contains " wmi" OR it contains " descendant") of relevance of it AND not exists mime fields "x-relevance-evaluation-period" of it)"""


def fixlet_xml_add_mime(fixlet_xml):
    """Update fixlet XML to add mime field."""
    root_xml = lxml.etree.fromstring(fixlet_xml)

    # get first MIMEField
    xml_first_mime = root_xml.find(".//*/MIMEField")

    xml_container = xml_first_mime.getparent()

    # new mime to set relevance eval to once an hour:
    new_mime = lxml.etree.XML(
        """<MIMEField>
			<Name>x-relevance-evaluation-period</Name>
			<Value>01:00:00</Value>
		</MIMEField>"""
    )

    # insert new mime BEFORE first MIME
    # https://stackoverflow.com/questions/7474972/append-element-after-another-element-using-lxml
    xml_container.insert(xml_container.index(xml_first_mime), new_mime)

    # validate against XSD
    besapi.besapi.validate_xsd(
        lxml.etree.tostring(root_xml, encoding="utf-8", xml_declaration=False)
    )

    return lxml.etree.tostring(root_xml, encoding="utf-8", xml_declaration=True).decode(
        "utf-8"
    )


def get_fixlet_content(bes_conn, fixlet_site_name, fixlet_id):
    """Get fixlet content by ID and site name."""
    # may need to escape other chars too?
    fixlet_site_name = fixlet_site_name.replace("/", "%2f")

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


def put_updated_xml(bes_conn, fixlet_site_name, fixlet_id, updated_xml):
    """PUT updated XML back to RESTAPI resource to modify.

    This works with fixlets, tasks, baselines, and analyses.
    """
    # may need to escape other chars too?
    fixlet_site_name = fixlet_site_name.replace("/", "%2f")

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
        print(
            f"ERROR: PermissionError updating fixlet {fixlet_site_name}/{fixlet_id}:{exc}"
        )


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    results = bes_conn.session_relevance_json_array(
        "(id of it, name of site of it) of " + session_relevance_multiple_fixlets
    )

    print(results)

    for result in results:
        fixlet_id = result[0]
        fixlet_site_name = result[1]

        print(fixlet_id, fixlet_site_name)

        fixlet_content = get_fixlet_content(bes_conn, fixlet_site_name, fixlet_id)

        # need to check if mime field already exists in case session relevance is behind
        if "x-relevance-evaluation-period" in fixlet_content.text.lower():
            print(f"INFO: skipping {fixlet_id}, it already has mime field")
            continue

        updated_xml = fixlet_xml_add_mime(fixlet_content.besxml)

        # print(updated_xml)

        _update_result = put_updated_xml(
            bes_conn, fixlet_site_name, fixlet_id, updated_xml
        )

        if _update_result is not None:
            print(f"Updated fixlet {fixlet_id} in site {fixlet_site_name}:")


if __name__ == "__main__":
    main()
