"""Validate GitHub REST pagination links without following remote URLs."""

import re
from urllib.parse import parse_qs, urlsplit

from loop_reviewer.integrations.gh_api import GhResponse
from loop_reviewer.review.selection import GitHubReadError


def has_next_page(
    response: GhResponse,
    host: str,
    paths: frozenset[str],
    page: int,
    count: int,
    *,
    invalid_message: str,
) -> bool:
    link = response.headers.get("link")
    if link is None:
        return count == 100
    cloud = host == "github.com" or host.endswith(".ghe.com")
    api_host = f"api.{host}" if cloud else host
    prefix = "" if cloud else "/api/v3"
    api_paths = {f"{prefix}{path}".lower() for path in paths}
    relations: dict[str, int] = {}
    try:
        for entry in link.split(","):
            match = re.fullmatch(
                r'\s*<([^<>]+)>;\s*rel="(first|prev|next|last)"\s*', entry
            )
            if match is None or match[2] in relations:
                raise ValueError
            destination = urlsplit(match[1])
            query = parse_qs(destination.query)
            if (
                destination.scheme != "https"
                or destination.netloc.lower() != api_host
                or destination.path.lower() not in api_paths
                or query.get("per_page", ["100"]) != ["100"]
            ):
                raise ValueError
            (number,) = query.get("page", [])
            if not number.isascii() or not number.isdecimal():
                raise ValueError
            relations[match[2]] = int(number)
        if "next" in relations:
            if (
                relations["next"] != page + 1
                or relations.get("last", page + 1) < page + 1
            ):
                raise ValueError
            return True
        if relations.get("last", page) > page:
            raise ValueError
        return False
    except ValueError:
        raise GitHubReadError(invalid_message) from None
