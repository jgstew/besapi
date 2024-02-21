"""
Example session relevance results from a string

requires `besapi`, install with command `pip install besapi`

Example Usage:
python rest_cmd_args.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD
"""

import argparse
import json

import besapi


def main():
    """Execution starts here"""
    print("main()")

    parser = argparse.ArgumentParser(
        description="Provde command line arguments for REST URL, username, and password"
    )
    parser.add_argument("-r", "--rest-url", help="Specify the REST URL", required=True)
    parser.add_argument(
        "-besserver", "--besserver", help="Specify the BES URL", required=False
    )
    parser.add_argument("-u", "--user", help="Specify the username", required=True)
    parser.add_argument("-p", "--password", help="Specify the password", required=True)
    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    rest_url = args.rest_url

    if rest_url.endswith("/api"):
        rest_url = rest_url.replace("/api", "")

    try:
        bes_conn = besapi.besapi.BESConnection(args.user, args.password, rest_url)
        # bes_conn.login()
    except (ConnectionRefusedError, besapi.besapi.requests.exceptions.ConnectionError):
        # print(args.besserver)
        bes_conn = besapi.besapi.BESConnection(args.user, args.password, args.besserver)

    session_relevance = 'unique values of (it as trimmed string) of (preceding text of last " (" of it | it) of operating systems of bes computers'

    data = {"output": "json", "relevance": session_relevance}

    result = bes_conn.post(bes_conn.url("query"), data)

    json_result = json.loads(str(result))

    json_string = json.dumps(json_result, indent=2)

    print(json_string)


if __name__ == "__main__":
    main()
