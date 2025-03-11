"""
Get set of mailbox files.

requires `besapi`, install with command `pip install besapi`
"""

import os

import besapi


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    session_rel = 'tuple string items 0 of concatenations ", " of (it as string) of ids of bes computers whose(root server flag of it AND now - last report time of it < 30 * day)'

    # get root server computer id:
    root_id = int(bes_conn.session_relevance_string(session_rel).strip())

    print(root_id)

    file_path = "examples/mailbox_files_create.py"
    file_name = os.path.basename(file_path)

    # https://developer.bigfix.com/rest-api/api/mailbox.html

    # Example Header::  Content-Disposition: attachment; filename="file.xml"
    headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}
    with open(file_path, "rb") as f:
        result = bes_conn.post(
            bes_conn.url(f"mailbox/{root_id}"), data=f, headers=headers
        )

    print(result)


if __name__ == "__main__":
    main()
