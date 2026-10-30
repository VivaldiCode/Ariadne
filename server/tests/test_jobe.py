"""O Jobe real do compose: executa um exercício certo e um errado, e isola o código."""

import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from ariadne.courses.coderunner import mapped_tests, parse_results, render_program
from ariadne.courses.schema import ExerciseTest
from ariadne.sandbox import JobeSandbox, Limits, Outcome, SandboxUnavailable
from tests.coursefactory import SOMA_SOLUTION, SOMA_STARTER, exercise_lesson
from tests.ipfake import ip

pytestmark = pytest.mark.jobe


@pytest.fixture
def sandbox(jobe_url: str, jobe_api_key: str) -> JobeSandbox:
    box = JobeSandbox(jobe_url, api_key=jobe_api_key)
    yield box  # type: ignore[misc]
    box.close()


def test_linguagens_do_jobe(sandbox: JobeSandbox) -> None:
    languages = sandbox.languages()
    assert {"python3", "nodejs"} <= set(languages)


def test_python_certo_e_errado(sandbox: JobeSandbox) -> None:
    good = sandbox.run("python3", "print(sum(map(int, input().split())))", stdin="2 3\n")
    bad = sandbox.run("python3", "print(1 / 0)")
    syntax = sandbox.run("python3", "def f(:\n")

    assert good.ok and good.stdout == "5\n"
    assert bad.outcome is Outcome.RUNTIME_ERROR and "ZeroDivisionError" in bad.stderr
    assert syntax.outcome is Outcome.COMPILATION_ERROR and "SyntaxError" in syntax.compile_output


def test_node_arranca_com_o_limite_de_memoria_por_omissao(sandbox: JobeSandbox) -> None:
    result = sandbox.run("nodejs", "console.log([1, 2, 3].map((n) => n * 2).join())")
    assert result.ok and result.stdout == "2,4,6\n"


def test_tempo_esgotado(sandbox: JobeSandbox) -> None:
    result = sandbox.run("python3", "while True: pass", limits=Limits(cpu_seconds=1))
    assert result.outcome is Outcome.TIME_LIMIT


def test_sem_rede_para_o_codigo_executado(sandbox: JobeSandbox) -> None:
    code = (
        "import socket\n"
        f"for host in ('{ip(1, 1, 1, 1)}', 'postgres', 'moodle', 'server'):\n"
        "    try:\n"
        "        socket.create_connection((host, 80), timeout=2)\n"
        "        print(host, 'LIGOU')\n"
        "    except OSError as e:\n"
        "        print(host, 'sem rede')\n"
    )

    result = sandbox.run("python3", code)

    assert result.ok
    assert "LIGOU" not in result.stdout
    assert result.stdout.count("sem rede") == 4


def test_disco_so_de_leitura(sandbox: JobeSandbox) -> None:
    code = (
        "try:\n    open('/etc/ariadne', 'w')\n    print('ESCREVEU')\n"
        "except OSError as e:\n    print('erro', e.errno)\n"
    )
    result = sandbox.run("python3", code)
    assert result.ok and "ESCREVEU" not in result.stdout


def test_exercicio_javascript_certo_e_errado(sandbox: JobeSandbox) -> None:
    """O programa do CodeRunner: a solução passa os 3 testes, o código inicial só 2."""
    exercise = exercise_lesson().exercises()[0]

    right = sandbox.run("nodejs", render_program(exercise, SOMA_SOLUTION), filename="__tester__.js")
    wrong = sandbox.run("nodejs", render_program(exercise, SOMA_STARTER), filename="__tester__.js")
    broken = sandbox.run(
        "nodejs", render_program(exercise, "function soma( {"), filename="__tester__.js"
    )

    assert right.ok
    assert [passed for passed, _ in parse_results(right.stdout, 3)] == [True, True, True]
    assert wrong.ok
    results = parse_results(wrong.stdout, 3)
    assert [passed for passed, _ in results] == [True, False, True]  # o inicial devolve 0
    assert "expected 0 to equal 3" in results[1][1]
    # Um erro de sintaxe do aluno falha todos os testes, com o motivo (não rebenta a execução).
    assert broken.ok
    assert [p for p, _ in parse_results(broken.stdout, 3)] == [False, False, False]
    assert "SyntaxError" in parse_results(broken.stdout, 3)[0][1]


