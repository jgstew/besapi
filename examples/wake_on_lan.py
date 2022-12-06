"""
Send Wake On Lan (WoL) request to given computer IDs

requires `besapi`, install with command `pip install besapi`

Related:

- http://localhost:__WebReportsPort__/json/wakeonlan?cid=_ComputerID_&cid=_NComputerID_
- https://localhost:52311/rd-proxy?RequestUrl=cgi-bin/bfenterprise/BESGatherMirrorNew.exe/-triggergatherdb?forwardtrigger
- https://localhost:52311/rd-proxy?RequestUrl=../../cgi-bin/bfenterprise/ClientRegister.exe?RequestType=GetComputerID
- https://localhost:52311/rd-proxy?RequestUrl=cgi-bin/bfenterprise/BESGatherMirror.exe/-besgather&body=SiteContents&url=http://_MASTHEAD_FQDN_:52311/cgi-bin/bfgather.exe/actionsite
- https://localhost:52311/rd-proxy?RequestUrl=cgi-bin/bfenterprise/BESMirrorRequest.exe/-textreport
- Gather Download Request: https://localhost:52311/rd-proxy?RequestUrl=bfmirror/downloads/_ACTION_ID_/_DOWNLOAD_ID_
"""
import sys

import besapi


def main():
    """Execution starts here"""
    print("main()")
    print("WORK IN PROGRESS! Does not work currently! Exiting!")
    sys.exit(-1)
    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    # SessionRelevance for root server id:
    session_relevance = """
    maxima of ids of bes computers
     whose(root server flag of it AND now - last report time of it < 1 * day)
    """

    root_server_id = int(bes_conn.session_relevance_string(session_relevance))

    print(
        f"Work in progress POST {bes_conn.rootserver}/data/wake-on-lan {root_server_id}"
    )

    print(
        bes_conn.session.post(
            f"{bes_conn.rootserver}/data/wake-on-lan", data=f"cid={root_server_id}"
        )
    )


if __name__ == "__main__":
    main()
