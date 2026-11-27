import os
from werkzeug.utils import secure_filename
from app.services.pdf_soal_extractor import PDFSoalExtractor
from app.models.rubric import Rubric, RubricVersion, RubricCriterion
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from app.extensions import db
from app.models.material import UKGMaterial
from app.services.audit_service import AuditService
from datetime import datetime, date


materials_bp = Blueprint('materials', __name__, url_prefix='/materials')

@materials_bp.route('/')
@login_required
def index():
    grade = request.args.get('grade', '').strip()
    semester = request.args.get('semester', '').strip()
    search = request.args.get('q', '').strip()

    query = UKGMaterial.query

    if grade:
        query = query.filter_by(grade_level=grade)
    if semester:
        query = query.filter_by(semester=semester)
    if search:
        query = query.filter(
            (UKGMaterial.title.ilike(f'%{search}%')) |
            (UKGMaterial.code.ilike(f'%{search}%'))
        )

    materials = query.order_by(UKGMaterial.grade_level, UKGMaterial.class_level, UKGMaterial.semester, UKGMaterial.title).all()
    return render_template('materials/index.html', materials=materials, grade=grade, semester=semester, search=search)

@materials_bp.route('/create', methods=['GET', 'POST'])
@login_required
def create():
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        title = request.form.get('title', '').strip()
        grade_level = request.form.get('grade_level', '').strip()
        class_level = request.form.get('class_level', '').strip()
        semester = request.form.get('semester', 'Semester 1').strip()
        academic_year = request.form.get('academic_year', '2025/2026').strip()
        subject = request.form.get('subject', 'Informatika / Komputer').strip()
        description = request.form.get('description', '').strip()

        if UKGMaterial.query.filter_by(code=code).first():
            flash(f"Kode materi '{code}' sudah digunakan.", 'danger')
            return render_template('materials/form.html', material=None, form_data=request.form)

        if not code or not title:
            flash("Kode materi dan judul materi wajib diisi.", 'warning')
            return render_template('materials/form.html', material=None, form_data=request.form)

                # 1. Simpan Materi UKG
        mat = UKGMaterial(
            code=code,
            title=title,
            grade_level=grade_level or 'SD',
            class_level=class_level or 'Kelas 1',
            semester=semester or 'Semester 1',
            academic_year=academic_year or '2025/2026',
            subject=subject,
            description=description,
            is_active=True
        )
        db.session.add(mat)
        db.session.flush()

        # 2. Otomatis Buat Rubrik Resmi Terkait di Menu Rubrik
        rubric = Rubric(
            material_id=mat.id,
            title=f"Rubrik Penilaian UKG - {title}",
            description=description or f"Standar evaluasi kompetensi {title} ({mat.grade_level} {mat.class_level}).",
            project_instructions=f"Pengerjaan proyek penugasan {title} sesuai ketentuan kurikulum {subject}.",
            competencies_measured=f"Kompetensi Profesional dan Teknikal {subject} ({mat.grade_level} {mat.class_level})",
            passing_grade=75.0,
            allowed_file_types=".jpg, .png, .pptx, .docx, .xlsx, .sb3, .pdf, .mp4, .zip",
            created_by_id=current_user.id
        )
        db.session.add(rubric)
        db.session.flush()

        # 3. Otomatis Buat Versi 1.0 (Langsung Aktif)
        version = RubricVersion(
            rubric_id=rubric.id,
            version_number=1,
            version_label='v1.0',
            status='active',
            effective_date=date.today(),
            change_notes='Otomatis dibuat bersamaan dengan penambahan materi UKG baru',
            approved_by_id=current_user.id,
            approved_at=datetime.utcnow()
        )
        db.session.add(version)
        db.session.flush()

        # 4. Otomatis Tambah 3 Kriteria Standar Sokrates (70%, 20%, 10%)
        default_criteria = [
            ("Ide / Kreatifitas / Tehnik", 70.0, True, 70.0, f"Kesesuaian penerapan konsep dan fungsionalitas {title}"),
            ("Kerapihan", 20.0, False, 60.0, "Kerapihan tata letak berkas, format, dan estetika hasil karya"),
            ("Tema", 10.0, False, 60.0, "Kesesuaian seluruh unsur karya terhadap tema penugasan UKG")
        ]
        for c_idx, (c_name, c_weight, c_mand, c_min, c_ind) in enumerate(default_criteria, 1):
            crit = RubricCriterion(
                version_id=version.id,
                name=c_name,
                description=f"Penilaian aspek {c_name} {mat.grade_level} {mat.class_level}",
                weight=c_weight,
                scale_min=0.0,
                scale_max=100.0,
                is_mandatory=c_mand,
                mandatory_min_score=c_min,
                evidence_indicators=c_ind,
                sort_order=c_idx
            )
            db.session.add(crit)

        db.session.commit()
        AuditService.log('CREATE_MATERIAL', 'UKGMaterial', mat.id, {'code': code, 'title': title, 'semester': semester})
        flash(f"Materi UKG '{title}' ({code}) dan Rubrik Standar resmi v1.0 berhasil otomatis dibuatkan!", 'success')
        return redirect(url_for('materials.index'))

    return render_template('materials/form.html', material=None, form_data={})

