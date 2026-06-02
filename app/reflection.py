"""Session-end personality reflection.

Background, off the chat path. A reaper closes idle sessions and enqueues a reflect job; a
pool of consumers runs an LLM reflection over the transcript and *gently* evolves the user's
personality — traits blended toward the model's suggestion via a learning rate, summary merged.
A bad/invalid LLM response never corrupts the stored personality (we only write after a clean
parse, and blend rather than overwrite).
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis

from app import obs
from app.analytics import event, track
from app.config import settings
from app.db import SessionLocal
from app.llm import CompletionClient
from app.prompt import TRAIT_KEYS, build_reflection_messages
from app.queries import (
    claim_idle_sessions,
    get_personality,
    session_transcript,
    set_session_title,
    update_personality,
)

REFLECT_QUEUE = "wave:reflect"


def _clamp01(v) -> float | None:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def _merge_traits(old: dict, proposed: dict, alpha: float) -> dict:
    """Blend known traits toward the proposal (gradual evolution, clamped 0..1)."""
    out: dict[str, float] = {}
    for k in TRAIT_KEYS:
        o = _clamp01(old.get(k)) if old else None
        p = _clamp01(proposed.get(k)) if proposed else None
        if o is None and p is None:
            continue
        if o is None:
            out[k] = round(p, 3)
        elif p is None:
            out[k] = round(o, 3)
        else:
            out[k] = round((1 - alpha) * o + alpha * p, 3)
    return out


class SessionReaper:
    """Closes idle sessions and enqueues the worthwhile ones for reflection."""

    def __init__(self, redis: Redis):
        self._redis = redis

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._tick()
            except Exception:
                obs.exception("reaper.error")
            try:  # sleep, but wake immediately on shutdown
                await asyncio.wait_for(stop.wait(), timeout=settings.reaper_interval_s)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.session_idle_timeout_s)
        async with SessionLocal() as db:
            claimed = await claim_idle_sessions(db, cutoff)
        for sid, uid, mcount in claimed:
            obs.set_corr(str(sid))
            event("session_closed", session_id=str(sid), messages=mcount)
            if mcount >= settings.reflect_min_messages:
                await self._redis.lpush(
                    REFLECT_QUEUE, json.dumps({"session_id": str(sid), "user_id": str(uid)})
                )
                track("reflect_enqueued", session_id=str(sid))
        obs.clear_context()


async def reflect_session(llm: CompletionClient, job: dict) -> None:
    """Run one reflection: load → LLM (JSON) → validate/blend → persist. Raises on failure."""
    sid, uid = job["session_id"], job["user_id"]
    obs.set_corr(sid)
    obs.bind(op="reflect", user_id=uid)
    event("reflect_started", session_id=sid)
    try:
        async with SessionLocal() as db:
            persona = await get_personality(db, uid)
            transcript = await session_transcript(db, sid, settings.reflect_msg_limit)
        if not transcript:
            return

        messages = build_reflection_messages(persona, transcript)
        async with obs.timed("reflect_llm"):
            raw = await llm.complete(messages, model_quality="standard", json=True)

        data = json.loads(raw)  # bad JSON -> caught below, personality left untouched
        old = persona.traits if persona and persona.traits else {}
        new_traits = _merge_traits(old, data.get("traits") or {}, settings.trait_alpha)
        summary = (data.get("summary") or "").strip()[: settings.summary_max_chars]
        if not summary and persona:
            summary = persona.summary
        title = (data.get("title") or "").strip()

        async with SessionLocal() as db:
            await update_personality(db, uid, traits=new_traits, summary=summary)
            if title:
                await set_session_title(db, sid, title)
        event("reflect_completed", session_id=sid, traits=new_traits)
    except Exception:
        event("reflect_failed", session_id=sid)
        obs.exception("reflect.error")
        raise
    finally:
        obs.clear_context()


async def consume(redis: Redis, llm: CompletionClient, stop: asyncio.Event) -> None:
    """One reflection consumer: BRPOP a job, reflect, retry once on failure.

    Exits between jobs when `stop` is set (the short BRPOP timeout bounds the wait), so an
    in-flight reflection is never cut off mid-LLM-call on shutdown.
    """
    while not stop.is_set():
        item = await redis.brpop(REFLECT_QUEUE, timeout=settings.worker_poll_timeout_s)
        if item is None:
            continue
        _key, raw = item
        job = json.loads(raw)
        try:
            await reflect_session(llm, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            if job.get("_retries", 0) < 1:
                job["_retries"] = job.get("_retries", 0) + 1
                await redis.lpush(REFLECT_QUEUE, json.dumps(job))
            # else: give up — already logged as reflect_failed
