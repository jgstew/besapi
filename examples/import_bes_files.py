"""
Import bes file into site.

- https://developer.bigfix.com/rest-api/api/import.html

requires `besapi`, install with command `pip install besapi`
"""

import glob

import besapi

SITE_PATH = "custom/demo"

# by default, get all BES files in examples folder:
BES_FOLDER_GLOB = "./examples/*.bes"


def main():
    """Execution starts here."""
    print("main()")

    print(f"besapi version: {besapi.__version__}")

    if not hasattr(besapi.besapi.BESConnection, "import_bes_to_site"):
        print("version of besapi is too old, must be >= 3.1.6")
        return None

    files = glob.glob(BES_FOLDER_GLOB)

    if len(files) > 0:
        bes_conn = besapi.besapi.get_bes_conn_using_config_file()
        bes_conn.login()
    else:
        print(f"No BES Files found using glob: {BES_FOLDER_GLOB}")
        return None

    # import all found BES files into site:
    for f in files:
        print(f"Importing file: {f}")
        # requires besapi 3.1.6
        result = bes_conn.import_bes_to_site(f, SITE_PATH)
        print(result)


if __name__ == "__main__":
    print(main())
