"""A site that refuses the server's address is read by a PC, while one is asking.

From 16 Nov 2026 Goibibo showed the production server only its "Sold Out"
page; the same adapter on a home connection read sixteen prices in the same
minute (checked again 18 Nov). The server parks those checks, the reader PC
collects them, loads the page and posts the offers back, and the server
ingests them as its own. When the PC stops asking, the server reads the site
itself again -- it must never wait on a machine that is switched off.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.adapters.base import FetchResult, NormalizedOffer
from app.api.v1 import reader as reader_api
from app.core.errors import ErrorClass, RateLimitedError
from app.services import remote_reader
from app.workers import tasks_fetch

TODAY = date(2026, 11, 18)


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def exists(self, key):
        return int(key in self.values)

    def set(self, key, value, ex=None):
        self.values[key] = str(value)

    def get(self, key):
        v = self.values.get(key)
        return v.encode() if v is not None else None

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value

    def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def hdel(self, name, key):
        return int(self.hashes.get(name, {}).pop(key, None) is not None)


def _settings(key="secret"):
    return SimpleNamespace(
        remote_reader_key=SecretStr(key) if key else None, remote_reader_adapters="goibibo",
        remote_reader_alive_seconds=300, timezone="Asia/Kolkata",
    )


@pytest.fixture
def queue(monkeypatch):
    fake = FakeRedis()
    now = [1_000_000.0]
    monkeypatch.setattr(remote_reader, "get_redis", lambda: fake)
    monkeypatch.setattr(remote_reader, "get_settings", _settings)
    monkeypatch.setattr(remote_reader.time, "time", lambda: now[0])
    return SimpleNamespace(redis=fake, now=now)


def _payload(hotel_source_id=20, check_in="2026-11-18"):
    return {"hotel_id": 9, "hotel_name": "ASG HOLIDAY RESORTS", "hotel_source_id": hotel_source_id,
            "source_id": 7, "adapter_key": "goibibo", "url": "https://www.goibibo.com/hotels/x",
            "external_id": None, "currency": "INR", "adapter_config": {}, "check_in": check_in,
            "check_out": "2026-11-19", "adults": 2, "children": 0, "rooms": 1,
            "meal_plan_filter": None, "target_ids": [41], "rate_limit_per_min": 1}


# -- who reads it -----------------------------------------------------
def test_without_a_key_every_site_is_read_on_the_server(queue, monkeypatch):
    monkeypatch.setattr(remote_reader, "get_settings", lambda: _settings(key=None))
    remote_reader.mark_seen()
    assert not remote_reader.handles("goibibo")
    assert not remote_reader.takes("goibibo")


def test_only_the_named_sites_go_to_the_reader(queue):
    remote_reader.mark_seen()
    assert remote_reader.takes("goibibo")
    assert not remote_reader.takes("playwright_ota")


def test_a_reader_that_is_not_asking_is_not_waited_for(queue):
    assert remote_reader.handles("goibibo")
    assert not remote_reader.takes("goibibo")


# -- the queue --------------------------------------------------------
def test_a_parked_check_is_handed_out_once_and_answered_once(queue):
    remote_reader.park(_payload(), "run-1", "scheduler")
    job = remote_reader.take(TODAY)
    assert job["payload"]["hotel_name"] == "ASG HOLIDAY RESORTS" and job["check_run_id"] == "run-1"
    assert remote_reader.take(TODAY) is None
    assert remote_reader.finish(job["id"])["check_run_id"] == "run-1"
    assert remote_reader.finish(job["id"]) is None


def test_the_oldest_parked_check_goes_first(queue):
    remote_reader.park(_payload(hotel_source_id=21), "sterling", "scheduler")
    queue.now[0] += 5
    remote_reader.park(_payload(hotel_source_id=20), "asg", "scheduler")
    assert remote_reader.take(TODAY)["check_run_id"] == "sterling"
    assert remote_reader.take(TODAY)["check_run_id"] == "asg"


def test_the_same_night_parked_twice_is_read_once(queue):
    assert remote_reader.park(_payload(), "run-1", "scheduler") is None
    assert remote_reader.park(_payload(), "run-2", "manual-check") == "run-1"
    assert remote_reader.take(TODAY)["check_run_id"] == "run-2"
    assert remote_reader.take(TODAY) is None


def test_a_check_nobody_collected_in_half_an_hour_is_dropped(queue):
    remote_reader.park(_payload(), "run-1", "scheduler")
    queue.now[0] += remote_reader.STALE_AFTER_SECONDS + 1
    assert remote_reader.take(TODAY) is None
    assert [j["check_run_id"] for j in remote_reader.sweep(TODAY)] == ["run-1"]
    assert remote_reader.sweep(TODAY) == []


def test_a_check_for_a_night_that_has_begun_is_dropped(queue):
    remote_reader.park(_payload(check_in="2026-11-17"), "run-1", "scheduler")
    assert remote_reader.take(TODAY) is None


# -- the offers, there and back ---------------------------------------
def test_prices_cross_the_wire_to_the_paisa():
    result = FetchResult(offers=[
        NormalizedOffer(raw_room_name="Luxury villa", price_inclusive=Decimal("7826.10"),
                        price_exclusive=Decimal("6078.00"), meal_plan="Breakfast",
                        rooms_left=19, site_room_id="860", raw_payload={"id": 860}),
        NormalizedOffer(raw_room_name="Classic Room", is_available=False),
    ], sold_out_detected=False, source_url="https://www.goibibo.com/hotels/x")
    back = remote_reader.result_from_json(json.loads(json.dumps(remote_reader.result_to_json(result))))
    assert back == result
    assert isinstance(back.offers[0].price_inclusive, Decimal)


# -- the worker parks instead of reading ------------------------------
@pytest.fixture
def bookkeeping(monkeypatch):
    calls = SimpleNamespace(runs=[], closed=[])
    monkeypatch.setattr(tasks_fetch, "_write_check_run",
                        lambda run_id, *a, **kw: calls.runs.append((run_id, kw.get("error_summary"))))
    monkeypatch.setattr(tasks_fetch, "_close_parked", lambda jobs, why: calls.closed.extend(jobs))
    monkeypatch.setattr(tasks_fetch, "get_settings", _settings)
    return calls


class _ReadHere(Exception):
    pass


def _reads_here(monkeypatch):
    def reached(_source_id):
        raise _ReadHere

    monkeypatch.setattr(tasks_fetch, "pace_take", reached)


def _run(payload):
    stay = tasks_fetch._stay_from_payload(payload)
    return tasks_fetch._run_fetch(None, payload, stay, payload["target_ids"], "run-1",
                                  "scheduler", tasks_fetch.datetime.now(tasks_fetch.UTC),
                                  tasks_fetch.log)


def test_goibibo_is_parked_while_the_reader_is_asking(queue, bookkeeping, monkeypatch):
    _reads_here(monkeypatch)
    remote_reader.mark_seen()
    assert _run(_payload())["status"] == "parked"
    assert bookkeeping.runs[0][0] == "run-1" and "reader PC" in bookkeeping.runs[0][1]
    assert remote_reader.take(TODAY)["check_run_id"] == "run-1"


def test_goibibo_is_read_on_the_server_when_the_reader_is_off(queue, bookkeeping, monkeypatch):
    _reads_here(monkeypatch)
    with pytest.raises(_ReadHere):
        _run(_payload())
    assert remote_reader.take(TODAY) is None


def test_other_sites_are_never_parked(queue, bookkeeping, monkeypatch):
    _reads_here(monkeypatch)
    remote_reader.mark_seen()
    with pytest.raises(_ReadHere):
        _run({**_payload(), "adapter_key": "playwright_ota"})


# -- what the reader posts back ---------------------------------------
def _job():
    return {"id": "j1", "payload": _payload(), "check_run_id": "run-1",
            "triggered_by": "scheduler", "parked_at": 1_000_000.0}


def test_the_readers_prices_are_ingested_like_the_workers_own(monkeypatch):
    seen = {}

    def ingest(readings, payload, target_ids, check_run_id, started, triggered_by, logger):
        seen.update(readings=readings, target_ids=target_ids, run=check_run_id)
        return {"status": "success"}

    monkeypatch.setattr(tasks_fetch, "_ingest", ingest)
    answer = remote_reader.result_to_json(FetchResult(offers=[
        NormalizedOffer(raw_room_name="Suite Room", price_inclusive=Decimal("5079"))]))
    assert tasks_fetch.finish_remote_read(_job(), answer) == {"status": "success"}
    (stay, result), = seen["readings"]
    assert stay.check_in == TODAY and result.offers[0].raw_room_name == "Suite Room"
    assert seen["target_ids"] == [41] and seen["run"] == "run-1"


def test_a_refusal_the_reader_met_is_recorded_as_one_and_not_retried(monkeypatch):
    seen = {}

    def failure(task, error, payload, stay, target_ids, check_run_id, started, triggered_by, logger):
        seen.update(error=error, retries=task.request.retries, drift_seen=payload.get("drift_seen"))
        return {"status": "failed"}

    monkeypatch.setattr(tasks_fetch, "_handle_failure", failure)
    out = tasks_fetch.finish_remote_read(
        _job(), {"error_class": str(ErrorClass.RATE_LIMITED), "error": "Sold Out page"})
    assert out == {"status": "failed"}
    assert isinstance(seen["error"], RateLimitedError) and "Sold Out" in str(seen["error"])
    assert seen["retries"] >= seen["error"].max_retries and seen["drift_seen"] is True


# -- the door ---------------------------------------------------------
@pytest.fixture
def client(queue, monkeypatch):
    monkeypatch.setattr(reader_api, "get_settings", _settings)
    # The parked checks are for TODAY, so the door must not ask the real clock.
    monkeypatch.setattr(reader_api, "local_today", lambda _tz: TODAY)
    app = FastAPI()
    app.include_router(reader_api.router, prefix="/api/v1")
    return TestClient(app)


def test_the_route_is_absent_until_a_key_is_set(client, monkeypatch):
    monkeypatch.setattr(reader_api, "get_settings", lambda: _settings(key=None))
    assert client.post("/api/v1/reader/next").status_code == 404


def test_a_wrong_key_is_refused_and_does_not_count_as_a_reader(client):
    assert client.post("/api/v1/reader/next", headers={"X-Reader-Key": "nope"}).status_code == 401
    assert not remote_reader.alive()


def test_asking_for_work_is_what_keeps_the_reader_alive(client):
    remote_reader.park(_payload(), "run-1", "scheduler")
    body = client.post("/api/v1/reader/next", headers={"X-Reader-Key": "secret"}).json()
    assert body["job"]["payload"]["adapter_key"] == "goibibo"
    assert remote_reader.alive()
    again = client.post("/api/v1/reader/next", headers={"X-Reader-Key": "secret"}).json()
    assert again["job"] is None and again["wait_seconds"] > 0


def test_an_answer_to_a_check_nobody_holds_is_refused(client):
    reply = client.post("/api/v1/reader/result", headers={"X-Reader-Key": "secret"},
                        json={"job_id": "never-issued", "offers": []})
    assert reply.status_code == 410
