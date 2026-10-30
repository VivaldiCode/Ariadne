"""Repositório Git do formato canónico (STRUCTURE.md, secção 6.2).

Disposição, dentro do repositório (`COURSES_REPO`):

    <curso>/<versão>/course.yaml
    <curso>/<versão>/<NNN>-<módulo>/module.yaml
    <curso>/<versão>/<NNN>-<módulo>/<NNN>-<lição>.yaml

Cada versão do curso é uma pasta: uma versão nova da fonte cria uma pasta nova e nunca apaga a
anterior. Cada alteração é um commit (feito com o `git` do sistema, sem depender da configuração do
utilizador). Guardar um curso igual ao que já está não faz commit. O Markdown e o código dos blocos
ficam em blocos literais YAML (`|`), legíveis e com diferenças limpas no Git.
"""

import contextlib
import fcntl
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ariadne.courses.schema import SLUG, Course, Lesson, Module

AUTHOR_NAME = "Ariadne"
AUTHOR_EMAIL = "ariadne@localhost"
GIT_TIMEOUT = 120
LOCK_FILE = ".ariadne.lock"
STALE_INDEX_LOCK_SECONDS = 10.0
_GITIGNORE = f".DS_Store\n{LOCK_FILE}\n"
_README = (
    "# Cursos canónicos do Ariadne\n\n"
    "Formato canónico (STRUCTURE.md, secção 6.2): uma pasta por curso e por versão. "
    "Gerido pelo Ariadne; cada alteração é um commit.\n"
)


class CourseRepoError(Exception):
    """Falha ao ler ou escrever o repositório de cursos."""


class CourseNotFound(CourseRepoError):
    pass


@dataclass(frozen=True)
class SaveResult:
    commit: str | None  # `None` se nada mudou (não há commit)
    changed: bool
    files_written: int
    files_removed: int


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    date: str
    subject: str


# --- YAML -----------------------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Texto com várias linhas em blocos literais (`|`): lê-se bem no ficheiro."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _represent_str)


def dump_yaml(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        width=100,
        default_flow_style=False,
    )


