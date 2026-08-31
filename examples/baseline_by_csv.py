import csv
import datetime


def read_items(filepath, indices):
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            # Extract only the specified 3 indices
            yield [row[i].strip() for i in indices]


def main():
    # Sample usage: read the last 3 items of each N-item row
    last_three_from_end = [-3, -2, -1]
    baseline_components_xml = ""

    for selected_fields in read_items("data.csv", indices=last_three_from_end):
        site_url = selected_fields[0]
        fixlet_id = selected_fields[1]
        action_name = selected_fields[2]

        baseline_components_xml += f"""
            <BaselineComponent IncludeInRelevance="true" SourceSiteURL="{site_url}" SourceID="{fixlet_id}" ActionName="{action_name}" />"""

    # print(baseline_components_xml)

    # generate XML for baseline with template:
    baseline = f"""<?xml version="1.0" encoding="UTF-8"?>
    <BES xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="BES.xsd">
      <Baseline>
        <Title>Custom Patching Baseline {datetime.datetime.today().strftime('%Y-%m-%d')}</Title>
        <Description />
        <Relevance>true</Relevance>
        <BaselineComponentCollection>
          <BaselineComponentGroup>{baseline_components_xml}
          </BaselineComponentGroup>
        </BaselineComponentCollection>
      </Baseline>
    </BES>"""

    print(baseline)

    # Optionally, write the baseline XML to a file
    with open("baseline_from_csv.bes", "w", encoding="utf-8") as f:
        f.write(baseline)


if __name__ == "__main__":
    main()
