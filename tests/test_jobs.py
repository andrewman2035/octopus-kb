from octopus.db import connect
from octopus.jobs import RETRY_DELAYS, JobQueue


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path):
    clock = Clock()
    return JobQueue(connect(tmp_path / "j.db", embed_dim=8), clock=clock), clock


def test_enqueue_claim_complete(tmp_path):
    q, _ = make(tmp_path)
    jid = q.enqueue("url", {"url": "https://a.com"}, "cli", "u")
    [job] = q.claim_due(5)
    assert (job.id, job.kind, job.payload, job.status) == (jid, "url", {"url": "https://a.com"}, "running")
    assert q.claim_due(5) == []
    q.complete(jid, "20261013-abc123")
    assert q.get(jid).status == "done"
    assert q.get(jid).entry_id == "20261013-abc123"


def test_claim_respects_limit_and_order(tmp_path):
    q, _ = make(tmp_path)
    ids = [q.enqueue("note", {"text": str(i)}, "cli", "u") for i in range(3)]
    assert [j.id for j in q.claim_due(2)] == ids[:2]
    assert q.claim_due(0) == []


def test_retry_schedule_then_fail(tmp_path):
    q, clock = make(tmp_path)
    jid = q.enqueue("url", {"url": "x"}, "cli", "u")
    for delay in RETRY_DELAYS:
        q.claim_due(1)
        assert q.retry_or_fail(jid, "net") is True
        assert q.claim_due(1) == []  # not due yet
        clock.now += delay
    [job] = q.claim_due(1)
    assert job.attempts == 3
    assert q.retry_or_fail(jid, "still broken") is False
    assert q.get(jid).status == "failed"
    assert q.get(jid).last_error == "still broken"
    assert [j.id for j in q.recent_failures()] == [jid]


def test_two_queues_on_one_connection_never_share_a_job(tmp_path):
    conn = connect(tmp_path / "j.db", embed_dim=8)
    q1, q2 = JobQueue(conn, clock=Clock()), JobQueue(conn, clock=Clock())
    ids = [q1.enqueue("note", {"text": str(i)}, "cli", "u") for i in range(3)]
    first = [j.id for j in q1.claim_due(2)]
    second = [j.id for j in q2.claim_due(5)]
    assert first == ids[:2]
    assert second == ids[2:]
    assert q1.claim_due(5) == []


def test_recover_and_counts(tmp_path):
    q, _ = make(tmp_path)
    q.enqueue("note", {"text": "a"}, "cli", "u")
    q.enqueue("note", {"text": "b"}, "cli", "u")
    q.claim_due(1)
    assert q.counts() == {"queued": 1, "running": 1}
    assert q.recover() == 1
    assert q.counts() == {"queued": 2}


def test_has_pending_url(tmp_path):
    q, _ = make(tmp_path)
    assert not q.has_pending_url("https://a.com")
    jid = q.enqueue("url", {"url": "https://a.com"}, "cli", "u")
    q.enqueue("note", {"text": "https://a.com"}, "cli", "u")
    assert q.has_pending_url("https://a.com")
    assert not q.has_pending_url("https://b.com")
    q.claim_due(1)  # running still counts
    assert q.has_pending_url("https://a.com")
    q.retry_or_fail(jid, "boom")  # queued, waiting for retry
    assert q.has_pending_url("https://a.com")
    q.complete(jid, "20260101-abcdef")
    assert not q.has_pending_url("https://a.com")


def test_has_pending_url_matches_refetch_jobs(tmp_path):
    q, _ = make(tmp_path)
    jid = q.enqueue("refetch", {"entry_id": "20261013-abcdef", "url": "https://a.com"}, "cli", "u")
    assert q.has_pending_url("https://a.com")
    q.complete(jid, "20261013-abcdef")
    assert not q.has_pending_url("https://a.com")


def test_retry_or_fail_ignores_job_that_is_not_running(tmp_path):
    q, _ = make(tmp_path)
    jid = q.enqueue("url", {"url": "x"}, "cli", "u")
    q.claim_due(1)
    q.complete(jid, "20261013-abc123")
    assert q.retry_or_fail(jid, "late failure") is False
    job = q.get(jid)
    assert (job.status, job.attempts, job.last_error) == ("done", 0, None)


