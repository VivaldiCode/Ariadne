import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ariadne import queue
from ariadne.db.models import Job

pytestmark = pytest.mark.db


def job_row(sessions: sessionmaker[Session], job_id: int) -> Job:
    with sessions() as s:
        return s.get_one(Job, job_id)


def test_enqueue_e_claim_por_prioridade(db: sessionmaker[Session]) -> None:
    with db() as s:
        low = queue.enqueue(s, "index_document", {"n": 1}, priority=1)
        high = queue.enqueue(s, "index_document", {"n": 2}, priority=5)
        other = queue.enqueue(s, "outro", {"n": 3}, priority=99)
        s.commit()

        first = queue.claim(s, ["index_document"], "w1")
        s.commit()
        second = queue.claim(s, ["index_document"], "w1")
        s.commit()
        third = queue.claim(s, ["index_document"], "w1")  # 'outro' não é do worker

    assert first is not None and second is not None and third is None
    assert (first.id, second.id) == (high, low)
    assert first.payload == {"n": 2} and first.attempts == 1
    row = job_row(db, high)
    assert (row.status, row.locked_by) == ("running", "w1") and row.locked_at is not None
    assert job_row(db, other).status == "pending"


def test_claim_respeita_run_after(db: sessionmaker[Session]) -> None:
    with db() as s:
        queue.enqueue(s, "k", {}, run_after=datetime.now(UTC) + timedelta(hours=1))
        s.commit()
        assert queue.claim(s, ["k"], "w") is None


def test_dedupe_sobe_a_prioridade_em_vez_de_duplicar(db: sessionmaker[Session]) -> None:
    with db() as s:
        first = queue.enqueue(s, "k", {"d": "a"}, priority=2, dedupe_key="k:a")
        again = queue.enqueue(s, "k", {"d": "a"}, priority=7, dedupe_key="k:a")
        lower = queue.enqueue(s, "k", {"d": "a"}, priority=1, dedupe_key="k:a")
        s.commit()
        assert first == again == lower
        assert s.get_one(Job, first).priority == 7
        assert len(s.scalars(select(Job)).all()) == 1

        # Depois de reclamado já não há pendente: a mesma chave cria um job novo.
        queue.claim(s, ["k"], "w")
        s.commit()
        fresh = queue.enqueue(s, "k", {"d": "a"}, dedupe_key="k:a")
        s.commit()
        assert fresh != first


def test_falha_recua_e_acaba_em_failed(db: sessionmaker[Session]) -> None:
    with db() as s:
        job_id = queue.enqueue(s, "k", {})
        s.commit()
        for attempt in range(1, 4):
            claimed = queue.claim(s, ["k"], "w")
            if attempt == 1:
                assert claimed is not None and claimed.attempts == 1
            else:  # o recuo põe run_after no futuro: antecipa-o para o teste
                assert claimed is None
                s.execute(update(Job).values(run_after=datetime.now(UTC) - timedelta(minutes=1)))
                claimed = queue.claim(s, ["k"], "w")
                assert claimed is not None and claimed.attempts == attempt
            state = queue.fail(s, job_id, f"erro {attempt}", "w", max_attempts=3)
            s.commit()
            assert state == ("failed" if attempt == 3 else "pending")
    row = job_row(db, job_id)
    assert (row.status, row.last_error, row.attempts) == ("failed", "erro 3", 3)
    assert row.finished_at is not None and row.locked_by is None


def test_backoff_exponencial_com_teto() -> None:
    assert [queue.backoff(n).total_seconds() for n in (1, 2, 3)] == [30, 60, 120]
    assert queue.backoff(30) == queue.BACKOFF_MAX


def test_falha_permanente_nao_repete(db: sessionmaker[Session]) -> None:
    with db() as s:
        job_id = queue.enqueue(s, "k", {})
        s.commit()
        queue.claim(s, ["k"], "w")
        assert queue.fail(s, job_id, "fonte desapareceu", "w", permanent=True) == "failed"
        s.commit()
    assert job_row(db, job_id).status == "failed"


