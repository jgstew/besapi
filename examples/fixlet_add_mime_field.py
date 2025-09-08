"""
Add mime field to custom content.

Need to url escape site name https://bigfix:52311/api/sites

TODO: make this work with multiple fixlets, not just one hardcoded

Use this session relevance to find fixlets missing the mime field:
- https://bigfix.me/relevance/details/3023816
"""

import lxml.etree

import besapi

FIXLET_NAME = "Install Microsoft Orca from local SDK - Windows"
MIME_FIELD = "x-relevance-evaluation-period"
session_relevance = (
    'custom bes fixlets whose(name of it = "'
    + FIXLET_NAME
    + '" AND not exists mime fields "'
    + MIME_FIELD
    + '" of it)'
)


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
    xml_container.insert(xml_container.index(xml_first_mime) - 1, new_mime)

    return lxml.etree.tostring(root_xml, encoding="utf-8", xml_declaration=True).decode(
        "utf-8"
    )


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    data = {"relevance": "id of " + session_relevance}

    result = bes_conn.post(bes_conn.url("query"), data)

    # example result: fixlet/custom/Public%2fWindows/21405
    # full example: https://bigfix:52311/api/fixlet/custom/Public%2fWindows/21405
    fixlet_id = int(result.besdict["Query"]["Result"]["Answer"])

    print(fixlet_id)

    data = {"relevance": "name of site of " + session_relevance}

    result = bes_conn.post(bes_conn.url("query"), data)

    fixlet_site_name = str(result.besdict["Query"]["Result"]["Answer"])

    # escape `/` in site name, if applicable
    # do spaces need escaped too? `%20`
    fixlet_site_name = fixlet_site_name.replace("/", "%2f")

    print(fixlet_site_name)

    fixlet_content = bes_conn.get_content_by_resource(
        f"fixlet/custom/{fixlet_site_name}/{fixlet_id}"
    )

    print(
        "\nPreview of new XML:\n ",
        update_fixlet_xml(fixlet_content.besxml),
    )

    # TODO: PUT changed XML back to RESTAPI resource to modify


if __name__ == "__main__":
    main()
