"""
This is to create a bigfix task to install windows software from a semi universal
template.

- https://github.com/jgstew/bigfix-content/blob/main/fixlet/Universal_Windows_Installer_Template_Example.bes
"""

import sys

import bigfix_prefetch
import generate_bes_from_template

import besapi
import besapi.plugin_utilities

# This is a template for a BigFix Task to install software on Windows
# from: https://github.com/jgstew/bigfix-content/blob/main/fixlet/Universal_Windows_Installer_Template_Example.bes
TEMPLATE = r"""<?xml version="1.0" encoding="UTF-8"?>
<BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
	<Task>
		<Title>Install {{{filename}}} - Windows</Title>
		<Description>This example will install {{{filename}}}. </Description>
		<Relevance>windows of operating system</Relevance>
		<Category></Category>
        <DownloadSize>{{DownloadSize}}{{^DownloadSize}}0{{/DownloadSize}}</DownloadSize>
		<Source>windows_software_template.py</Source>
		<SourceID>jgstew</SourceID>
		<SourceReleaseDate>{{{SourceReleaseDate}}}</SourceReleaseDate>
		<SourceSeverity></SourceSeverity>
		<CVENames></CVENames>
		<SANSID></SANSID>
		<MIMEField>
			<Name>x-fixlet-modification-time</Name>
			<Value>{{{x-fixlet-modification-time}}}</Value>
		</MIMEField>
		<Domain>BESC</Domain>
		<DefaultAction ID="Action1">
			<Description>
				<PreLink>Click </PreLink>
				<Link>here</Link>
				<PostLink> to deploy this action.</PostLink>
			</Description>
			<ActionScript MIMEType="application/x-Fixlet-Windows-Shell">// The goal of this is to be used as a template with minimal input
// ideally only the URL would need to be provided or URL and cmd_args
// the filename would by default be derived from the URL
// the prefetch would be generated from the URL

// Download using prefetch block
begin prefetch block
	parameter "filename"="{{{filename}}}"
	parameter "cmd_args"="{{{arguments}}}"
	{{{prefetch}}}
	// if windows and if filename ends with .zip, download unzip.exe
	if {(parameter "filename") as lowercase ends with ".zip"}
	add prefetch item name=unzip.exe sha1=84debf12767785cd9b43811022407de7413beb6f size=204800 url=http://software.bigfix.com/download/redist/unzip-6.0.exe sha256=2122557d350fd1c59fb0ef32125330bde673e9331eb9371b454c2ad2d82091ac
	endif
	if {(parameter "filename") as lowercase ends with ".7z"}
	add prefetch item name=7zr.exe sha1=ec3b89ef381fd44deaf386b49223857a47b66bd8 size=593408 url=https://www.7-zip.org/a/7zr.exe sha256=d2c0045523cf053a6b43f9315e9672fc2535f06aeadd4ffa53c729cd8b2b6dfe
	endif
end prefetch block

// Disable wow64 redirection on x64 OSes
action uses wow64 redirection {not x64 of operating system}

// TODO: handle .7z case

// if windows and if filename ends with .zip, extract with unzip.exe to __Download folder
if {(parameter "filename") as lowercase ends with ".zip"}
wait __Download\unzip.exe -o "__Download\{parameter "filename"}" -d __Download
// add unzip to utility cache
utility __Download\unzip.exe
endif

// use relevance to find exe or msi in __Download folder (in case it came from archive)
parameter "installer" = "{ if (parameter "filename") as lowercase does not end with ".zip" then (parameter "filename") else ( tuple string items 0 of concatenations ", " of names of files whose(following text of last "." of name of it is contained by set of ("exe";"msi";"msix";"msixbundle")) of folder "__Download" of client folder of current site ) }"

if {(parameter "installer") as lowercase ends with ".exe"}
override wait
timeout_seconds=3600
hidden=true
wait "__Download\{parameter "installer"}" {parameter "cmd_args"}
endif

if {(parameter "installer") as lowercase ends with ".msi"}
override wait
timeout_seconds=3600
hidden=true
wait msiexec.exe /i "__Download\{parameter "installer"}" {if (parameter "cmd_args") as lowercase does not contain "/qn" then "/qn " else ""}{parameter "cmd_args"}
endif

if {(parameter "installer") as lowercase ends with ".ps1"}
override wait
timeout_seconds=3600
hidden=true
wait powershell -ExecutionPolicy Bypass -File "__Download\{parameter "installer"}" {parameter "cmd_args"}
endif

if {(parameter "installer") as lowercase ends with ".msix" OR (parameter "installer") as lowercase ends with ".msixbundle"}
override wait
timeout_seconds=3600
hidden=true
wait powershell -ExecutionPolicy Bypass -Command 'Add-AppxProvisionedPackage -Online -SkipLicense -PackagePath "__Download\{parameter "installer"}" {parameter "cmd_args"}'
endif
</ActionScript>
		</DefaultAction>
	</Task>
</BES>
"""


def get_args():
    """Get arguments from command line."""
    parser = besapi.plugin_utilities.setup_plugin_argparse()

    parser.add_argument(
        "--url",
        required=False,
        help="url to download the file from, required if using cmd line",
    )

    parser.add_argument(
        "-a",
        "--arguments",
        required=False,
        help="arguments to pass to the installer",
        default="",
    )

    # allow unknown args to be parsed instead of throwing an error:
    args, _unknown = parser.parse_known_args()

    return args


def get_prefetch_block(url):
    """Get prefetch string for a file."""
    prefetch_dict = bigfix_prefetch.prefetch_from_url.url_to_prefetch(url, True)
    prefetch_block = bigfix_prefetch.prefetch_from_dictionary.prefetch_from_dictionary(
        prefetch_dict, "block"
    )
    return prefetch_block


def main():
    """Execution starts here."""

    if len(sys.argv) > 1:
        args = get_args()
    else:
        print("need to provide cmd args, use -h for help")
        # TODO: open GUI to get args?
        return 1

    print("downloading file and calculating prefetch")
    prefetch = get_prefetch_block(args.url)

    # get filename from end of URL:
    filename = args.url.split("/")[-1]

    # remove ? and after from filename if present:
    index = filename.find("?")
    if index != -1:
        filename = filename[:index]

    template_dictionary = {
        "filename": filename,
        "arguments": args.arguments,
        "prefetch": prefetch,
    }

    template_dictionary = (
        generate_bes_from_template.generate_bes_from_template.get_missing_bes_values(
            template_dictionary
        )
    )

    # print(template_dictionary)
    task_xml = generate_bes_from_template.generate_bes_from_template.generate_content_from_template_string(
        template_dictionary,
        TEMPLATE,
    )

    print(task_xml)

    return 0


if __name__ == "__main__":
    sys.exit(main())
