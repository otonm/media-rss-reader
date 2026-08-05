"""API request/response shapes.

TypedDicts name the return contracts for IDE/type-checker support. Each route
sets `response_model=None` so FastAPI does not use the TypedDict as an
implicit response model and start filtering the dict (a row with an extra
column would otherwise 500). PrefetchHint is a pydantic BaseModel because
it crosses the trust boundary and must be validated on input.
"""

from typing import TypedDict

from pydantic import BaseModel, StrictBool


class MediaSlide(TypedDict):
    url: str
    type: str


class ItemOut(TypedDict):
    id: str
    feed_id: str
    title: str | None
    media_url: str
    media_type: str
    media: list[MediaSlide]
    pub_date: str | None
    fetched_at: str | None
    seen_at: str | None
    cached: bool


class FeedOut(TypedDict):
    id: str
    title: str
    url: str
    last_fetched_at: str | None
    item_count: int
    unseen_count: int


class SeenResponse(TypedDict):
    seen_at: str


class PrefetchHintResponse(TypedDict):
    status: str


class PrefetchHint(BaseModel):
    item_id: str
    unseen: StrictBool = True