@materials_bp.route('/<int:material_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(material_id):
    mat = UKGMaterial.query.get_or_404(material_id)

    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        title = request.form.get('title', '').strip()
        grade_level = request.form.get('grade_level', '').strip()
        class_level = request.form.get('class_level', '').strip()
        semester = request.form.get('semester', 'Semester 1').strip()
        academic_year = request.form.get('academic_year', '2025/2026').strip()
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        is_active = bool(request.form.get('is_active'))

        existing = UKGMaterial.query.filter(UKGMaterial.code == code, UKGMaterial.id != mat.id).first()
        if existing:
            flash(f"Kode materi '{code}' sudah dipakai materi lain.", 'danger')
            return render_template('materials/form.html', material=mat, form_data=request.form)

        mat.code = code
        mat.title = title
        mat.grade_level = grade_level
        mat.class_level = class_level
        mat.semester = semester
        mat.academic_year = academic_year
        mat.subject = subject
        mat.description = description
        mat.is_active = is_active

        db.session.commit()
        AuditService.log('UPDATE_MATERIAL', 'UKGMaterial', mat.id, {'code': code, 'title': title, 'semester': semester})
        flash(f"Materi UKG '{title}' berhasil diperbarui.", 'success')
        return redirect(url_for('materials.index'))

    return render_template('materials/form.html', material=mat, form_data={})

def _safe_float(val, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _list_existing_soal_pdfs(upload_dir: str):
    if not os.path.isdir(upload_dir):
        return []
    files = []
    for name in sorted(os.listdir(upload_dir)):
        if name.lower().endswith('.pdf'):
            full = os.path.join(upload_dir, name)
            if os.path.isfile(full):
                files.append({
                    'name': name,
                    'path': full,
                    'size_kb': round(os.path.getsize(full) / 1024, 1)
                })
    return files


@materials_bp.route('/import-pdf', methods=['GET', 'POST'])
@login_required
def import_pdf():
    upload_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'soal_pdf')
    existing_pdfs = _list_existing_soal_pdfs(upload_dir)

    if request.method == 'POST':
        analyze_existing = request.form.get('analyze_existing') == '1'
        selected_existing = request.form.getlist('existing_files[]')
        saved_paths = []

        if analyze_existing:
            if selected_existing:
                allowed_map = {item['name']: item['path'] for item in existing_pdfs}
                saved_paths = [allowed_map[n] for n in selected_existing if n in allowed_map]
            else:
                saved_paths = [item['path'] for item in existing_pdfs]

            if not saved_paths:
                flash('Tidak ditemukan berkas PDF di folder soal_pdf.', 'warning')
                return redirect(request.url)
        else:
            uploaded_files = request.files.getlist('pdf_files') or request.files.getlist('pdf_file')
            valid_uploads = [f for f in uploaded_files if f and f.filename]
            if not valid_uploads:
                flash('Pilih minimal satu berkas PDF soal terlebih dahulu.', 'warning')
                return redirect(request.url)

            os.makedirs(upload_dir, exist_ok=True)
            for file in valid_uploads:
                if not file.filename.lower().endswith('.pdf'):
                    flash(f"Berkas '{file.filename}' dilewati karena bukan format .pdf.", 'warning')
                    continue
                safe_name = secure_filename(file.filename)
                saved_path = os.path.join(upload_dir, safe_name)
                file.save(saved_path)
                saved_paths.append(saved_path)

            if not saved_paths:
                flash('Format berkas harus berupa dokumen PDF (.pdf).', 'danger')
                return redirect(request.url)

        extractor = PDFSoalExtractor()
        result = extractor.analyze_multiple_pdfs(saved_paths)

        if not result.get('success'):
            flash(f"Gagal membaca PDF: {result.get('error')}", 'danger')
            return redirect(request.url)

        modules = result.get('modules', [])
        file_reports = result.get('file_reports', [])
        source_names = ", ".join(r['filename'] for r in file_reports)

        return render_template(
            'materials/import_review.html',
            modules=modules,
            source_file=source_names,
            total_modules=len(modules),
            file_reports=file_reports,
            unit_matrix=result.get('unit_matrix', {}),
            cross_file_report=result.get('cross_file_report', {})
        )

    return render_template('materials/import_form.html', existing_pdfs=existing_pdfs)


@materials_bp.route('/import-pdf/confirm', methods=['POST'])
@login_required
def confirm_import():
    # Ambil seluruh array modul yang dicentang pada halaman review
    selected_indexes = request.form.getlist('selected_modules[]')
    if not selected_indexes:
        flash("Tidak ada modul kelas yang dipilih untuk diimpor.", 'warning')
        return redirect(url_for('materials.import_pdf'))

    saved_count = 0
    for idx in selected_indexes:
        code = request.form.get(f'code_{idx}', '').strip().upper()[:45]
        title = request.form.get(f'title_{idx}', '').strip()[:150]
        grade_level = request.form.get(f'grade_level_{idx}', 'SD').strip()[:50]
        class_level = request.form.get(f'class_level_{idx}', '').strip()[:50]
        semester = request.form.get(f'semester_{idx}', 'Semester 1').strip()[:30]
        academic_year = request.form.get(f'academic_year_{idx}', '2025/2026').strip()[:50]
        subject = request.form.get(f'subject_{idx}', 'Komputer Dasar').strip()[:100]
        description = request.form.get(f'description_{idx}', '').strip()
        project_instructions = request.form.get(f'project_instructions_{idx}', '').strip()
        competencies_measured = request.form.get(f'competencies_measured_{idx}', '').strip()
        raw_allowed = request.form.get(f'allowed_file_types_{idx}', '').strip()
        passing_grade = _safe_float(request.form.get(f'passing_grade_{idx}', 75.0), 75.0)
        source_file = request.form.get(f'source_file_{idx}', '').strip()

        if not raw_allowed or raw_allowed.lower() == 'belum tersedia':
            allowed_file_types = '.pdf, .docx, .pptx, .jpg, .png, .zip'
            format_note = ' | Format berkas belum tercantum di PDF, memakai ekstensi umum sementara.'
        else:
            allowed_file_types = raw_allowed[:255]
            format_note = ''

        if not code:
            code = f"UKG-{grade_level}-{idx}"[:45]
        if not title:
            title = f"[{grade_level} {class_level}] Materi UKG"[:150]

        # Pastikan kode materi unik agar materi baru selalu BERTAMBAH (tidak menimpa data materi lama)
        base_code = code
        counter = 2
        while UKGMaterial.query.filter_by(code=code).first():
            suffix = f"-{counter}"
            code = f"{base_code[:50 - len(suffix)]}{suffix}"
            counter += 1

        # 1. Selalu buat materi UKG baru
        material = UKGMaterial(
            code=code,
            title=title,
            grade_level=grade_level,
            class_level=class_level,
            semester=semester,
            academic_year=academic_year,
            subject=subject,
            description=description,
            is_active=True
        )
        db.session.add(material)
        db.session.flush()

        # 2. Selalu buat Rubrik resmi baru yang terikat khusus ke materi ini
        rubric = Rubric(
            material_id=material.id,
            title=f"Rubrik Penilaian UKG - {title}"[:200],
            description=description,
            project_instructions=project_instructions or f"Pengerjaan proyek {title}.",
            competencies_measured=competencies_measured or f"Kompetensi Profesional Teknikal ({grade_level} {class_level})",
            passing_grade=passing_grade,
            allowed_file_types=allowed_file_types,
            created_by_id=current_user.id
        )
        db.session.add(rubric)
        db.session.flush()

        # 3. Buat Versi Rubrik v1.0
        change_note = f"Diekstrak otomatis dari PDF Soal UKG ({source_file or 'Unggahan PDF'}){format_note}"
        version = RubricVersion(
            rubric_id=rubric.id,
            version_number=1,
            version_label='v1.0',
            status='active',
            effective_date=date.today(),
            change_notes=change_note,
            approved_by_id=current_user.id,
            approved_at=datetime.utcnow()
        )
        db.session.add(version)
        db.session.flush()

        # 4. Gunakan Kriteria & Bobot Dinamis dari hasil ekstraksi PDF / review form
        crit_names = request.form.getlist(f'crit_name_{idx}[]')
        crit_weights = request.form.getlist(f'crit_weight_{idx}[]')
        crit_mandatories = request.form.getlist(f'crit_mandatory_{idx}[]')
        crit_min_scores = request.form.getlist(f'crit_min_score_{idx}[]')
        crit_indicators = request.form.getlist(f'crit_indicator_{idx}[]')
        crit_descriptions = request.form.getlist(f'crit_description_{idx}[]')

        parsed_criteria = []
        for c_pos, raw_name in enumerate(crit_names):
            c_name = (raw_name or '').strip()
            if not c_name:
                continue
            c_weight = _safe_float(crit_weights[c_pos] if c_pos < len(crit_weights) else 0, 0.0)
            c_mand = (crit_mandatories[c_pos] == '1') if c_pos < len(crit_mandatories) else False
            c_min = _safe_float(crit_min_scores[c_pos] if c_pos < len(crit_min_scores) else 60.0, 60.0)
            c_ind = (crit_indicators[c_pos] if c_pos < len(crit_indicators) else '').strip() or c_name
            c_desc = (crit_descriptions[c_pos] if c_pos < len(crit_descriptions) else '').strip() or f"Aspek {c_name} untuk {class_level}"
            parsed_criteria.append((c_name[:150], c_weight, c_mand, c_min, c_ind, c_desc))

        if not parsed_criteria:
            parsed_criteria = [
                ("Ide / Kreatifitas / Tehnik", 70.0, True, 70.0, f"Kesesuaian teknis pengerjaan {title}", f"Aspek teknikal untuk {class_level}"),
                ("Kerapihan", 20.0, False, 60.0, "Kerapihan tata letak berkas tugas", f"Aspek kerapihan untuk {class_level}"),
                ("Tema", 10.0, False, 60.0, "Kesesuaian karya terhadap tema UKG", f"Aspek tema untuk {class_level}")
            ]

        for c_idx, (c_name, c_weight, c_mand, c_min, c_ind, c_desc) in enumerate(parsed_criteria, 1):
            crit = RubricCriterion(
                version_id=version.id,
                name=c_name,
                description=c_desc,
                weight=c_weight,
                scale_min=0.0,
                scale_max=100.0,
                is_mandatory=c_mand,
                mandatory_min_score=c_min,
                evidence_indicators=c_ind,
                sort_order=c_idx
            )
            db.session.add(crit)

        saved_count += 1

    db.session.commit()
    AuditService.log('IMPORT_MULTI_SOAL_PDF', 'UKGMaterial', None, {'total_imported': saved_count})
    flash(f"Berhasil! Sebanyak {saved_count} Modul Materi & Rubrik per tingkatan kelas berhasil disimpan ke sistem.", 'success')
    return redirect(url_for('materials.index'))


@materials_bp.route('/<int:material_id>/delete', methods=['POST'])
@login_required
def delete(material_id):
    mat = UKGMaterial.query.get_or_404(material_id)
    title = mat.title
    code = mat.code
    
    try:
        db.session.delete(mat)
        db.session.commit()
        AuditService.log('DELETE_MATERIAL', 'UKGMaterial', material_id, {'code': code, 'title': title})
        flash(f"Materi UKG '{title}' ({code}) beserta rubrik terkait berhasil dihapus.", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f"Gagal menghapus materi: {str(e)}", 'danger')
        
    return redirect(url_for('materials.index'))
 