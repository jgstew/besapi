"""
create baseline by session relevance result

requires `besapi`, install with command `pip install besapi`
"""
import datetime
import os

import besapi


def main():
    """Execution starts here"""
    print("main()")
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    print(bes_conn.last_connected)

    # change the relevance here to adjust which content gets put in a baseline:
    fixlets_rel = 'fixlets whose(name of it starts with "Update:") of bes sites whose( external site flag of it AND name of it = "Updates for Windows Applications Extended" )'

    # this gets the info needed from the items to make the baseline:
    session_relevance = f"""(it as string) of (url of site of it, ids of it, content id of default action of it | "Action1") of it whose(exists default action of it AND globally visible flag of it AND name of it does not contain "(Superseded)" AND exists applicable computers of it) of {fixlets_rel}"""

    print("getting items to add to baseline...")
    result = bes_conn.session_relevance_array(session_relevance)
    print(f"{len(result)} items found")

    # print(result)

    baseline_components = ""

    for item in result:
        # print(item)
        tuple_items = item.split(", ")
        baseline_components += f"""
        <BaselineComponent IncludeInRelevance="true" SourceSiteURL="{tuple_items[0]}" SourceID="{tuple_items[1]}" ActionName="{tuple_items[2]}" />"""

    # print(baseline_components)

    baseline = f"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
  <Baseline>
    <Title>Custom Patching Baseline {datetime.datetime.today().strftime('%Y-%m-%d')}</Title>
    <Description />
    <Relevance>true</Relevance>
    <BaselineComponentCollection>
      <BaselineComponentGroup>{baseline_components}
      </BaselineComponentGroup>
    </BaselineComponentCollection>
  </Baseline>
</BES>"""

    # print(baseline)

    file_path = "tmp_baseline.bes"

    # Does not work through console import:
    with open(file_path, "w") as f:
        f.write(baseline)

    print("Importing generated baseline...")
    import_result = bes_conn.import_bes_to_site(file_path, "custom/Demo")

    print(import_result)

    os.remove(file_path)

    print("Finished")


if __name__ == "__main__":
    main()