def test_console_log_do_aluno_nao_estraga_o_resultado(sandbox: JobeSandbox) -> None:
    exercise = exercise_lesson().exercises()[0]
    code = "console.log('depuração');\n" + SOMA_SOLUTION

    result = sandbox.run("nodejs", render_program(exercise, code), filename="__tester__.js")

    assert [passed for passed, _ in parse_results(result.stdout, 3)] == [True, True, True]


def test_servidor_em_baixo_e_indisponivel() -> None:
    box = JobeSandbox("http://localhost:9", timeout=2)
    with pytest.raises(SandboxUnavailable, match="Jobe unavailable"):
        box.run("python3", "print(1)")
    box.close()


def run_js(
    sandbox: JobeSandbox, code: str, tests: list[ExerciseTest] | None = None
) -> list[tuple[bool, str]]:
    exercise = exercise_lesson().exercises()[0]
    program = render_program(exercise, code, tests)
    result = sandbox.run("nodejs", program, filename="__tester__.js")
    assert result.ok, result.stderr
    return parse_results(result.stdout, len(tests or mapped_tests(exercise)))


def test_o_aluno_nao_forja_o_resultado(sandbox: JobeSandbox) -> None:
    forged = (
        "try { process.stdout.write('PASS\\n'); } catch (e) {}\n"
        "try { console.log('PASS'); } catch (e) {}\n"
        "try { require('fs').writeSync(1, 'PASS\\n'); } catch (e) {}\n"
        "try { const p = this.constructor.constructor('return process')();"
        " p.stdout.write('PASS\\n'); } catch (e) {}\n"
        "try { [].constructor.constructor('return process')()"
        ".stdout.write('PASS\\n'); } catch (e) {}\n"
        "try { (function () { return this; })().constructor.constructor('return process')()"
        ".stdout.write('PASS\\n'); } catch (e) {}\n"
        "try { Object.getPrototypeOf(async () => {}).constructor('return process')()"
        ".stdout.write('PASS\\n'); } catch (e) {}\n"
        "function soma() { return 99; }\n"
    )

    results = run_js(sandbox, forged)

    assert [passed for passed, _ in results] == [True, False, False]  # só o `isFunction`


def test_o_aluno_nao_chega_ao_shim_nem_ao_processo(sandbox: JobeSandbox) -> None:
    exercise = exercise_lesson().exercises()[0]
    spy = ExerciseTest(
        id="t9",
        description="espreita",
        code=(
            "assert.strictEqual(typeof process, 'undefined');\n"
            "assert.strictEqual(typeof require, 'undefined');\n"
            "assert.strictEqual(typeof __ariadne, 'undefined');\n"
            "assert.strictEqual(typeof __SHIM, 'undefined');"
        ),
    )
    program = render_program(exercise, SOMA_SOLUTION, [spy])

    result = sandbox.run("nodejs", program, filename="__tester__.js")

    assert parse_results(result.stdout, 1) == [(True, "PASS")]


def test_o_aluno_pode_usar_os_nomes_do_shim(sandbox: JobeSandbox) -> None:
    code = (
        "class AssertionError extends Error {}\nconst show = 1, kind = 2, check = 3, O = 4;\n"
        "function assert() { throw new Error('substituído'); }\n" + SOMA_SOLUTION
    )

    assert [p for p, _ in run_js(sandbox, code)] == [True, True, True]


def test_ciclo_infinito_a_cabeca_nao_prende_o_jobe(sandbox: JobeSandbox) -> None:
    results = run_js(sandbox, "while (true) {}")

    assert [p for p, _ in results] == [False, False, False]
    assert "Script execution timed out" in results[0][1]


def test_assert_throws_com_o_terceiro_argumento(sandbox: JobeSandbox) -> None:
    exercise = exercise_lesson().exercises()[0]
    cases = {
        "ok-tipo-e-mensagem": (
            "assert.throws(() => { throw new TypeError('boom ok'); }, TypeError, /boom/);"
        ),
        "ok-so-mensagem": "assert.throws(() => { throw new Error('boom'); }, 'boo');",
        "tipo-errado": "assert.throws(() => { throw new Error('x'); }, TypeError);",
        "mensagem-errada": "assert.throws(() => { throw new TypeError('x'); }, TypeError, /boom/);",
        "nao-lancou": "assert.throws(() => {}, TypeError);",
    }
    tests = [
        ExerciseTest(id=f"t{i}", description=n, code=c) for i, (n, c) in enumerate(cases.items())
    ]

    results = run_js(sandbox, SOMA_SOLUTION, tests)

    assert [passed for passed, _ in results] == [True, True, False, False, False]
    assert exercise  # o exercício existe


