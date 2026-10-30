"""`CourseBackend` contra o Moodle do compose (com o CodeRunner a falar com o Jobe).

Marca `moodle`: precisa de `docker compose up -d moodle jobe-gateway` e do token do serviço
(`ariadne moodle bootstrap`). Cada teste usa um curso com id único e apaga-o no fim.
"""

import json
import secrets
from collections.abc import Iterator

import psycopg
import pytest

from ariadne.courses.backend import CourseBackendError, CourseBackendUnavailable
from ariadne.courses.moodle import MoodleBackend, build_payload
from ariadne.courses.schema import Course, ExerciseTest, Module, TextBlock
from tests.coursefactory import SOMA_SOLUTION, make_course
from tests.ipfake import block_edges, ip, ip6_linklocal, ip6_loopback, reserved_blocks

pytestmark = pytest.mark.moodle


@pytest.fixture
def backend(moodle_access: tuple[str, str]) -> Iterator[MoodleBackend]:
    url, token = moodle_access
    moodle = MoodleBackend(url, token, public_url=url, timeout=120)
    yield moodle
    moodle.close()


@pytest.fixture
def course(backend: MoodleBackend) -> Iterator[Course]:
    """Um curso canónico com id único, apagado do Moodle no fim."""
    unique = secrets.token_hex(3)
    course = make_course(f"teste-{unique}", extra_lessons=True)
    yield course
    backend.delete_course(course)


def quiz_of(result, ref: str):  # type: ignore[no-untyped-def]
    activity = result.activities[ref]
    assert activity.kind == "quiz"
    return activity


def test_so_o_jobe_passa_na_protecao_contra_ssrf(
    backend: MoodleBackend, jobe_gateway_ip: str
) -> None:
    """Pergunta ao Moodle real (`curl_security_helper::url_is_blocked`) o que ele deixa chamar."""
    prefix = jobe_gateway_ip.rsplit(".", 1)[0]
    last = int(jobe_gateway_ip.rsplit(".", 1)[1])
    allowed = [f"http://{jobe_gateway_ip}/", "http://jobe-gateway/jobe/index.php/restapi/languages"]
    blocked = [
        # vizinhos do gateway na mesma rede (o CIDR dividido não pode deixar buracos)
        f"http://{prefix}.{last - 1}/",
        f"http://{prefix}.{last + 1}/",
        f"http://{prefix}.1/",
        f"http://{prefix}.254/",
        # os outros serviços do compose e as redes privadas
        "http://postgres/",
        "http://server:8800/",
        "http://kiwix:8080/",
        "http://jobe/",
        f"http://{ip(172, 16, 0, 1)}/",
        f"http://{ip(172, 17, 0, 1)}/",
        f"http://{ip(172, 31, 255, 254)}/",
        f"http://{ip(192, 168, 1, 1)}/",
        f"http://{ip(127, 0, 0, 1)}/",
        "http://localhost/",
        f"http://{ip(169, 254, 169, 254)}/latest/meta-data/",
        f"http://{ip(0, 0, 0, 0)}/",
        f"http://{ip(100, 64, 0, 1)}/",
        f"http://[{ip6_loopback()}]/",
        f"http://[{ip6_linklocal()}]/",
        "http://2130706433/",  # o loopback em decimal
        "http://0x7f.1/",
    ]

    # o primeiro e o último endereço de cada bloco reservado que o config.php manda bloquear (o 10/8
    # incluído), também como IPv4 mapeado em IPv6; o do gateway fica de fora (é o único que passa)
    for net, bits in reserved_blocks():
        for edge in block_edges(net, bits):
            if edge != jobe_gateway_ip:
                blocked += [f"http://{edge}/", f"http://[{':'.join(['', '', 'ffff', edge])}]/"]

    verdict = backend.url_blocked(allowed + blocked)

    assert [url for url in allowed if verdict[url]] == [], "o Jobe devia passar"
    assert [url for url in blocked if not verdict[url]] == [], "devia estar bloqueado"


