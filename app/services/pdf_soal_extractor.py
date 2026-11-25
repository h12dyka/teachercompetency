import os
import re
import json
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path
from pypdf import PdfReader
from flask import current_app

logger = logging.getLogger(__name__)

ROMAN_TO_CLASS = {
    'I': 'Kelas 1', 'II': 'Kelas 2', 'III': 'Kelas 3',
    'IV': 'Kelas 4', 'V': 'Kelas 5', 'VI': 'Kelas 6',
    'VII': 'Kelas 7', 'VIII': 'Kelas 8', 'IX': 'Kelas 9',
    'X': 'Kelas 10', 'XI': 'Kelas 11', 'XII': 'Kelas 12',
    'X - XII': 'Kelas 10-12', 'X-XII': 'Kelas 10-12',
    '10': 'Kelas 10', '11': 'Kelas 11', '12': 'Kelas 12',
    '7': 'Kelas 7', '8': 'Kelas 8', '9': 'Kelas 9',
    '1': 'Kelas 1', '2': 'Kelas 2', '3': 'Kelas 3',
    '4': 'Kelas 4', '5': 'Kelas 5', '6': 'Kelas 6',
    'A': 'TK A', 'B': 'TK B', 'A & B': 'TK A & B'
}

def clean_extracted_text(txt: str) -> str:
    """Menyambung kembali teks singkatan yang patah baris akibat font tabel PDF."""
    t = re.sub(r'S\s*[\r\n]+\s*MP', 'SMP', txt, flags=re.IGNORECASE)
    t = re.sub(r'S\s*[\r\n]+\s*MA', 'SMA', t, flags=re.IGNORECASE)
    t = re.sub(r'S\s*[\r\n]+\s*D', 'SD', t, flags=re.IGNORECASE)
    t = re.sub(r'VI\s*[\r\n]+\s*II', 'VIII', t, flags=re.IGNORECASE)
    t = re.sub(r'VI\s*[\r\n]+\s*I', 'VII', t, flags=re.IGNORECASE)
    return t

def infer_grade_level(class_str: str, text: str = "", filename: str = "") -> str:
    """Memastikan jenjang pendidikan (TK/SD/SMP/SMA) selalu sinkron dan valid 100%."""
    c_upper = (class_str or '').upper()
    if any(k in c_upper for k in ['KELAS 7', 'KELAS 8', 'KELAS 9', 'VII', 'VIII', 'IX', 'SMP']):
        return 'SMP'
    if any(k in c_upper for k in ['KELAS 10', 'KELAS 11', 'KELAS 12', 'X', 'XI', 'XII', 'SMA', 'SMK']):
        return 'SMA'
    if 'TK' in c_upper:
        return 'TK'
    if any(k in c_upper for k in ['KELAS 1', 'KELAS 2', 'KELAS 3', 'KELAS 4', 'KELAS 5', 'KELAS 6']) or re.search(r'\b[1-6]\b', c_upper):
        return 'SD'

    if filename:
        fn_upper = filename.upper()
        if re.search(r'[-_]SMP[-_.]|\bSMP\b', fn_upper): return 'SMP'
        if re.search(r'[-_]SMA[-_.]|[-_]SMK[-_.]|\bSMA\b|\bSMK\b', fn_upper): return 'SMA'
        if re.search(r'[-_]TK[-_.]|\bTK\b', fn_upper): return 'TK'
        if re.search(r'[-_]SD[-_.]|\bSD\b', fn_upper): return 'SD'

    t_upper = text[:2500].upper()
    if 'SMP' in t_upper: return 'SMP'
    if 'SMA' in t_upper or 'SMK' in t_upper: return 'SMA'
    if 'TK' in t_upper: return 'TK'
    return 'SD'


