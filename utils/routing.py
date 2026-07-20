"""Shared dot-command routing seam.

Part of the Issue #48 architecture: features register the command token(s) they
own instead of ``bot.py`` growing an ``if _first == ...`` ladder in ``on_message``.
The registry maps an exact first token (the word after the leading ``.``) to an
async handler that takes the Discord message.

Parser-driven commands (``.rg``, ``.roll5``, ``.build``, ``.session``, ``.draft``,
``.help``) are not simple tokens and remain in the parser fallback until their own
feature phases; this seam covers exact-token commands and lets `bot.py` resolve
them in one lookup.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Iterable

CommandHandler = Callable[[object], Awaitable[None]]


class CommandRegistry:
    """Maps exact dot-command tokens to feature-owned async handlers."""

    def __init__(self) -> None:
        self._exact: dict[str, CommandHandler] = {}

    def register(self, tokens: Iterable[str], handler: CommandHandler) -> None:
        for token in tokens:
            key = token.lower()
            if key in self._exact:
                raise ValueError(f"Command token already registered: {key}")
            self._exact[key] = handler

    def resolve(self, first_token: str) -> CommandHandler | None:
        return self._exact.get(first_token.lower())

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(self._exact)