def test_defer_nao_gasta_tentativas(db: sessionmaker[Session]) -> None:
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    with db() as s:
        job_id = queue.enqueue(s, "k", {})
        s.commit()
        queue.claim(s, ["k"], "w")
        assert queue.defer(s, job_id, tomorrow, "w", "orçamento esgotado") == "pending"
        s.commit()
        assert queue.claim(s, ["k"], "w") is None
    row = job_row(db, job_id)
    assert (row.status, row.attempts, row.last_error) == ("pending", 0, "orçamento esgotado")
    assert abs((row.run_after - tomorrow).total_seconds()) < 1


def test_requeue_com_outro_pendente_da_mesma_chave_da_o_job_por_feito(
    db: sessionmaker[Session],
) -> None:
    with db() as s:
        running = queue.enqueue(s, "k", {}, dedupe_key="x")
        s.commit()
        queue.claim(s, ["k"], "w")
        s.commit()
        newer = queue.enqueue(s, "k", {}, dedupe_key="x")  # chegou enquanto o outro corria
        s.commit()
        assert queue.fail(s, running, "boom", "w") == "done"
        s.commit()
    assert job_row(db, running).status == "done"
    assert job_row(db, newer).status == "pending"


def test_recupera_jobs_presos(db: sessionmaker[Session]) -> None:
    with db() as s:
        stuck = queue.enqueue(s, "k", {}, priority=3)
        spent = queue.enqueue(s, "k", {}, priority=2)
        fresh = queue.enqueue(s, "k", {}, priority=1)
        s.commit()
        for _ in range(3):
            queue.claim(s, ["k"], "morto")
        s.execute(update(Job).where(Job.id == spent).values(attempts=5))
        old = datetime.now(UTC) - timedelta(minutes=30)
        s.execute(update(Job).where(Job.id.in_([stuck, spent])).values(locked_at=old))
        s.commit()

        assert queue.recover_stale(s, timedelta(minutes=15), max_attempts=5) == 2
        s.commit()
    assert job_row(db, stuck).status == "pending"
    assert job_row(db, spent).status == "failed"
    assert job_row(db, fresh).status == "running"  # bloqueio recente: não se toca


def test_claim_concorrente_nunca_entrega_o_mesmo_job_duas_vezes(db: sessionmaker[Session]) -> None:
    total, workers = 60, 6
    with db() as s:
        for n in range(total):
            queue.enqueue(s, "k", {"n": n}, priority=n % 5)
        s.commit()
    claimed: list[int] = []
    guard = threading.Lock()
    barrier = threading.Barrier(workers)

    def work(name: str) -> None:
        barrier.wait()
        while True:
            with db() as s:
                job = queue.claim(s, ["k"], name)
                s.commit()
            if job is None:
                return
            with guard:
                claimed.append(job.id)

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(claimed) == total
    assert len(set(claimed)) == total


def test_so_o_dono_de_um_job_running_o_pode_mexer(db: sessionmaker[Session]) -> None:
    with db() as s:
        job_id = queue.enqueue(s, "k", {})
        s.commit()
        # Ainda pendente: ninguém o pode concluir.
        assert queue.complete(s, job_id, "w1") is False
        claimed = queue.claim(s, ["k"], "w1")
        s.commit()
        assert claimed is not None and claimed.worker_id == "w1"

        # Outro worker não o toca.
        assert queue.touch(s, job_id, "w2") is False
        assert queue.complete(s, job_id, "w2") is False
        assert queue.fail(s, job_id, "x", "w2") is None
        assert queue.defer(s, job_id, datetime.now(UTC), "w2") is None
        s.commit()
        assert job_row(db, job_id).status == "running"

        # Recuperado como preso e reclamado por outro: o dono antigo já não pode concluir.
        s.execute(update(Job).values(locked_at=datetime.now(UTC) - timedelta(hours=1)))
        s.commit()
        queue.recover_stale(s, timedelta(minutes=15), 5)
        s.commit()
        assert queue.claim(s, ["k"], "w2") is not None
        s.commit()
        assert queue.complete(s, job_id, "w1") is False
        assert queue.touch(s, job_id, "w2") is True
        assert queue.complete(s, job_id, "w2") is True
        s.commit()
    assert job_row(db, job_id).status == "done"
