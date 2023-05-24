"""
create baseline by session relevance result

requires `besapi`, install with command `pip install besapi`
"""
import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    print(bes_conn.last_connected)

    # change the relevance here to adjust which content gets put in a baseline:
    fixlets_rel = 'fixlets whose(name of it starts with "Update:") of bes sites whose( external site flag of it AND name of it = "Updates for Windows Applications Extended" )'

    # this does not currently work with things in the actionsite:
    session_relevance = f"""(it as string) of (url of site of it, ids of it, content id of default action of it) of it whose(exists default action of it AND globally visible flag of it AND name of it does not contain "(Superseded)" AND exists applicable computers of it) of {fixlets_rel}"""

    result = bes_conn.session_relevance_array(session_relevance)

    # print(result)

    baseline_components = ""

    for item in result:
        # print(item)
        tuple_items = item.split(", ")
        baseline_components += f"""
        <BaselineComponent IncludeInRelevance="true" SourceSiteURL="{tuple_items[0]}" SourceID="{tuple_items[1]}" ActionName="{tuple_items[2]}" />"""
        break

    # print(baseline_components)

    baseline = f"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
  <Baseline>
    <Title>Custom Patching Baseline</Title>
    <Description />
    <Relevance>true</Relevance>
    <BaselineComponentCollection>
      <BaselineComponentGroup>{baseline_components}
      </BaselineComponentGroup>
    </BaselineComponentCollection>
  </Baseline>
</BES>"""

    print(baseline)

    with open("baseline.bes", "w") as f:
        f.write(baseline)

    print("WARNING: Work In Progress, resulting baseline is not yet correct")


if __name__ == "__main__":
    main()
