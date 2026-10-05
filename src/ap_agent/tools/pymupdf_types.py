"""The slice of PyMuPDF this package actually uses.

PyMuPDF ships incomplete annotations, so every attribute read off a document
comes back as ``Unknown`` under a strict type checker. Rather than scatter casts
and ignores across the tools, the exact surface depended on is declared here
once and documents are cast to it at the point they are opened.

That makes the dependency legible as well as typed: these few members are all
that would need re-verifying if the library changed, and anything not listed
here is something this package does not use.

Per-method docstrings are switched off for this module in ``pyproject.toml`` -
these are structural stubs mirroring someone else's API, and "Return the page
count." on ``page_count`` documents nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, overload

if TYPE_CHECKING:
    from types import TracebackType


class TextSpan(TypedDict):
    """One run of text in one font and colour. ``color`` is packed sRGB."""

    text: str
    color: int
    bbox: tuple[float, float, float, float]


class TextLine(TypedDict):
    spans: list[TextSpan]


class TextBlock(TypedDict):
    """Image blocks carry no ``lines``."""

    lines: NotRequired[list[TextLine]]


class TextPage(TypedDict):
    blocks: list[TextBlock]


class PdfPixmap(Protocol):
    """A rendered page. ``samples`` is row-padded to ``stride`` bytes."""

    @property
    def samples(self) -> bytes: ...
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...
    @property
    def stride(self) -> int: ...


class PdfRect(Protocol):
    @property
    def x0(self) -> float: ...
    @property
    def y0(self) -> float: ...
    @property
    def x1(self) -> float: ...
    @property
    def y1(self) -> float: ...
    @property
    def width(self) -> float: ...
    @property
    def height(self) -> float: ...


class PdfPage(Protocol):
    @property
    def rect(self) -> PdfRect: ...
    @overload
    def get_text(
        self, option: Literal["text"] = ..., *, flags: int = ..., clip: object = ...
    ) -> str: ...
    @overload
    def get_text(
        self, option: Literal["dict"], *, flags: int = ..., clip: object = ...
    ) -> TextPage: ...
    def get_pixmap(self, *, matrix: object, colorspace: object) -> PdfPixmap: ...


class PdfDocument(Protocol):
    """PyMuPDF exposes ``__getitem__`` and ``__len__`` but no ``__iter__``."""

    @property
    def page_count(self) -> int: ...
    @property
    def needs_pass(self) -> bool: ...
    def __getitem__(self, index: int) -> PdfPage: ...
    def __enter__(self) -> PdfDocument: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
