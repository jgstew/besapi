"""
Get dashboard variable value.

requires `besapi` v3.2.6+

install with command `pip install -U besapi`
"""

import besapi


def main():
    """Execution starts here."""
    print("main()")

    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    print(bes_conn.last_connected)

    print(bes_conn.get_dashboard_variable_value("WebUIAppAdmin", "Current_Sites"))

    # dashboard_name = "PyBESAPITest"
    # var_name = "TestVar"

    # print(
    #     bes_conn.set_dashboard_variable_value(
    #         dashboard_name, var_name, "dashboard_variable_get_value.py 12345678"
    #     )
    # )

    # print(bes_conn.get_dashboard_variable_value(dashboard_name, var_name))

    # print(bes_conn.delete(f"dashboardvariable/{dashboard_name}/{var_name}"))


if __name__ == "__main__":
    main()
