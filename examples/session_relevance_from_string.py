"""
Example session relevance results from a file

requires `besapi`, install with command `pip install besapi`
"""
import json

import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    session_relevance = '(multiplicity of it, it) of unique values of (it as trimmed string) of (preceding text of first "|" of it | it) of values of results of bes properties "Installed Applications - Windows"'

    data = {"output": "json", "relevance": session_relevance}

    result = bes_conn.post(bes_conn.url("query"), data)

    json_result = json.loads(str(result))

    json_string = json.dumps(json_result, indent=2)

    print(json_string)

    with open(
        "examples/session_relevance_query_from_string_output.json", "w"
    ) as file_out:
        file_out.write(json_string)


if __name__ == "__main__":
    main()
