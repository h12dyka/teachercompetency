import base64
import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import current_app
from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader

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

UNIT_CLASS_SLOTS = {
    'SD': ['Kelas 1', 'Kelas 2', 'Kelas 3', 'Kelas 4', 'Kelas 5', 'Kelas 6'],
    'SMP': ['Kelas 7', 'Kelas 8', 'Kelas 9'],
    'SMA': ['Kelas 10', 'Kelas 11', 'Kelas 12'],
}


# --- Pydantic Schemas for Deep PDF Extraction ---

def _coerce_float(val: Any, default: float) -> float:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().replace('%', '').replace(',', '.')
        if not s or 'belum' in s.lower() or s == '-':
            return default
        m = re.search(r'-?\d+(?:\.\d+)?', s)
        if m:
            return float(m.group(0))
    return default


class ExtractedCriterionSchema(BaseModel):
    name: str = Field(description="Nama aspek/kriteria penilaian")
    weight: float = Field(default=0.0, ge=0.0, le=100.0, description="Bobot persentase (0-100); 0 jika tidak tercantum persentase")
    description: str = Field(default="", description="Deskripsi aspek penilaian atau skala level (mis. Mahir/Cakap/Dasar)")
    evidence_indicators: str = Field(default="", description="Indikator keberhasilan yang tercantum pada PDF")
    is_mandatory: bool = Field(default=False, description="Apakah kriteria bersifat wajib/utama")
    mandatory_min_score: float = Field(default=60.0, ge=0.0, le=100.0)

    @field_validator('weight', mode='before')
    @classmethod
    def parse_weight(cls, v: Any) -> float:
        return min(100.0, max(0.0, _coerce_float(v, 0.0)))

    @field_validator('mandatory_min_score', mode='before')
    @classmethod
    def parse_min_score(cls, v: Any) -> float:
        return min(100.0, max(0.0, _coerce_float(v, 60.0)))

    @field_validator('is_mandatory', mode='before')
    @classmethod
    def parse_mandatory(cls, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ('true', '1', 'yes', 'ya', 'wajib')
        return bool(v)

    @field_validator('description', 'evidence_indicators', mode='before')
    @classmethod
    def parse_str_fields(cls, v: Any) -> str:
        return "" if v is None else str(v)


class ExtractedModuleSchema(BaseModel):
    grade_level: str = Field(description="Unit pendidikan: TK, SD, SMP, atau SMA")
    class_level: str = Field(description="Tingkat kelas spesifik, misal: Kelas 1, Kelas 7, Kelas 10, Kelas 10-12, atau TK A & B")
    semester: str = Field(default="Belum tersedia", description="Semester 1, Semester 2, Semester 1 & 2, atau Belum tersedia")
    curriculum_track: str = Field(default="Belum tersedia", description="Nama kurikulum/jalur pada dokumen, misal: Komputer Dasar, SD Edexcel, Informatika v3")
    specialization: str = Field(default="", description="Nama peminatan jika ada (misal: Desain Grafis, Animasi, Web, Video Editing)")
    subject: str = Field(default="Informatika", description="Mata pelajaran")
    title: str = Field(description="Judul materi/proyek yang spesifik")
    description: str = Field(default="", description="Ringkasan proyek dan tujuan/goal kurikulum dari PDF")
    project_instructions: str = Field(default="", description="Rincian instruksi tugas, ketentuan teknis, soal essay jika ada")
    competencies_measured: str = Field(default="", description="Kompetensi yang dicapai dan tujuan proyek")
    allowed_file_types: str = Field(default="Belum tersedia", description="Ekstensi berkas output resmi dari PDF (misal: .sb3, .mp4, .pptx) atau 'Belum tersedia'")
    applications: str = Field(default="Belum tersedia", description="Aplikasi/software yang diwajibkan pada PDF")
    timeline: str = Field(default="Belum tersedia", description="Batas waktu/perkiraan pengerjaan pada PDF atau 'Belum tersedia'")
    submission_email: str = Field(default="Belum tersedia", description="Alamat email/tujuan pengumpulan pada PDF atau 'Belum tersedia'")
    submission_format: str = Field(default="Belum tersedia", description="Format penamaan file/folder pengumpulan pada PDF")
    passing_grade: float = Field(default=75.0, ge=0.0, le=100.0)
    source_pages: str = Field(default="", description="Nomor halaman sumber di PDF, misal: Hal. 1-3, 10")
    criteria: List[ExtractedCriterionSchema] = Field(default_factory=list)
    missing_info: List[str] = Field(default_factory=list, description="Daftar informasi yang tidak tercantum secara eksplisit di PDF")
    internal_inconsistencies: List[str] = Field(default_factory=list, description="Ketidaksesuaian internal yang ditemukan di PDF untuk modul ini")

    @field_validator('passing_grade', mode='before')
    @classmethod
    def parse_passing_grade(cls, v: Any) -> float:
        return min(100.0, max(0.0, _coerce_float(v, 75.0)))

    @field_validator(
        'semester', 'curriculum_track', 'specialization', 'subject',
        'description', 'project_instructions', 'competencies_measured',
        'allowed_file_types', 'applications', 'timeline', 'submission_email',
        'submission_format', 'source_pages',
        mode='before'
    )
    @classmethod
    def parse_optional_str(cls, v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return str(v)


class PDFDocumentAnalysisSchema(BaseModel):
    document_summary: str = Field(default="", description="Ringkasan isi keseluruhan dokumen PDF")
    detected_units: List[str] = Field(default_factory=list, description="Daftar unit pendidikan yang ditemukan (TK/SD/SMP/SMA)")
    document_inconsistencies: List[str] = Field(default_factory=list, description="Ketidaksesuaian internal di tingkat dokumen")
    document_missing_info: List[str] = Field(default_factory=list, description="Informasi umum yang tidak tercantum di dokumen")
    modules: List[ExtractedModuleSchema] = Field(default_factory=list)


def clean_extracted_text(txt: str) -> str:
    """Menyambung kembali teks singkatan yang patah baris akibat font tabel PDF."""
    t = re.sub(r'S\s*[\r\n]+\s*MP', 'SMP', txt, flags=re.IGNORECASE)
    t = re.sub(r'S\s*[\r\n]+\s*MA', 'SMA', t, flags=re.IGNORECASE)
    t = re.sub(r'S\s*[\r\n]+\s*D', 'SD', t, flags=re.IGNORECASE)
    t = re.sub(r'VI\s*[\r\n]+\s*II', 'VIII', t, flags=re.IGNORECASE)
    t = re.sub(r'VI\s*[\r\n]+\s*I', 'VII', t, flags=re.IGNORECASE)
    return t


def infer_grade_level(class_str: str, text: str = "", filename: str = "") -> str:
    """Memastikan jenjang pendidikan (TK/SD/SMP/SMA) selalu sinkron dan valid."""
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
        if re.search(r'[-_]SMP[-_.]|\bSMP\b', fn_upper):
            return 'SMP'
        if re.search(r'[-_]SMA[-_.]|[-_]SMK[-_.]|\bSMA\b|\bSMK\b', fn_upper):
            return 'SMA'
        if re.search(r'[-_]TK[-_.]|\bTK\b', fn_upper):
            return 'TK'
        if re.search(r'[-_]SD[-_.]|\bSD\b', fn_upper):
            return 'SD'

    t_upper = text[:2500].upper()
    if 'SMP' in t_upper:
        return 'SMP'
    if 'SMA' in t_upper or 'SMK' in t_upper:
        return 'SMA'
    if 'TK' in t_upper:
        return 'TK'
    return 'SD'


def normalize_class_level(raw_class: str, grade_level: str) -> str:
    """Menormalisasi penulisan tingkat kelas agar konsisten (misal: 'Kelas 1', 'Kelas 10-12', 'TK A & B')."""
    s = re.sub(r'\s+', ' ', (raw_class or '')).strip()
    if not s:
        if grade_level == 'TK':
            return 'TK A & B'
        if grade_level == 'SMP':
            return 'Kelas 7'
        if grade_level == 'SMA':
            return 'Kelas 10'
        return 'Kelas 1'

    s_upper = s.upper()
    if s_upper in ROMAN_TO_CLASS:
        return ROMAN_TO_CLASS[s_upper]

    if re.search(r'X\s*-\s*XII|10\s*-\s*12', s_upper):
        return 'Kelas 10-12'

    # Ekstrak peminatan jika tertulis di dalam class_level
    m_num = re.search(r'\b(1[0-2]|[1-9])\b', s_upper)
    if m_num and grade_level in ('SD', 'SMP', 'SMA'):
        return f"Kelas {m_num.group(1)}"

    for rom, mapped in ROMAN_TO_CLASS.items():
        if len(rom) <= 4 and re.search(rf'\b{re.escape(rom)}\b', s_upper):
            return mapped

    if 'TK' in s_upper:
        if 'A' in s_upper and 'B' in s_upper:
            return 'TK A & B'
        if 'A' in s_upper:
            return 'TK A'
        if 'B' in s_upper:
            return 'TK B'
        return 'TK A & B'

    return s


def normalize_semester(raw_sem: str) -> str:
    """Menormalisasi string semester ke format standar aplikasi."""
    s = (raw_sem or '').lower()
    if not s or 'belum tersedia' in s:
        return 'Belum tersedia'
    has_1 = any(k in s for k in ['1', 'ganjil', 'satu'])
    has_2 = any(k in s for k in ['2', 'genap', 'dua', 'ii'])
    if (has_1 and has_2) or ('1 & 2' in s) or ('1 dan 2' in s):
        return 'Semester 1 & 2'
    if has_2:
        return 'Semester 2'
    if has_1:
        return 'Semester 1'
    return raw_sem.strip()


def expand_class_coverage(class_level: str, grade_level: str) -> List[str]:
    """Mengubah rentang kelas (mis. 'Kelas 10-12') menjadi daftar slot kelas yang tercakup."""
    cl = (class_level or '').strip()
    if cl == 'Kelas 10-12':
        return ['Kelas 10', 'Kelas 11', 'Kelas 12']
    m_range = re.match(r'^Kelas\s+(\d+)\s*-\s*(\d+)$', cl, re.I)
    if m_range:
        start_c, end_c = int(m_range.group(1)), int(m_range.group(2))
        if start_c <= end_c:
            return [f"Kelas {n}" for n in range(start_c, end_c + 1)]
    if grade_level == 'TK':
        return [cl]
    return [cl]


class PDFSoalExtractor:
    """
    Menelaah dokumen PDF Paket Soal UKG secara menyeluruh (AI + Fallback Regex),
    memecah per unit pendidikan & tingkatan kelas, mengekstrak rubrik asli,
    mendeteksi duplikasi file, inkonsistensi, serta informasi yang belum tersedia.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        if current_app:
            self.api_key = api_key or current_app.config.get('GEMINI_API_KEY', '')
            self.model = model or current_app.config.get('GEMINI_MODEL', 'gemini-3-flash-preview')
        else:
            self.api_key = api_key or os.getenv('GEMINI_API_KEY', '')
            self.model = model or os.getenv('GEMINI_MODEL', 'gemini-3-flash-preview')

    @staticmethod
    def _read_pdf_bytes_and_name(pdf_file) -> Tuple[bytes, str]:
        if isinstance(pdf_file, (str, Path)):
            path = Path(pdf_file)
            return path.read_bytes(), path.name

        filename = getattr(pdf_file, 'filename', '') or getattr(pdf_file, 'name', '') or 'dokumen.pdf'
        filename = os.path.basename(str(filename))
        if hasattr(pdf_file, 'seek'):
            pdf_file.seek(0)
        raw_bytes = pdf_file.read()
        if hasattr(pdf_file, 'seek'):
            pdf_file.seek(0)
        return raw_bytes, filename

    @staticmethod
    def extract_full_text(pdf_file) -> str:
        reader = PdfReader(pdf_file)
        pages_text = []
        for idx, page in enumerate(reader.pages):
            txt = page.extract_text() or ''
            pages_text.append(f"\n<<<HALAMAN_{idx + 1}>>>\n{txt}")
        raw = "\n".join(pages_text)
        return clean_extracted_text(raw)

    @staticmethod
    def _inspect_pdf_bytes(raw_bytes: bytes, filename: str) -> Dict[str, Any]:
        import io
        md5_hash = hashlib.md5(raw_bytes).hexdigest()
        reader = PdfReader(io.BytesIO(raw_bytes))
        pages_text = []
        empty_pages = []
        has_images = False
        for idx, page in enumerate(reader.pages, 1):
            txt = page.extract_text() or ''
            if len(txt.strip()) < 25:
                empty_pages.append(idx)
            try:
                if len(page.images) > 0:
                    has_images = True
            except Exception:
                pass
            pages_text.append(f"\n<<<HALAMAN_{idx}>>>\n{txt}")

        full_text = clean_extracted_text("\n".join(pages_text))
        return {
            'filename': filename,
            'md5': md5_hash,
            'file_size': len(raw_bytes),
            'page_count': len(reader.pages),
            'empty_pages': empty_pages,
            'has_images': has_images,
            'full_text': full_text,
            'raw_bytes': raw_bytes,
        }

    def extract_and_parse(self, pdf_file) -> Dict[str, Any]:
        """Analisis menyeluruh satu file PDF (kompatibel dengan pemanggilan lama & fitur baru)."""
        raw_bytes, filename = self._read_pdf_bytes_and_name(pdf_file)
        pdf_info = self._inspect_pdf_bytes(raw_bytes, filename)
        if not pdf_info['full_text'].strip() and not pdf_info['has_images']:
            return {
                'success': False,
                'error': 'Dokumen PDF tidak memuat teks atau konten yang dapat dibaca.'
            }

        analysis = self._analyze_single_pdf_info(pdf_info)
        if not analysis.get('success'):
            return analysis

        modules = analysis['modules']
        unit_matrix = self.build_unit_matrix(modules)
        return {
            'success': True,
            'modules': modules,
            'total_modules': len(modules),
            'method': analysis.get('method', 'ai_deep_analyzer'),
            'file_reports': [analysis['file_report']],
            'unit_matrix': unit_matrix,
            'cross_file_report': self.build_cross_file_report([analysis['file_report']], modules, unit_matrix)
        }

    def analyze_multiple_pdfs(self, pdf_files: List[Any]) -> Dict[str, Any]:
        """
        Membaca dan menganalisis seluruh file PDF yang diberikan secara menyeluruh:
        - Mendeteksi duplikasi biner (MD5) agar tidak membuang panggilan AI dan tetap melaporkan duplikatnya
        - Mengekstrak modul per unit/kelas beserta rubrik asli dan missing/inconsistency notes
        - Memetakan ke matriks Unit Pendidikan (SD 1-6, SMP 7-9, SMA 10-12, serta TK bila ada)
        """
        if not pdf_files:
            return {'success': False, 'error': 'Tidak ada file PDF untuk dianalisis.'}

        inspected_list = []
        for f in pdf_files:
            try:
                raw_bytes, filename = self._read_pdf_bytes_and_name(f)
                info = self._inspect_pdf_bytes(raw_bytes, filename)
                inspected_list.append(info)
            except Exception as e:
                logger.exception(f"Gagal membaca PDF {f}")
                inspected_list.append({
                    'filename': os.path.basename(str(f)),
                    'md5': '',
                    'file_size': 0,
                    'page_count': 0,
                    'empty_pages': [],
                    'has_images': False,
                    'full_text': '',
                    'raw_bytes': b'',
                    'read_error': str(e),
                })

        # Kelompokkan berdasarkan MD5 agar file identik hanya dianalisis 1x
        unique_map: Dict[str, Dict[str, Any]] = {}
        ordered_entries: List[Tuple[Dict[str, Any], Optional[str]]] = []
        for info in inspected_list:
            md5 = info['md5']
            if not md5 or info.get('read_error'):
                ordered_entries.append((info, None))
                continue
            if md5 not in unique_map:
                unique_map[md5] = info
                ordered_entries.append((info, None))
            else:
                ordered_entries.append((info, unique_map[md5]['filename']))

        # Jalankan analisis paralel terbatas agar tidak memicu rate limit API (max_workers=2)
        unique_results: Dict[str, Dict[str, Any]] = {}
        unique_infos = list(unique_map.values())
        if len(unique_infos) == 1:
            u = unique_infos[0]
            unique_results[u['md5']] = self._analyze_single_pdf_info(u)
        elif len(unique_infos) > 1:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {executor.submit(self._analyze_single_pdf_info, u): u['md5'] for u in unique_infos}
                for fut, md5 in futures.items():
                    try:
                        unique_results[md5] = fut.result()
                    except Exception as e:
                        logger.exception("Error pada thread analisis PDF")
                        unique_results[md5] = {'success': False, 'error': str(e)}

        all_modules: List[Dict[str, Any]] = []
        file_reports: List[Dict[str, Any]] = []

        for info, duplicate_of in ordered_entries:
            if info.get('read_error'):
                file_reports.append({
                    'filename': info['filename'],
                    'md5': '',
                    'page_count': 0,
                    'method': 'error',
                    'is_duplicate': False,
                    'duplicate_of': None,
                    'module_count': 0,
                    'document_summary': f"Gagal membaca berkas: {info['read_error']}",
                    'detected_units': [],
                    'inconsistencies': [],
                    'missing_info': [],
                    'error': info['read_error']
                })
                continue

            res = unique_results.get(info['md5'], {})
            if duplicate_of:
                rep = dict(res.get('file_report', {}))
                rep['filename'] = info['filename']
                rep['is_duplicate'] = True
                rep['duplicate_of'] = duplicate_of
                rep['module_count'] = len(res.get('modules', []))
                file_reports.append(rep)
                # Tidak menggandakan modul ke daftar impor agar tidak mengimpor modul kembar,
                # namun tetap dicatat di laporan duplikasi file.
                continue

            if not res.get('success'):
                file_reports.append({
                    'filename': info['filename'],
                    'md5': info['md5'][:10],
                    'page_count': info['page_count'],
                    'method': 'error',
                    'is_duplicate': False,
                    'duplicate_of': None,
                    'module_count': 0,
                    'document_summary': res.get('error', 'Gagal mengekstrak PDF'),
                    'detected_units': [],
                    'inconsistencies': [],
                    'missing_info': [],
                    'error': res.get('error', 'Gagal mengekstrak PDF')
                })
                continue

            file_reports.append(res['file_report'])
            all_modules.extend(res['modules'])

        if not all_modules:
            return {
                'success': False,
                'error': 'Tidak ditemukan modul penugasan pada berkas PDF yang dianalisis.'
            }

        # Pastikan seluruh kode materi unik dalam satu batch
        self._deduplicate_module_codes(all_modules)

        unit_matrix = self.build_unit_matrix(all_modules)
        cross_file_report = self.build_cross_file_report(file_reports, all_modules, unit_matrix)

        return {
            'success': True,
            'modules': all_modules,
            'total_modules': len(all_modules),
            'file_reports': file_reports,
            'unit_matrix': unit_matrix,
            'cross_file_report': cross_file_report,
            'method': 'multi_file_deep_analyzer'
        }

    def _analyze_single_pdf_info(self, pdf_info: Dict[str, Any]) -> Dict[str, Any]:
        filename = pdf_info['filename']
        full_text = pdf_info['full_text']

        ai_result = None
        method = 'regex_fallback'

        if self.api_key and self.api_key != 'your_gemini_api_key_here':
            ai_result = self._extract_with_gemini(pdf_info)
            if ai_result and ai_result.modules:
                used_multimodal = bool(pdf_info['empty_pages']) and pdf_info['file_size'] <= 8 * 1024 * 1024
                method = 'ai_multimodal' if used_multimodal else 'ai_text_deep'

        if ai_result and ai_result.modules:
            modules = [
                self._normalize_ai_module(mod, idx, filename)
                for idx, mod in enumerate(ai_result.modules, 1)
            ]
            file_report = {
                'filename': filename,
                'md5': pdf_info['md5'][:10],
                'page_count': pdf_info['page_count'],
                'method': method,
                'is_duplicate': False,
                'duplicate_of': None,
                'module_count': len(modules),
                'document_summary': ai_result.document_summary,
                'detected_units': ai_result.detected_units or sorted({m['grade_level'] for m in modules}),
                'inconsistencies': ai_result.document_inconsistencies,
                'missing_info': ai_result.document_missing_info,
                'error': None
            }
        else:
            # Fallback ke regex parser jika API tidak tersedia / gagal
            if not full_text.strip():
                return {
                    'success': False,
                    'error': f"Berkas '{filename}' tidak memiliki teks terbaca dan ekstraksi AI tidak tersedia."
                }
            modules = self._dissect_modules(full_text, filename)
            for m in modules:
                m['source_file'] = filename
            file_report = {
                'filename': filename,
                'md5': pdf_info['md5'][:10],
                'page_count': pdf_info['page_count'],
                'method': 'regex_fallback',
                'is_duplicate': False,
                'duplicate_of': None,
                'module_count': len(modules),
                'document_summary': f"Ekstraksi fallback regex menghasilkan {len(modules)} modul dari {filename}.",
                'detected_units': sorted({m['grade_level'] for m in modules}),
                'inconsistencies': [],
                'missing_info': ['Analisis fallback regex digunakan; periksa ulang detail rubrik dan ketentuan.'],
                'error': None
            }

        self._deduplicate_module_codes(modules)
        return {
            'success': True,
            'modules': modules,
            'method': method,
            'file_report': file_report
        }

    def _extract_with_gemini(self, pdf_info: Dict[str, Any]) -> Optional[PDFDocumentAnalysisSchema]:
        """Memanggil Gemini API untuk membaca seluruh isi PDF secara mendalam dan menghasilkan JSON terstruktur."""
        system_instruction = (
            "Anda adalah Analis Kurikulum dan Soal Uji Kompetensi Guru (UKG) yang sangat teliti.\n"
            "Tugas Anda membaca SELURUH halaman dokumen PDF paket soal UKG secara menyeluruh dan "
            "memecahnya menjadi daftar modul penugasan per unit pendidikan, tingkatan kelas, semester, dan peminatan.\n\n"
            "ATURAN WAJIB:\n"
            "1. JANGAN PERNAH membuat asumsi. Jika ada informasi (misal: semester, waktu pengerjaan, format file, email tujuan, "
            "bobot persentase rubrik, dll.) yang tidak tercantum secara eksplisit dalam PDF, isi dengan 'Belum tersedia' "
            "dan catat di daftar 'missing_info'.\n"
            "2. Pisahkan SETIAP tingkatan kelas atau peminatan menjadi modul tersendiri. Contoh:\n"
            "   - Jika satu halaman memuat 'Kelas II, III, V, VI' dengan tugas berbeda tiap kelas, WAJIB pecah menjadi modul Kelas 2, Kelas 3, Kelas 5, dan Kelas 6.\n"
            "   - Jika Kelas 6 memiliki beberapa peminatan (Desain Grafis, Video Editing, Web Desain, Animasi), buat modul terpisah per peminatan.\n"
            "   - Jika dokumen mencantumkan rentang 'X - XII' dalam satu proyek tunggal, gunakan class_level 'Kelas 10-12'.\n"
            "3. Periksa seluruh halaman dokumen! Sering kali rubrik penilaian berada di halaman awal (misal Hal. 1-3) dan rincian proyek di halaman berikutnya (Hal. 4-13). "
            "Gabungkan rubrik yang sesuai dengan masing-masing kelas/peminatan.\n"
            "4. Ekstrak KRITERIA & BOBOT PENILAIAN ASLI dari PDF (misal 30/35/35, 40/30/30, 50/30/20, 25/25/20/20/10, atau skala Mahir/Cakap/Dasar). "
            "Jika rubrik berupa level kualitatif tanpa angka persentase, isi weight=0 dan jelaskan skalanya di 'description' serta catat di 'missing_info'. "
            "Jika tidak ada kriteria sama sekali di PDF, biarkan criteria kosong [] dan catat di 'missing_info'.\n"
            "5. Deteksi KETIDAKSESUAIAN INTERNAL di dalam PDF (contoh: instruksi menyebut Ms.Word Mading tetapi hasil proyek menyebut Photoscape; "
            "header bertuliskan SMP padahal tugas untuk SMA; instruksi PowerPoint vs unggah ke Scratch online). Catat di 'internal_inconsistencies' & 'document_inconsistencies'.\n"
            "6. Field 'grade_level' HARUS salah satu dari: 'TK', 'SD', 'SMP', 'SMA'."
        )

        user_prompt = (
            f"Nama File PDF: {pdf_info['filename']}\n"
            f"Jumlah Halaman: {pdf_info['page_count']}\n\n"
            "Lakukan pembacaan menyeluruh terhadap seluruh isi dokumen berikut dan kembalikan JSON sesuai skema.\n\n"
            f"=== TEKS LENGKAP PDF ===\n{pdf_info['full_text']}\n\n"
            "Format JSON yang diharapkan:\n"
            "{\n"
            '  "document_summary": "Ringkasan dokumen...",\n'
            '  "detected_units": ["SD"],\n'
            '  "document_inconsistencies": ["..."],\n'
            '  "document_missing_info": ["..."],\n'
            '  "modules": [\n'
            '    {\n'
            '      "grade_level": "SD",\n'
            '      "class_level": "Kelas 1",\n'
            '      "semester": "Semester 1",\n'
            '      "curriculum_track": "Komputer Dasar / SD Edexcel",\n'
            '      "specialization": "",\n'
            '      "subject": "Komputer Dasar",\n'
            '      "title": "Poster Gemar Membaca (TuxPaint)",\n'
            '      "description": "Ringkasan tujuan & goal kurikulum...",\n'
            '      "project_instructions": "Rincian lengkap instruksi, ketentuan teknis, soal essay jika ada...",\n'
            '      "competencies_measured": "Profesional (Tehnikal) - ...",\n'
            '      "allowed_file_types": ".jpg, .jpeg",\n'
            '      "applications": "TuxPaint",\n'
            '      "timeline": "7 hari kerja",\n'
            '      "submission_email": "sokrates.event@gmail.com",\n'
            '      "submission_format": "Folder Nama_Tingkat_Sekolah",\n'
            '      "passing_grade": 75.0,\n'
            '      "source_pages": "Hal. 2-3",\n'
            '      "criteria": [\n'
            '        {"name": "Ide / Kreatifitas / Tehnik", "weight": 70.0, "description": "...", "evidence_indicators": "...", "is_mandatory": true, "mandatory_min_score": 70.0}\n'
            '      ],\n'
            '      "missing_info": [],\n'
            '      "internal_inconsistencies": []\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        # Kirim multimodal hanya jika ada halaman tanpa teks/scan agar hemat token TPM & cepat
        use_multimodal = bool(pdf_info['empty_pages']) and 0 < pdf_info['file_size'] <= 8 * 1024 * 1024
        if use_multimodal:
            parts = [
                {"text": user_prompt},
                {
                    "inline_data": {
                        "mime_type": "application/pdf",
                        "data": base64.b64encode(pdf_info['raw_bytes']).decode('utf-8')
                    }
                }
            ]
            parsed = self._call_gemini_json(system_instruction, parts)
            if parsed:
                return parsed

        # Analisis teks lengkap
        return self._call_gemini_json(system_instruction, [{"text": user_prompt}])

    def _call_gemini_json(self, system_instruction: str, parts: List[Dict[str, Any]]) -> Optional[PDFDocumentAnalysisSchema]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"parts": parts}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.1,
            }
        }
        for attempt in range(3):
            try:
                response = requests.post(url, json=payload, timeout=90)
                if response.status_code == 429 and attempt < 2:
                    wait_sec = 2 * (attempt + 1)
                    logger.warning(f"Gemini PDF analyzer hit 429 rate limit, retrying in {wait_sec}s...")
                    time.sleep(wait_sec)
                    continue
                if response.status_code != 200:
                    logger.warning(f"Gemini PDF analyzer error {response.status_code}: {response.text[:300]}")
                    return None

                result_json = response.json()
                candidates = result_json.get('candidates', [])
                if not candidates:
                    return None

                raw_text = candidates[0].get('content', {}).get('parts', [{}])[0].get('text', '')
                clean_text = raw_text.strip()
                if clean_text.startswith("```"):
                    clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text, flags=re.IGNORECASE)
                    clean_text = re.sub(r"\s*```$", "", clean_text)

                data = json.loads(clean_text)
                if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                    data = data[0]
                return PDFDocumentAnalysisSchema.model_validate(data)
            except Exception as e:
                logger.warning(f"Gagal memanggil/memvalidasi Gemini PDF analyzer: {e}")
                return None
        return None

    def _normalize_ai_module(self, mod: ExtractedModuleSchema, index: int, filename: str) -> Dict[str, Any]:
        grade_level = (mod.grade_level or '').strip().upper()
        if grade_level not in ('TK', 'SD', 'SMP', 'SMA'):
            grade_level = infer_grade_level(mod.class_level, mod.title + ' ' + mod.project_instructions, filename)

        class_level = normalize_class_level(mod.class_level, grade_level)
        # Sinkronkan ulang jika kelas jelas SMP/SMA/TK
        inferred_g = infer_grade_level(class_level, mod.title, filename)
        if class_level != 'Kelas 1' or grade_level == 'SD':
            grade_level = inferred_g

        semester = normalize_semester(mod.semester)
        missing_info = list(dict.fromkeys([m.strip() for m in (mod.missing_info or []) if m and m.strip()]))
        inconsistencies = list(dict.fromkeys([i.strip() for i in (mod.internal_inconsistencies or []) if i and i.strip()]))

        # Normalisasi spesialisasi agar nilai kosong / 'Belum tersedia' tidak menempel di judul/kode
        raw_spec = (mod.specialization or '').strip()
        if raw_spec.lower() in ('', '-', 'none', 'null', 'tidak ada', 'belum tersedia', 'n/a'):
            spec_clean = ''
        else:
            spec_clean = raw_spec

        # Susun judul yang jelas dengan prefix [Unit Kelas] + peminatan jika ada
        base_title = (mod.title or 'Proyek UKG').strip()
        if spec_clean and spec_clean.lower() not in base_title.lower():
            base_title = f"{base_title} - {spec_clean}"

        prefix = f"[{grade_level} {class_level}]"
        if class_level.lower() not in base_title.lower():
            full_title = f"{prefix} {base_title}"
        else:
            full_title = base_title
        full_title = full_title[:145]

        # Normalisasi kriteria & bobot
        normalized_criteria = self._normalize_criteria(mod.criteria, grade_level, class_level, full_title, missing_info)

        # Kode materi otomatis
        num_m = re.search(r'\d+', class_level)
        num_str = f"{int(num_m.group(0)):02d}" if num_m else f"{index:02d}"
        if class_level == 'Kelas 10-12':
            num_str = '10-12'
        spec_slug = ''
        if spec_clean:
            spec_words = re.findall(r'[A-Za-z0-9]+', spec_clean.upper())
            spec_slug = '-' + ''.join(w[0] for w in spec_words[:3]) if spec_words else ''
        code = f"UKG-{grade_level}-{num_str}{spec_slug}"[:45]

        # Allowed file types (bersihkan catatan dalam kurung agar valid di gatekeeper evaluasi)
        allowed_raw = (mod.allowed_file_types or '').strip()
        if not allowed_raw or 'belum tersedia' in allowed_raw.lower():
            allowed_display = 'Belum tersedia'
            if not any('format berkas' in m.lower() or 'ekstensi' in m.lower() for m in missing_info):
                missing_info.append('Format ekstensi berkas output tidak tercantum secara eksplisit di PDF.')
        else:
            cleaned_exts = re.sub(r'\([^)]*\)', '', allowed_raw)
            ext_tokens = [tok.strip() for tok in cleaned_exts.split(',') if tok.strip()]
            allowed_display = ", ".join(dict.fromkeys(ext_tokens))[:250] or 'Belum tersedia'

        subject = (mod.subject or ('Informatika' if grade_level in ('SMP', 'SMA') else 'Komputer Dasar')).strip()[:100]
        instructions = (mod.project_instructions or mod.description or f"Pengerjaan proyek {full_title}.").strip()
        if mod.applications and mod.applications.lower() != 'belum tersedia' and mod.applications.lower() not in instructions.lower():
            instructions = f"Aplikasi: {mod.applications}\n\n{instructions}"

        desc_parts = [mod.description.strip()] if mod.description else []
        if mod.curriculum_track and mod.curriculum_track.lower() != 'belum tersedia':
            desc_parts.append(f"Kurikulum: {mod.curriculum_track}")
        if mod.source_pages:
            desc_parts.append(f"Sumber: {filename} ({mod.source_pages})")
        description = " | ".join(desc_parts) or f"Uji Kompetensi Guru untuk {grade_level} {class_level}."

        return {
            'code': code,
            'title': full_title,
            'grade_level': grade_level,
            'class_level': class_level,
            'class_coverage': expand_class_coverage(class_level, grade_level),
            'semester': semester if semester != 'Belum tersedia' else 'Semester 1',
            'semester_raw': semester,
            'curriculum_track': mod.curriculum_track or 'Belum tersedia',
            'specialization': spec_clean,
            'academic_year': '2025/2026',
            'subject': subject,
            'description': description,
            'project_instructions': instructions,
            'competencies_measured': (mod.competencies_measured or f"Kompetensi Profesional Teknikal ({grade_level} {class_level})").strip(),
            'allowed_file_types': allowed_display,
            'applications': mod.applications or 'Belum tersedia',
            'timeline': mod.timeline or 'Belum tersedia',
            'submission_email': mod.submission_email or 'Belum tersedia',
            'submission_format': mod.submission_format or 'Belum tersedia',
            'passing_grade': float(mod.passing_grade or 75.0),
            'source_file': filename,
            'source_pages': mod.source_pages or '-',
            'criteria': normalized_criteria,
            'missing_info': missing_info,
            'internal_inconsistencies': inconsistencies,
        }

    @staticmethod
    def _normalize_criteria(
        raw_criteria: List[ExtractedCriterionSchema],
        grade_level: str,
        class_level: str,
        title: str,
        missing_info: List[str]
    ) -> List[Dict[str, Any]]:
        if not raw_criteria:
            missing_info.append('Kriteria & bobot penilaian tidak tercantum di PDF (menggunakan template standar sementara).')
            return [
                {
                    'name': 'Ide / Kreatifitas / Tehnik',
                    'weight': 70.0,
                    'evidence_indicators': f"Kesesuaian teknis pengerjaan {title}.",
                    'description': f"Penilaian kemampuan teknikal {grade_level} {class_level} (template default).",
                    'is_mandatory': True,
                    'mandatory_min_score': 70.0,
                },
                {
                    'name': 'Kerapihan',
                    'weight': 20.0,
                    'evidence_indicators': 'Kerapihan tata letak dan struktur berkas proyek.',
                    'description': 'Kualitas kerapihan berkas tugas (template default).',
                    'is_mandatory': False,
                    'mandatory_min_score': 60.0,
                },
                {
                    'name': 'Tema',
                    'weight': 10.0,
                    'evidence_indicators': 'Kesesuaian karya terhadap tema penugasan.',
                    'description': 'Kesesuaian tema penugasan (template default).',
                    'is_mandatory': False,
                    'mandatory_min_score': 60.0,
                }
            ]

        items = []
        total_w = sum(max(0.0, float(c.weight or 0.0)) for c in raw_criteria)

        if total_w <= 0.0:
            # Rubrik kualitatif (mis. Mahir/Cakap/Dasar) tanpa angka persentase
            missing_info.append('Bobot persentase numerik per kriteria tidak tercantum di PDF (didistribusikan merata).')
            even_w = round(100.0 / len(raw_criteria), 2)
            for i, c in enumerate(raw_criteria):
                w = even_w if i < len(raw_criteria) - 1 else round(100.0 - even_w * (len(raw_criteria) - 1), 2)
                items.append({
                    'name': c.name[:145],
                    'weight': w,
                    'evidence_indicators': c.evidence_indicators or c.description or c.name,
                    'description': c.description or f"Aspek {c.name} ({grade_level} {class_level})",
                    'is_mandatory': bool(c.is_mandatory or i == 0),
                    'mandatory_min_score': float(c.mandatory_min_score or 70.0),
                })
            return items

        for i, c in enumerate(raw_criteria):
            w = max(0.0, float(c.weight or 0.0))
            if abs(total_w - 100.0) > 1.0 and total_w > 0:
                w = round((w / total_w) * 100.0, 2)
            items.append({
                'name': c.name[:145],
                'weight': w,
                'evidence_indicators': c.evidence_indicators or c.description or c.name,
                'description': c.description or f"Aspek {c.name} ({grade_level} {class_level})",
                'is_mandatory': bool(c.is_mandatory),
                'mandatory_min_score': float(c.mandatory_min_score or (70.0 if c.is_mandatory else 60.0)),
            })
        return items

    @staticmethod
    def _deduplicate_module_codes(modules: List[Dict[str, Any]]) -> None:
        seen: Dict[str, int] = {}
        for m in modules:
            base = m['code']
            if base not in seen:
                seen[base] = 1
            else:
                seen[base] += 1
                m['code'] = f"{base}-{seen[base]}"[:48]

    @staticmethod
    def build_unit_matrix(modules: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """
        Memetakan seluruh modul ke Unit Pendidikan standar:
        SD (Kelas 1-6), SMP (Kelas 7-9), SMA (Kelas 10-12),
        serta unit TK jika terdapat modul TK.
        Slot kelas tanpa modul ditandai eksplisit sebagai 'Belum tersedia'.
        """
        matrix: Dict[str, List[Dict[str, Any]]] = {}

        for unit, slots in UNIT_CLASS_SLOTS.items():
            unit_rows = []
            for cls in slots:
                matched = [
                    m for m in modules
                    if m['grade_level'] == unit and (
                        m['class_level'] == cls or cls in m.get('class_coverage', [])
                    )
                ]
                sem_set = sorted({m.get('semester_raw') or m.get('semester') for m in matched})
                missing_semesters = []
                if matched:
                    has_s1 = any('1' in s for s in sem_set)
                    has_s2 = any('2' in s for s in sem_set)
                    if not has_s1:
                        missing_semesters.append('Semester 1 belum tersedia')
                    if not has_s2:
                        missing_semesters.append('Semester 2 belum tersedia')

                unit_rows.append({
                    'class_level': cls,
                    'is_available': len(matched) > 0,
                    'modules': matched,
                    'semesters': sem_set,
                    'missing_semesters': missing_semesters,
                    'status_label': f"{len(matched)} Modul Ditemukan" if matched else "Informasi belum tersedia",
                })
            matrix[unit] = unit_rows

        tk_modules = [m for m in modules if m['grade_level'] == 'TK']
        if tk_modules:
            matrix['TK'] = [{
                'class_level': 'TK A & B',
                'is_available': True,
                'modules': tk_modules,
                'semesters': sorted({m.get('semester_raw') or m.get('semester') for m in tk_modules}),
                'missing_semesters': [],
                'status_label': f"{len(tk_modules)} Modul Ditemukan",
            }]

        return matrix

    @staticmethod
    def build_cross_file_report(
        file_reports: List[Dict[str, Any]],
        modules: List[Dict[str, Any]],
        unit_matrix: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        """Menyusun laporan perbandingan lintas file PDF, duplikasi, inkonsistensi, dan gap kelas."""
        duplicates = [
            {'file': r['filename'], 'duplicate_of': r['duplicate_of']}
            for r in file_reports if r.get('is_duplicate')
        ]

        missing_slots = []
        for unit, rows in unit_matrix.items():
            for row in rows:
                if not row['is_available']:
                    missing_slots.append(f"Unit {unit} — {row['class_level']}: Informasi belum tersedia")
                elif row['missing_semesters']:
                    missing_slots.append(f"Unit {unit} — {row['class_level']}: {', '.join(row['missing_semesters'])}")

        # Perbedaan ketentuan lintas modul/file
        timelines = sorted({m.get('timeline', 'Belum tersedia') for m in modules})
        emails = sorted({m.get('submission_email', 'Belum tersedia') for m in modules})
        tracks = sorted({m.get('curriculum_track', 'Belum tersedia') for m in modules})

        scoring_schemes = sorted({
            " / ".join(f"{c['name']} ({c['weight']:.0f}%)" for c in m.get('criteria', []))
            for m in modules if m.get('criteria')
        })

        all_inconsistencies = []
        for r in file_reports:
            if r.get('is_duplicate'):
                continue
            for inc in r.get('inconsistencies', []):
                all_inconsistencies.append(f"[{r['filename']}] {inc}")
        for m in modules:
            for inc in m.get('internal_inconsistencies', []):
                entry = f"[{m.get('source_file', '-')} | {m['grade_level']} {m['class_level']}] {inc}"
                if entry not in all_inconsistencies:
                    all_inconsistencies.append(entry)

        return {
            'total_files': len(file_reports),
            'unique_files': sum(1 for r in file_reports if not r.get('is_duplicate') and not r.get('error')),
            'duplicates': duplicates,
            'missing_slots': missing_slots,
            'inconsistencies': all_inconsistencies,
            'timelines': timelines,
            'submission_emails': emails,
            'curriculum_tracks': tracks,
            'scoring_schemes': scoring_schemes,
        }

    # --- Regex Fallback Parser (bila API key kosong / gagal koneksi) ---

    def _dissect_modules(self, full_text: str, filename: str = "") -> List[Dict[str, Any]]:
        doc_grade = infer_grade_level("", full_text[:2000], filename)

        doc_class = 'Kelas 10' if doc_grade == 'SMA' else ('Kelas 7' if doc_grade == 'SMP' else ('TK A & B' if doc_grade == 'TK' else 'Kelas 1'))
        class_m = re.search(r'Kelas\s*:\s*([^\n\r/]+)', full_text, re.I)
        if class_m:
            raw_c = class_m.group(1).strip().upper()
            doc_class = normalize_class_level(raw_c, doc_grade)

        global_sem = "Semester 1"
        if re.search(r"ganjil\s+dan\s+genap|1\s*&\s*2|1\s+dan\s+2", full_text, re.I):
            global_sem = "Semester 1 & 2"
        elif re.search(r"semester\s*:\s*2|genap", full_text, re.I):
            global_sem = "Semester 2"

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

        sec_h = re.search(r'SOAL PROYEK\s+(TK|SD|SMP|SMA|SMK)?\s*([IVX0-9AB\s&-]+?)(?=\r|\n|$)', text, re.I)
        if sec_h:
            if sec_h.group(1):
                grade_level = sec_h.group(1).strip().upper()
            raw_c = sec_h.group(2).strip().upper()
            class_level = normalize_class_level(raw_c, grade_level)
        else:
            km = re.search(r'KELAS\s+([IVX0-9]+)', text, re.I)
            if km:
                raw_c = km.group(1).strip().upper()
                class_level = normalize_class_level(raw_c, grade_level)

        class_level = re.sub(r'[\r\n]+', ' ', class_level).strip()
        grade_level = infer_grade_level(class_level, text, filename)

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

        base_title = f"{project_name} ({app_name or media_name})" if (app_name or media_name) and (app_name or media_name).lower() not in project_name.lower() else project_name

        if class_level.lower() not in base_title.lower():
            title = f"[{grade_level} {class_level}] {base_title}"
        else:
            title = base_title
        title = title[:145]

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

        rincian_match = re.search(r'3\.\s*RINCIAN PROYEK[\s\S]+?(?=4\.\s*WAKTU|5\.\s*KRITERIA|6\.\s*KRITERIA|4\.\s*OUTPUT|1\.\s*WAKTU|$)', text, re.I)
        if rincian_match:
            instructions = rincian_match.group(0).strip()
        else:
            instructions = f"Pengerjaan proyek {title} menggunakan {app_name or 'aplikasi pembelajaran'} sesuai instruksi penugasan."

        num_m = re.search(r'\d+', class_level)
        num_str = f"{int(num_m.group(0)):02d}" if num_m else f"{index:02d}"
        if class_level == 'Kelas 10-12':
            num_str = '10-12'
        code = f"UKG-{grade_level}-{num_str}"

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
            "class_coverage": expand_class_coverage(class_level, grade_level),
            "semester": default_sem,
            "semester_raw": default_sem,
            "curriculum_track": "Belum tersedia",
            "specialization": "",
            "academic_year": "2025/2026",
            "subject": "Informatika" if grade_level in ['SMA', 'SMP'] else "Komputer Dasar",
            "description": f"Uji Kompetensi Guru Informatika untuk tingkatan {grade_level} {class_level}.",
            "project_instructions": instructions,
            "competencies_measured": f"Kompetensi Profesional Teknikal Informatika ({grade_level} {class_level})",
            "allowed_file_types": allowed_file_types,
            "applications": app_name or "Belum tersedia",
            "timeline": "Belum tersedia",
            "submission_email": "Belum tersedia",
            "submission_format": "Belum tersedia",
            "passing_grade": 75.0,
            "source_file": filename,
            "source_pages": "-",
            "criteria": criteria,
            "missing_info": [],
            "internal_inconsistencies": [],
        }
