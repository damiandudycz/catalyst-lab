import requests
from urllib.parse import ParseResult, urljoin, urlparse
from .architecture import Architecture


def get_stage3_urls(architecture: Architecture = Architecture.HOST) -> list[ParseResult] | Exception:
    """Fetch stage3 archives for the selected architecture."""
    base_url = f"https://distfiles.gentoo.org/releases/{architecture.releases_directory()}/autobuilds/"
    try:
        response = requests.get(urljoin(base_url, "latest-stage3.txt"))
        response.raise_for_status()
        paths = (line.split()[0] for line in response.text.splitlines()
                 if line.strip() and line[0].isdigit() and line.split()[0].endswith(".tar.xz"))
        return [urlparse(urljoin(base_url, path)) for path in paths]
    except requests.RequestException as error:
        print(f"Error fetching data: {error}")
        return error
