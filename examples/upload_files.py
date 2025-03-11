"""
Upload files in folder.

requires `besapi`, install with command `pip install besapi`
"""

import os

import besapi


def main(path_folder="./tmp"):
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    print(f"INFO: Uploading new files within: {os.path.abspath(path_folder)}")

    for entry in os.scandir(path_folder):
        if entry.is_file() and "README.md" not in entry.path:
            # this check for spaces is not required for besapi>=3.1.9
            if " " in os.path.basename(entry.path):
                print(f"ERROR: files cannot contain spaces! skipping: {entry.path}")
                continue
            print(f"Processing: {entry.path}")
            output = bes_conn.upload(entry.path)
            # print(output)
            print(bes_conn.parse_upload_result_to_prefetch(output))


if __name__ == "__main__":
    main("./examples/upload_files")
