"""O notebook do lote: JSON válido e células de código que compilam (sem GPU não se executa)."""

import json
import re
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parents[2] / "deploy" / "gpu" / "ariadne_batch.ipynb"


def test_o_notebook_e_valido_e_o_codigo_compila() -> None:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert notebook["nbformat"] == 4
    code = ["".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"]
    assert len(code) >= 5
    for number, source in enumerate(code):
        stripped = re.sub(r"^%.*$", "pass", source, flags=re.MULTILINE)  # %pip é do IPython
        compile(stripped, f"celula-{number}", "exec")


def test_o_notebook_nao_conhece_o_tipo_de_tarefa_e_so_fala_com_o_gateway() -> None:
    text = NOTEBOOK.read_text(encoding="utf-8")
    assert "requests.post" not in text and "httpx" not in text
    assert "curate_page" not in text and "format_rescue" not in text
    assert "tasks.jsonl" in text and "results.jsonl" in text
    # A única coisa do Ariadne que alcança: as rotas do gateway, e só do manifesto (nada fixo).
    assert text.count("/gpu/v1/runs/") == 1  # a única rota montada no código
    assert "/api/" not in text and "https://" not in text.replace("https://colab", "")
