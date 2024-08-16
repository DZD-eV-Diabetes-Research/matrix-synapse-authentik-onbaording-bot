import os
import asyncio
from typing import (
    List,
    Any,
    Dict,
    Union,
    Literal,
    Tuple,
    Type,
    Generator,
    get_type_hints,
    Optional,
    Mapping,
    Awaitable,
)
from urllib.parse import urlsplit, unquote
from typing import BinaryIO
from io import BytesIO
from dataclasses import dataclass
import requests
import logging
import mimetypes
from urllib.parse import urlsplit, unquote, parse_qs
import hashlib

log = logging.getLogger(__name__)


def get_nested_dict_val_by_path(
    data: Dict, key_path: List[str], fallback_val: Any = Any
) -> Any:
    """Provide multiple dict keys as a list to acces a nested dict attribute.

    Args:
        data (Dict): _description_
        keys (List[str]): _description_
        default (Any, optional): _description_. Defaults to Any.

    Returns:
        Any: _description_
    """

    # i know. the `fallback_val` solution with Any as default value to emulate an optional parameter is hacky. Overloading in python is hard :)
    try:
        # thanks to https://stackoverflow.com/a/47969823/12438690
        return (
            get_nested_dict_val_by_path(data[key_path[0]], key_path[1:], fallback_val)
            if key_path
            else data
        )
    except KeyError:
        if fallback_val != Any:
            return fallback_val
        else:
            raise


def create_nested_dict_by_path(
    path: str, value: Any, path_seperator: str = "."
) -> Dict:
    """_summary_

    Args:
        path (str): _description_
        value (Any): _description_

    Returns:
        Dict: _description_
    """
    keys = path.split(path_seperator)
    nested_dict = {}
    current_dict = nested_dict

    for key in keys[:-1]:
        current_dict[key] = {}
        current_dict = current_dict[key]

    current_dict[keys[-1]] = value

    return nested_dict


def synchronize_async_helper(to_await_func: Awaitable):
    # https://stackoverflow.com/a/71489745/12438690
    async_response = []

    async def run_and_capture_result():
        r = await to_await_func
        async_response.append(r)

    loop: asyncio.BaseEventLoop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    coroutine = run_and_capture_result()
    loop.run_until_complete(coroutine)
    # print("async_response", async_response)
    return async_response[0]


@dataclass
class DownloadedFile:
    content: BinaryIO
    filename: Optional[str]
    mime_type: Optional[str]


def download_file(url: str) -> DownloadedFile:
    log.debug(f"Download {url}")
    response = requests.get(url)
    response.raise_for_status()

    # Extract filename from the Content-Disposition header if available

    # Determine the MIME type from the response headers
    mime_type = response.headers.get("Content-Type", None)
    content_disposition = response.headers.get("Content-Disposition")
    filename = None
    if content_disposition and "filename=" in content_disposition:
        filename = content_disposition.split("filename=")[1].strip('"')
    else:
        # Try to extract filename from URL query parameters
        parsed_url = urlsplit(url)
        query_params = parse_qs(parsed_url.query)
        if "file" in query_params:
            filename = query_params["file"][0]

        # Fallback to URL path
        if not filename:
            filename = os.path.basename(parsed_url.path) or None
            filename = unquote(filename) if filename else None
        # fallfallback to hashed id
        if not filename:
            filename = hashlib.md5(response.content).hexdigest()
            extension = ".bin"
            if mime_type:
                extension = mimetypes.guess_extension(mime_type)
            filename = f"{filename}{extension}"

    # Create a BytesIO object from the content
    content = BytesIO(response.content)

    # Create and return the DownloadedFile object
    downloaded_file = DownloadedFile(
        content=content, filename=filename, mime_type=mime_type
    )

    return downloaded_file
