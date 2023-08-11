"""
Get set of mailbox files

requires `besapi`, install with command `pip install besapi`
"""
import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    session_rel = 'tuple string items 0 of concatenations ", " of (it as string) of ids of bes computers whose(root server flag of it AND now - last report time of it < 30 * day)'

    # get root server computer id:
    root_id = int(bes_conn.session_relevance_string(session_rel).strip())

    print(root_id)

    # list mailbox files:
    result = bes_conn.get(bes_conn.url(f"mailbox/{root_id}"))

    print(result)


if __name__ == "__main__":
    main()
