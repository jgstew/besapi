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

    site_type = "custom"
    site_name = "Demo"
    content_type = "task"

    for content_id in ids:
        rest_url = f"{content_type}/{site_type}/{site_name}/{int(content_id)}"
        print(f"Deleting: {rest_url}")
        result = bes_conn.delete(rest_url)
        print(result.text)


if __name__ == "__main__":
    main()
