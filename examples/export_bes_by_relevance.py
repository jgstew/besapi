"""
Example export bes files by session relevance result

requires `besapi`, install with command `pip install besapi`
"""
import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    print(bes_conn.last_connected)

    session_relevance = '(type of it as lowercase & "/custom/" & name of site of it & "/" & id of it as string) of custom bes fixlets whose(name of it as lowercase contains "oracle")'

    result = bes_conn.session_relevance_array(session_relevance)

    for item in result:
        print(item)
        # export bes file:
        print(bes_conn.export_item_by_resource(item))


if __name__ == "__main__":
    main()
