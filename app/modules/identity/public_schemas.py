"""Request and response bodies for the unauthenticated `/public` routes."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

__all__ = [
    "Accepted",
    "ContactCreate",
    "HandoffExchange",
    "HandoffIssued",
    "PasswordForgot",
    "PasswordReset",
    "PlanRead",
    "SignupComplete",
    "SignupResend",
    "SignupStart",
    "SignupToken",
    "SignupVerify",
]

Code = Annotated[str, Field(pattern=r"^\s*\d{6}\s*$", description="The six digits from the email.")]


def _lower(value: str) -> str:
    return value.strip().lower()


class Accepted(BaseModel):
    """The same answer whatever happened, so the answer reveals nothing."""

    status: Literal["sent"] = "sent"


class PlanRead(BaseModel):
    code: str
    name: str
    price_inr: int
    interval_days: int
    features: list[str]


class SignupStart(BaseModel):
    full_name: Annotated[str, Field(min_length=2, max_length=200)]
    email: EmailStr
    phone: Annotated[str | None, Field(max_length=20)] = None
    hospital_name: Annotated[str, Field(min_length=2, max_length=200)]
    plan_code: Annotated[str, Field(min_length=1, max_length=32)]

    _email = field_validator("email")(_lower)

    @field_validator("full_name", "hospital_name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())


class SignupResend(BaseModel):
    email: EmailStr

    _email = field_validator("email")(_lower)


class SignupVerify(BaseModel):
    email: EmailStr
    code: Code

    _email = field_validator("email")(_lower)


class SignupToken(BaseModel):
    signup_token: str
    expires_in: int


class SignupComplete(BaseModel):
    signup_token: Annotated[str, Field(min_length=1, max_length=2048)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class HandoffIssued(BaseModel):
    handoff_code: str
    expires_in: int


class HandoffExchange(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=256)]


class PasswordForgot(BaseModel):
    email: EmailStr

    _email = field_validator("email")(_lower)


class PasswordReset(BaseModel):
    email: EmailStr
    code: Code
    password: Annotated[str, Field(min_length=1, max_length=256)]

    _email = field_validator("email")(_lower)


class ContactCreate(BaseModel):
    name: Annotated[str, Field(min_length=2, max_length=200)]
    email: EmailStr
    phone: Annotated[str | None, Field(max_length=20)] = None
    hospital_name: Annotated[str | None, Field(max_length=200)] = None
    message: Annotated[str, Field(min_length=10, max_length=4000)]
    # The honeypot: a field people never see and bots fill in.
    website: Annotated[str | None, Field(max_length=500)] = None

    _email = field_validator("email")(_lower)
