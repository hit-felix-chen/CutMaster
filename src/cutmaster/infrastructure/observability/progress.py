from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from typing import TypeVar

from tqdm import tqdm


T = TypeVar("T")


def progress_bar(
    iterable: Iterable[T] | None = None,
    *,
    total: int | None = None,
    description: str,
    unit: str,
) -> tqdm[T]:
    output = sys.stdout
    interactive = bool(getattr(output, "isatty", lambda: False)())
    return tqdm(
        iterable,
        total=total,
        desc=description,
        unit=unit,
        file=output,
        disable=not interactive,
        mininterval=1.0,
        maxinterval=5.0,
        dynamic_ncols=True,
        leave=True,
    )


def progress_iter(
    iterable: Iterable[T],
    *,
    total: int,
    description: str,
    unit: str,
) -> Iterator[T]:
    with progress_bar(
        iterable,
        total=total,
        description=description,
        unit=unit,
    ) as progress:
        yield from progress
