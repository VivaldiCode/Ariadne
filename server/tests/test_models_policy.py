"""Regra do projeto: nenhum modelo chinês (ARCHITECTURE.md, «Modelos de IA»)."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from ariadne.config import Settings
from ariadne.models_policy import (
    BLOCKED_FAMILIES,
    BLOCKED_PREFIXES,
    DEFAULT_BATCH_MODEL,
    ModelNotAllowed,
    assert_allowed_model,
    is_blocked,
)

CHINESES = [
    "Qwen/Qwen2.5-7B-Instruct-AWQ",
    "qwen/qwen3-8b",
    "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
    "BAAI/bge-m3",
    "THUDM/glm-4-9b-chat",
    "01-ai/Yi-1.5-9B-Chat",
    "internlm/internlm2_5-7b-chat",
    "openbmb/MiniCPM-V-2_6",
    "baichuan-inc/Baichuan2-7B-Chat",
    "someone/Qwen2.5-7B-Instruct-GPTQ-Int4",  # espelho de terceiros: apanha-se pelo nome
    "someone/bge-small-en-v1.5",
    "qwen2.5-7b",  # alias do servidor local
]
PERMITIDOS = [
    DEFAULT_BATCH_MODEL,
    "google/gemma-3-12b-it",
    "gemma-3-12b-it",
    "mistralai/Mistral-7B-Instruct-v0.3",
    "hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
    "microsoft/Phi-4",
    "intfloat/multilingual-e5-small",
    "",
]
NOTEBOOK = Path(__file__).resolve().parents[2] / "deploy" / "gpu" / "ariadne_batch.ipynb"


@pytest.mark.parametrize("name", CHINESES)
def test_recusa_os_modelos_chineses(name: str) -> None:
    assert is_blocked(name)
    with pytest.raises(ModelNotAllowed):
        assert_allowed_model(name)


@pytest.mark.parametrize("name", PERMITIDOS)
def test_aceita_os_outros(name: str) -> None:
    assert not is_blocked(name)
    assert assert_allowed_model(name) == name


@pytest.mark.parametrize("field", ["kaggle_model", "curator_model", "embedder_model"])
def test_o_arranque_recusa_um_modelo_chines(field: str) -> None:
    with pytest.raises(ValidationError, match="excluded from Ariadne"):
        Settings(database_url="postgresql://x/x", **{field: "Qwen/Qwen2.5-7B-Instruct-AWQ"})  # type: ignore[arg-type]


def test_o_modelo_por_omissao_do_lote_e_permitido() -> None:
    settings = Settings(database_url="postgresql://x/x")
    assert settings.kaggle_model == DEFAULT_BATCH_MODEL and not is_blocked(settings.kaggle_model)


def test_o_notebook_repete_a_mesma_lista() -> None:
    """A lista do notebook (que não importa o Ariadne) tem de ser igual à de `models_policy`."""
    source = "".join(
        line for cell in json.loads(NOTEBOOK.read_text("utf-8"))["cells"] for line in cell["source"]
    )

    def tuple_of(name: str) -> tuple[str, ...]:
        found = re.search(rf"^{name} = \((.*?)\)$", source, re.MULTILINE)
        assert found is not None, name
        return tuple(json.loads(f"[{found.group(1)}]"))

    assert tuple_of("BLOCKED_PREFIXES") == BLOCKED_PREFIXES
    assert tuple_of("BLOCKED_FAMILIES") == BLOCKED_FAMILIES
    assert f'DEFAULT_MODEL = "{DEFAULT_BATCH_MODEL}"' in source
