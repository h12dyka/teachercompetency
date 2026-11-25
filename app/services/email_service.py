import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Tuple
from flask import current_app


logger = logging.getLogger(__name__)

class EmailService:
    @staticmethod
    def send_evaluation_result(submission) -> Tuple[bool, str]:
        """
        Mengirimkan laporan hasil evaluasi UKG resmi ke email guru yang bersangkutan.
        Mengembalikan: (is_success: bool, message: str)
        """
        teacher = submission.teacher
        if not teacher or not teacher.email:
            return False, f"Guru '{teacher.name if teacher else 'Unknown'}' belum memiliki alamat email yang terdaftar."
        
        evaluation = submission.ai_evaluation
        if not evaluation:
            return False, "Proyek ini belum selesai dinilai, laporan email belum dapat dikirim."
        
        # Konfigurasi SMTP dari config / environment
        smtp_server = current_app.config.get('MAIL_SERVER', 'smtp.gmail.com')
        smtp_port = current_app.config.get('MAIL_PORT', 587)
        smtp_user = current_app.config.get('MAIL_USERNAME', '')
        smtp_password = current_app.config.get('MAIL_PASSWORD', '')
        sender_email = current_app.config.get('MAIL_DEFAULT_SENDER', smtp_user or 'noreply@sokrates.id')
        # Cek apakah kredensial SMTP sudah dipasang
        if not smtp_user or not smtp_password:
            logger.warning("Kredensial SMTP belum diset di .env. Pengiriman email dilewati (Simulasi Sukses).")
            return True, f"[Simulasi Sukses] Alamat SMTP belum diset di .env, namun format email untuk {teacher.email} sudah siap."
        # Siapkan data email
        is_passed = submission.status == 'passed' or evaluation.calculated_status == 'passed'
        status_label = "LULUS" if is_passed else "BELUM LULUS"
        status_color = "#10b981" if is_passed else "#ef4444"
        score_val = f"{submission.final_score:.2f}" if submission.final_score is not None else f"{evaluation.calculated_ai_score:.2f}"
        # Susun baris tabel kriteria
        criteria_rows = ""
        for cr in evaluation.criteria_results:
            criteria_rows += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 12px; font-weight: 600; color: #1e293b;">{cr.criterion_name}</td>
                <td style="padding: 10px 12px; text-align: center; color: #64748b;">{cr.weight:.0f}%</td>
                <td style="padding: 10px 12px; text-align: right; font-weight: bold; color: #0f172a;">{cr.score:.1f}</td>
            </tr>
            """
        # Template HTML Email Resmi
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 20px; }}
                .card {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; overflow: hidden; }}
                .header {{ background: #4f46e5; color: #ffffff; padding: 24px; text-align: center; }}
                .content {{ padding: 24px; }}
                .badge {{ display: inline-block; padding: 6px 14px; border-radius: 999px; font-weight: bold; font-size: 13px; color: #ffffff; background: {status_color}; }}
                .score-box {{ background: #f1f5f9; border-radius: 12px; padding: 18px; text-align: center; margin: 20px 0; }}
                .score-num {{ font-size: 36px; font-weight: 900; color: #1e293b; }}
                .footer {{ background: #f8fafc; padding: 16px; text-align: center; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="header">
                    <h2 style="margin: 0; font-size: 20px;">SISTEM UJI KOMPETENSI GURU (UKG)</h2>
                    <p style="margin: 4px 0 0; font-size: 13px; opacity: 0.9;">Laporan Resmi Hasil Asesmen Berbasis AI</p>
                </div>
                <div class="content">
                    <p style="font-size: 14px; color: #334155;">Yth. Bapak/Ibu <strong>{teacher.name}</strong>,</p>
                    <p style="font-size: 13px; color: #475569; line-height: 1.5;">
                        Berikut adalah hasil evaluasi resmi proyek UKG yang telah Anda selesaikan pada materi 
                        <strong>{submission.material.title}</strong> ({submission.material.grade_level} - {submission.material.class_level}).
                    </p>
                    <div class="score-box">
                        <div style="font-size: 12px; font-weight: 600; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px;">Nilai Akhir Asesmen</div>
                        <div class="score-num">{score_val} <span style="font-size: 16px; color: #64748b; font-weight: 500;">/ 100</span></div>
                        <div style="margin-top: 8px;">
                            <span class="badge">{status_label}</span>
                        </div>
                    </div>
                    <h4 style="color: #1e293b; margin: 20px 0 8px; font-size: 14px;">Rincian Nilai per Kriteria:</h4>
                    <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 20px;">
                        <thead>
                            <tr style="background: #f8fafc; border-bottom: 2px solid #cbd5e1; color: #475569;">
                                <th style="padding: 10px 12px; text-align: left;">Kriteria</th>
                                <th style="padding: 10px 12px; text-align: center;">Bobot</th>
                                <th style="padding: 10px 12px; text-align: right;">Skor</th>
                            </tr>
                        </thead>
                        <tbody>
                            {criteria_rows}
                        </tbody>
                    </table>
                    <h4 style="color: #1e293b; margin: 16px 0 6px; font-size: 14px;">Catatan & Rekomendasi Evaluator AI:</h4>
                    <div style="background: #f8fafc; border-left: 4px solid #4f46e5; padding: 12px 14px; border-radius: 4px; font-size: 12px; color: #334155; line-height: 1.6;">
                        {evaluation.overall_summary or evaluation.status_reason or '-'}
                    </div>
                    <p style="font-size: 12px; color: #64748b; margin-top: 24px;">
                        Jika Anda memerlukan informasi lebih lanjut terkait hasil penilaian ini, silakan menghubungi Tim Asesor / Administrator UKG sekolah Anda.
                    </p>
                </div>
                <div class="footer">
                    &copy; 2026 Sokrates UKG Assessment System &bull; Email ini dikirim otomatis oleh sistem, mohon tidak membalas email ini.
                </div>
            </div>
        </body>
        </html>
        """
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"[{status_label}] Hasil Uji Kompetensi Guru (UKG) - {submission.material.title}"
            msg["From"] = sender_email
            msg["To"] = teacher.email
            msg.attach(MIMEText(html_body, "html"))
            # Kirim via SMTP
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            if current_app.config.get('MAIL_USE_TLS', True):
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(sender_email, [teacher.email], msg.as_string())
            server.quit()
            logger.info(f"Hasil evaluasi berhasil dikirim ke email: {teacher.email}")
            return True, f"Laporan hasil evaluasi berhasil dikirim ke email guru: {teacher.email}"
        except Exception as e:
            logger.exception("Gagal mengirim email evaluasi")
            return False, f"Gagal mengirim email: {str(e)}"