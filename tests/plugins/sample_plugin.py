"""A minimal BigFix plugin used only by the test suite.

It exercises besapi.plugin_utilities the way a real plugin does: it runs as
`__main__`, so invoke path, logging, config and trigger file handling are all
resolved relative to wherever this script is copied to.

The last line of stdout is a JSON report of what the SDK resolved, which
tests/test_sample_plugin.py checks.
"""

import json
import logging
import sys

import besapi.plugin_utilities

__version__ = "0.0.1"


def get_log_file():
    """Get the path of the log file the root logger writes to."""
    for handler in logging.getLogger().handlers:
        if hasattr(handler, "baseFilename"):
            return handler.baseFilename
    return None


def main():
    """Execution starts here."""
    parser = besapi.plugin_utilities.setup_plugin_argparse(
        description="besapi sample plugin for tests"
    )
    parser.add_argument("--trigger-file", help="only report if this file exists")
    parser.add_argument("--config", action="store_true", help="load plugin config")
    parser.add_argument(
        "--connect", action="store_true", help="require a BigFix connection"
    )
    parser.add_argument(
        "--relevance-echo", help="evaluate this text as an escaped relevance string"
    )

    # NOTE: parse args first to know if a connection is required:
    args, _unknown = parser.parse_known_args()

    with besapi.plugin_utilities.init_plugin(
        __version__, parser=parser, require_connection=args.connect
    ) as (args, bes_conn):
        logging.log(besapi.plugin_utilities.SESSION_LOG_LEVEL, "sample plugin ran")

        report = {
            "invoke_folder": besapi.plugin_utilities.get_invoke_folder(),
            "invoke_file_name": besapi.plugin_utilities.get_invoke_file_name(),
            "log_file": get_log_file(),
            "description": parser.description,
            "connected": bes_conn is not None,
            "timeout": getattr(bes_conn, "timeout", None),
        }

        if args.relevance_echo is not None:
            escaped = besapi.besapi.relevance_string_escape(args.relevance_echo)
            result = bes_conn.session_relevance_json(
                f'(it, length of it) of "{escaped}"'
            )
            report["relevance_echo"], report["relevance_echo_length"] = result[
                "result"
            ][0]

        if args.trigger_file:
            report["trigger_consumed"] = besapi.plugin_utilities.consume_trigger_file(
                args.trigger_file
            )

        if args.config:
            report["config"] = besapi.plugin_utilities.get_plugin_config()

        print(json.dumps(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
