"""
Validate BigFix XML file against XML Schema.

requires `besapi`, install with command `pip install besapi`
"""

import besapi


def validate_xml_bes_file(file_path):
    with open(file_path, "rb") as file:
        file_data = file.read()

    return besapi.besapi.validate_xsd(file_data)


def main(file_path):
    """Execution starts here"""
    print("main()")

    print(validate_xml_bes_file(file_path))


if __name__ == "__main__":
    main("./examples/content/RelaySelectAction.bes")
