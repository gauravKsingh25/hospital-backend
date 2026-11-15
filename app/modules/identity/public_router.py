"""Routes anyone may call: plans, signup, password reset, contact.

No authentication, so every route is rate-limited by client address
(`core.rate_limit`, failing open). Thin: the work is in `signup.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request, params, status

from app.core.deps import SessionDep, get_client_ip
from app.core.rate_limit import limit_by_ip
from app.modules.identity import signup
from app.modules.identity.public_schemas import (
    Accepted,
    ContactCreate,
    HandoffExchange,
    HandoffIssued,
    PasswordForgot,
    PasswordReset,
    PlanRead,
    SignupComplete,
    SignupResend,
    SignupStart,
    SignupToken,
    SignupVerify,
)
from app.modules.identity.schemas import TokenPair
from app.modules.subscriptions import service as subscriptions_service

public_router = APIRouter(prefix="/public", tags=["public"])

_HOUR = 3600


def _limit(bucket: str, per_hour: int) -> list[params.Depends]:
    return [Depends(limit_by_ip(bucket, limit=per_hour, window_seconds=_HOUR))]


def _agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


@public_router.get(
    "/plans",
    response_model=list[PlanRead],
    summary="The plans on sale, with prices",
    dependencies=_limit("plans", 300),
)
async def list_plans(session: SessionDep) -> list[PlanRead]:
    plans = await subscriptions_service.list_active_plans(session)
    return [
        PlanRead(
            code=plan.code,
            name=plan.name,
            price_inr=plan.price_inr,
            interval_days=plan.interval_days,
            features=subscriptions_service.plan_feature_keys(plan.code),
        )
        for plan in plans
    ]


@public_router.post(
    "/signup/start",
    response_model=Accepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a signup: email a verification code",
    dependencies=_limit("signup-start", 10),
)
async def signup_start(payload: SignupStart, session: SessionDep, request: Request) -> Accepted:
    await signup.start(
        session,
        email=payload.email,
        full_name=payload.full_name,
        hospital_name=payload.hospital_name,
        plan_code=payload.plan_code,
        phone=payload.phone,
        ip=await get_client_ip(request),
    )
    await session.commit()
    return Accepted()


@public_router.post(
    "/signup/resend",
    response_model=Accepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send the verification code again",
    dependencies=_limit("signup-resend", 10),
)
async def signup_resend(payload: SignupResend, session: SessionDep, request: Request) -> Accepted:
    await signup.resend(session, email=payload.email, ip=await get_client_ip(request))
    await session.commit()
    return Accepted()


@public_router.post(
    "/signup/verify",
    response_model=SignupToken,
    summary="Check the emailed code",
    dependencies=_limit("signup-verify", 30),
)
async def signup_verify(payload: SignupVerify, session: SessionDep) -> SignupToken:
    token, expires_in = await signup.verify(session, email=payload.email, code=payload.code)
    await session.commit()
    return SignupToken(signup_token=token, expires_in=expires_in)


@public_router.post(
    "/signup/complete",
    response_model=HandoffIssued,
    summary="Choose a password; the account is created",
    dependencies=_limit("signup-complete", 10),
)
async def signup_complete(
    payload: SignupComplete, session: SessionDep, request: Request
) -> HandoffIssued:
    code = await signup.complete(
        session,
        signup_token=payload.signup_token,
        password=payload.password,
        ip=await get_client_ip(request),
        user_agent=_agent(request),
    )
    await session.commit()
    return HandoffIssued(handoff_code=code, expires_in=signup.HANDOFF_SECONDS)


@public_router.post(
    "/signup/handoff",
    response_model=TokenPair,
    summary="Exchange the one-time signup code for a session",
    dependencies=_limit("signup-handoff", 30),
)
async def signup_handoff(
    payload: HandoffExchange, session: SessionDep, request: Request
) -> TokenPair:
    user, access, refresh, expires_in = await signup.exchange_handoff(
        session, code=payload.code, ip=await get_client_ip(request), user_agent=_agent(request)
    )
    await session.commit()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        must_change_password=user.must_change_password,
    )


@public_router.post(
    "/password/forgot",
    response_model=Accepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a password reset code",
    dependencies=_limit("password-forgot", 5),
)
async def password_forgot(
    payload: PasswordForgot, request: Request, background: BackgroundTasks
) -> Accepted:
    # Everything that depends on the address runs after the answer, so a real
    # account answers exactly as fast as an unknown one.
    background.add_task(signup.forgot_password_later, payload.email, await get_client_ip(request))
    return Accepted()


@public_router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password with the emailed code",
    dependencies=_limit("password-reset", 20),
)
async def password_reset(payload: PasswordReset, session: SessionDep, request: Request) -> None:
    await signup.reset_password(
        session,
        email=payload.email,
        code=payload.code,
        password=payload.password,
        ip=await get_client_ip(request),
        user_agent=_agent(request),
    )
    await session.commit()


@public_router.post(
    "/contact",
    response_model=Accepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send a message from the website's contact form",
    dependencies=_limit("contact", 5),
)
async def contact(payload: ContactCreate, session: SessionDep, request: Request) -> Accepted:
    if payload.website:
        # The honeypot was filled in: a bot. Thank it and keep nothing.
        return Accepted()
    await signup.create_contact_message(
        session,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        hospital_name=payload.hospital_name,
        message=payload.message,
        ip=await get_client_ip(request),
    )
    await session.commit()
    return Accepted()
