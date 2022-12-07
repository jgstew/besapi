"""
Example session relevance results in json format

This is much more fragile because it uses GET instead of POST

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

    data = {"output": "json", "relevance": session_relevance}

    result = bes_conn.post(bes_conn.url("query"), data)

    print(result)


if __name__ == "__main__":
    main()
