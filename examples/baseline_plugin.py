# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "besapi[plugins]>=4.3.1",
# ]
#
# [tool.uv]
# # supply chain: skip releases under a week old, except besapi itself:
# exclude-newer = "7 days"
# exclude-newer-package = { besapi = false }
# ///
"""
Generate patching baselines from sites.

Config is read from `baseline_plugin.config.yaml` next to this script.

requires `besapi[plugins]`, install with command `pip install besapi[plugins]`

or run it with its PEP 723 dependencies installed automatically:
`uv run baseline_plugin.py`

Example Usage:
python baseline_plugin.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

Example Usage with besapi config file:
python baseline_plugin.py

This can also be run as a BigFix Server Plugin Service.

References:
- https://github.com/jgstew/besapi/blob/master/examples/rest_cmd_args.py
- https://github.com/jgstew/besapi/blob/master/examples/baseline_by_relevance.py
"""

import datetime
import logging
import os

import besapi.plugin_utilities

__version__ = "1.3.0"
bes_conn = None


def create_baseline_from_site(site):
    """Create a patching baseline from a site config.

    References:
    - https://github.com/jgstew/besapi/blob/master/examples/baseline_by_relevance.py
    """

    site_name = site["name"]

    # create action automatically?
    auto_remediate = site["auto_remediate"] if "auto_remediate" in site else False

    # eval old baselines?
    superseded_eval = site["superseded_eval"] if "superseded_eval" in site else False

    logging.info("Create patching baseline for site: %s", site_name)

    # Example:
    # fixlets of bes sites whose(exists (it as trimmed string as lowercase) whose(it = "Updates for Windows Applications Extended" as trimmed string as lowercase) of (display names of it; names of it))
    fixlets_rel = f'fixlets of bes sites whose(exists (it as trimmed string as lowercase) whose(it = "{site_name}" as trimmed string as lowercase) of (display names of it; names of it))'

    session_relevance = f"""(it as string) of (url of site of it, ids of it, content id of default action of it | "Action1") of it whose(exists default action of it AND globally visible flag of it AND name of it does not contain "(Superseded)" AND exists applicable computers whose(now - last report time of it < 60 * day) of it) of {fixlets_rel}"""

    result = bes_conn.session_relevance_array(session_relevance)

    num_items = len(result)

    if num_items > 1:
        logging.info("Number of items to add to baseline: %s", num_items)

        baseline_components = ""

        IncludeInRelevance = "true"

        fixlet_ids_str = "0"

        if num_items > 100:
            IncludeInRelevance = "false"

        for item in result:
            tuple_items = item.split(", ")
            fixlet_ids_str += " ; " + tuple_items[1]
            baseline_components += f"""
            <BaselineComponent IncludeInRelevance="{IncludeInRelevance}" SourceSiteURL="{tuple_items[0]}" SourceID="{tuple_items[1]}" ActionName="{tuple_items[2]}" />"""

        logging.debug(baseline_components)

        superseded_eval_rel = ""

        if superseded_eval:
            superseded_eval_rel = ' OR ( exists (it as string as integer) whose(it = 1) of values of settings whose(name of it ends with "_EnableSupersededEval" AND name of it contains "BESClient_") of client )'

        # only have the baseline be relevant for 60 days after creation:
        baseline_rel = f'( exists absolute values whose(it < 60 * day) of (current date - "{datetime.datetime.today().strftime("%d %b %Y")}" as date) ){superseded_eval_rel}'

        if num_items > 100:
            site_rel_query = f"""unique value of site level relevances of bes sites whose(exists (it as trimmed string as lowercase) whose(it = "{site_name}" as trimmed string as lowercase) of (display names of it; names of it))"""
            site_rel = bes_conn.session_relevance_string(site_rel_query)

            baseline_rel = f"""( {baseline_rel} ) AND ( {site_rel} )"""

        # # This does not appear to work as expected:
        # # create baseline relevance such that only relevant if 1+ fixlet is relevant
        # if num_items > 100:
        #     baseline_rel = f"""exists relevant fixlets whose(id of it is contained by set of ({ fixlet_ids_str })) of sites whose("Fixlet Site" = type of it AND "{ site_name }" = name of it)"""

        # generate XML for baseline with template:
        baseline_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
        <Baseline>
            <Title>Remediations from {site_name} - {datetime.datetime.today().strftime('%Y-%m-%d')}</Title>
            <Description />
            <Relevance><![CDATA[{baseline_rel}]]></Relevance>
            <Delay>PT12H</Delay>
            <BaselineComponentCollection>
            <BaselineComponentGroup>{baseline_components}
            </BaselineComponentGroup>
            </BaselineComponentCollection>
        </Baseline>
        </BES>"""

        logging.debug("Baseline XML:\n%s", baseline_xml)

        file_path = "tmp_baseline.bes"

        # the custom site to import the baseline into:
        import_site_name = "Demo"
        site_path = f"custom/{import_site_name}"

        # Does not work through console import:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(baseline_xml)

        logging.info("Importing generated baseline for %s ...", site_name)
        import_result = bes_conn.import_bes_to_site(file_path, site_path)

        logging.info("Result: Import XML:\n%s", import_result)

        os.remove(file_path)

        if auto_remediate:
            baseline_id = import_result.besobj.Baseline.ID

            # get targeting xml with relevance
            # target only machines currently relevant
            target_rel = f'("<ComputerID>" & it & "</ComputerID>") of concatenations "</ComputerID><ComputerID>" of (it as string) of ids of elements of unions of applicable computer sets of it whose(exists default action of it AND globally visible flag of it AND name of it does not contain "(Superseded)") of {fixlets_rel}'

            targeting_result = bes_conn.session_relevance_json_string(target_rel)

            offer_xml = ""

            offer_action = site["offer_action"] if "offer_action" in site else True

            if offer_action:
                logging.info("creating baseline offer action...")
                offer_xml = """<IsOffer>true</IsOffer>
            <AnnounceOffer>false</AnnounceOffer>
            <OfferCategory>Remediation</OfferCategory>
            <OfferDescriptionHTML><![CDATA[Offer to remediate issues.]]></OfferDescriptionHTML>"""
            else:
                logging.info("creating baseline action...")

            BES_SourcedFixletAction = f"""\
        <BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
            <SourcedFixletAction>
                <SourceFixlet>
                    <Sitename>{import_site_name}</Sitename>
                    <FixletID>{baseline_id}</FixletID>
                    <Action>Action1</Action>
                </SourceFixlet>
                <Target>
                    {targeting_result}
                </Target>
                <Settings>
                    <HasEndTime>true</HasEndTime>
                    <EndDateTimeLocalOffset>P10D</EndDateTimeLocalOffset>
                    <ContinueOnErrors>true</ContinueOnErrors>
                    <PostActionBehavior Behavior="Nothing"></PostActionBehavior>{offer_xml}
                </Settings>
            </SourcedFixletAction>
        </BES>
        """

            logging.debug("Action XML:\n%s", BES_SourcedFixletAction)

            action_result = bes_conn.post("actions", BES_SourcedFixletAction)

            logging.info("Result: Action XML:\n%s", action_result)


def process_baselines(config):
    """Generate baselines for each site in config."""

    for site in config:
        create_baseline_from_site(site)


def main():
    """Execution starts here."""
    global bes_conn

    with besapi.plugin_utilities.init_plugin(__version__) as (_args, conn):
        bes_conn = conn

        config = besapi.plugin_utilities.get_plugin_config()
        automation = config["bigfix"]["content"]["Baselines"]["automation"]

        if besapi.plugin_utilities.consume_trigger_file(
            automation["trigger_file_path"]
        ):
            process_baselines(automation["sites"])
        else:
            logging.info("Trigger File Does Not Exist, skipping execution!")

    return 0


if __name__ == "__main__":
    main()
