"""
This will export all bigfix sites to a folder called `export`.

This is equivalent of running `python -m besapi export_all_sites`
"""

import os

import besapi


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    # Create export folder if it doesn't exist
    try:
        os.mkdir("export")
    except FileExistsError:
        pass

    # Change working directory to export folder
    os.chdir("export")

    # Export all sites
    bes_conn.export_all_sites()


if __name__ == "__main__":
    main()
