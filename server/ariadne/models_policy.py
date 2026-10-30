"""Regra do projeto (ARCHITECTURE.md, «Modelos de IA»): nenhum modelo chinês no Ariadne.

Vale para o curador, o lote em GPU externa, o embedder e o que mais vier. A lista é de prefixos de
repositório (`Qwen/…`) e de famílias pelo nome (`bge-…`, `glm-…`), sem distinguir maiúsculas.
Recusa-se no arranque (`Settings`), ao criar uma corrida de lote e no notebook (que repete a lista).
"""

# Organizações: o prefixo do repositório no Hugging Face.
BLOCKED_PREFIXES = (
    "qwen/",
    "deepseek-ai/",
    "baai/",
    "thudm/",
    "01-ai/",
    "internlm/",
    "openbmb/",
    "baichuan-inc/",
)
# Famílias: o nome do modelo (depois da última «/»), para os espelhos e quantizações de terceiros.
BLOCKED_FAMILIES = ("qwen", "deepseek", "bge-", "glm-", "yi-", "internlm", "minicpm", "baichuan")

# O modelo do lote em GPU gratuita (T4 de 16 GB, sem bf16): Mistral 7B Instruct v0.3 em AWQ de 4
# bits, Apache-2.0, sem acesso restrito, fp16 sem transbordos. Escolhido pela fidelidade
# medida contra o Llama 3.1 8B; o Gemma 3 não corre na T4 (ver ARCHITECTURE.md).
DEFAULT_BATCH_MODEL = "solidrust/Mistral-7B-Instruct-v0.3-AWQ"


class ModelNotAllowed(ValueError):
    """O modelo é de uma família excluída do projeto."""


def is_blocked(name: str) -> bool:
    lowered = name.strip().lower()
    base = lowered.rsplit("/", 1)[-1]
    return lowered.startswith(BLOCKED_PREFIXES) or base.startswith(BLOCKED_FAMILIES)


def assert_allowed_model(name: str, what: str = "model") -> str:
    """O nome, se for permitido; senão `ModelNotAllowed`. Vazio passa («sem modelo»)."""
    if name and is_blocked(name):
        raise ModelNotAllowed(
            f"The {what} {name!r} is from a family excluded from Ariadne (Chinese models: Qwen, "
            "DeepSeek, bge/BAAI, GLM, Yi, InternLM, MiniCPM, Baichuan)."
        )
    return name
