"""
import bes file into site

- https://developer.bigfix.com/rest-api/api/import.html

requires `besapi`, install with command `pip install besapi`
"""

import glob

import besapi

SITE_PATH = "custom/demo"

# by default, get all BES files in examples folder:
BES_FOLDER_GLOB = "./examples/*.bes"


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    files = glob.glob(BES_FOLDER_GLOB)
    # import all found BES files into site:
    for f in files:
        # requires besapi 3.1.6
        result = bes_conn.import_bes_to_site(f, SITE_PATH)
        print(result)


if __name__ == "__main__":
    main()
