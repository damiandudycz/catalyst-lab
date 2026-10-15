import requests
from typing import final
from typing import Callable
from urllib.parse import urlparse, urljoin, ParseResult
from .architecture import Architecture
from concurrent.futures import ThreadPoolExecutor

@final
class ToolsetEnvBuilder:

    @staticmethod
    def get_stage3_urls(completion_handler: Callable[[list[ParseResult] | Exception], None], architecture: Architecture = Architecture.HOST):
        base_url = f"https://distfiles.gentoo.org/releases/{architecture.releases_directory()}/autobuilds/"
        file_url = urljoin(base_url, "latest-stage3.txt")
        with ThreadPoolExecutor() as executor:
            future = executor.submit(ToolsetEnvBuilder._fetch_stage3_urls, file_url, base_url, completion_handler)
            future.result()

    @staticmethod
    def _fetch_stage3_urls(file_url: str, base_url: str, completion_handler: Callable[[list[ParseResult] | Exception], None]) -> None:
        try:
            response = requests.get(file_url)
            response.raise_for_status()
            lines = response.text.splitlines()
            stage3_files = [
                line.split()[0] for line in lines
                if line.strip() and line[0].isdigit() and line.split()[0].endswith('.tar.xz')
            ]
            urls = [urlparse(urljoin(base_url, path)) for path in stage3_files]
            completion_handler(urls)

        except requests.RequestException as e:
            print(f"Error fetching data: {e}")
            completion_handler(e)

