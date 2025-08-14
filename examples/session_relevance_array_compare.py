"""
Get session relevance results from an array and compare the speed of each statement.

requires `besapi`, install with command `pip install besapi`
"""

import json
import time

import besapi

session_relevance_array = ["True", "number of integers in (1,10000000)"]


def get_session_result(session_relevance, bes_conn, iterations=1):
    """Get session relevance result and measure timing.

    returns a tuple: (timing_py, timing_eval, json_result)
    """

    data = {"output": "json", "relevance": session_relevance}

    total_time_py = 0
    total_time_eval = 0
    result = None
    for i in range(iterations):
        start_time = time.perf_counter()
        result = bes_conn.post(bes_conn.url("query"), data)
        end_time = time.perf_counter()
        total_time_py += end_time - start_time
        json_result = json.loads(str(result))
        total_time_eval += get_evaltime_ms(json_result)

        if i < iterations - 1:
            time.sleep(1)

    timing_py = total_time_py / iterations

    timing_eval = total_time_eval / iterations

    return timing_py, timing_eval, json_result


def get_evaltime_ms(json_result):
    """Extract evaluation time in milliseconds from JSON result."""
    try:
        return float(json_result["evaltime_ms"]) / 1000
    except KeyError:
        return None


def string_truncate(text, max_length=70):
    """Truncate a string to a maximum length and append ellipsis if truncated."""
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    iterations = 2

    print("\n---- Getting results from array: ----")
    print(f"- timing averaged over {iterations} iterations -\n")
    for session_relevance in session_relevance_array:
        timing, timing_eval, result = get_session_result(
            session_relevance, bes_conn, iterations
        )
        print(f" API took: {timing:0.4f} seconds")
        print(f"Eval time: {timing_eval:0.4f} seconds")
        print(
            f"Result array for '{string_truncate(session_relevance)}':\n{result['result']}\n"
        )

    print("---------------- END ----------------")


if __name__ == "__main__":
    main()
