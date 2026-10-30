"""Comandos `ariadne courses`, `ariadne moodle`, `ariadne sandbox` (Fase 10)."""

import os
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer

from ariadne.config import Settings
from ariadne.courses import index as course_index
from ariadne.courses.backend import CourseBackendError
from ariadne.courses.moodle import MoodleBackend
from ariadne.courses.publish import (
    CoursePublishError,
    get_backend,
    import_fcc,
    publish_course,
)
from ariadne.courses.repo import CourseRepo, CourseRepoError, normalize_course
from ariadne.courses.verify import apply_verification, verify_course
from ariadne.licenses import PrivateSendBlocked, host_is_allowed
from ariadne.sandbox import JobeSandbox, Sandbox, SandboxError
from ariadne.services import Services
from ariadne.vault.config import effective_settings

courses_app = typer.Typer(help="Courses: canonical format (Git), index, Moodle and verification.")
moodle_app = typer.Typer(help="Moodle + CodeRunner: bootstrap and verification.")
sandbox_app = typer.Typer(help="Code sandbox (Jobe).")


def _services() -> Services:
    return Services.open(effective_settings())


def _sandbox(settings: Settings) -> Sandbox:
    if not settings.jobe_url:
        typer.echo("JOBE_URL is not set (in the compose: http://jobe-gateway).", err=True)
        raise typer.Exit(2)
    return JobeSandbox(
        settings.jobe_url, api_key=settings.jobe_api_key, timeout=settings.jobe_timeout_seconds
    )


# --- courses --------------------------------------------------------------------------------------


@courses_app.command("import-fcc")
def courses_import_fcc(
    source: Annotated[str, typer.Option(help="Id of the freeCodeCamp ZIM (see `sources sync`).")],
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help="Run the official solution of each exercise in Jobe and drop the tests it "
            "fails (needs JOBE_URL).",
        ),
    ] = False,
    chain: Annotated[
        bool,
        typer.Option(
            "--chain/--no-chain",
            help="Chain the lessons of each module as prerequisites (freeCodeCamp does not "
            "declare them: the curriculum order is used).",
        ),
    ] = True,
) -> None:
    """Extract the courses of a freeCodeCamp ZIM into the canonical repository (one commit per
    course and version; nothing if unchanged)."""
    services = _services()
    sandbox = _sandbox(services.settings) if verify else None
    try:
        with services.sessions() as session:
            services.registry.sync(session)
        result = import_fcc(services, source, sandbox=sandbox, chain_requires=chain)
    except (CoursePublishError, CourseRepoError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        if sandbox is not None:
            sandbox.close()
        services.close()
    for item in result.courses:
        counts = item.course.counts()
        state = (
            f"commit {item.saved.commit[:10]}"
            if item.saved.changed and item.saved.commit
            else "no changes"
        )
        typer.echo(
            f"{item.course.id} {item.course.version}: {counts['modules']} modules, "
            f"{counts['lessons']} lessons, {counts['exercises']} exercises, "
            f"{counts['tests']} tests ({counts['unmapped_tests']} unmapped) — {state}"
        )
        if item.verification:
            ok = sum(1 for v in item.verification if v.ok and not v.skipped)
            skipped = sum(1 for v in item.verification if v.skipped)
            bad = [v for v in item.verification if not v.ok]
            typer.echo(
                f"  Jobe verification: {ok} passing, {skipped} without a solution, "
                f"{len(bad)} failing"
            )
    for reason, count in result.report.unmapped.most_common():
        typer.echo(f"  unmapped ({count}): {reason}")
    for skipped_path in result.report.skipped:
        typer.echo(f"  skipped: {skipped_path}", err=True)


@courses_app.command("sync")
def courses_sync(
    force: Annotated[
        bool, typer.Option(help="Rebuild the index even for what did not change.")
    ] = False,
    enqueue: Annotated[
        bool, typer.Option("--enqueue/--no-enqueue", help="Request indexing of the new lessons.")
    ] = True,
) -> None:
    """Update the index (courses, lessons, exercises, documents and `requires`) from Git."""
    services = _services()
    try:
        report = course_index.sync_courses(services, force=force, enqueue=enqueue)
    finally:
        services.close()
    typer.echo(
        f"{report.courses} courses reindexed, {report.lessons} lessons ({report.changed_lessons} "
        f"changed), {report.exercises} exercises; requires +{report.requires_added} "
        f"-{report.requires_removed}; {report.held} held by curation; "
        f"{report.enqueued} indexing requests."
    )
    for error in report.errors:
        typer.echo(f"error: {error}", err=True)
    if report.errors:
        raise typer.Exit(1)


@courses_app.command("list")
def courses_list() -> None:
    """The courses in the canonical repository and their versions."""
    repo = CourseRepo(effective_settings().courses_repo)
    courses = repo.courses()
    for slug in courses:
        versions = repo.versions(slug)
        typer.echo(f"{slug}  versions: {', '.join(versions)}  (current: {versions[-1]})")
    if not courses:
        typer.echo(f"No courses in {repo.path}.")


@courses_app.command("show")
def courses_show(slug: str, version: Annotated[str | None, typer.Option()] = None) -> None:
    """One course: modules, lessons, exercises and the commits that touched it."""
    repo = CourseRepo(effective_settings().courses_repo)
    try:
        course = repo.load(slug, version)
    except CourseRepoError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"{course.title} {course.version} ({course.license.name}, {course.license.license_class})"
    )
    for module in course.modules:
        typer.echo(f"  {module.title}: {len(module.lessons)} lessons")
    for commit in repo.log(slug, course.version, limit=10):
        typer.echo(f"  {commit.sha[:10]} {commit.date[:10]} {commit.subject}")


