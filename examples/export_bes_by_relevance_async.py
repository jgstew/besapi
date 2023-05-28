"""
Example export bes files by session relevance result

requires `besapi`, install with command `pip install besapi`
"""
import asyncio
import configparser
import os
import time

import aiofiles
import aiohttp

import besapi


def get_bes_pass_using_config_file(conf_file=None):
    """
    read connection values from config file
    return besapi connection
    """
    config_paths = [
        "/etc/besapi.conf",
        os.path.expanduser("~/besapi.conf"),
        os.path.expanduser("~/.besapi.conf"),
        "besapi.conf",
    ]
    # if conf_file specified, then only use that:
    if conf_file:
        config_paths = [conf_file]

    configparser_instance = configparser.ConfigParser()

    found_config_files = configparser_instance.read(config_paths)

    if found_config_files and configparser_instance:
        print("Attempting BESAPI Connection using config file:", found_config_files)

        try:
            BES_PASSWORD = configparser_instance.get("besapi", "BES_PASSWORD")
        except BaseException:  # pylint: disable=broad-except
            BES_PASSWORD = None

        return BES_PASSWORD


async def fetch(session, url):
    """get items async"""
    async with session.get(url) as response:
        response_text = await response.text()

        # Extract the filename from the URL
        url_parts = url.split("/")

        file_dir = "./tmp/" + url_parts[-2] + "/" + url_parts[-4]

        os.makedirs(file_dir, exist_ok=True)

        filename = file_dir + "/" + url_parts[-1] + ".bes"

        # Write the response to a file asynchronously
        async with aiofiles.open(filename, "w") as file:
            await file.write(response_text)

        print(f"{filename} downloaded and saved.")


async def main():
    """Execution starts here"""
    print("main()")

    # Create a semaphore with a maximum concurrent requests
    semaphore = asyncio.Semaphore(3)

    # TODO: get max mod time of existing bes files:
    # https://github.com/jgstew/tools/blob/master/Python/get_max_time_bes_files.py

    bes_conn = besapi.besapi.get_bes_conn_using_config_file()
    bes_conn.login()

    print(bes_conn.last_connected)

    # change the relevance here to adjust which content gets exported:
    fixlets_rel = 'custom bes fixlets whose(name of it as lowercase contains "oracle")'

    # this does not currently work with things in the actionsite:
    session_relevance = f'(type of it as lowercase & "/custom/" & name of site of it & "/" & id of it as string) of {fixlets_rel}'

    result = bes_conn.session_relevance_array(session_relevance)

    print(f"{len(result)} items to export...")

    absolute_urls = []

    for item in result:
        absolute_urls.append(bes_conn.url(item))

    # Create a session for making HTTP requests
    async with aiohttp.ClientSession(
        auth=aiohttp.BasicAuth(bes_conn.username, get_bes_pass_using_config_file()),
        connector=aiohttp.TCPConnector(ssl=False),
    ) as session:
        # Define a list of URLs to fetch
        urls = absolute_urls

        # Create a list to store the coroutines for fetching the URLs
        tasks = []

        # Create coroutines for fetching each URL
        for url in urls:
            # Acquire the semaphore before starting the request
            async with semaphore:
                task = asyncio.ensure_future(fetch(session, url))
                tasks.append(task)

        # Wait for all the coroutines to complete
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    # Start the timer
    start_time = time.time()

    # Run the main function
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

    # Calculate the elapsed time
    elapsed_time = time.time() - start_time
    print(f"Execution time: {elapsed_time:.2f} seconds")
