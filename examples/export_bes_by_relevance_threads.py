"""
Example export bes files by session relevance result

requires `besapi`, install with command `pip install besapi`
"""
import concurrent.futures
import time

import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    print(bes_conn.last_connected)

    # change the relevance here to adjust which content gets exported:
    fixlets_rel = 'custom bes fixlets whose(name of it as lowercase contains "oracle")'

    # this does not currently work with things in the actionsite:
    session_relevance = f'(type of it as lowercase & "/custom/" & name of site of it & "/" & id of it as string) of {fixlets_rel}'

    result = bes_conn.session_relevance_array(session_relevance)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(bes_conn.export_item_by_resource, item, "./tmp/")
            for item in result
        ]
        # Wait for all tasks to complete
        concurrent.futures.wait(futures)


if __name__ == "__main__":
    # Start the timer
    start_time = time.time()
    main()
    # Calculate the elapsed time
    elapsed_time = time.time() - start_time
    print(f"Execution time: {elapsed_time:.2f} seconds")
