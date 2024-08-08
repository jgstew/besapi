"""
This will send a BigFix UI message to ALL computers!
"""

import besapi

MESSAGE_TITLE = """Test message from besapi"""
MESSAGE = MESSAGE_TITLE

CONTENT_XML = rf"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
    <SingleAction>
        <Title>Send Message: {MESSAGE_TITLE}</Title>
        <Relevance><![CDATA[(if (windows of operating system) then (exists key whose ((it as string = "IBM BigFix Self Service Application" OR it as string = "IBM BigFix Self-Service Application" OR it as string = "BigFix Self-Service Application") of value "DisplayName" of it AND value "DisplayVersion" of it as string as version >= ("3.1.0" as version)) of key "HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall" of x32 registry) else if (mac of operating system) then (exists application "BigFixSSA.app" whose (version of it >= "3.1.0")) else false) AND (exists line whose (it = "disableMessagesTab: false") of file (if (windows of operating system) then (pathname of parent folder of parent folder of client) & "\BigFix Self Service Application\resources\ssa.config" else "/Library/Application Support/BigFix/BigFixSSA/ssa.config"))]]></Relevance>
        <ActionScript MIMEType="application/x-Fixlet-Windows-Shell">//Nothing to do</ActionScript>
        <SuccessCriteria Option="RunToCompletion"></SuccessCriteria>
        <Settings>
            <ActionUITitle>{MESSAGE_TITLE}</ActionUITitle>
            <PreActionShowUI>true</PreActionShowUI>
            <PreAction>
                <Text><![CDATA[<p>{MESSAGE}</p>]]></Text>
                <AskToSaveWork>false</AskToSaveWork>
                <ShowActionButton>false</ShowActionButton>
                <ShowCancelButton>false</ShowCancelButton>
                <DeadlineBehavior>ForceToRun</DeadlineBehavior>
                <DeadlineType>Interval</DeadlineType>
                <DeadlineInterval>P3D</DeadlineInterval>
                <ShowConfirmation>false</ShowConfirmation>
            </PreAction>
            <HasRunningMessage>false</HasRunningMessage>
            <HasTimeRange>false</HasTimeRange>
            <HasStartTime>false</HasStartTime>
            <HasEndTime>false</HasEndTime>
            <HasDayOfWeekConstraint>false</HasDayOfWeekConstraint>
            <UseUTCTime>false</UseUTCTime>
            <ActiveUserRequirement>NoRequirement</ActiveUserRequirement>
            <ActiveUserType>AllUsers</ActiveUserType>
            <HasWhose>false</HasWhose>
            <PreActionCacheDownload>false</PreActionCacheDownload>
            <Reapply>false</Reapply>
            <HasReapplyLimit>true</HasReapplyLimit>
            <ReapplyLimit>3</ReapplyLimit>
            <HasReapplyInterval>false</HasReapplyInterval>
            <HasRetry>false</HasRetry>
            <HasTemporalDistribution>false</HasTemporalDistribution>
            <ContinueOnErrors>false</ContinueOnErrors>
            <PostActionBehavior Behavior="Nothing"></PostActionBehavior>
            <IsOffer>false</IsOffer>
        </Settings>
        <SettingsLocks>
            <ActionUITitle>false</ActionUITitle>
            <PreActionShowUI>false</PreActionShowUI>
            <PreAction>
                <Text>false</Text>
                <AskToSaveWork>false</AskToSaveWork>
                <ShowActionButton>false</ShowActionButton>
                <ShowCancelButton>false</ShowCancelButton>
                <DeadlineBehavior>false</DeadlineBehavior>
                <ShowConfirmation>false</ShowConfirmation>
            </PreAction>
            <HasRunningMessage>false</HasRunningMessage>
            <RunningMessage>
                <Text>false</Text>
            </RunningMessage>
            <TimeRange>false</TimeRange>
            <StartDateTimeOffset>false</StartDateTimeOffset>
            <EndDateTimeOffset>false</EndDateTimeOffset>
            <DayOfWeekConstraint>false</DayOfWeekConstraint>
            <ActiveUserRequirement>false</ActiveUserRequirement>
            <ActiveUserType>false</ActiveUserType>
            <Whose>false</Whose>
            <PreActionCacheDownload>false</PreActionCacheDownload>
            <Reapply>false</Reapply>
            <ReapplyLimit>false</ReapplyLimit>
            <RetryCount>false</RetryCount>
            <RetryWait>false</RetryWait>
            <TemporalDistribution>false</TemporalDistribution>
            <ContinueOnErrors>false</ContinueOnErrors>
            <PostActionBehavior>
                <Behavior>false</Behavior>
                <AllowCancel>false</AllowCancel>
                <Deadline>false</Deadline>
                <Title>false</Title>
                <Text>false</Text>
            </PostActionBehavior>
            <IsOffer>false</IsOffer>
            <AnnounceOffer>false</AnnounceOffer>
            <OfferCategory>false</OfferCategory>
            <OfferDescriptionHTML>false</OfferDescriptionHTML>
        </SettingsLocks>
        <IsUrgent>true</IsUrgent>
        <Target>
            <AllComputers>true</AllComputers>
        </Target>
        <MIMEFieldSingleAction>
            <Name>action-ui-metadata</Name>
            <Value>{{"type":"notification","sender":"broadcast","expirationDays":3}}</Value>
        </MIMEFieldSingleAction>
    </SingleAction>
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