def test_o_servico_responde_com_o_plugin_e_o_coderunner(backend: MoodleBackend) -> None:
    info = backend.check()

    assert info["moodle"].startswith("4.5")
    assert info["coderunner"].startswith("5.")
    assert info["jobe_host"] == "jobe-gateway"  # nunca o servidor público do CodeRunner


def test_o_teste_de_ligacao_das_integracoes_aceita_o_token(moodle_access: tuple[str, str]) -> None:
    """O botão «Testar ligação» (ecrã Integrações) usa uma função que o serviço `ariadne` tem."""
    from ariadne.config import Settings
    from ariadne.vault.testers import run_test

    url, token = moodle_access
    result = run_test("moodle", Settings(database_url="", moodle_url=url, moodle_token=token))
    assert result.ok, result.message


def test_publica_o_curso_e_e_idempotente(backend: MoodleBackend, course: Course) -> None:
    first = backend.publish(course)

    assert first.created == 1 + 3  # o curso e 3 atividades
    assert set(first.activities) == {"bases/variaveis", "bases/somar", "funcoes/repetir"}
    assert first.activities["bases/variaveis"].kind == "quiz"  # texto + quiz de escolha
    assert first.activities["funcoes/repetir"].kind == "page"
    assert quiz_of(first, "bases/somar").question_ids  # a pergunta CodeRunner foi importada
    assert "/course/view.php?id=" in first.course_url

    again = backend.publish(course)

    assert (again.created, again.updated, again.removed) == (0, 0, 0)
    assert again.unchanged == 4
    assert {a.status for a in again.activities.values()} == {"unchanged"}
    assert again.course_id == first.course_id
    assert (
        again.activities["bases/somar"].activity_id == first.activities["bases/somar"].activity_id
    )


def test_licao_com_quiz_de_escolha_vira_quiz_do_moodle(
    backend: MoodleBackend, course: Course
) -> None:
    from ariadne.courses.schema import QuizBlock, QuizChoice, QuizQuestion

    lesson = course.modules[0].lessons[0]  # «variáveis»: texto, exemplo, quiz e vídeo
    assert any(isinstance(b, QuizBlock) for b in lesson.blocks)
    extra = lesson.model_copy(
        update={
            "blocks": [
                *lesson.blocks,
                QuizBlock(
                    id="quiz-dois",
                    questions=[
                        QuizQuestion(
                            prompt="Quais declaram variáveis?",
                            choices=[
                                QuizChoice(text="let", correct=True),
                                QuizChoice(text="const", correct=True),
                                QuizChoice(text="print"),
                            ],
                            explanation="let e const.",
                        )
                    ],
                ),
            ]
        }
    )
    modules = [
        Module(id="bases", title="Bases", lessons=[extra, course.modules[0].lessons[1]]),
        course.modules[1],
    ]
    changed = Course.model_validate(
        course.model_copy(update={"modules": modules}).model_dump(mode="json")
    )

    result = backend.publish(changed)

    activity = result.activities["bases/variaveis"]
    assert activity.kind == "quiz" and len(activity.question_ids) == 2
    right = backend.grade_submission(activity.instance_id, ["const", ["let", "const"]])
    assert right.grade == 100.0


def test_o_conteudo_chega_ao_moodle_por_seccoes(backend: MoodleBackend, course: Course) -> None:
    result = backend.publish(course)

    contents = backend.call("core_course_get_contents", {"courseid": result.course_id})
    named = {s["name"]: [m["name"] for m in s["modules"]] for s in contents if s["section"] > 0}
    assert named == {
        "Bases": ["Variáveis e valores", "Somar dois números"],
        "Funções": ["Repetir um texto"],
    }


