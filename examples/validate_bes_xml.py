"""
Validate BigFix XML file against XML Schema.

requires `besapi`, install with command `pip install besapi`
"""

import besapi


def main(file_path):
    """Execution starts here"""
    print("main()")

    with open(file_path, "rb") as file:
        file_data = file.read()

    print(besapi.besapi.validate_xsd(file_data))


if __name__ == "__main__":
    main("./examples/content/RelaySelectAction.bes")
