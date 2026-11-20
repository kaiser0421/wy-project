from tests.events.helpers import EVENTS, seed_event, seed_zone


async def draft(db, owner):
    event = await seed_event(db, owner)
    await seed_zone(db, event)
    return event


async def upload(session, event, content, *, mode="all_or_nothing", format_="csv"):
    return await session.client.post(
        f"{EVENTS}/{event}/seat-imports",
        files={"file": (f"seats.{format_}", content)},
        data={"mode": mode},
        headers=session.headers(),
    )