def test_exercicio_certo_e_errado_corrigidos_pelo_coderunner_no_jobe(
    backend: MoodleBackend, course: Course
) -> None:
    result = backend.publish(course)
    quiz = quiz_of(result, "bases/somar")

    right = backend.grade_submission(quiz.instance_id, [SOMA_SOLUTION])
    wrong = backend.grade_submission(quiz.instance_id, ["function soma(a, b) { return a - b; }"])
    broken = backend.grade_submission(quiz.instance_id, ["function soma( {"])

    assert (right.state, right.grade) == ("finished", 100.0)
    assert (wrong.state, wrong.grade) == ("finished", 0.0)
    assert broken.grade == 0.0
    assert "Incorrect" in wrong.feedback or "incorrect" in wrong.feedback.lower()


def test_texto_alterado_atualiza_so_essa_atividade(backend: MoodleBackend, course: Course) -> None:
    backend.publish(course)
    modules = list(course.modules)
    lesson = (
        modules[1].lessons[0].model_copy(update={"blocks": [TextBlock(markdown="Texto **novo**.")]})
    )
    changed = Course.model_validate(
        course.model_copy(
            update={
                "modules": [modules[0], Module(id="funcoes", title="Funções", lessons=[lesson])]
            }
        ).model_dump(mode="json")
    )

    result = backend.publish(changed)

    assert result.updated == 1 and result.created == 0
    assert result.activities["funcoes/repetir"].status == "updated"
    assert result.activities["bases/somar"].status == "unchanged"


def test_quiz_com_tentativas_nao_se_reestrutura(backend: MoodleBackend, course: Course) -> None:
    first = backend.publish(course)
    backend.grade_submission(quiz_of(first, "bases/somar").instance_id, [SOMA_SOLUTION])
    somar = course.modules[0].lessons[1]
    exercise = somar.exercises()[0]
    extra = exercise.model_copy(
        update={
            "tests": [
                *exercise.tests,
                ExerciseTest(
                    id="t4", description="soma(2, 2)", code="assert.strictEqual(soma(2, 2), 4);"
                ),
            ]
        }
    )
    lesson = somar.model_copy(
        update={"blocks": [extra if b is exercise else b for b in somar.blocks]}
    )
    changed = Course.model_validate(
        course.model_copy(
            update={
                "modules": [
                    Module(
                        id="bases", title="Bases", lessons=[course.modules[0].lessons[0], lesson]
                    ),
                    course.modules[1],
                ]
            }
        ).model_dump(mode="json")
    )

    result = backend.publish(changed)

    assert result.blocked_by_attempts == 1
    assert result.activities["bases/somar"].status == "blocked_by_attempts"
    assert any("tentativas" in w for w in result.warnings)


def test_licao_retida_pela_curadoria_nao_vai(backend: MoodleBackend, course: Course) -> None:
    result = backend.publish(
        course, gate=lambda ref: "à espera do curador" if ref == "bases/somar" else None
    )

    assert result.held == {"bases/somar": "à espera do curador"}
    assert set(result.activities) == {"bases/variaveis", "funcoes/repetir"}


def test_atividade_que_sai_do_canonico_sai_do_moodle(
    backend: MoodleBackend, course: Course
) -> None:
    backend.publish(course)
    smaller = Course.model_validate(
        course.model_copy(update={"modules": course.modules[:1]}).model_dump(mode="json")
    )

    result = backend.publish(smaller)

    assert result.removed == 1
    assert "funcoes/repetir" not in result.activities


def test_a_solucao_oficial_nunca_vai_para_o_moodle(course: Course) -> None:
    import xml.etree.ElementTree as ET

    payload, _ = build_payload(course)

    quiz = next(
        a for s in payload["sections"] for a in s["activities"] if a["key"] == "bases/somar"
    )
    question = ET.fromstring(quiz["questions_xml"]).find("question")
    assert question is not None
    assert (question.findtext("answer") or "") == ""
    assert "return a + b" not in (question.findtext("questiontext/text") or "")
    assert "return a + b" not in (question.findtext("template") or "")
    assert "return a + b" not in (question.findtext("answerpreload") or "")
    for activity in (
        a for s in payload["sections"] for a in s["activities"] if a["kind"] == "page"
    ):
        assert "return a + b" not in activity["html"]
    assert quiz["questions_xml"].count("<testcase ") == 3
    assert "return a + b" not in json.dumps(payload)


