"""
Send Wake On Lan (WoL) request to given computer IDs

requires `besapi`, install with command `pip install besapi`

Related:

- http://localhost:__WebReportsPort__/json/wakeonlan?cid=_ComputerID_&cid=_NComputerID_
- POST(binary) http://localhost:52311/data/wake-on-lan
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

    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    # SessionRelevance for root server id:
    session_relevance = """
    maxima of ids of bes computers
     whose(root server flag of it AND now - last report time of it < 1 * day)
    """

    computer_id_array = bes_conn.session_relevance_array(session_relevance)

    print(computer_id_array)

    computer_id_xml_string = ""

    for item in computer_id_array:
        computer_id_xml_string += '<Computer ComputerID="' + str(item) + '" />'

    print(computer_id_xml_string)

    soap_xml = (
        """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/" xmlns:tns="http://schemas.bigfix.com/WakeOnLAN" targetNamespace="http://schemas.bigfix.com/WakeOnLAN" xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/">
  <soap:Header />
  <soap:Body>
    <message name="WakeOnLANIn">
      <WakeOnLANRequest>
        """
        + computer_id_xml_string
        + """
      </WakeOnLANRequest>
    </message>
    <portType name="WakeOnLANPortType">
      <operation name="WakeOnLAN">
        <input message="tns:WakeOnLANIn" />
      </operation>
    </portType>
    <binding name="WakeOnLANBinding" type="tns:WakeOnLANPortType">
      <soap:binding transport="http://schemas.xmlsoap.org/soap/http" />
      <operation name="WakeOnLAN">
        <soap:operation soapAction="http://schemas.bigfix.com/WakeOnLAN" />
        <input>
          <soap:body use="literal" />
        </input>
      </operation>
    </binding>
    <service name="WakeOnLANService">
      <port name="WakeOnLANPort" binding="tns:WakeOnLANBinding">
        <soap:address location="https://localhost:52311/WakeOnLAN" />
      </port>
    </service>
  </soap:Body>
</soap:Envelope>
"""
    )

    print(bes_conn.session.post(f"{bes_conn.rootserver}/WakeOnLan", data=soap_xml))

    print("Finished, Response 200 means succces.")


if __name__ == "__main__":
    main()