@courses_app.command("verify")
def courses_verify(
    slug: str,
    version: Annotated[str | None, typer.Option()] = None,
    apply: Annotated[
        bool, typer.Option(help="Mark the failing tests as unmapped and commit.")
    ] = False,
) -> None:
    """Run the official solution of each exercise in Jobe and report what fails."""
    settings = effective_settings()
    repo = CourseRepo(settings.courses_repo)
    sandbox = _sandbox(settings)
    try:
        course = repo.load(slug, version)
        if course.license.license_class.value == "private" and not host_is_allowed(
            settings.jobe_url, settings.private_allowed_hosts
        ):
            typer.echo(
                "Private course: the exercise code does not go to a Jobe outside "
                "PRIVATE_ALLOWED_HOSTS.",
                err=True,
            )
            raise typer.Exit(1)
        results = verify_course(
            sandbox,
            course,
            progress=lambda r: typer.echo(
                f"{r.ref}: "
                + (r.error or f"{r.passed} passing, {len(r.failed)} failing")
                + (f" ({r.skipped})" if r.skipped else "")
                + (" — the starter code already passes all the tests" if r.vacuous else "")
            ),
        )
        if apply:
            fixed = normalize_course(apply_verification(course, results))
            saved = repo.save(fixed, f"Verifica {course.title} {course.version} no Jobe")
            typer.echo("Changes saved." if saved.changed else "Nothing to change.")
    except (CourseRepoError, SandboxError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        sandbox.close()
    bad = [r for r in results if not r.ok]
    typer.echo(f"{len(results) - len(bad)} exercises passing, {len(bad)} failing.")
    if bad and not apply:
        raise typer.Exit(1)


@courses_app.command("publish")
def courses_publish(slug: str, version: Annotated[str | None, typer.Option()] = None) -> None:
    """Publish the course to Moodle (idempotent; lessons held by curation are left out)."""
    services = _services()
    try:
        outcome = publish_course(services, slug, version)
    except (CoursePublishError, PrivateSendBlocked) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        services.close()
    typer.echo(
        f"{outcome.course_id} {outcome.version}: {outcome.platform_url} — "
        f"{outcome.created} created, {outcome.updated} updated, "
        f"{outcome.unchanged} unchanged, {len(outcome.held)} held."
    )
    for ref, reason in outcome.held.items():
        typer.echo(f"  held {ref}: {reason}")
    for warning in outcome.warnings:
        typer.echo(f"  warning: {warning}", err=True)


# --- moodle ---------------------------------------------------------------------------------------


def _env_values(env_file: Path | None) -> dict[str, str]:
    from ariadne.cli import _read_env_file

    return _read_env_file(env_file) if env_file is not None and env_file.exists() else {}


def request_token(base: str, user: str, password: str, wait: float) -> tuple[str, str]:
    """Ask Moodle for the `ariadne` service token (`login/token.php`), waiting for the install
    to finish. Returns `(token, "")` or `("", the last reason)` if time runs out."""
    deadline = time.monotonic() + wait
    last = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.post(
                f"{base}/login/token.php",
                data={"username": user, "password": password, "service": "ariadne"},
                timeout=30,
            )
            body = response.json() if response.status_code == 200 else {}
            if isinstance(body, dict) and body.get("token"):
                return str(body["token"]), ""
            last = str(body.get("error") or response.status_code) if isinstance(body, dict) else "?"
        except (httpx.HTTPError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(5)
    return "", last


@moodle_app.command("bootstrap")
def moodle_bootstrap(
    url: Annotated[
        str | None,
        typer.Option(help="Moodle URL (default: MOODLE_URL or http://localhost:8805)."),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option(
            help="The .env: MOODLE_WS_PASSWORD (and MOODLE_PORT) are read from it and "
            "MOODLE_TOKEN is written to it."
        ),
    ] = None,
    wait: Annotated[float, typer.Option(help="Seconds to wait for Moodle.")] = 900.0,
) -> None:
    """Connect Ariadne to Moodle: wait for the install to finish (the container installs Moodle,
    enables the REST web services, creates the service and its user and points CodeRunner at
    Jobe), request the service token, write it to MOODLE_TOKEN and check the plugin and
    CodeRunner."""
    from_file = _env_values(env_file)

    def setting(name: str) -> str:
        return os.environ.get(name) or from_file.get(name, "")

    base = (
        url or setting("MOODLE_URL") or f"http://localhost:{setting('MOODLE_PORT') or 8805}"
    ).rstrip("/")
    user = setting("MOODLE_WS_USER") or "ariadnews"
    password = setting("MOODLE_WS_PASSWORD")
    if not password:
        typer.echo("MOODLE_WS_PASSWORD is missing (from the environment or --env-file).", err=True)
        raise typer.Exit(2)
    token, last = request_token(base, user, password, wait)
    if not token:
        typer.echo(
            f"Moodle at {base} did not give a token ({last}). Is it still installing? "
            "Check: docker compose logs moodle",
            err=True,
        )
        raise typer.Exit(1)
    backend = MoodleBackend(base, token, timeout=60)
    try:
        info = backend.check()
    except CourseBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        backend.close()
    if env_file is not None:
        from ariadne.cli import _set_env_value

        _set_env_value(env_file, "MOODLE_TOKEN", token)
        typer.echo(f"Service token written to MOODLE_TOKEN ({env_file}).")
        typer.echo("Restart the server and the worker to use it: docker compose up -d")
    else:
        typer.echo("Service token (save it as MOODLE_TOKEN in .env):")
        typer.echo(token)
    typer.echo(
        f"Moodle {info.get('moodle')}, plugin {info.get('plugin')}, CodeRunner "
        f"{info.get('coderunner')}, Jobe at {info.get('jobe_host')}."
    )


@moodle_app.command("check")
def moodle_check() -> None:
    """Check that Moodle responds with the configured token and that the plugin is there."""
    backend = get_backend(effective_settings())
    try:
        info = backend.check()
    except CourseBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        backend.close()
    for key, value in info.items():
        typer.echo(f"{key}: {value}")


# --- sandbox --------------------------------------------------------------------------------------


@sandbox_app.command("languages")
def sandbox_languages() -> None:
    """The languages Jobe runs."""
    sandbox = _sandbox(effective_settings())
    try:
        for language, version in sandbox.languages().items():
            typer.echo(f"{language} {version}")
    except SandboxError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        sandbox.close()


@sandbox_app.command("run")
def sandbox_run(
    language: str,
    file: Path,
    stdin: Annotated[str, typer.Option(help="What is fed to the program on standard input.")] = "",
) -> None:
    """Run a file in Jobe (isolated, no network, with limits)."""
    sandbox = _sandbox(effective_settings())
    try:
        result = sandbox.run(language, file.read_text(encoding="utf-8"), stdin=stdin)
    except SandboxError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        sandbox.close()
    typer.echo(result.stdout, nl=False)
    if result.stderr or result.compile_output:
        typer.echo(result.compile_output + result.stderr, err=True, nl=False)
    typer.echo(f"[{result.outcome.label}]", err=True)
    if not result.ok:
        raise typer.Exit(1)
