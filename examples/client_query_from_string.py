"""
Example session relevance results from a string

requires `besapi`, install with command `pip install besapi`
"""

import json
import sys
import time

import besapi

CLIENT_RELEVANCE = "(computer names, model name of main processor, (it as string) of (it / (1024 * 1024 * 1024)) of total amount of ram)"


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    # get the ~10 most recent computers to report into BigFix:
    session_relevance = 'tuple string items (integers in (0,9)) of concatenations ", " of (it as string) of ids of bes computers whose(now - last report time of it < 25 * minute)'

    data = {"output": "json", "relevance": session_relevance}

    # submitting session relevance query using POST to reduce problems:
    result = bes_conn.post(bes_conn.url("query"), data)

    json_result = json.loads(str(result))

    # json_string = json.dumps(json_result, indent=2)
    # print(json_string)

    # for item in json_result["result"]:
    #     print(item)

    # this is the client relevance we are going to get the results of:
    client_relevance = CLIENT_RELEVANCE

    # generate target XML substring from list of computer ids:
    target_xml = (
        "<ComputerID>"
        + "</ComputerID><ComputerID>".join(json_result["result"])
        + "</ComputerID>"
    )

    # python template for ClientQuery BESAPI XML:
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

    # send the client query: (need it's ID to get results)
    query_submit_result = bes_conn.post(bes_conn.url("clientquery"), data=query_payload)

    # print(query_submit_result)
    # print(query_submit_result.besobj.ClientQuery.ID)

    previous_result = ""
    i = 0
    try:
        # loop ~90 second for results
        while i < 9:
            print("... waiting for results ... Ctrl+C to quit loop")

            # TODO: loop this to keep getting more results until all return or any key pressed
            time.sleep(10)

            # get the actual results:
            # NOTE: this might not return anything if no clients have returned results
            #       this can be checked again and again for more results:
            query_result = bes_conn.get(
                bes_conn.url(
                    f"clientqueryresults/{query_submit_result.besobj.ClientQuery.ID}"
                )
            )

            if previous_result != str(query_result):
                print(query_result)
                previous_result = str(query_result)

            i += 1

            # if not running interactively:
            # https://stackoverflow.com/questions/2356399/tell-if-python-is-in-interactive-mode
            if not sys.__stdin__.isatty():
                print("not interactive, stopping loop")
                break
    except KeyboardInterrupt:
        print("\nloop interuppted")

    print("script finished")


if __name__ == "__main__":
    main()
