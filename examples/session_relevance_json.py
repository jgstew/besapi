"""
Example session relevance results in json format

This is much more fragile because it uses GET instead of POST

requires `besapi`, install with command `pip install besapi`
"""
import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    session_relevance = """names of members of bes computer groups whose (name of it = "Windows non-BigFix")"""

    result = bes_conn.get(
        bes_conn.url(f"query?output=json&relevance={session_relevance}")
    )

    print(result)


if __name__ == "__main__":
    main()
