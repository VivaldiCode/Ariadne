"""`MoodleBackend`: publica um curso canónico no Moodle (REST + plugin `local_ariadne`).

Cada lição vira uma atividade: com exercício corrigível (JavaScript e testes mapeados) é um *quiz*
com uma pergunta CodeRunner (o enunciado é o texto da pergunta, os testes correm no Jobe); as
outras são uma *página*. Cada módulo é uma secção. O curso do Moodle identifica-se por
`ariadne:<curso>:<versão>`: cada versão do curso é um curso do Moodle, e repetir a publicação da
mesma versão atualiza-o no lugar (o plugin compara o `hash` de cada atividade e só toca no que
mudou). As soluções nunca vão para o Moodle.
"""

import hashlib
import html as htmllib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from ariadne.courses.backend import (
    CourseBackend,
    CourseBackendError,
    CourseBackendUnavailable,
    Gate,
    PublishedActivity,
    PublishResult,
)
from ariadne.courses.coderunner import (
    QuestionXml,
    is_gradable,
    multichoice_xml,
    question_xml,
    quiz_xml,
)
from ariadne.courses.render import (
    lesson_html,
    markdown_to_html,
    module_summary_html,
    overview_html,
    text_html_before_exercise,
)
from ariadne.courses.schema import Course, Lesson, Module, QuizBlock

WS = "/webservice/rest/server.php"
SHORTNAME_MAX = 255
CHUNK_ACTIVITIES = 12  # lições por chamada ao plugin


def moodle_idnumber(course: Course) -> str:
    return f"ariadne:{course.id}:{course.version}"


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def gradable_exercises(lesson: Lesson) -> list[Any]:
    return [e for e in lesson.exercises() if is_gradable(e)]


def activity_for(module: Module, lesson: Lesson) -> dict[str, Any]:
    """A atividade do Moodle de uma lição (JSON para o plugin)."""
    ref = f"{module.id}/{lesson.id}"
    exercises = gradable_exercises(lesson)
    quizzes = [b for b in lesson.blocks if isinstance(b, QuizBlock)]
    if exercises or quizzes:
        questions: list[QuestionXml] = [question_xml(lesson, e) for e in exercises]
        for quiz in quizzes:
            questions += multichoice_xml(quiz, lesson)
        xml = quiz_xml(questions)
        # Com um exercício o texto é o enunciado da pergunta; só com escolhas, a introdução.
        intro = (
            markdown_to_html(lesson.summary)
            if exercises and lesson.summary
            else ("" if exercises else text_html_before_exercise(lesson))
        )
        return {
            "key": ref,
            "kind": "quiz",
            "name": lesson.title,
            "intro": intro,
            "questions_xml": xml,
            "hash": _hash("quiz", lesson.title, intro, xml),
        }
    body = lesson_html(lesson)
    notes = []
    if lesson.exercises():
        notes.append(
            "<p><em>Este exercício não tem correção automática no Moodle "
            "(os testes dependem do browser ou de outra linguagem).</em></p>"
        )
    page = "".join(notes) + body
    return {
        "key": ref,
        "kind": "page",
        "name": lesson.title,
        "html": page,
        "hash": _hash("page", lesson.title, page),
    }


