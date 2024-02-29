"""
Delete computers in file

requires `besapi`, install with command `pip install besapi`
"""

import os

import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    # get the directory this script is running within:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    # get the file "computers_delete_by_file.txt" within the folder of the script:
    comp_file_path = os.path.join(script_dir, "computers_delete_by_file.txt")

    comp_file_lines = []
    with open(comp_file_path, "r") as comp_file:
        for line in comp_file:
            line = line.strip()
            if line != "":
                comp_file_lines.append(line)

    # print(comp_file_lines)

    computers = '"' + '";"'.join(comp_file_lines) + '"'

    # by default, this will only return computers that have not reported in >90 days:
    session_relevance = f"unique values of ids of bes computers whose(now - last report time of it > 90 * day AND exists elements of intersections of (it; sets of ({computers})) of sets of (name of it; id of it as string))"

    # get session relevance result of computer ids from list of computer ids or computer names:
    results = bes_conn.session_relevance_array(session_relevance)

    # print(results)

    if "Nothing returned, but no error." in results[0]:
        print("WARNING: No computers found to delete!")
        return None

    # delete computers:
    for item in results:
        if item.strip() != "":
            computer_id = str(int(item))
            print(f"INFO: Attempting to delete Computer ID: {computer_id}")
            result_del = bes_conn.delete(bes_conn.url(f"computer/{computer_id}"))
            if "ok" not in result_del.text:
                print(f"ERROR: {result_del} for id: {computer_id}")
                continue


if __name__ == "__main__":
    main()
