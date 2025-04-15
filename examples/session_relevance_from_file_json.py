"""
Example session relevance results in json format.

This is much more fragile because it uses GET instead of POST

requires `besapi`, install with command `pip install besapi`
"""

import json
import urllib.parse

import besapi


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    with open("examples/session_relevance_query_input.txt") as file:
        session_relevance = file.read()

    session_relevance = urllib.parse.quote(session_relevance, safe=":+")
    data = {"output": "json", "relevance": session_relevance}

    result = bes_conn.post(bes_conn.url("query"), data)

    if __debug__:
        print(result)

    json_result = json.loads(str(result))

    json_string = json.dumps(json_result, indent=2)

    if __debug__:
        print(json_string)

    with open(
        "examples/session_relevance_query_from_file_output.json", "w"
    ) as file_out:
        file_out.write(json_string)


if __name__ == "__main__":
    main()