def load_yaml(text: str, where: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CourseRepoError(f"Invalid YAML in {where}: {exc}") from exc


def _normalize(text: str) -> str:
    """O código e o Markdown guardam-se com `\\n` e sem espaços no fim das linhas: o PyYAML não
    escreve blocos literais com espaços no fim e cai em aspas duplas, ilegíveis."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip(" \t") for line in text.split("\n"))


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return _normalize(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def normalize_course(course: Course) -> Course:
    """O curso com todo o texto normalizado (o que se escreve é o que se lê de volta)."""
    return Course.model_validate(_clean(course.model_dump(mode="json")))


# --- versões --------------------------------------------------------------------------------------


def version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    """Ordem natural das versões: '2026-08' < '2026-10', 'v2' < 'v10'."""
    parts = re.split(r"(\d+)", version)
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p)


# --- o repositório --------------------------------------------------------------------------------


class CourseRepo:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._local = threading.local()  # o bloqueio é reentrante dentro do mesmo fio

    # --- git ---

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [
            "git",
            "-c", "safe.directory=*",
            "-c", f"user.name={AUTHOR_NAME}",
            "-c", f"user.email={AUTHOR_EMAIL}",
            "-c", "commit.gpgsign=false",
            "-c", "core.quotepath=off",
            *args,
        ]  # fmt: skip
        try:
            result = subprocess.run(
                command,
                cwd=self.path,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CourseRepoError(f"git {args[0]} failed: {exc}") from exc
        if check and result.returncode != 0:
            raise CourseRepoError(f"git {' '.join(args)} failed: {result.stderr.strip()[:500]}")
        return result

    def init(self) -> None:
        """Cria o repositório se ainda não existe (idempotente; sob o bloqueio de escrita)."""
        with self._locked():
            pass

    def _init_unlocked(self) -> None:
        if (self.path / ".git").exists():
            return
        self._git("init", "--quiet", "--initial-branch=main")
        (self.path / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
        (self.path / "README.md").write_text(_README, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "--quiet", "-m", "Inicia o repositório de cursos")

    def _drop_stale_index_lock(self) -> None:
        """Um `index.lock` que sobrou de um git que morreu a meio bloqueava todos os commits. Quem
        escreve tem o bloqueio exclusivo, por isso nenhum git nosso o está a usar: um que já tem
        alguns segundos é órfão e tira-se."""
        lock = self.path / ".git" / "index.lock"
        try:
            if lock.is_file() and time.time() - lock.stat().st_mtime > STALE_INDEX_LOCK_SECONDS:
                lock.unlink()
        except OSError:
            pass

    @contextlib.contextmanager
    def _locked(self, *, shared: bool = False) -> Iterator[None]:
        """Escritores (server e worker partilham o repositório) um de cada vez; leitores juntos,
        mas nunca a meio de uma escrita. Reentrante dentro do mesmo fio."""
        depth: int = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        handle = None
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            handle = open(self.path / LOCK_FILE, "a")  # noqa: SIM115 - fecha-se no finally
        except OSError:
            if not shared:
                raise  # uma pasta só de leitura lê-se sem bloqueio, mas não se escreve nela
        try:
            if handle is not None:
                fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            self._local.depth = 1
            if not shared:
                self._init_unlocked()
                self._drop_stale_index_lock()
            yield
        finally:
            self._local.depth = 0
            if handle is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()

    def head(self) -> str | None:
        """O commit atual, sem lançar processos (é chamado a cada pedido de pesquisa)."""
        git = self.path / ".git"
        try:
            ref = (git / "HEAD").read_text().strip()
            if not ref.startswith("ref: "):
                return ref
            name = ref[5:]
            loose = git / name
            if loose.is_file():
                return loose.read_text().strip()
            packed = git / "packed-refs"
            if packed.is_file():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + name):
                        return line.split(" ", 1)[0]
        except OSError:
            return None
        return None

    def _log(self, slug: str, version: str | None, limit: int) -> list[CommitInfo]:
        target = f"{slug}/{version}" if version else slug
        if self.head() is None or not (self.path / target).exists():
            return []
        out = self._git(
            "log", f"-{limit}", "--format=%H%x1f%cI%x1f%s", "--", target, check=False
        ).stdout
        commits: list[CommitInfo] = []
        for line in out.splitlines():
            sha, date, subject = line.split("\x1f", 2)
            commits.append(CommitInfo(sha, date, subject))
        return commits

    def _last_commit(self, slug: str, version: str) -> str | None:
        if self.head() is None:
            return None
        out = self._git("log", "-1", "--format=%H", "--", f"{slug}/{version}", check=False).stdout
        return out.strip() or None

    # --- leitura (sob o bloqueio partilhado: nunca a meio de uma escrita) ---

    @staticmethod
    def _check(slug: str, version: str | None = None) -> None:
        """Os ids entram em caminhos: só o padrão dos ids de curso e de versão passa."""
        for what, value in (("course", slug), ("version", version)):
            if value is not None and not SLUG.match(value):
                raise CourseRepoError(f"Invalid {what} id: {value!r}.")

    def log(self, slug: str, version: str | None = None, limit: int = 50) -> list[CommitInfo]:
        """Os commits que tocaram no curso (ou numa versão), do mais recente ao mais antigo."""
        self._check(slug, version)
        with self._locked(shared=True):
            return self._log(slug, version, limit)

    def last_commit(self, slug: str, version: str) -> str | None:
        """O último commit que tocou nesta versão do curso (`None` se ainda não há)."""
        self._check(slug, version)
        with self._locked(shared=True):
            return self._last_commit(slug, version)

    def courses(self) -> list[str]:
        with self._locked(shared=True):
            return self._courses()

    def versions(self, slug: str) -> list[str]:
        self._check(slug)
        with self._locked(shared=True):
            return self._versions(slug)

    def load(self, slug: str, version: str | None = None) -> Course:
        self._check(slug, version)
        with self._locked(shared=True):
            return self._load(slug, version)

    # --- listar ---

    def _courses(self) -> list[str]:
        if not self.path.is_dir():
            return []
        return sorted(
            p.name
            for p in self.path.iterdir()
            if p.is_dir() and SLUG.match(p.name) and any(self._versions(p.name))
        )

    def _versions(self, slug: str) -> list[str]:
        base = self.path / slug
        if not base.is_dir():
            return []
        found = [p.name for p in base.iterdir() if (p / "course.yaml").is_file()]
        return sorted(found, key=version_key)

    def current_version(self, slug: str) -> str:
        versions = self.versions(slug)
        if not versions:
            raise CourseNotFound(f"Course {slug!r} does not exist in the repository.")
        return versions[-1]

    # --- ler ---

    def _load(self, slug: str, version: str | None) -> Course:
        version = version or self.current_version(slug)
        base = self.path / slug / version
        header = base / "course.yaml"
        if not header.is_file():
            raise CourseNotFound(f"Course {slug!r} version {version!r} does not exist.")
        data = load_yaml(header.read_text(encoding="utf-8"), str(header))
        if not isinstance(data, dict):
            raise CourseRepoError(f"{header}: a mapping was expected")
        modules: list[dict[str, Any]] = []
        for module_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            meta_file = module_dir / "module.yaml"
            if not meta_file.is_file():
                continue
            module = load_yaml(meta_file.read_text(encoding="utf-8"), str(meta_file))
            lessons = []
            for file in sorted(module_dir.glob("*.yaml")):
                if file.name == "module.yaml":
                    continue
                lessons.append(load_yaml(file.read_text(encoding="utf-8"), str(file)))
            module["lessons"] = lessons
            modules.append(module)
        data["modules"] = modules
        try:
            return Course.model_validate(data)
        except ValidationError as exc:
            raise CourseRepoError(f"Invalid course {slug}/{version}: {exc}") from exc

    # --- escrever ---

    @staticmethod
    def render(course: Course) -> dict[str, str]:
        """Os ficheiros do curso: caminho relativo à pasta da versão -> texto."""
        course = normalize_course(course)
        header = course.model_dump(mode="json", exclude={"modules"}, exclude_none=True)
        header["modules"] = [module.id for module in course.modules]
        files = {"course.yaml": dump_yaml(header)}
        for m_index, module in enumerate(course.modules, start=1):
            folder = f"{m_index:03d}-{module.id}"
            meta = module.model_dump(mode="json", exclude={"lessons"}, exclude_none=True)
            files[f"{folder}/module.yaml"] = dump_yaml(meta)
            for l_index, lesson in enumerate(module.lessons, start=1):
                data = lesson.model_dump(mode="json", exclude_none=True)
                files[f"{folder}/{l_index:03d}-{lesson.id}.yaml"] = dump_yaml(data)
        return files

    def save(self, course: Course, message: str | None = None) -> SaveResult:
        """Escreve a versão do curso e faz um commit (nenhum se nada mudou). Os ficheiros que a
        versão já não tem apagam-se; as outras versões nunca se tocam."""
        files = self.render(course)
        target = Path(course.id) / course.version
        subject = message or (
            f"Guarda {course.title} {course.version}: {course.counts()['lessons']} lições"
        )
        with self._locked():
            return self._write_tree(target, files, subject)

    def update_lesson(
        self, slug: str, version: str, module_id: str, lesson: Lesson, message: str | None = None
    ) -> SaveResult:
        """Substitui (ou acrescenta) uma lição de um módulo e faz um commit. Ler e escrever
        fazem-se sob o mesmo bloqueio: ninguém escreve entre uma coisa e a outra."""
        self._check(slug, version)
        with self._locked():
            return self._update_lesson(slug, version, module_id, lesson, message)

    def _update_lesson(
        self, slug: str, version: str, module_id: str, lesson: Lesson, message: str | None
    ) -> SaveResult:
        course = self._load(slug, version)
        module = next((m for m in course.modules if m.id == module_id), None)
        if module is None:
            raise CourseNotFound(f"Module {module_id!r} does not exist in {slug}/{version}.")
        lessons = [lesson if existing.id == lesson.id else existing for existing in module.lessons]
        if lesson.id not in {existing.id for existing in module.lessons}:
            lessons.append(lesson)
        updated = course.model_copy(
            update={
                "modules": [
                    Module(id=m.id, title=m.title, summary=m.summary, lessons=lessons)
                    if m.id == module_id
                    else m
                    for m in course.modules
                ]
            }
        )
        updated = Course.model_validate(updated.model_dump(mode="json"))
        return self.save(updated, message or f"Altera a lição {module_id}/{lesson.id}")

    def _write_tree(self, target: Path, files: dict[str, str], subject: str) -> SaveResult:
        base = self.path / target
        existing = (
            {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}
            if base.is_dir()
            else set()
        )
        written = 0
        for relative, text in files.items():
            file = base / relative
            if file.is_file() and file.read_text(encoding="utf-8") == text:
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text, encoding="utf-8")
            written += 1
        stale = sorted(existing - set(files))
        for relative in stale:
            (base / relative).unlink()
        for folder in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                shutil.rmtree(folder)
        self._git("add", "-A", "--", str(target))
        if self._git("diff", "--cached", "--quiet", "--", str(target), check=False).returncode == 0:
            return SaveResult(None, False, written, len(stale))
        self._git("commit", "--quiet", "-m", subject, "--", str(target))
        return SaveResult(self.head(), True, written, len(stale))


__all__ = [
    "CommitInfo",
    "CourseNotFound",
    "CourseRepo",
    "CourseRepoError",
    "SaveResult",
    "dump_yaml",
    "load_yaml",
    "normalize_course",
    "version_key",
]
