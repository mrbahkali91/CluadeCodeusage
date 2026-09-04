"""Bootstrap an organisation and its first user.

A deployment with no organisation and no user cannot be signed into, and
because authentication fails closed it would serve nothing at all. This is the
one-time step that makes it usable.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_persistence.credentials import hash_password
from sreoi_persistence.db import session_scope
from sreoi_persistence.models_identity import Membership, Organization, Role, User


def ensure_organization(session: Session, *, slug: str, name: str) -> Organization:
    org = session.scalar(select(Organization).where(Organization.slug == slug))
    if org is None:
        org = Organization(slug=slug, name=name)
        session.add(org)
        session.flush()
    return org


def ensure_user(
    session: Session,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
    subject: str | None = None,
) -> tuple[User, bool]:
    """Create the user if absent. An existing user's password is left alone."""
    user = session.scalar(select(User).where(User.email == email))
    if user is not None:
        return user, False
    user = User(
        subject=subject or f"local:{uuid.uuid4()}",
        email=email,
        display_name=display_name or email.split("@")[0],
        password_hash=hash_password(password),
    )
    session.add(user)
    session.flush()
    return user, True


def ensure_membership(
    session: Session, *, user: User, organization: Organization, role: Role
) -> Membership:
    membership = session.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.organization_id == organization.id,
        )
    )
    if membership is None:
        membership = Membership(user_id=user.id, organization_id=organization.id, role=role.value)
        session.add(membership)
        session.flush()
    else:
        membership.role = role.value
    return membership


def bootstrap(
    *, slug: str, name: str, email: str, password: str | None, role: Role
) -> dict[str, str]:
    generated = password is None
    secret = password or secrets.token_urlsafe(18)
    with session_scope() as session:
        org = ensure_organization(session, slug=slug, name=name)
        user, created = ensure_user(session, email=email, password=secret)
        ensure_membership(session, user=user, organization=org, role=role)
        # Three genuinely different outcomes; conflating them in security
        # tooling is how a generated password gets lost.
        if not created:
            password_note = "(existing user; password unchanged)"
        elif generated:
            password_note = secret
        else:
            password_note = "(as supplied on the command line)"

        return {
            "organization": org.slug,
            "organization_id": str(org.id),
            "email": user.email,
            "role": role.value,
            "user_created": str(created),
            "password": password_note,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sreoi-identity",
        description="Create an organisation and its first user.",
    )
    parser.add_argument("--org-slug", default="default")
    parser.add_argument("--org-name", default="Default organisation")
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--password",
        default=None,
        help="omit to generate one (printed once, then unrecoverable)",
    )
    parser.add_argument(
        "--role",
        default=Role.ORG_ADMIN.value,
        choices=[r.value for r in Role],
    )
    args = parser.parse_args(argv)

    result = bootstrap(
        slug=args.org_slug,
        name=args.org_name,
        email=args.email,
        password=args.password,
        role=Role(args.role),
    )
    for key, value in result.items():
        print(f"  {key:16} {value}")
    if result["password"].startswith("("):
        return 0
    print("\n  Store this password now: it is not recoverable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
