"""Converte o multilingual-e5-small para GGUF com o conversor oficial do llama.cpp.

O e5-small tem arquitetura BERT mas tokenizador XLM-R (SentencePiece unigram). O conversor
oficial trata o `BertModel` como WordPiece e estraga o vocabulário; aqui usa-se o caminho
XLM-R (`_xlmroberta_set_vocab`, tokenizador UGM) que o próprio conversor já tem, sem o
desvio de posições do XLMRobertaModel (o e5-small usa posições BERT a partir de 0).

Uso: python convert_e5.py <llama.cpp> <modelo HF> --outtype f32|q8_0 --outfile X.gguf
"""

import runpy
import sys
from pathlib import Path

llama_dir = Path(sys.argv.pop(1)).resolve()
sys.path[:0] = [str(llama_dir / "gguf-py"), str(llama_dir)]

from conversion.bert import BertModel  # noqa: E402  (precisa do sys.path acima)


def set_vocab(self: BertModel) -> None:
    self.vocab_size = self.hparams.get("vocab_size")
    self._xlmroberta_set_vocab()


BertModel.set_vocab = set_vocab  # type: ignore[method-assign]
sys.argv[0] = str(llama_dir / "convert_hf_to_gguf.py")
runpy.run_path(sys.argv[0], run_name="__main__")
