"""
Add mime field to custom content.

Need to url escape site name https://bigfix:52311/api/sites

TODO: make this work with multiple fixlets, not just one hardcoded

Use this session relevance to find fixlets missing the mime field:
- https://bigfix.me/relevance/details/3023816
"""

import lxml.etree

import besapi

# Must return fixlet or task objects:
session_relevance_multiple_fixlets = """custom bes fixlets whose(exists (it as lowercase) whose(it contains " wmi" OR it contains " descendant") of relevance of it AND not exists mime fields "x-relevance-evaluation-period" of it AND "Demo" = name of site of it)"""


def update_fixlet_xml(fixlet_xml):
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


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    results = bes_conn.session_relevance_json_array(
        "(id of it, name of site of it) of it whose(not analysis flag of it) of "
        + session_relevance_multiple_fixlets
    )

    print(results)

    for result in results:
        fixlet_id = result[0]
        # may need to escape other chars too?
        fixlet_site_name = result[1].replace("/", "%2f")

        print(fixlet_id, fixlet_site_name)

        fixlet_content = bes_conn.get_content_by_resource(
            f"fixlet/custom/{fixlet_site_name}/{fixlet_id}"
        )
        # print(fixlet_content.text)

        # need to check if mime field already exists in case session relevance is behind
        if "x-relevance-evaluation-period" in fixlet_content.text.lower():
            print(f"INFO: skipping {fixlet_id}, it already has mime field")
            continue

        updated_xml = update_fixlet_xml(fixlet_content.besxml)

        # print(updated_xml)

        # PUT changed XML back to RESTAPI resource to modify
        _update_result = bes_conn.put(
            f"fixlet/custom/{fixlet_site_name}/{fixlet_id}",
            updated_xml,
            headers={"Content-Type": "application/xml"},
        )
        print(f"Updated fixlet {result[1]}/{fixlet_id}")

        print(_update_result)
        # break


if __name__ == "__main__":
    main()
