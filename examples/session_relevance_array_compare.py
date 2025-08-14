"""
Get session relevance results from an array and compare the speed of each statement.

requires `besapi`, install with command `pip install besapi`
"""

import json
import time

import besapi

session_relevance_array = ["True", "number of integers in (1,10000000)"]


def get_session_result(session_relevance, bes_conn):
    """Get session relevance result and measure timing."""
    start_time = time.perf_counter()
    data = {"output": "json", "relevance": session_relevance}
    result = bes_conn.post(bes_conn.url("query"), data)
    end_time = time.perf_counter()

    json_result = json.loads(str(result))
    timing = end_time - start_time
    return timing, json_result


def get_evaltime_ms(json_result):
    """Extract evaluation time in milliseconds from JSON result."""
    try:
        return json_result["evaltime_ms"]
    except KeyError:
        return None


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    print("Getting results from array")
    for session_relevance in session_relevance_array:
        timing, result = get_session_result(session_relevance, bes_conn)
        print(f"Took {timing:0.4f} seconds in python for '{session_relevance}'")
        print(f"Eval time in ms: {get_evaltime_ms(result)}")
        print(f"Result for '{session_relevance}':\n{result}")


if __name__ == "__main__":
    main()