def test_o_aluno_nao_ve_a_solucao_na_revisao(backend: MoodleBackend, course: Course) -> None:
    result = backend.publish(course)
    quiz = quiz_of(result, "bases/somar")

    wrong = backend.grade_submission(quiz.instance_id, ["function soma(a, b) { return a - b; }"])

    assert "return a + b" not in wrong.feedback


def test_moodle_em_baixo_diz_o_que_fazer() -> None:
    backend = MoodleBackend("http://localhost:9", "token", timeout=2)

    with pytest.raises(
        CourseBackendUnavailable, match=r"not responding.*docker compose up -d moodle"
    ):
        backend.check()
    backend.close()


def test_sem_token_diz_o_que_fazer() -> None:
    backend = MoodleBackend("http://localhost:8805", "")
    with pytest.raises(CourseBackendUnavailable, match="MOODLE_TOKEN"):
        backend.check()
    backend.close()


def test_token_invalido(moodle_access: tuple[str, str]) -> None:
    backend = MoodleBackend(moodle_access[0], "0" * 32)
    with pytest.raises(CourseBackendUnavailable, match="token"):
        backend.check()
    backend.close()


def test_erro_do_moodle_chega_com_a_mensagem(backend: MoodleBackend) -> None:
    with pytest.raises(CourseBackendError, match="payload não é JSON"):
        backend.call("local_ariadne_publish_course", {"payload": "isto não é json"})


# --- o papel mínimo, o âmbito do plugin e a robustez da publicação -----------------------------


def moodle_db() -> "psycopg.Connection[tuple[object, ...]]":
    from tests.conftest import TestEnv

    env = TestEnv()
    return psycopg.connect(
        f"host=localhost port={env.postgres_port} user=ariadne password={env.postgres_password} "
        "dbname=moodle"
    )


def test_o_papel_ariadne_e_minimo(backend: MoodleBackend) -> None:
    with moodle_db() as conn:
        archetype = conn.execute(
            "SELECT archetype FROM mdl_role WHERE shortname = 'ariadne'"
        ).fetchone()
        caps = {
            row[0]
            for row in conn.execute(
                "SELECT capability FROM mdl_role_capabilities rc"
                " JOIN mdl_role r ON r.id = rc.roleid"
                " WHERE r.shortname = 'ariadne' AND rc.permission = 1"
            )
        }
        roles = conn.execute(
            "SELECT r.shortname FROM mdl_role_assignments ra JOIN mdl_role r ON r.id = ra.roleid"
            " JOIN mdl_user u ON u.id = ra.userid WHERE u.username = 'ariadnews'"
        ).fetchall()
        admins = conn.execute("SELECT value FROM mdl_config WHERE name = 'siteadmins'").fetchone()
        siteadmins = admins[0] if admins else ""
        user = conn.execute("SELECT id FROM mdl_user WHERE username = 'ariadnews'").fetchone()
    assert archetype == ("",)  # sem arquétipo: nada herdado de «gestor»
    assert roles == [("ariadne",)]
    assert user is not None and str(user[0]) not in str(siteadmins).split(",")
    forbidden = {
        "moodle/site:config",
        "moodle/user:create",
        "moodle/user:update",
        "moodle/user:delete",
        "enrol/manual:enrol",
        "moodle/role:assign",
        "moodle/role:manage",
        "moodle/backup:backupcourse",
        "moodle/restore:restorecourse",
        "moodle/course:changecategory",
        "moodle/site:accessallgroups",
    }
    assert not (caps & forbidden), caps & forbidden
    assert {"moodle/course:create", "moodle/course:manageactivities", "mod/quiz:attempt"} <= caps
    assert len(caps) < 45


