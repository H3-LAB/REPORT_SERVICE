"""PDF (A4, 1 ou 2 painéis por página, logo e título) e ZIP de PNGs."""

import io
import re
import unicodedata
import zipfile
from datetime import datetime

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

MARGIN = 36
HEADER = 48
FOOTER = 24
GAP = 16
CAPTION = 14
GREY = (0.45, 0.45, 0.45)


def ascii_name(text):
    """``Diário & PKI`` → ``Diario_PKI``: sem acentos nem símbolos, para nomes de ficheiro."""
    plain = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Za-z0-9]+', '_', plain).strip('_')


def file_stem(name, start):
    """Nome do ficheiro: ``NOME_DO_REPORT_2026-10-27``."""
    return (ascii_name(name).upper() or 'REPORT') + start.strftime('_%Y-%m-%d')


def build_pdf(title, period_text, panels, logo=None, per_page=2, orientation='portrait'):
    """``panels``: lista de ``(título, png ou None, erro, legenda)``. Um painel em falta fica como aviso na página."""
    size = landscape(A4) if orientation == 'landscape' else A4
    width, height = size
    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=size)
    pdf.setTitle(title)
    pages = [panels[i:i + per_page] for i in range(0, len(panels), per_page)] or [[]]
    generated = datetime.now().strftime('%d/%m/%Y %H:%M')
    logo_image = ImageReader(io.BytesIO(logo)) if logo else None

    for number, page in enumerate(pages, start=1):
        top = height - MARGIN
        # Cabeçalho: logo à esquerda, título e período à direita
        if logo_image:
            logo_w, logo_h = logo_image.getSize()
            scale = min(28 / logo_h, 180 / logo_w)
            pdf.drawImage(logo_image, MARGIN, top - 28, width=logo_w * scale, height=logo_h * scale, mask='auto')
        pdf.setFont('Helvetica-Bold', 13)
        pdf.drawRightString(width - MARGIN, top - 12, title)
        pdf.setFont('Helvetica', 9)
        pdf.setFillColorRGB(*GREY)
        pdf.drawRightString(width - MARGIN, top - 26, period_text)
        pdf.setStrokeColorRGB(0.85, 0.85, 0.85)
        pdf.line(MARGIN, top - HEADER + 8, width - MARGIN, top - HEADER + 8)
        pdf.setFillColorRGB(0, 0, 0)

        # Painéis: dividem a área útil em partes iguais, mantendo a proporção da imagem
        area_top = top - HEADER
        area_h = area_top - MARGIN - FOOTER
        slot_h = (area_h - GAP * (per_page - 1)) / per_page
        slot_w = width - 2 * MARGIN
        for index, (panel_title, png, error, caption) in enumerate(page):
            slot_top = area_top - index * (slot_h + GAP)
            if caption:
                pdf.setFont('Helvetica', 8)
                pdf.setFillColorRGB(*GREY)
                pdf.drawString(MARGIN, slot_top - 8, caption)
                pdf.setFillColorRGB(0, 0, 0)
                slot_top -= CAPTION
            available_h = slot_h - (CAPTION if caption else 0)
            if png:
                image = ImageReader(io.BytesIO(png))
                img_w, img_h = image.getSize()
                scale = min(slot_w / img_w, available_h / img_h)
                draw_w, draw_h = img_w * scale, img_h * scale
                pdf.drawImage(image, (width - draw_w) / 2, slot_top - draw_h, width=draw_w, height=draw_h)
            else:
                pdf.setStrokeColorRGB(0.86, 0.15, 0.15)
                pdf.rect(MARGIN, slot_top - available_h, slot_w, available_h)
                pdf.setFillColorRGB(0.86, 0.15, 0.15)
                pdf.setFont('Helvetica-Bold', 11)
                pdf.drawCentredString(width / 2, slot_top - available_h / 2 + 6, f'{panel_title}: painel indisponível')
                pdf.setFont('Helvetica', 9)
                pdf.drawCentredString(width / 2, slot_top - available_h / 2 - 10, (error or '')[:120])
                pdf.setFillColorRGB(0, 0, 0)

        pdf.setFont('Helvetica', 8)
        pdf.setFillColorRGB(*GREY)
        pdf.drawString(MARGIN, MARGIN - 8, f'Gerado em {generated}')
        pdf.drawRightString(width - MARGIN, MARGIN - 8, f'Página {number} de {len(pages)}')
        pdf.setFillColorRGB(0, 0, 0)
        pdf.showPage()
    pdf.save()
    return out.getvalue()


def build_zip(stem, panels):
    """PNGs numerados pela ordem do report; os painéis em falta ficam de fora."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for number, (panel_title, png, _, _) in enumerate(panels, start=1):
            if png:
                safe = ascii_name(panel_title) or f'painel_{number}'
                archive.writestr(f'{stem}/{number:02d}_{safe}.png', png)
    return out.getvalue()
