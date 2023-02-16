"""
Get existing upload info by sha1 and filename

requires `besapi`, install with command `pip install besapi`
"""

import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    result = bes_conn.get_upload(
        "test_besapi_upload.txt", "092bd8ef7b91507bb3848640ef47bb392e7d95b1"
    )

    print(result)

    if result:
        print(bes_conn.parse_upload_result_to_prefetch(result))
        print("Info: Upload found.")
    else:
        print("ERROR: Upload not found!")


if __name__ == "__main__":
    main()
