"""
Example sourced fixlet action with parameters

requires `besapi`, install with command `pip install besapi`
"""
import besapi

# reference: https://software.bigfix.com/download/bes/100/util/BES10.0.7.52.xsd
# https://forum.bigfix.com/t/api-sourcedfixletaction-including-end-time/37117/2
# https://forum.bigfix.com/t/secret-parameter-actions/38847/13

CONTENT_XML = r"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
    <SourcedFixletAction>
        <SourceFixlet>
            <Sitename>BES Support</Sitename>
            <FixletID>15</FixletID>
            <Action>Action1</Action>
        </SourceFixlet>
        <Target>
            <ComputerName>BIGFIX</ComputerName>
        </Target>
        <Parameter Name="test_name">test_value</Parameter>
        <Title>Test parameters - SourcedFixletAction - BES Clients Have Incorrect Clock Time</Title>
    </SourcedFixletAction>
</BES>
"""


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    result = bes_conn.post("actions", CONTENT_XML)

    print(result)


if __name__ == "__main__":
    main()
