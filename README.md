# besapi

[![PyPI version](https://img.shields.io/pypi/v/besapi.svg)](https://pypi.org/project/besapi/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE.txt)

**besapi** is a Python library that makes it easy to work with the [BigFix REST API](https://developer.bigfix.com/rest-api/api/). It handles authentication, requests, and XML parsing for you, so you can focus on automating your BigFix environment instead of wrangling raw HTTP and XML.

It also includes **bescli**, an interactive command-line interface for exploring the REST API — great for trying things out before writing any code.

## Installation

```bash
pip install besapi
```

## Quick Start

Connect to your BigFix root server and start making requests:

```python
import besapi

b = besapi.besapi.BESConnection(
    "my_username", "my_password", "https://rootserver.domain.org:52311"
)
rr = b.get("sites")

# Every request returns a RESTResult with several handy views of the response:
#   rr.request - the original requests object
#   rr.text    - the raw text returned by the server
#   rr.besxml  - the response as an XML string
#   rr.besobj  - the response as an lxml.objectify.ObjectifiedElement

print(rr)
```

```xml
<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BESAPI.xsd">
<ExternalSite Resource="http://rootserver.domain.org:52311/api/site/external/BES%20Support">
<Name>BES Support</Name>
</ExternalSite>
<!---...--->
<CustomSite Resource="http://rootserver.domain.org:52311/api/site/custom/Org">
<Name>Org</Name>
</CustomSite>
<CustomSite Resource="http://rootserver.domain.org:52311/api/site/custom/Org%2fMac">
<Name>Org/Mac</Name>
</CustomSite>
<CustomSite Resource="http://rootserver.domain.org:52311/api/site/custom/Org%2fWindows">
<Name>Org/Windows</Name>
</CustomSite>
<CustomSite Resource="http://rootserver.domain.org:52311/api/site/custom/ContentDev">
<Name>ContentDev</Name>
</CustomSite>
<OperatorSite Resource="http://rootserver.domain.org:52311/api/site/operator/mah60">
<Name>mah60</Name>
</OperatorSite>
<ActionSite Resource="http://rootserver.domain.org:52311/api/site/master">
<Name>ActionSite</Name>
</ActionSite>
</BESAPI>
```

### Working with results

The `besobj` attribute lets you navigate the XML response like a regular Python object:

```pycon
>>> rr.besobj.attrib
{'{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation': 'BESAPI.xsd'}

>>> rr.besobj.ActionSite.attrib
{'Resource': 'http://rootserver.domain.org:52311/api/site/master'}

>>> rr.besobj.ActionSite.attrib["Resource"]
'http://rootserver.domain.org:52311/api/site/master'

>>> rr.besobj.ActionSite.Name
'ActionSite'

>>> rr.besobj.OperatorSite.Name
'mah60'

>>> for cSite in rr.besobj.CustomSite:
...     print(cSite.Name)
...
Org
Org/Mac
Org/Windows
ContentDev
```

### Downloading, uploading, and deleting content

```python
# save a task to a file:
rr = b.get("task/operator/mah60/823975")
with open("/Users/Shared/Test.bes", "wb") as bes_file:
    bes_file.write(rr.text.encode("utf-8"))

# delete a task:
b.delete("task/operator/mah60/823975")

# create or update a task from a file:
with open("/Users/Shared/Test.bes") as bes_file:
    b.post("tasks/operator/mah60", bes_file)
    b.put("task/operator/mah60/823975", bes_file)
```

Looking for more? The [examples folder](https://github.com/jgstew/besapi/tree/master/examples) has ready-to-run scripts for many common tasks — exporting sites, importing content, taking actions, running session relevance, and more.

## Command-Line Interface

The included `bescli` interactive shell is the fastest way to poke around the REST API:

```
$ python -m bescli

BigFix> login
User [mah60]: mah60
Root Server (ex. https://server.institution.edu:52311): https://my.company.org:52311
Password:
Login Successful!
BigFix> get help
...
BigFix> get sites
...
BigFix> get sites.OperatorSite.Name
mah60
BigFix> get help/fixlets
GET:
/api/fixlets/{site}
POST:
/api/fixlets/{site}
BigFix> get fixlets/operator/mah60
...
```

## Requirements

- Python 3.9 or later
  - besapi 1.1.3 was the last version with partial Python 2 support
- [lxml](https://pypi.org/project/lxml/)
- [requests](https://pypi.org/project/requests/)
- [cmd2](https://pypi.org/project/cmd2/) (for the CLI)

All dependencies are installed automatically by pip.

## More Examples Using besapi

- https://github.com/jgstew/besapi/tree/master/examples
- https://github.com/jgstew/generate_bes_from_template/blob/master/examples/generate_uninstallers.py
- https://github.com/jgstew/jgstew-recipes/blob/main/SharedProcessors/BESImport.py
- https://github.com/jgstew/jgstew-recipes/blob/main/SharedProcessors/BigFixActioner.py
- https://github.com/jgstew/jgstew-recipes/blob/main/SharedProcessors/BigFixSessionRelevance.py

## Building a Standalone Binary

You can package the CLI into a single executable with [PyInstaller](https://pyinstaller.org/):

```bash
pyinstaller --clean --collect-all besapi --onefile .\src\bescli\bescli.py
```

Note: using UPX to compress the binary only saves about 2MB out of 16MB on Windows.

## BigFix REST API Documentation

- https://developer.bigfix.com/rest-api/
- http://bigfix.me/restapi

## Related Items

- https://forum.bigfix.com/t/rest-api-python-module/2170
- https://gist.github.com/hansen-m/58667f370047af92f634
- https://docs.google.com/presentation/d/1pME28wdjkzj9378py9QjFyMOyOHcamB6bk4k8z-c-r0/edit#slide=id.g69e753e75_039
- https://forum.bigfix.com/t/bigfix-documentation-resources/12540
- https://forum.bigfix.com/t/query-for-finding-who-deleted-tasks-fixlets/13668/6
- https://forum.bigfix.com/t/rest-api-java-wrapper/12693

## Contributing

Issues and pull requests are welcome! If you have a script built on besapi that others might find useful, consider adding it to the [examples](https://github.com/jgstew/besapi/tree/master/examples).

## License

[MIT License](LICENSE.txt)
