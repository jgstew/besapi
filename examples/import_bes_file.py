"""
import bes file into site

requires `besapi`, install with command `pip install besapi`
"""

import besapi

SITE_PATH = "custom/demo"
BES_FILE_PATH = "examples/example.bes"


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    with open(BES_FILE_PATH) as f:
        content = f.read()
        result = bes_conn.post(f"import/{SITE_PATH}", content)
        print(result)


if __name__ == "__main__":
    main()
