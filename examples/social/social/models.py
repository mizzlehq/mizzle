"""API request and response models.

Request models come from the tables with an explicit ``include=`` allowlist, so a client
can only send the fields listed here (no ``id``, no ``author_id``, no ``created_at``).
"""

from pydantic import BaseModel

from mizzle.pydantic import insert_model, select_model
from social.schema import Bookmarks, Comments, Posts, Users


class UserIn(insert_model(Users, include=["username", "email", "display_name", "bio"])):
    pass


class UserOut(select_model(Users, exclude=["email"])):  # emails stay private
    pass


class PostIn(insert_model(Posts, include=["body", "visibility", "meta"])):
    pass


class PostOut(select_model(Posts)):
    pass


class CommentIn(insert_model(Comments, include=["body"])):
    pass


class CommentOut(select_model(Comments)):
    pass


class BookmarkIn(insert_model(Bookmarks, include=["note"])):
    pass


class Profile(BaseModel):
    user: UserOut
    followers: int
    following: int
    posts: int


class CommentView(BaseModel):
    comment: CommentOut
    author: UserOut


class PostView(BaseModel):
    post: PostOut
    author: UserOut
    likes: int
    comments: int


class PostDetail(BaseModel):
    post: PostOut
    author: UserOut
    likes: int
    comments: list[CommentView]


class Feed(BaseModel):
    items: list[PostView]
    next_before: int | None  # pass as ?before= to get the next page