def test_o_servico_so_tem_as_funcoes_que_usa(backend: MoodleBackend) -> None:
    with moodle_db() as conn:
        functions = {
            row[0]
            for row in conn.execute(
                "SELECT functionname FROM mdl_external_services_functions f"
                " JOIN mdl_external_services s ON s.id = f.externalserviceid"
                " WHERE s.shortname = 'ariadne'"
            )
        }
    assert functions == {
        "local_ariadne_ping",
        "local_ariadne_publish_course",
        "local_ariadne_delete_course",
        "local_ariadne_url_blocked",
        "core_course_get_contents",
        "mod_quiz_start_attempt",
        "mod_quiz_get_attempt_data",
        "mod_quiz_process_attempt",
        "mod_quiz_get_attempt_review",
    }
    for function, params in (
        ("core_user_create_users", {}),
        ("enrol_manual_enrol_users", {}),
        ("core_course_delete_courses", {"courseids": [1]}),
        ("core_course_get_courses", {}),
    ):
        with pytest.raises(CourseBackendError):
            backend.call(function, params)


def test_o_token_tem_validade(backend: MoodleBackend) -> None:
    import time

    with moodle_db() as conn:
        row = conn.execute(
            "SELECT t.validuntil, t.iprestriction FROM mdl_external_tokens t"
            " JOIN mdl_user u ON u.id = t.userid WHERE u.username = 'ariadnews'"
            " AND t.token = %s",
            (backend.token,),
        ).fetchone()
    assert row is not None
    assert row[0] and row[0] > time.time() + 300 * 86400  # ≈ um ano, renovado a 30 dias do fim


def test_o_plugin_so_toca_em_cursos_ariadne(backend: MoodleBackend) -> None:
    payload = {"idnumber": "outro:curso", "shortname": "outro", "fullname": "Outro", "sections": []}
    with pytest.raises(CourseBackendError, match="ariadne:"):
        backend.call("local_ariadne_publish_course", {"payload": json.dumps(payload)})
    with pytest.raises(CourseBackendError, match="ariadne:"):
        backend.call("local_ariadne_delete_course", {"idnumber": "outro:curso"})
    assert backend.call("local_ariadne_delete_course", {"idnumber": "ariadne:nao-existe"}) is False


def test_quiz_nao_perde_as_perguntas_se_a_troca_falhar(
    backend: MoodleBackend, course: Course
) -> None:
    first = backend.publish(course)
    before = quiz_of(first, "bases/somar").question_ids
    payload, _ = build_payload(course)
    quiz = next(
        a for s in payload["sections"] for a in s["activities"] if a["key"] == "bases/somar"
    )
    quiz["questions_xml"] = (
        '<?xml version="1.0"?><quiz><question type="inexistente"></question></quiz>'
    )
    quiz["hash"] = "outro-hash"

    with pytest.raises(CourseBackendError):
        backend.call("local_ariadne_publish_course", {"payload": json.dumps(payload)})

    again = backend.publish(
        course
    )  # o canónico não mudou: tudo igual, e as perguntas são as mesmas
    assert quiz_of(again, "bases/somar").question_ids == before
    assert {a.status for a in again.activities.values()} == {"unchanged"}
    graded = backend.grade_submission(quiz_of(again, "bases/somar").instance_id, [SOMA_SOLUTION])
    assert graded.grade == 100.0


def test_pergunta_nova_so_apaga_a_antiga_no_fim(backend: MoodleBackend, course: Course) -> None:
    first = backend.publish(course)
    old = set(quiz_of(first, "bases/somar").question_ids)
    somar = course.modules[0].lessons[1]
    exercise = somar.exercises()[0]
    changed = exercise.model_copy(update={"tests": exercise.tests[:2]})
    lesson = somar.model_copy(
        update={"blocks": [changed if b is exercise else b for b in somar.blocks]}
    )
    modules = [
        Module(id="bases", title="Bases", lessons=[course.modules[0].lessons[0], lesson]),
        course.modules[1],
    ]
    new_course = Course.model_validate(
        course.model_copy(update={"modules": modules}).model_dump(mode="json")
    )

    result = backend.publish(new_course)

    new = set(quiz_of(result, "bases/somar").question_ids)
    assert result.activities["bases/somar"].status == "updated"
    assert new and not (new & old)  # perguntas novas; as antigas já saíram
    assert (
        backend.grade_submission(quiz_of(result, "bases/somar").instance_id, [SOMA_SOLUTION]).grade
        == 100.0
    )