class PDFSoalExtractor:
    """
    Menelaah dokumen PDF Paket Soal UKG Sokrates dan memecahnya secara cerdas
    menjadi daftar modul penugasan per tingkat kelas (TK, SD, SMP, SMA).
    """

    def __init__(self, api_key: Optional[str] = None):
        if current_app:
            self.api_key = api_key or current_app.config.get('GEMINI_API_KEY', '')
        else:
            self.api_key = api_key or os.getenv('GEMINI_API_KEY', '')

    @staticmethod
    def extract_full_text(pdf_file) -> str:
        reader = PdfReader(pdf_file)
        pages_text = []
        for idx, page in enumerate(reader.pages):
            txt = page.extract_text() or ''
            pages_text.append(f"\n<<<HALAMAN_{idx + 1}>>>\n{txt}")
        raw = "\n".join(pages_text)
        return clean_extracted_text(raw)

    def extract_and_parse(self, pdf_file) -> Dict[str, Any]:
        try:
            filename = getattr(pdf_file, 'filename', '') or (os.path.basename(str(pdf_file)) if isinstance(pdf_file, (str, Path)) else '')
            full_text = self.extract_full_text(pdf_file)
            if not full_text.strip():
                return {
                    'success': False,
                    'error': 'Dokumen PDF tidak memuat teks yang dapat dibaca.'
                }

            modules = self._dissect_modules(full_text, filename)
            if not modules:
                return {
                    'success': False,
                    'error': 'Tidak ditemukan pola penugasan modul di dalam dokumen PDF.'
                }

            return {
                'success': True,
                'modules': modules,
                'total_modules': len(modules),
                'method': 'multi_module_parser'
            }

        except Exception as e:
            logger.exception("Gagal menelaah PDF Soal UKG")
            return {
                'success': False,
                'error': f"Terjadi kesalahan saat memproses PDF: {str(e)}"
            }

    def _dissect_modules(self, full_text: str, filename: str = "") -> List[Dict[str, Any]]:
        # 1. Deteksi Jenjang Global dari Cover Dokumen
        doc_grade = infer_grade_level("", full_text[:2000], filename)

        # 2. Deteksi Kelas Global dari Cover Dokumen
        doc_class = 'Kelas 10' if doc_grade == 'SMA' else ('Kelas 7' if doc_grade == 'SMP' else ('TK A & B' if doc_grade == 'TK' else 'Kelas 1'))
        class_m = re.search(r'Kelas\s*:\s*([^\n\r/]+)', full_text, re.I)
        if class_m:
            raw_c = class_m.group(1).strip().upper()
            doc_class = ROMAN_TO_CLASS.get(raw_c, raw_c)
            if not doc_class.lower().startswith('kelas') and not doc_class.lower().startswith('tk'):
                doc_class = f'Kelas {doc_class}'

        # 3. Deteksi Semester Global
        global_sem = "Semester 1"
        if re.search(r"ganjil\s+dan\s+genap|1\s*&\s*2|1\s+dan\s+2", full_text, re.I):
            global_sem = "Semester 1 & 2"
        elif re.search(r"semester\s*:\s*2|genap", full_text, re.I):
            global_sem = "Semester 2"

        # 4. Pecah teks berdasarkan header penugasan (Mendukung "SOAL PROYEK" dan "PROYEK ... KELAS ...")
        split_pattern = r'(?=(?:SOAL\s+PROYEK\s+[^\n\r]+|PROYEK[\s\S]{1,150}?KELAS\s+[IVX0-9]+))'
        raw_sections = re.split(split_pattern, full_text, flags=re.I)
        valid_sections = [s for s in raw_sections if re.search(r'^\s*SOAL\s+PROYEK|PROYEK[\s\S]{1,150}?KELAS\s+[IVX0-9]+', s, re.I)]

        if not valid_sections:
            valid_sections = [full_text]

        modules = []
        for idx, sec in enumerate(valid_sections, 1):
            mod_data = self._parse_single_section(sec, idx, doc_grade, doc_class, global_sem, filename)
            if mod_data:
                modules.append(mod_data)

        return modules

    def _parse_single_section(self, text: str, index: int, def_grade: str, def_class: str, default_sem: str, filename: str = "") -> Optional[Dict[str, Any]]:
        grade_level = def_grade
        class_level = def_class

        # Deteksi penugasan spesifik di section ini
        sec_h = re.search(r'SOAL PROYEK\s+(TK|SD|SMP|SMA|SMK)?\s*([IVX0-9AB\s&-]+?)(?=\r|\n|$)', text, re.I)
        if sec_h:
            if sec_h.group(1):
                grade_level = sec_h.group(1).strip().upper()
            raw_c = sec_h.group(2).strip().upper()
            class_level = ROMAN_TO_CLASS.get(raw_c, f"Kelas {raw_c}")
        else:
            km = re.search(r'KELAS\s+([IVX0-9]+)', text, re.I)
            if km:
                raw_c = km.group(1).strip().upper()
                class_level = ROMAN_TO_CLASS.get(raw_c, f"Kelas {raw_c}")

        class_level = re.sub(r'[\r\n]+', ' ', class_level).strip()

        # Validasi ulang jenjang berdasarkan kelas agar 100% konsisten
        grade_level = infer_grade_level(class_level, text, filename)

        # 2. Judul Proyek, Materi, Media, dan Aplikasi
        mat_m = re.search(r'Materi\s*:\s*([^\n\r]+)', text, re.I)
        p_name_match = re.search(r'Nama Proyek\s*:\s*([^\n\r]+)', text, re.I)

        if mat_m:
            project_name = mat_m.group(1).strip()
        elif p_name_match:
            project_name = p_name_match.group(1).strip()
        else:
            project_name = "Penerapan Materi Komputer / Informatika"

        media_match = re.search(r'(?:3\.3\.1\s*)?Media\s*:\s*([^\n\r]+)', text, re.I)
        media_name = media_match.group(1).strip() if media_match else ""
        media_name = re.sub(r'^\d+\.\d+\.\d+\s*Media\s*:\s*', '', media_name).strip()

        app_match = re.search(r'Aplikasi\s*:\s*([^\n\r]+)', text, re.I)
        app_name = app_match.group(1).strip() if app_match else ""

        title = f"{project_name} ({app_name or media_name})" if (app_name or media_name) and (app_name or media_name).lower() not in project_name.lower() else project_name

        # 3. Format Berkas yang Diizinkan
        exts = []
        t_low = text.lower()
        if 'cpp' in t_low or 'c++' in t_low: exts.append('.cpp')
        if 'pascal' in t_low or '.pas' in t_low: exts.append('.pas')
        if 'python' in t_low or '.py' in t_low: exts.append('.py')
        if 'java' in t_low and '.java' not in exts: exts.append('.java')
        if 'pdf' in t_low: exts.append('.pdf')
        if 'jpeg' in t_low or 'jpg' in t_low: exts.extend(['.jpg', '.png'])
        if 'png' in t_low and '.png' not in exts: exts.append('.png')
        if 'mp4' in t_low or 'video' in t_low: exts.append('.mp4')
        if 'pptx' in t_low or 'powerpoint' in t_low: exts.append('.pptx')
        if 'docx' in t_low or 'word' in t_low: exts.append('.docx')
        if 'xlsx' in t_low or 'excel' in t_low: exts.append('.xlsx')
        if 'scratch' in t_low or '.sb' in t_low: exts.append('.sb3')
        if not exts: exts = ['.pdf', '.pptx', '.docx']

        allowed_file_types = ", ".join(list(dict.fromkeys(exts)))

        # 4. Rincian Instruksi
        rincian_match = re.search(r'3\.\s*RINCIAN PROYEK[\s\S]+?(?=4\.\s*WAKTU|5\.\s*KRITERIA|6\.\s*KRITERIA|4\.\s*OUTPUT|1\.\s*WAKTU|$)', text, re.I)
        if rincian_match:
            instructions = rincian_match.group(0).strip()
        else:
            instructions = f"Pengerjaan proyek {title} menggunakan {app_name or 'aplikasi pembelajaran'} sesuai instruksi penugasan."

        # 5. Kode Materi Otomatis (Contoh: UKG-SMP-07, UKG-SMP-08, UKG-SMA-10, UKG-SD-01)
        num_m = re.search(r'\d+', class_level)
        num_str = f"{int(num_m.group(0)):02d}" if num_m else f"{index:02d}"
        code = f"UKG-{grade_level}-{num_str}"

        # 6. Kriteria Penilaian Resmi Sokrates (70% Ide/Tehnik, 20% Kerapihan, 10% Tema)
        criteria = [
            {
                "name": "Ide / Kreatifitas / Tehnik",
                "weight": 70.0,
                "evidence_indicators": f"Kesesuaian sintaks, fungsionalitas {app_name or 'aplikasi'}, dan orisinalitas proyek.",
                "description": f"Penilaian kemampuan teknikal {grade_level} {class_level}.",
                "is_mandatory": True,
                "mandatory_min_score": 70.0
            },
            {
                "name": "Kerapihan",
                "weight": 20.0,
                "evidence_indicators": "Kerapihan struktur kode, dokumentasi penjelas, dan kejelasan antarmuka.",
                "description": "Kualitas kerapihan dan keterbacaan berkas tugas.",
                "is_mandatory": False,
                "mandatory_min_score": 60.0
            },
            {
                "name": "Tema",
                "weight": 10.0,
                "evidence_indicators": "Kesesuaian seluruh unsur proyek dengan tema yang ditentukan.",
                "description": "Kesesuaian tema penugasan.",
                "is_mandatory": False,
                "mandatory_min_score": 60.0
            }
        ]

        return {
            "code": code,
            "title": title,
            "grade_level": grade_level,
            "class_level": class_level,
            "semester": default_sem,
            "academic_year": "2025/2026",
            "subject": "Informatika" if grade_level in ['SMA', 'SMP'] else "Komputer Dasar",
            "description": f"Uji Kompetensi Guru Informatika untuk tingkatan {grade_level} {class_level}.",
            "project_instructions": instructions,
            "competencies_measured": f"Kompetensi Profesional Teknikal Informatika ({grade_level} {class_level})",
            "allowed_file_types": allowed_file_types,
            "passing_grade": 75.0,
            "criteria": criteria
        }