def build_payload(
    course: Course, gate: Gate | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """O JSON que o plugin recebe e as lições retidas pelo `gate` (referência -> motivo)."""
    held: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    for module in course.modules:
        activities = []
        for lesson in module.lessons:
            ref = f"{module.id}/{lesson.id}"
            if gate is not None and (reason := gate(ref)):
                held[ref] = reason
                continue
            activities.append(activity_for(module, lesson))
        sections.append(
            {
                "key": module.id,
                "name": module.title,
                "summary": module_summary_html(module),
                "activities": activities,
            }
        )
    shortname = f"{course.id}-{course.version}"[:SHORTNAME_MAX]
    payload = {
        "idnumber": moodle_idnumber(course),
        "shortname": shortname,
        "fullname": f"{course.title} ({course.version})",
        "summary": overview_html(course),
        "sections": sections,
    }
    return payload, held


def chunk_payload(payload: dict[str, Any], size: int = CHUNK_ACTIVITIES) -> list[dict[str, Any]]:
    """O payload em chamadas curtas: cada uma leva as secções todas (só os nomes) e as atividades
    de uma secção, no máximo `size`; a última só diz que atividades ficam (`prune`)."""
    sections = payload["sections"]
    keys = [a["key"] for s in sections for a in s["activities"]]
    base = {k: v for k, v in payload.items() if k != "sections"}
    calls: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        activities = section["activities"]
        for start in range(0, len(activities), size):
            part = [
                {**s, "activities": activities[start : start + size] if i == index else None}
                for i, s in enumerate(sections)
            ]
            calls.append({**base, "sections": part})
    if not calls:  # um curso sem atividades: o curso e as secções, só
        calls.append({**base, "sections": [{**s, "activities": None} for s in sections]})
    calls.append({**base, "sections": [{**s, "activities": None} for s in sections], "prune": keys})
    return calls


@dataclass(frozen=True)
class GradeResult:
    state: str  # 'finished' quando corrigido
    grade: float  # nota do quiz (0 a `max_grade`)
    max_grade: float
    feedback: str  # o resultado dos testes, em texto


class MoodleBackend(CourseBackend):
    name = "moodle"

    def __init__(
        self,
        url: str,
        token: str,
        *,
        public_url: str = "",
        timeout: float = 300.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.public_url = (public_url or url).rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    # --- chamadas ---

    def call(self, function: str, params: dict[str, Any] | None = None) -> Any:
        if not self.url or not self.token:
            raise CourseBackendUnavailable(
                "Moodle is not configured: MOODLE_URL and MOODLE_TOKEN are missing "
                "(docker compose up -d moodle; ariadne moodle bootstrap --env-file .env)."
            )
        data = {"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json"}
        data.update(_flatten(params or {}))
        try:
            response = self._client.post(self.url + WS, data=data)
        except httpx.HTTPError as exc:
            raise CourseBackendUnavailable(
                f"Moodle is not responding at {self.url} ({type(exc).__name__}). "
                "Is it running? (docker compose up -d moodle)"
            ) from exc
        if response.status_code != 200:
            raise CourseBackendUnavailable(
                f"Moodle answered {response.status_code} at {self.url}{WS}."
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise CourseBackendError(f"Unreadable Moodle response: {response.text[:200]}") from exc
        if isinstance(body, dict) and "exception" in body:
            code = str(body.get("errorcode") or body.get("exception"))
            message = f"Moodle ({function}): {body.get('message')} [{code}]"
            if body.get("debuginfo"):
                message += f" — {str(body['debuginfo'])[:300]}"
            if code in {"invalidtoken", "accessexception", "servicenotavailable"}:
                raise CourseBackendUnavailable(
                    message + " — invalid token? `ariadne moodle bootstrap`"
                )
            raise CourseBackendError(message)
        return body

    def check(self) -> dict[str, str]:
        raw = self.call("local_ariadne_ping")
        try:
            info = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CourseBackendError(f"local_ariadne_ping returned {raw!r}") from exc
        return {key: str(value) for key, value in info.items() if value is not None}

    def publish(self, course: Course, *, gate: Gate | None = None) -> PublishResult:
        """Publica em várias chamadas curtas (uma por secção ou parte dela, e uma última que retira
        o que o canónico já não tem): cada uma é idempotente e cabe no tempo de um pedido. O plugin
        bloqueia por curso, por isso duas publicações do mesmo curso nunca se cruzam."""
        payload, held = build_payload(course, gate)
        bodies = [self._publish_call(part) for part in chunk_payload(payload)]
        first = bodies[0]
        activities: dict[str, PublishedActivity] = {}
        totals = {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "blocked_by_attempts": 0,
            "removed": 0,
        }
        warnings: list[str] = []
        for body in bodies:
            for key, value in totals.items():
                totals[key] = value + int(body[key])
            warnings += [str(w) for w in body.get("warnings", [])]
            found = body.get("activities")  # o PHP codifica um mapa vazio como lista
            for key, info in found.items() if isinstance(found, dict) else []:
                activities[key] = PublishedActivity(
                    ref=key,
                    kind=str(info["kind"]),
                    status=str(info["status"]),
                    url=self._public(str(info["url"])),
                    activity_id=int(info["cmid"]),
                    instance_id=int(info["instance"]),
                    question_ids=tuple(int(q) for q in info.get("questions", [])),
                )
        status = str(first.get("course_status", "unchanged"))
        if status in ("created", "updated", "unchanged"):
            totals[status] += 1  # o curso conta uma vez, pela 1.ª chamada
        return PublishResult(
            course_id=int(first["course"]["id"]),
            course_url=self._public(str(first["course"]["url"])),
            created=totals["created"],
            updated=totals["updated"],
            unchanged=totals["unchanged"],
            blocked_by_attempts=totals["blocked_by_attempts"],
            removed=totals["removed"],
            activities=activities,
            held=held,
            warnings=warnings,
        )

    def _publish_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = self.call(
            "local_ariadne_publish_course", {"payload": json.dumps(payload, ensure_ascii=False)}
        )
        try:
            body = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CourseBackendError(f"The plugin returned {str(raw)[:200]!r}") from exc
        if not isinstance(body, dict):
            raise CourseBackendError(f"The plugin returned {str(raw)[:200]!r}")
        return body

    def delete_course(self, course: Course) -> bool:
        """Apaga do Moodle o curso desta versão (só os publicados por nós: `ariadne:…`)."""
        return bool(self.call("local_ariadne_delete_course", {"idnumber": moodle_idnumber(course)}))

    def url_blocked(self, urls: Sequence[str]) -> dict[str, bool]:
        """Que destinos o Moodle bloqueia nos pedidos que faz (a proteção contra SSRF)."""
        raw = self.call("local_ariadne_url_blocked", {"urls": json.dumps(list(urls))})
        return {str(k): bool(v) for k, v in json.loads(raw).items()}

    def _public(self, url: str) -> str:
        """O URL como o utilizador o vê: o Moodle responde com o Host do pedido (`moodle`)."""
        parts = urlsplit(url)
        public = urlsplit(self.public_url)
        return urlunsplit((public.scheme, public.netloc, parts.path, parts.query, parts.fragment))

    # --- correção (testes e verificação) ---

    def grade_submission(
        self, quiz_instance_id: int, answers: Sequence[str | Sequence[str]]
    ) -> GradeResult:
        """Faz uma tentativa no quiz, responde às perguntas (por ordem) e termina-a: o CodeRunner
        corre o código no Jobe. Serve para confirmar a correção automática de ponta a ponta. Cada
        resposta é o código (pergunta CodeRunner) ou o texto da(s) opção(ões) a marcar (escolha)."""
        started = self.call("mod_quiz_start_attempt", {"quizid": quiz_instance_id})
        attempt_id = int(started["attempt"]["id"])
        page = self.call("mod_quiz_get_attempt_data", {"attemptid": attempt_id, "page": 0})
        form: dict[str, str] = {}
        questions = page["questions"]
        if len(questions) != len(answers):
            raise CourseBackendError(
                f"The quiz has {len(questions)} questions and {len(answers)} answers were given."
            )
        for question, answer in zip(questions, answers, strict=True):
            form.update(_answer_fields(str(question["html"]), answer))
        self.call(
            "mod_quiz_process_attempt",
            {
                "attemptid": attempt_id,
                "finishattempt": 1,
                "data": [{"name": k, "value": v} for k, v in form.items()],
            },
        )
        review = self.call("mod_quiz_get_attempt_review", {"attemptid": attempt_id})
        feedback = " ".join(
            re.sub(r"<[^>]+>", " ", str(q.get("html", ""))) for q in review["questions"]
        )
        return GradeResult(
            state=str(review["attempt"]["state"]),
            grade=float(review["grade"]),
            max_grade=100.0,
            feedback=re.sub(r"\s+", " ", htmllib.unescape(feedback)).strip(),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MoodleBackend":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _answer_fields(html_text: str, answer: str | Sequence[str]) -> dict[str, str]:
    """Os campos do formulário de uma pergunta com a resposta dada: os escondidos (o `sequencecheck`
    do Moodle) mais a resposta (código numa caixa de texto, ou as opções marcadas)."""
    soup = BeautifulSoup(html_text, "html.parser")
    fields = {
        str(tag["name"]): str(tag.get("value", ""))
        for tag in soup.find_all("input", attrs={"type": "hidden"})
        if tag.get("name")
    }
    area = soup.find("textarea")
    if isinstance(area, Tag) and isinstance(answer, str):
        fields[str(area["name"])] = answer
        return fields
    wanted = {answer} if isinstance(answer, str) else set(answer)
    for tag in soup.find_all("input", attrs={"type": ["radio", "checkbox"]}):
        label = soup.find(id=f"{tag.get('id')}_label")
        text = re.sub(r"^[a-z]\.\s*", "", label.get_text(" ", strip=True)) if label else ""
        if text in wanted:
            fields[str(tag["name"])] = str(tag.get("value", "1")) if tag["type"] == "radio" else "1"
    return fields


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """Os parâmetros no formato do REST do Moodle: `data[0][name]=x`."""
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}[{key}]" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        flat[prefix] = str(value)
    return flat
