"""Compare this service's answers against Flask's, endpoint by endpoint.

WHY THIS EXISTS AND THE PYTEST SUITE DOES NOT REPLACE IT

The suite runs on SQLite and builds its tables FROM the models, so a mirror with a wrong
column name or type builds happily and passes. Only Postgres can tell you the mirror is
right. This script runs both services against the REAL database with the SAME token and
compares status and body exactly.

That is what makes retiring a Flask route safe. A group is not done because its tests are
green -- it is done when this reports MATCH (or a registered EXPECTED) for every endpoint
in it.

IT SWEEPS RATHER THAN SAMPLES

One entity exercises one shape of wizard state. A single subject cannot tell you whether
step derivation agrees for an entity with no modules, one with Xero connected, and one
already finalized. The dev database holds a spread of real entities in assorted states,
which is a better test set than anything hand-built, so every entity-scoped endpoint is
compared against all of them.

USAGE

    # Flask must be running on :5001 against the same database.
    .venv/Scripts/python scripts/parity.py
    .venv/Scripts/python scripts/parity.py --group B
    .venv/Scripts/python scripts/parity.py --entities 40

EXPECTED DIFFERENCES

The comparison is exact. Where the two are MEANT to differ, the json path is registered in
EXPECTED_DIFFS with the reason: a response whose only differences are registered reports
EXPECTED and does not fail, while anything else still fails. Without that, the one
deliberate divergence would make the tool cry wolf on every run and it would stop being
read.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import jwt  # noqa: E402
from django.conf import settings  # noqa: E402
from django.test import Client  # noqa: E402

from shared_models.models import Entity, User, UserEntity  # noqa: E402

#: GET endpoints per group: (group, path, needs_token, needs_entity).
#:
#: Write endpoints are deliberately absent. Comparing two POSTs means each one mutating the
#: same row, so the second call sees the first call's effect and "differs" for a reason that
#: is not a bug. Those get hand-written parity tests per group instead.
ENDPOINTS = [
    ("A", "/api/onboarding/server-time", False, False),
    ("A", "/api/onboarding/currencies", False, False),
    ("A", "/api/onboarding/countries", False, False),
    ("A", "/api/onboarding/plans", True, False),
    ("B", "/api/onboarding/state", True, True),
    ("D", "/api/onboarding/sales-methods", True, True),
    ("E", "/api/onboarding/invite", True, True),
]

#: path -> [(json path, why)]. A difference at a registered path is not a failure.
EXPECTED_DIFFS: dict[str, list[tuple[str, str]]] = {
    # Empty, and worth keeping empty. Every ported read is byte-identical to Flask.
    #
    # There was one entry here: /state carried an extra `steps` key, added so the frontend
    # could drop its duplicate step ordering. Reading the frontend showed the two orderings
    # are not duplicates -- see onboarding/services/state.py -- so the key was withdrawn
    # rather than left as an unused divergence.
}

FLASK = os.environ.get("FLASK_APP_URL", "http://localhost:5001").rstrip("/")


def mint(user_id) -> str:
    """A token shaped exactly as Flask mints it, signed with the shared secret."""
    return jwt.encode(
        {
            "user_id": str(user_id),
            "scope": "onboarding",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=60),
            "iat": datetime.now(timezone.utc),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def subjects(limit: int):
    """(user_id, entity_id) pairs to compare. In-progress entities first.

    Only entities with at least one member are usable: both services check membership, so
    an orphaned entity would just produce two matching 403s and prove nothing.
    """
    pairs = []
    seen = set()
    for onboarding_only in (True, False):
        qs = Entity.objects.all()
        if onboarding_only:
            qs = qs.filter(status="onboarding")
        for ent_id in qs.order_by("id").values_list("id", flat=True):
            if ent_id in seen:
                continue
            member = (
                UserEntity.objects.filter(entity_id=ent_id)
                .order_by("user_id")
                .values_list("user_id", flat=True)
                .first()
            )
            if member is None:
                continue
            seen.add(ent_id)
            pairs.append((member, ent_id))
            if len(pairs) >= limit:
                return pairs
    return pairs


def flask_get(path, token, params):
    url = FLASK + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"null")
        except ValueError:
            return e.code, {"_raw": body[:200].decode(errors="replace")}


def django_get(client, path, token, params):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    r = client.get(path, data=params or None, **headers)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"_raw": r.content[:200].decode(errors="replace")}


def diff_paths(a, b, trail=""):
    """Every json path at which the two differ, so a diff can be matched against
    EXPECTED_DIFFS."""
    if type(a) is not type(b):
        return {trail or "<root>"}
    if isinstance(a, dict):
        paths = set()
        for key in set(a) | set(b):
            if key not in a or key not in b:
                paths.add(f"{trail}.{key}")
            elif a[key] != b[key]:
                paths |= diff_paths(a[key], b[key], f"{trail}.{key}")
        return paths
    if isinstance(a, list):
        if len(a) != len(b):
            return {trail or "<root>"}
        paths = set()
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                paths |= diff_paths(x, y, f"{trail}[{i}]")
        return paths
    return {trail or "<root>"}


def describe_diff(a, b, trail=""):
    """The first few concrete differences, without dumping hundreds of rows."""
    out = []
    if type(a) is not type(b):
        return [
            f"{trail or '<root>'}: type flask={type(a).__name__} django={type(b).__name__}"
        ]
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"{trail}.{key}: missing in flask")
            elif key not in b:
                out.append(f"{trail}.{key}: missing in django")
            elif a[key] != b[key]:
                out += describe_diff(a[key], b[key], f"{trail}.{key}")
            if len(out) >= 6:
                break
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{trail}: length flask={len(a)} django={len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                out += describe_diff(x, y, f"{trail}[{i}]")
                break
    else:
        out.append(f"{trail or '<root>'}: flask={a!r} django={b!r}")
    return out[:6]


class Tally:
    def __init__(self):
        self.compared = 0
        self.failed = 0
        self.expected = 0
        self.skipped = 0


def compare(client, tally, group, path, token, params, entity_id):
    label = f"[{group}] {path}"
    if entity_id:
        label += f"  entity={entity_id}"

    try:
        flask_status, flask_body = flask_get(path, token, params)
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP     {label}  (flask unreachable: {exc})")
        tally.skipped += 1
        return

    dj_status, dj_body = django_get(client, path, token, params)
    tally.compared += 1

    if flask_status == dj_status and flask_body == dj_body:
        print(f"  MATCH    {label}  ({flask_status})")
        return

    registered = {jp for jp, _why in EXPECTED_DIFFS.get(path, [])}
    actual = diff_paths(flask_body, dj_body) if flask_status == dj_status else {"<status>"}

    if not (actual - registered):
        # Every difference is one we registered a reason for. Printed rather than
        # swallowed: "the only diff is the one we intended" is the claim being made, and it
        # should be visible on every run.
        print(
            f"  EXPECTED {label}  ({flask_status})  "
            f"diffs: {', '.join(sorted(actual))}"
        )
        tally.expected += 1
        return

    print(f"  DIFFERS  {label}  flask={flask_status} django={dj_status}")
    for line in describe_diff(flask_body, dj_body):
        print(f"            {line}")
    for jsonpath, why in EXPECTED_DIFFS.get(path, []):
        if jsonpath in actual:
            print(f"            (registered) {jsonpath} -- {why}")
    tally.failed += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="only this group, e.g. B")
    ap.add_argument(
        "--entities",
        type=int,
        default=12,
        help="how many entities to sweep for entity-scoped endpoints (default 12)",
    )
    args = ap.parse_args()

    pairs = subjects(args.entities)
    fallback_user = User.objects.order_by("id").values_list("id", flat=True).first()
    if not pairs and not fallback_user:
        print("No users in the database -- nothing to compare against.")
        return 2

    client = Client()
    tally = Tally()

    print(f"Flask   {FLASK}")
    print("Django  in-process")
    print(f"Sweep   {len(pairs)} entity/user pairs\n")

    for group, path, needs_token, needs_entity in ENDPOINTS:
        if args.group and group != args.group:
            continue

        if needs_entity:
            if not pairs:
                print(f"  SKIP     [{group}] {path}  (no entity with a member)")
                tally.skipped += 1
                continue
            cases = pairs
        else:
            cases = [((pairs[0][0] if pairs else fallback_user), None)]

        for user_id, entity_id in cases:
            token = mint(user_id) if needs_token else None
            params = {"entity_id": entity_id} if needs_entity else None
            compare(client, tally, group, path, token, params, entity_id)

    print(
        f"\n{tally.compared} compared, {tally.failed} differing, "
        f"{tally.expected} differing as expected, {tally.skipped} skipped"
    )
    return 1 if tally.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
