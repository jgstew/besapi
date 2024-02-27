"""
Example session relevance results from a string

requires `besapi`, install with command `pip install besapi`
"""

import json
import time

import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    # get the ~10 most recent computers to report into BigFix:
    session_relevance = 'tuple string items (integers in (0,9)) of concatenations ", " of (it as string) of ids of bes computers whose(now - last report time of it < 25 * minute)'

    data = {"output": "json", "relevance": session_relevance}

    result = bes_conn.post(bes_conn.url("query"), data)

    json_result = json.loads(str(result))

    # json_string = json.dumps(json_result, indent=2)
    # print(json_string)

    # print()

    for item in json_result["result"]:
        print(item)

    # this is the client relevance we are going to get the results of:
    client_relevance = "(computer names, operating systems)"

    target_xml = (
        "<ComputerID>"
        + "</ComputerID><ComputerID>".join(json_result["result"])
        + "</ComputerID>"
    )

    query_payload = f"""<BESAPI>
<ClientQuery>
    <ApplicabilityRelevance>true</ApplicabilityRelevance>
    <QueryText>{client_relevance}</QueryText>
    <Target>
        {target_xml}
    </Target>
</ClientQuery>
</BESAPI>"""

    # print(query_payload)

    query_submit_result = bes_conn.post(bes_conn.url("clientquery"), data=query_payload)

    # print(query_submit_result)
    # print(query_submit_result.besobj.ClientQuery.ID)

    print("... waiting for results ...")

    # TODO: loop this to keep getting more results until all return or any key pressed
    time.sleep(20)

    query_result = bes_conn.get(
        bes_conn.url(f"clientqueryresults/{query_submit_result.besobj.ClientQuery.ID}")
    )

    print(query_result)


if __name__ == "__main__":
    main()
