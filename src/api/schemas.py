"""API request shapes.

Only inputs live here. PrefetchHint is a pydantic BaseModel because it crosses
the trust boundary and is validated on the way in.

Response shapes are deliberately not modelled: every route sets
`response_model=None` so FastAPI does not filter the returned dict (a row with
an extra column would otherwise 500), which means a TypedDict here would be
checked by nothing — no runtime validation by design, and no type checker
configured. The one that existed declared `title: str` against a nullable
column and went unnoticed through a hand-edited shape change.
"""

from typing import Annotated

from pydantic import BaseModel, Field, StrictBool


class PrefetchHint(BaseModel):
    item_id: Annotated[str, Field(min_length=1)]
    # Defaults to False to match /api/items. prefetch_ahead's docstring says
    # `unseen` "mirrors the filter the page itself used" and documents R12, the
    # bug where the two disagreed; opposite defaults re-armed it.
    unseen: StrictBool = False
