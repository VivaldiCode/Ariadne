from ariadne.chunking import chunk_document, split_text, strip_title


def words(n: int, tag: str = "w") -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def test_texto_curto_e_um_so_chunk_com_o_titulo() -> None:
    assert chunk_document("Lisboa", "Lisboa é a capital.\n\nFica no Tejo.", 180) == [
        "Lisboa — Lisboa é a capital.\n\nFica no Tejo."
    ]


def test_texto_vazio_nao_da_chunks() -> None:
    assert chunk_document("Lisboa", "  \n\n \n", 180) == []


def test_junta_paragrafos_inteiros_ate_ao_tamanho() -> None:
    text = "\n\n".join(words(50, p) for p in "abcd")  # 4 parágrafos de 50 palavras
    chunks = split_text(text, 120)
    assert [c.count("\n\n") for c in chunks] == [1, 1]  # 2 + 2, nunca um parágrafo a meio
    assert all(len(c.split()) <= 120 for c in chunks)


def test_paragrafo_grande_parte_se_por_frases() -> None:
    sentences = [f"Frase {i} " + words(18) + "." for i in range(30)]  # ≈ 600 palavras
    chunks = split_text(" ".join(sentences), 100)
    assert len(chunks) > 5
    assert all(len(c.split()) <= 100 for c in chunks)
    assert all(c.rstrip().endswith(".") for c in chunks)  # cortes em fim de frase
    assert " ".join(chunks).split() == " ".join(sentences).split()  # nada se perde


def test_frase_gigante_parte_se_por_palavras() -> None:
    chunks = split_text(words(450), 100)
    assert [len(c.split()) for c in chunks] == [100, 100, 100, 100, 50]


def test_titulo_de_seccao_nao_fica_sozinho_no_fim() -> None:
    text = f"{words(60, 'a')}\n\n## História\n\n{words(60, 'b')}"
    chunks = split_text(text, 80)
    assert len(chunks) == 2
    assert chunks[0] == words(60, "a")
    assert chunks[1].startswith("## História\n\n")


def test_cauda_minuscula_cola_se_ao_chunk_anterior() -> None:
    text = f"{words(100, 'a')}\n\n{words(5, 'b')}"
    chunks = split_text(text, 100)
    assert len(chunks) == 1
    assert chunks[0].endswith("b4")


def test_strip_title_e_o_inverso() -> None:
    (chunk,) = chunk_document("Ray Charles", "Foi um músico.", 180)
    assert strip_title(chunk, "Ray Charles") == "Foi um músico."
    assert strip_title("Outro texto", "Ray Charles") == "Outro texto"


def test_determinista() -> None:
    text = "\n\n".join(words(70, p) for p in "abcdefg")
    assert split_text(text, 100) == split_text(text, 100)


def test_texto_sem_espacos_parte_se_por_caracteres() -> None:
    sentence = "这是一个很长的句子，" * 5 + "。"
    text = "".join(f"第{n}句话的内容比较多。" + sentence for n in range(40))  # ≈ 2300 caracteres
    chunks = split_text(text, 100)
    assert len(chunks) > 15
    assert all(len(c) <= 130 for c in chunks)  # ≈ 100 unidades; nunca um bloco de milhares
    assert "".join(chunks).replace(" ", "") == text  # nada se perde

    one_block = "字" * 1000  # sem pontuação nem espaços
    pieces = split_text(one_block, 100)
    assert [len(p) for p in pieces] == [100] * 10


def test_sequencia_gigante_sem_espacos_nao_e_uma_so_palavra() -> None:
    chunks = split_text("a" * 5000, 100)  # URL ou texto colado
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == "a" * 5000
