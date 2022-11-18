import besapi

# another session relevance option:
# ids of bes actions whose( ("Expired" = state of it OR "Stopped" = state of it) AND (now - time issued of it > 180 * day) )

SESSION_RELEVANCE = """ids of bes actions whose( (targeted by list flag of it OR targeted by id flag of it) AND not reapply flag of it AND not group member flag of it AND "Open"=state of it AND (now - time issued of it) >= 8 * day )"""


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    session_result = bes_conn.session_relevance_array(SESSION_RELEVANCE)

    # print(session_result)

    # https://developer.bigfix.com/rest-api/api/action.html
    for action_id in session_result:
        print("Stopping Action:", action_id)
        action_stop_result = bes_conn.post("action/" + action_id + "/stop", "")
        print(action_stop_result)


if __name__ == "__main__":
    main()