def test_recover_counts_crashes_and_fails_crash_loops(tmp_path):
    q, _ = make(tmp_path)
    jid = q.enqueue("url", {"url": "x"}, "cli", "u")
    for _ in range(len(RETRY_DELAYS)):
        q.claim_due(1)
        q.recover()
        assert q.get(jid).status == "queued"
    q.claim_due(1)
    q.recover()
    job = q.get(jid)
    assert job.status == "failed" and job.last_error == "程序在处理这个任务时多次中断"


def test_recover_failed_returns_the_jobs_it_failed(tmp_path):
    q, _ = make(tmp_path)
    looping = q.enqueue("url", {"url": "x"}, "tg", "me")
    for _ in range(len(RETRY_DELAYS)):
        q.claim_due(1)
        q.recover()
    other = q.enqueue("note", {"text": "b"}, "tg", "me")
    q.claim_due(2)  # both running when the process dies
    failed = q.recover_failed()
    assert [(j.id, j.status, j.channel, j.user_id) for j in failed] == [(looping, "failed", "tg", "me")]
    assert q.get(other).status == "queued"
    assert q.recover_failed() == []  # nothing running any more
    assert q.recover() == 0  # recover() still returns the re-queued count


def test_redirect_merges_moves_only_the_users_unfinished_window_merges_since_the_batch(tmp_path):
    q, clock = make(tmp_path)
    old, new = "20261013-aaaaaa", "20261013-bbbbbb"
    payload = {"entry_id": old, "files": [], "text": "补充", "fallback_id": "20261013-cccccc", "via": "window"}
    before = q.enqueue("merge", payload, "tg", "me")  # a retry from before the batch
    clock.now += 10
    first = q.enqueue("merge", payload, "tg", "me")  # the batch's first merge
    queued = q.enqueue("merge", payload, "tg", "me")
    running = q.enqueue("merge", payload, "tg", "me")
    for job in q.claim_due(5):
        if job.id in (before, first):
            q.complete(job.id, old)
    q.conn.execute("UPDATE jobs SET status = 'queued' WHERE id IN (?, ?)", (before, queued))
    explicit = q.enqueue("merge", {k: v for k, v in payload.items() if k != "via"}, "tg", "me")  # /add
    other_user = q.enqueue("merge", payload, "tg", "you")
    other_entry = q.enqueue("merge", {**payload, "entry_id": "20261013-dddddd"}, "tg", "me")
    other_kind = q.enqueue("attachments", {"entry_id": old, "files": []}, "tg", "me")
    assert q.redirect_merges("tg", "me", old, new, since_job=first) == 2
    assert q.get(queued).payload == {**payload, "entry_id": new} and q.get(running).payload["entry_id"] == new
    assert [q.get(i).payload["entry_id"] for i in (before, first, explicit, other_user, other_kind)] == [old] * 5
    assert q.get(other_entry).payload["entry_id"] == "20261013-dddddd"


def test_creating_and_postpone(tmp_path):
    q, clock = make(tmp_path)
    eid = "20261013-aaaaaa"
    att = q.enqueue("attachments", {"files": [], "caption": "", "entry_id": eid}, "tg", "me")
    merge = q.enqueue("merge", {"entry_id": eid, "files": [], "text": ""}, "tg", "me")
    assert q.creating("tg", "me", eid) and not q.creating("tg", "you", eid)
    assert not q.creating("tg", "me", "20261013-bbbbbb")
    for job in q.claim_due(5):
        if job.id == merge:
            q.postpone(job.id, 5)
    assert (q.get(merge).status, q.get(merge).attempts) == ("queued", 0)
    assert [j.id for j in q.claim_due(5)] == []  # att is running, merge not due yet
    clock.now += 5
    assert [j.id for j in q.claim_due(5)] == [merge]
    q.complete(att, eid)
    assert not q.creating("tg", "me", eid)
    assert q.get(att).created_at == 1000.0
