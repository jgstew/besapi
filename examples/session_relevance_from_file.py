"""
Example session relevance results from a file

requires `besapi`, install with command `pip install besapi`
"""

import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    with open("examples/session_relevance_query_input.txt") as file:
        session_relevance = file.read()

    result = bes_conn.session_relevance_string(session_relevance)

    print(result)

    with open("examples/session_relevance_query_output.txt", "w") as file_out:
        file_out.write(result)


if __name__ == "__main__":
    main()
