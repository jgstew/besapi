"""
import bes file into site

- https://developer.bigfix.com/rest-api/api/import.html

requires `besapi`, install with command `pip install besapi`
"""

import besapi

SITE_PATH = "custom/demo"
BES_FILE_PATH = "examples/example.bes"


def main():
    """Execution starts here"""
    print("main()")

    print(f"besapi version: {besapi.__version__}")

    if not hasattr(besapi.besapi.BESConnection, "import_bes_to_site"):
        print("version of besapi is too old, must be >= 3.1.6")
        return None

    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    # requires besapi 3.1.6
    result = bes_conn.import_bes_to_site(BES_FILE_PATH, SITE_PATH)

    print(result)


if __name__ == "__main__":
    main()
