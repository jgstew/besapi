"""
This will sync baselines that are not in sync.

requires `besapi`, install with command `pip install besapi`

LIMITATION: This does not work with baselines in the actionsite
- Only works on baselines in custom sites

Example Usage:
python baseline_sync_plugin.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

Example Usage with config file:
python baseline_sync_plugin.py

This can also be run as a BigFix Server Plugin Service.

References:
- https://developer.bigfix.com/rest-api/api/admin.html
- https://github.com/jgstew/besapi/blob/master/examples/rest_cmd_args.py
"""

import logging

import besapi.plugin_utilities

__version__ = "1.2.0"
bes_conn = None


def baseline_sync(baseline_id, site_path):
    """Sync a baseline."""
    logging.info("Syncing baseline: %s/%s", site_path, baseline_id)

    # get baseline sync xml:
    results = bes_conn.get(f"baseline/{site_path}/{baseline_id}/sync")

    baseline_xml_sync = results.text

    results = bes_conn.put(
        f"baseline/{site_path}/{baseline_id}", data=baseline_xml_sync
    )

    logging.debug("Sync results: %s", results.text)

    logging.info("Baseline %s/%s synced successfully", site_path, baseline_id)
    return results.text


def process_baseline(baseline_id, site_path):
    """Check a single baseline if it needs syncing."""
    logging.info("Processing baseline: %s/%s", site_path, baseline_id)

    # get baseline xml:
    results = bes_conn.get(f"baseline/{site_path}/{baseline_id}")

    baseline_xml = results.text

    if 'SyncStatus="source fixlet differs"' in baseline_xml:
        logging.info("Baseline %s/%s is out of sync", site_path, baseline_id)
        return baseline_sync(baseline_id, site_path)
    else:
        logging.info("Baseline %s/%s is in sync", site_path, baseline_id)
        return baseline_xml


def process_site(site_path):
    """Process a single site to find baselines to check."""
    logging.info("Processing site: %s", site_path)

    # get site name from end of path:
    # if site_path does not have / then use site_path as site_name
    site_name = site_path.split("/")[-1]

    # get baselines in site:
    session_relevance = f"""ids of fixlets whose(baseline flag of it) of bes custom sites whose(name of it = "{site_name}")"""

    logging.debug("Getting baselines in site: %s", site_name)
    results = bes_conn.session_relevance_json(session_relevance)

    logging.info("Found %i baselines in site: %s", len(results["result"]), site_name)

    for baseline_id in results["result"]:
        process_baseline(baseline_id, site_path)


def main():
    """Execution starts here."""
    global bes_conn

    with besapi.plugin_utilities.init_plugin(__version__) as (_args, conn):
        bes_conn = conn

        logging.warning(
            "Results may be incorrect if not run as a MO or an account without scope of all computers"
        )

        session_relevance = """names of bes custom sites whose(exists fixlets whose(baseline flag of it) of it)"""

        logging.info("Getting custom sites with baselines")
        results = bes_conn.session_relevance_json(session_relevance)

        logging.info(
            "Processing %i custom sites with baselines", len(results["result"])
        )

        logging.debug("Custom sites with baselines:\n%s", results["result"])

        for site in results["result"]:
            try:
                process_site("custom/" + site)
            except PermissionError:
                logging.error(
                    "Error processing site %s: Permission Denied, skipping site.", site
                )
                continue

    return 0


if __name__ == "__main__":
    main()