def test_licao_que_muda_de_tipo_nao_duplica_a_atividade(
    backend: MoodleBackend, course: Course
) -> None:
    backend.publish(course)
    lesson = course.modules[1].lessons[0]  # «repetir»: uma página
    exercise = course.modules[0].lessons[1].exercises()[0].model_copy(update={"id": "repetir-ex"})
    with_exercise = lesson.model_copy(update={"blocks": [*lesson.blocks, exercise]})
    modules = [course.modules[0], Module(id="funcoes", title="Funções", lessons=[with_exercise])]
    as_quiz = Course.model_validate(
        course.model_copy(update={"modules": modules}).model_dump(mode="json")
    )

    result = backend.publish(as_quiz)

    assert result.activities["funcoes/repetir"].kind == "quiz"
    contents = backend.call("core_course_get_contents", {"courseid": result.course_id})
    names = [m["name"] for s in contents for m in s["modules"]]
    assert names.count("Repetir um texto") == 1  # a página antiga saiu
    back = backend.publish(course)  # e de volta a página
    assert back.activities["funcoes/repetir"].kind == "page"
    contents = backend.call("core_course_get_contents", {"courseid": back.course_id})
    assert [m["name"] for s in contents for m in s["modules"]].count("Repetir um texto") == 1


def test_publicacoes_concorrentes_nao_duplicam(
    moodle_access: tuple[str, str], course: Course
) -> None:
    import threading

    url, token = moodle_access
    results: list[object] = []

    def publish() -> None:
        with MoodleBackend(url, token, public_url=url, timeout=180) as own:
            try:
                results.append(own.publish(course))
            except (
                CourseBackendError
            ) as exc:  # o bloqueio demorou demais: aceitável, mas sem estragos
                results.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert any(not isinstance(r, Exception) for r in results)
    with MoodleBackend(url, token, public_url=url) as check:
        found = [
            c for c in check.call("core_course_get_contents", {"courseid": 1}) if False
        ]  # só confirma que o serviço responde
        _ = found
    with moodle_db() as conn:
        courses = conn.execute(
            "SELECT count(*) FROM mdl_course WHERE idnumber = %s",
            (f"ariadne:{course.id}:{course.version}",),
        ).fetchone()
        cms = conn.execute(
            "SELECT cm.idnumber, count(*) FROM mdl_course_modules cm"
            " JOIN mdl_course c ON c.id = cm.course"
            " WHERE c.idnumber = %s AND cm.idnumber <> '' GROUP BY cm.idnumber",
            (f"ariadne:{course.id}:{course.version}",),
        ).fetchall()
    assert courses == (1,)
    assert {k: n for k, n in cms} == {"bases/variaveis": 1, "bases/somar": 1, "funcoes/repetir": 1}


def test_publicacao_em_partes(backend: MoodleBackend, course: Course) -> None:
    from ariadne.courses.moodle import chunk_payload

    payload, _ = build_payload(course)
    calls = chunk_payload(payload, size=1)

    # uma chamada por lição (3) e a última, só com o que fica
    assert len(calls) == 4
    assert [sum(1 for s in c["sections"] if s["activities"]) for c in calls] == [1, 1, 1, 0]
    assert calls[-1]["prune"] == ["bases/variaveis", "bases/somar", "funcoes/repetir"]
    assert all("prune" not in c for c in calls[:-1])

    result = backend.publish(course)

    assert result.created == 4 and set(result.activities) == set(calls[-1]["prune"])
