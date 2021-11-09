import binascii
import io
import os
import typing as t

import multipart.multipart as multipart
from starlette.formparsers import _user_safe_decode  # noqa
from starlette.formparsers import Headers, MultiPartMessage
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from ...exceptions import BentoMLException

_ItemsBody = t.TypeVar(
    "_ItemsBody",
    bound=t.List[t.Tuple[str, t.List[t.Tuple[bytes, bytes]], bytes]],
)

_ResponseList = t.TypeVar("_ResponseList", bound=t.List[t.Tuple[str, Response]])


class MultiPartParser:
    """
    An modified version of starlette MultiPartParser.
    """

    def __init__(self, headers: Headers, stream: t.AsyncGenerator[bytes, None]) -> None:
        assert (
            multipart is not None
        ), "The `python-multipart` library must be installed to use form parsing."
        self.headers = headers
        self.stream = stream
        self.messages: t.List[t.Tuple[MultiPartMessage, bytes]] = []

    def on_part_begin(self) -> None:
        message = (MultiPartMessage.PART_BEGIN, b"")
        self.messages.append(message)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        message = (MultiPartMessage.PART_DATA, data[start:end])
        self.messages.append(message)

    def on_part_end(self) -> None:
        message = (MultiPartMessage.PART_END, b"")
        self.messages.append(message)

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        message = (MultiPartMessage.HEADER_FIELD, data[start:end])
        self.messages.append(message)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        message = (MultiPartMessage.HEADER_VALUE, data[start:end])
        self.messages.append(message)

    def on_header_end(self) -> None:
        message = (MultiPartMessage.HEADER_END, b"")
        self.messages.append(message)

    def on_headers_finished(self) -> None:
        message = (MultiPartMessage.HEADERS_FINISHED, b"")
        self.messages.append(message)

    def on_end(self) -> None:
        message = (MultiPartMessage.END, b"")
        self.messages.append(message)

    async def parse(self) -> _ItemsBody:
        # Parse the Content-Type header to get the multipart boundary.
        content_type, params = multipart.parse_options_header(
            self.headers["Content-Type"]
        )
        params = t.cast(t.Dict[bytes, bytes], params)
        charset = params.get(b"charset", b"utf-8")
        charset = charset.decode("latin-1")
        boundary = params.get(b"boundary")

        # Callbacks dictionary.
        callbacks = {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

        # Create the parser.
        parser = multipart.MultipartParser(boundary, callbacks)
        header_field = b""
        header_value = b""
        content_disposition = None
        field_name = ""

        data = b""

        items = t.cast(_ItemsBody, list())
        headers: t.List[t.Tuple[bytes, bytes]] = list()

        # Feed the parser with data from the request.
        async for chunk in self.stream:
            parser.write(chunk)
            messages = list(self.messages)
            self.messages.clear()
            for message_type, message_bytes in messages:
                if message_type == MultiPartMessage.PART_BEGIN:
                    content_disposition = None
                    data = b""
                    headers = list()
                elif message_type == MultiPartMessage.HEADER_FIELD:
                    header_field += message_bytes
                elif message_type == MultiPartMessage.HEADER_VALUE:
                    header_value += message_bytes
                elif message_type == MultiPartMessage.HEADER_END:
                    field = header_field.lower()
                    if field == b"content-disposition":
                        content_disposition = header_value
                    else:
                        headers.append((field, header_value))
                    header_field = b""
                    header_value = b""
                elif message_type == MultiPartMessage.HEADERS_FINISHED:
                    _, options = multipart.parse_options_header(content_disposition)
                    options = t.cast(t.Dict[bytes, bytes], options)
                    field_name = _user_safe_decode(options[b"name"], charset)
                elif message_type == MultiPartMessage.PART_DATA:
                    data += message_bytes
                elif message_type == MultiPartMessage.PART_END:
                    items.append((field_name, headers, data))

        parser.finalize()
        return items


async def populate_multipart_requests(request: Request) -> t.Dict[str, Request]:
    content_type_header = request.headers.get("Content-Type")
    content_type, _ = multipart.parse_options_header(content_type_header)
    assert content_type == b"multipart/form-data"
    multipart_parser = MultiPartParser(request.headers, request.stream())
    try:
        form = await multipart_parser.parse()  # type: ignore[var-annotated]
    except multipart.MultipartParseError:
        raise BentoMLException("Invalid multipart requests")

    # NOTE: This is the equivalent of form = await request.form()
    request._form = form  # noqa

    reqs = dict()
    for field_name, headers, data in form:
        scope = dict(request.scope)
        ori_headers = dict(scope.get("headers", list()))
        ori_headers = t.cast(t.Dict[bytes, bytes], ori_headers)
        ori_headers.update(dict(headers))
        scope["headers"] = list(ori_headers.items())
        req = Request(scope)
        req._body = data
        reqs[field_name] = req
    return reqs


async def concat_to_multipart_responses(responses: _ResponseList) -> StreamingResponse:
    resp = io.BytesIO()
    for _resp in [req_[1] for req_ in responses]:
        resp.write(_resp.body)
    boundary = binascii.hexlify(os.urandom(16)).decode("ascii")
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
    return StreamingResponse(resp, headers=headers)