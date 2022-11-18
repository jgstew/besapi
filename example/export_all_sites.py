import os

import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    os.mkdir("export")

    os.chdir("export")

    bes_conn.export_all_sites()


if __name__ == "__main__":
    main()