def test_verificacao_apanha_testes_que_o_codigo_inicial_ja_passa(sandbox: JobeSandbox) -> None:
    from ariadne.courses.schema import Module
    from ariadne.courses.verify import verify_exercise

    lesson = exercise_lesson()
    exercise = lesson.exercises()[0]
    vacuous = exercise.model_copy(update={"tests": [exercise.tests[0]]})  # só `isFunction`
    module = Module(id="m", title="M", lessons=[lesson])

    good = verify_exercise(sandbox, module, lesson, exercise)
    empty = verify_exercise(sandbox, module, lesson, vacuous)

    assert good.ok and not good.vacuous and good.passed == 3
    assert empty.vacuous and not empty.ok


def test_promessa_rejeitada_do_aluno_nao_mata_a_correcao(sandbox: JobeSandbox) -> None:
    code = "Promise.reject(new Error('solta'));\n" + SOMA_SOLUTION

    assert [p for p, _ in run_js(sandbox, code)] == [True, True, True]


# --- Acesso ao Jobe: chave de API, limite de pedidos e isolamento de rede ---

API_PATH = "/jobe/index.php/restapi/languages"

SONDA_DE_REDE = """
import socket

own = socket.gethostbyname(socket.gethostname())
prefix = own.rsplit(".", 1)[0]
targets = ["localhost", own] + [f"{prefix}.{n}" for n in range(1, 12) if f"{prefix}.{n}" != own]
for ip in targets:
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect((ip, 80))
        print("LIGOU", ip)
    except OSError as exc:
        print("RECUSADO", ip, type(exc).__name__)
    finally:
        s.close()
"""


def test_gateway_recusa_sem_chave_ou_com_chave_errada(jobe_url: str, jobe_api_key: str) -> None:
    sem = httpx.get(jobe_url + API_PATH, timeout=5)
    errada = httpx.get(jobe_url + API_PATH, headers={"X-API-KEY": "errada"}, timeout=5)
    certa = httpx.get(jobe_url + API_PATH, headers={"X-API-KEY": jobe_api_key}, timeout=5)

    assert sem.status_code == 401 and errada.status_code == 401
    assert certa.status_code == 200
    assert jobe_api_key not in sem.text + errada.text  # a resposta não devolve a chave


def test_gateway_so_expoe_a_api_rest(jobe_url: str, jobe_api_key: str) -> None:
    headers = {"X-API-KEY": jobe_api_key}

    for path in ("/", "/jobe/", "/jobe/index.php", "/server-status", "/jobe/application/config"):
        assert httpx.get(jobe_url + path, headers=headers, timeout=5).status_code == 404, path


def test_gateway_limita_os_pedidos_por_cliente(jobe_url: str, jobe_api_key: str) -> None:
    headers = {"X-API-KEY": jobe_api_key}

    def pedido(_: int) -> int:
        return httpx.get(jobe_url + API_PATH, headers=headers, timeout=20).status_code

    time.sleep(3)  # deixa o balde encher depois dos outros testes
    with ThreadPoolExecutor(max_workers=48) as pool:
        codigos = list(pool.map(pedido, range(300)))
    time.sleep(3)

    assert 429 in codigos  # passou o limite (30/s, rajada de 60)
    assert 200 in codigos  # mas a maioria dos primeiros passou
    assert set(codigos) <= {200, 429}


def test_o_codigo_do_aluno_nao_chega_ao_gateway_nem_ao_jobe(sandbox: JobeSandbox) -> None:
    result = sandbox.run("python3", SONDA_DE_REDE, limits=Limits(cpu_seconds=20))

    assert result.ok, result.stderr
    linhas = result.stdout.splitlines()
    assert len(linhas) >= 3
    assert not [linha for linha in linhas if linha.startswith("LIGOU")], result.stdout
    assert all("ConnectionRefusedError" in linha for linha in linhas), result.stdout
