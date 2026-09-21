"""表（xlsx）と文書（docx）を、標準ライブラリだけで作る（zipfile＋XML。新しい依存は足さない）。

- 中身は文字と数字だけ。マクロ・外部リンク・画像は入れない。制御文字は取り除き、XML は必ずエスケープする。
- 最小の書式（見出し行を太字・列幅・段落）。Excel / Word / LibreOffice で開けることを目標にする（実機での開封は、別に確認する）。
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_ROWS, MAX_COLS, MAX_CELL = 200, 12, 500
MAX_PARAS, MAX_PARA = 60, 2000

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_FORMULA = re.compile(r"^[=+\-@\t\r]")


def clean(s, limit: int) -> str:
    return _CTRL.sub("", str(s if s is not None else "")).replace("\r\n", "\n")[:limit]


def _cell_text(v) -> str:
    t = clean(v, MAX_CELL)
    # 表計算の「式」として読まれないようにする（先頭が = + - @ の文字は、先に ' を付けて文字にする）
    return ("'" + t) if _FORMULA.match(t) and not re.fullmatch(r"-?\d+(\.\d+)?", t) else t


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def make_xlsx(title: str, columns: list[str], rows: list[list]) -> bytes:
    columns = [clean(c, 60) for c in columns[:MAX_COLS]]
    data = [[(_cell_text(v)) for v in r[:len(columns)]] for r in rows[:MAX_ROWS]]
    sst: list[str] = []
    idx: dict[str, int] = {}

    def s(t: str) -> int:
        if t not in idx:
            idx[t] = len(sst)
            sst.append(t)
        return idx[t]

    def cell(ci: int, ri: int, t: str, style: int = 0) -> str:
        ref = f"{_col(ci)}{ri}"
        if re.fullmatch(r"-?\d+(\.\d+)?", t) and len(t) < 15 and style == 0:
            return f'<c r="{ref}"><v>{t}</v></c>'
        return f'<c r="{ref}" t="s"' + (f' s="{style}"' if style else "") + f"><v>{s(t)}</v></c>"

    xml_rows = ['<row r="1">' + "".join(cell(i, 1, c, 1) for i, c in enumerate(columns)) + "</row>"]
    for n, r in enumerate(data, start=2):
        xml_rows.append(f'<row r="{n}">' + "".join(cell(i, n, v) for i, v in enumerate(r)) + "</row>")
    widths = [max([len(columns[i])] + [len(r[i]) for r in data if i < len(r)]) for i in range(len(columns))]
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{min(max(w * 1.8 + 2, 10), 60):.0f}" customWidth="1"/>' for i, w in enumerate(widths))
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f"<cols>{cols}</cols><sheetData>{''.join(xml_rows)}</sheetData></worksheet>")
    sst_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(sst)}" uniqueCount="{len(sst)}">'
               + "".join(f'<si><t xml:space="preserve">{escape(t)}</t></si>' for t in sst) + "</sst>")
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Meiryo UI"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Meiryo UI"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FF2F80FF"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    name = escape(clean(title, 31).replace("/", "／").replace("\\", "＼").replace("?", "？").replace("*", "＊").replace("[", "［").replace("]", "］").replace(":", "：") or "表")
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
              '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
              '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wbrels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/sharedStrings.xml", sst_xml)
        z.writestr("xl/styles.xml", styles)
    return b.getvalue()


def _para(text: str, *, bold: bool = False, size: int = 0) -> str:
    rpr = ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size else "") + '<w:rFonts w:eastAsia="Meiryo UI"/>'
    runs = []
    for i, line in enumerate(text.split("\n")):
        runs.append(f'<w:r><w:rPr>{rpr}</w:rPr>' + ("<w:br/>" if i else "") + f'<w:t xml:space="preserve">{escape(line)}</w:t></w:r>')
    return f"<w:p>{''.join(runs)}</w:p>"


def make_docx(title: str, sections: list[dict]) -> bytes:
    """sections: [{"heading": str, "paragraphs": [str, ...]}, ...]"""
    body = [_para(clean(title, 120), bold=True, size=36)]
    n = 0
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        h = clean(sec.get("heading", ""), 120)
        if h:
            body.append(_para(h, bold=True, size=28))
        for p in sec.get("paragraphs", []) if isinstance(sec.get("paragraphs"), list) else []:
            if n >= MAX_PARAS:
                break
            body.append(_para(clean(p, MAX_PARA)))
            n += 1
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
           "</w:body></w:document>")
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return b.getvalue()
