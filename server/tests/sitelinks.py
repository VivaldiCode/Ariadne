"""Dumps SQL minúsculos no formato real da Wikimedia, para os testes das Fases 7."""

import gzip
from pathlib import Path

ITEMS_HEADER = """-- MySQL dump 10.19  Distrib 10.3.38-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: db1167    Database: wikidatawiki
-- ------------------------------------------------------
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET NAMES binary */;

--
-- Table structure for table `wb_items_per_site`
--

DROP TABLE IF EXISTS `wb_items_per_site`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `wb_items_per_site` (
  `ips_row_id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `ips_item_id` int(10) unsigned NOT NULL,
  `ips_site_id` varbinary(32) NOT NULL,
  `ips_site_page` varbinary(310) NOT NULL,
  PRIMARY KEY (`ips_row_id`),
  UNIQUE KEY `wb_ips_item_site_page` (`ips_site_id`,`ips_site_page`),
  KEY `wb_ips_site_page` (`ips_site_page`,`ips_site_id`)
) ENGINE=InnoDB AUTO_INCREMENT=140 DEFAULT CHARSET=binary ROW_FORMAT=COMPRESSED;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `wb_items_per_site`
--

LOCK TABLES `wb_items_per_site` WRITE;
/*!40000 ALTER TABLE `wb_items_per_site` DISABLE KEYS */;
"""
ITEMS_FOOTER = """/*!40000 ALTER TABLE `wb_items_per_site` ENABLE KEYS */;
UNLOCK TABLES;
-- Dump completed on 2026-10-01 03:12:11
"""

# Q-ids de teste. Os títulos têm as dificuldades dos dumps reais: aspas simples escapadas,
# barras, vírgulas e parênteses dentro das strings, acentos e símbolos.
Q_CRAVOS = 1_187_566
Q_DIFICIL = 4_242

PT_CRAVOS = "Revolução dos Cravos"
EN_CRAVOS = "Carnation Revolution"
# Um título com tudo o que parte um parser ingénuo: ', \, (, ), vírgula, ponto e vírgula.
TRICKY_PT = "O'Neil \\ (teste), parte; 2"
TRICKY_EN = "Tricky title"


def items_rows() -> list[tuple[int, int, str, str]]:
    return [
        (1, 26, "afwiki", "Test"),
        (2, Q_CRAVOS, "ptwiki", PT_CRAVOS),
        (3, Q_CRAVOS, "enwiki", EN_CRAVOS),
        (4, Q_CRAVOS, "ptwikibooks", PT_CRAVOS),
        (5, Q_CRAVOS, "frwiki", "Révolution des œillets"),
        (6, Q_DIFICIL, "ptwiki", TRICKY_PT),
        (7, Q_DIFICIL, "enwiki", TRICKY_EN),
        (8, 77, "ptwiki", "Lisboa"),
        (9, 77, "enwiki", "Lisbon"),
        (10, 78, "dewiki", "Lissabon"),
    ]


def sql_string(value: str) -> str:
    """Como o mysqldump escreve uma string: aspas e barras escapadas."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"


def items_dump(rows: list[tuple[int, int, str, str]] | None = None, per_insert: int = 4) -> str:
    """O dump completo, com vários `INSERT` (cada um com várias tuplas na mesma linha)."""
    rows = items_rows() if rows is None else rows
    lines = []
    for start in range(0, len(rows), per_insert):
        batch = rows[start : start + per_insert]
        values = ",".join(f"({r},{i},{sql_string(s)},{sql_string(t)})" for r, i, s, t in batch)
        lines.append(f"INSERT INTO `wb_items_per_site` VALUES {values};\n")
    return ITEMS_HEADER + "".join(lines) + ITEMS_FOOTER


def write_items_dump(path: Path, text: str | None = None) -> Path:
    path.write_bytes(gzip.compress((text or items_dump()).encode("utf-8")))
    return path


# --- dumps `page` e `langlinks` de uma Wikipédia (ptwiki) -------------------------------------

PAGE_HEADER = """-- MySQL dump 10.19  Distrib 10.3.38-MariaDB, for debian-linux-gnu (x86_64)
DROP TABLE IF EXISTS `page`;
CREATE TABLE `page` (
  `page_id` int(8) unsigned NOT NULL AUTO_INCREMENT,
  `page_namespace` int(11) NOT NULL DEFAULT 0,
  `page_title` varbinary(255) NOT NULL DEFAULT '',
  `page_is_redirect` tinyint(1) unsigned NOT NULL DEFAULT 0,
  `page_is_new` tinyint(1) unsigned NOT NULL DEFAULT 0,
  `page_random` double unsigned NOT NULL DEFAULT 0,
  `page_touched` binary(14) NOT NULL,
  `page_links_updated` varbinary(14) DEFAULT NULL,
  `page_latest` int(8) unsigned NOT NULL DEFAULT 0,
  `page_len` int(8) unsigned NOT NULL DEFAULT 0,
  `page_content_model` varbinary(32) DEFAULT NULL,
  `page_lang` varbinary(35) DEFAULT NULL,
  PRIMARY KEY (`page_id`)
) ENGINE=InnoDB DEFAULT CHARSET=binary;
"""
LANGLINKS_HEADER = """-- MySQL dump 10.19
DROP TABLE IF EXISTS `langlinks`;
CREATE TABLE `langlinks` (
  `ll_from` int(8) unsigned NOT NULL DEFAULT 0,
  `ll_lang` varbinary(35) NOT NULL DEFAULT '',
  `ll_title` varbinary(255) NOT NULL DEFAULT '',
  PRIMARY KEY (`ll_from`,`ll_lang`)
) ENGINE=InnoDB DEFAULT CHARSET=binary;
"""


def page_dump(pages: list[tuple[int, int, str, int]]) -> str:
    """(id, espaço de nomes, título com `_`, é redirect)."""
    values = ",".join(
        f"({i},{ns},{sql_string(t)},{redirect},0,0.123456789,'20261001000000','20261001000000',"
        f"{i * 10},1234,'wikitext',NULL)"
        for i, ns, t, redirect in pages
    )
    return PAGE_HEADER + f"INSERT INTO `page` VALUES {values};\n"


def langlinks_dump(links: list[tuple[int, str, str]]) -> str:
    values = ",".join(f"({i},{sql_string(lang)},{sql_string(title)})" for i, lang, title in links)
    return LANGLINKS_HEADER + f"INSERT INTO `langlinks` VALUES {values};\n"
