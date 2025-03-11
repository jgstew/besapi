"""
Delete tasks by id.

- https://developer.bigfix.com/rest-api/api/task.html

requires `besapi`, install with command `pip install besapi`
"""

import besapi


def main():
    """Execution starts here."""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    ids = [0, "0"]

    # https://developer.bigfix.com/rest-api/api/task.html
    # task/{site type}/{site name}/{task id}

    for id in ids:
        rest_url = f"task/custom/CUSTOM_SITE_NAME/{int(id)}"
        print(f"Deleting: {rest_url}")
        result = bes_conn.delete(rest_url)
        print(result.text)


if __name__ == "__main__":
    main()
