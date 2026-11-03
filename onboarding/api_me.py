"""The person, not the company: the sidebar's My Profile sheet.

The wizard draws ``@minty/shared``'s sidebar, and its My Profile sheet reads and saves the
person's own profile and lists their subscriptions. Those live elsewhere -- the profile is
Flask's (``GET``/``PATCH /api/me/profile``), the subscriptions the subscription API's
(``GET /api/me/subscriptions``) -- and neither admits the onboarding origin through CORS.
So the wizard reaches them through here, keeping its ONE base URL (see api_billing.py).

Pure proxies: the caller's own bearer goes upstream unchanged and the upstream's status and
body come back unchanged, so Flask and the subscription API apply exactly the checks they
would to a direct call (the profile is keyed on the token's person; ``?entity=`` is checked
for membership by Flask). Nothing is validated here -- Flask owns the profile rules -- and
this service adds no privilege of its own. Token-gated by the API's default auth.
"""

from ninja import Body, Router

from core import minty_client, subscription_client

me_router = Router()


def _present(**query) -> dict | None:
    """Only the query values the caller actually sent -- an absent ``entity`` means "the
    person alone" to Flask, which an empty ``entity=`` must not blur."""
    return {key: value for key, value in query.items() if value} or None


@me_router.get("/me/profile")
def get_my_profile(request, entity: str = ""):
    """Flask's ``GET /api/me/profile``; ``?entity=`` names the company it was opened from."""
    return minty_client.proxy(
        request, "/api/me/profile", method="GET", params=_present(entity=entity)
    )


@me_router.patch("/me/profile")
def patch_my_profile(request, entity: str = "", payload: dict = Body(default={})):
    """Flask's ``PATCH /api/me/profile`` -- ``{first_name?, last_name?, email?}``, saved on the
    caller's own row; Flask validates and answers 400/422 with the sentence to show."""
    return minty_client.proxy(
        request,
        "/api/me/profile",
        method="PATCH",
        json=payload,
        params=_present(entity=entity),
    )


@me_router.get("/me/subscriptions")
def get_my_subscriptions(request, page: str = "", per_page: str = ""):
    """The subscription API's ``GET /api/me/subscriptions`` (the sheet's Subscriptions
    Overview); paging is passed through for it to validate."""
    return subscription_client.proxy(
        request,
        "/api/me/subscriptions",
        method="GET",
        params=_present(page=page, per_page=per_page),
    )
