import os
import json
import time
from supabase import create_client, Client
from google import genai
from google.genai import types
import requests
from flask import Flask, request, jsonify
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from pathlib import Path
import re
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY")
GEMINI_API_KEY_BACKUP = os.getenv("GEMINI_API_KEY_BACKUP")

client        = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
client_backup = genai.Client(api_key=GEMINI_API_KEY_BACKUP) if GEMINI_API_KEY_BACKUP else None

def gemini_generate(model_name, **kwargs):
    """
    Wrapper generate_content dengan fallback ke API key cadangan.
    Fallback dipicu jika primary kena error quota / rate-limit (kode 429 atau
    pesan mengandung 'quota', 'rate', 'RESOURCE_EXHAUSTED', 'exhausted').
    """
    try:
        return client.models.generate_content(model=model_name, **kwargs)
    except Exception as primary_err:
        err_str = str(primary_err).lower()
        is_quota_error = any(k in err_str for k in [
            "429", "quota", "rate", "resource_exhausted", "exhausted", "limit"
        ])
        if is_quota_error and client_backup:
            print(f"DEBUG [gemini_generate]: Primary API quota habis ({primary_err}). "
                  f"Beralih ke API key cadangan untuk {model_name}...", flush=True)
            return client_backup.models.generate_content(model=model_name, **kwargs)
        raise


FONNTE_TOKEN    = os.getenv("FONNTE_TOKEN")
FONNTE_GROUP_ID = os.getenv("FONNTE_GROUP_ID", "120363407069913309@g.us")
TZ = os.getenv("TZ", "Asia/Jakarta")

UPLOAD_FOLDER = 'uploads'
Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = 'settings.json'
PENDING_FILE  = 'pending_task.json'

def load_settings():
    """Load konfigurasi dari settings.json, kembalikan default jika tidak ada."""
    defaults = {"reminder_hour": 18, "reminder_minute": 0}
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
                defaults.update(data)
    except Exception as e:
        print(f"DEBUG: Gagal load settings: {e}")
    return defaults

def save_settings(settings):
    """Simpan konfigurasi ke settings.json."""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
        print(f"DEBUG: Settings disimpan: {settings}")
    except Exception as e:
        print(f"DEBUG: Gagal simpan settings: {e}")

def save_pending_task(data):
    """Simpan tugas pending (belum lengkap) ke file JSON."""
    try:
        data['timestamp'] = datetime.now().isoformat()
        with open(PENDING_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"DEBUG: Pending task disimpan: {data}", flush=True)
    except Exception as e:
        print(f"DEBUG: Gagal simpan pending task: {e}")

def get_pending_task():
    """Ambil tugas pending dari file JSON, None jika tidak ada."""
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"DEBUG: Gagal baca pending task: {e}")
    return None

def clear_pending_task():
    """Hapus file pending task setelah selesai diproses."""
    try:
        if os.path.exists(PENDING_FILE):
            os.remove(PENDING_FILE)
            print("DEBUG: Pending task dihapus.", flush=True)
    except Exception as e:
        print(f"DEBUG: Gagal hapus pending task: {e}")

scheduler = BackgroundScheduler(timezone="Asia/Jakarta")

def get_all_tasks():
    try:
        response = supabase.table('tb_tugas').select("*").order("deadline", desc=False).execute()
        return response.data
    except Exception as e:
        print(f"DEBUG: get_all_tasks fallback due to: {e}")
        response = supabase.table('tb_tugas').select("id, tugas, deadline, created_at").order("deadline", desc=False).execute()
        return response.data

def add_task(mapel, tugas, deadline):
    data = {"tugas": tugas, "deadline": deadline}
    try:
        data["mapel"] = mapel
        response = supabase.table('tb_tugas').insert(data).execute()
        return response.data
    except Exception as e:
        print(f"DEBUG: add_task fallback (mapel column might be missing): {e}")
        data.pop("mapel", None)
        response = supabase.table('tb_tugas').insert(data).execute()
        return response.data

def delete_task_by_id(task_id):
    response = supabase.table('tb_tugas').delete().eq("id", task_id).execute()
    return response.data

def update_task_by_id(task_id, n_mapel, n_tugas, n_dl):
    data = {"tugas": n_tugas, "deadline": n_dl}
    try:
        data["mapel"] = n_mapel
        response = supabase.table('tb_tugas').update(data).eq("id", task_id).execute()
        return response.data
    except Exception as e:
        print(f"DEBUG: update_task fallback (mapel column might be missing): {e}")
        data.pop("mapel", None)
        response = supabase.table('tb_tugas').update(data).eq("id", task_id).execute()
        return response.data

def cleanup_old_tasks():
    try:
        today = datetime.now().date().isoformat()
        response = supabase.table('tb_tugas').delete().lt("deadline", today).execute()
        print(f"DEBUG: Pembersihan database berhasil. Data dihapus: {len(response.data if response.data else [])}", flush=True)
    except Exception as e:
        print(f"Cleanup error: {e}", flush=True)

def is_silent_audio(filepath, rms_threshold=200, silent_ratio_threshold=0.95):
    """
    Deteksi apakah audio hanya berisi keheningan atau noise.
    - rms_threshold      : nilai RMS per-frame di bawah ini dianggap senyap
    - silent_ratio_threshold: jika >95% frame senyap, audio dianggap tidak ada suara
    Mengembalikan True jika audio diam/noise, False jika ada suara bermakna.
    """
    import wave
    import struct
    import math
    try:
        with wave.open(filepath, 'rb') as wf:
            n_channels   = wf.getnchannels()
            sampwidth    = wf.getsampwidth()
            n_frames     = wf.getnframes()

            if n_frames == 0:
                print("DEBUG [silence]: File audio kosong (0 frame).")
                return True

            raw = wf.readframes(n_frames)

        fmt_map = {1: 'B', 2: 'h', 4: 'i'}
        fmt = fmt_map.get(sampwidth, 'h')
        total_samples = n_frames * n_channels
        samples = struct.unpack(f'{total_samples}{fmt}', raw[:total_samples * sampwidth])

        chunk_size = 1024
        silent_chunks = 0
        total_chunks  = 0

        for i in range(0, len(samples), chunk_size):
            chunk = samples[i:i + chunk_size]
            if not chunk:
                continue
            rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
            if rms < rms_threshold:
                silent_chunks += 1
            total_chunks += 1

        if total_chunks == 0:
            return True

        ratio = silent_chunks / total_chunks
        print(f"DEBUG [silence]: silent_ratio={ratio:.2f} ({silent_chunks}/{total_chunks} chunks), threshold={rms_threshold}")
        return ratio >= silent_ratio_threshold

    except Exception as e:
        print(f"DEBUG [silence]: Gagal cek audio, dianggap tidak senyap: {e}")
        return False


def process_audio_with_gemini(filepath):
    try:
        with open(filepath, 'rb') as f:
            audio_data = f.read()
        
        if is_silent_audio(filepath):
            print("DEBUG: Audio terdeteksi senyap/noise. Proses dihentikan.")
            return None

        today = datetime.now().strftime("%Y-%m-%d")
        prompt = (
            f"Dengarkan audio ini dengan seksama. "
            f"Jika audio TIDAK mengandung ucapan manusia yang jelas (hanya keheningan, noise, suara latar, atau suara tidak bermakna), "
            f'kembalikan TEPAT JSON ini tanpa komentar apapun: {{"mapel": null, "tugas": null, "deadline": null}}. '
            f"Jika ada ucapan yang jelas: ekstrak Mata Pelajaran (mapel), instruksi tugas, dan deadline-nya. "
            f"Hari ini adalah {today}. Konversikan kata waktu relatif berikut menjadi tanggal: "
            f"besok = +1 hari, lusa = +2 hari, tulat = +3 hari, tubin = +4 hari, cekelong = +5 hari, minggu depan = +7 hari. "
            f'Berikan respon HANYA dalam format JSON: {{"mapel": "...", "tugas": "...", "deadline": "YYYY-MM-DD"}}.'
        )
        
        models_to_try = [
            "gemini-3.1-flash-lite",
            "gemini-3-flash-preview",
            "gemini-2.5-flash", 
            "gemini-2.0-flash", 
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash-lite-preview-06-17"
        ]
        
        for model_name in models_to_try:
            try:
                print(f"DEBUG: Mencoba proses audio dengan {model_name}...")
                
                config = None
                if "gemini-3" in model_name:
                    config = types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_level="MEDIUM")
                    )
                
                response = gemini_generate(
                    model_name,
                    contents=[prompt, genai.types.Part.from_bytes(data=audio_data, mime_type='audio/wav')],
                    config=config
                )
                
                json_text = response.text
                if "```json" in json_text:
                    json_text = json_text.split("```json")[1].split("```")[0].strip()
                elif "```" in json_text:
                    json_text = json_text.split("```")[1].split("```")[0].strip()
                
                res_json = json.loads(json_text)
                
                if not res_json.get("tugas"):
                    print(f"DEBUG: Audio tidak dapat dikenali (tidak ada tugas).")
                    return None
                
                print(f"DEBUG: Sukses proses audio dengan {model_name} (partial={not res_json.get('deadline') or not res_json.get('mapel')})")
                return res_json
            except Exception as model_e:
                print(f"DEBUG: Model {model_name} gagal proses audio: {model_e}")
                continue
        
        return None
    except Exception as e:
        import traceback
        print(f"DEBUG ERROR Gemini audio processing: {str(e)}")
        traceback.print_exc()
        return None

def resolve_relative_dates(text):
    """
    Pra-proses teks: ganti kata waktu relatif lokal/slang menjadi tanggal eksplisit (YYYY-MM-DD)
    sebelum dikirim ke Gemini, agar AI tidak perlu menebak artinya.

    Kamus hari:
        besok    = +1 hari
        lusa     = +2 hari
        tulat    = +3 hari
        tubin    = +4 hari
        cekelong = +5 hari
        minggu depan = +7 hari
    """
    from datetime import timedelta
    today = datetime.now().date()

    relative_map = [
        (r'\bcekelong\b',     today + timedelta(days=5)),
        (r'\bminggu\s+depan\b', today + timedelta(days=7)),
        (r'\btubin\b',        today + timedelta(days=4)),
        (r'\btulat\b',        today + timedelta(days=3)),
        (r'\blusa\b',         today + timedelta(days=2)),
        (r'\bbesok\b',        today + timedelta(days=1)),
    ]

    result = text
    for pattern, date_obj in relative_map:
        date_str = date_obj.isoformat()
        new_result = re.sub(pattern, date_str, result, flags=re.IGNORECASE)
        if new_result != result:
            print(f"DEBUG [resolve_dates]: '{pattern}' -> '{date_str}'")
        result = new_result

    return result


def process_text_with_gemini(text):
    print(f"DEBUG: Memulai process_text_with_gemini dengan teks: '{text}'")
    try:
        text = resolve_relative_dates(text)
        print(f"DEBUG: Teks setelah resolve_dates: '{text}'")

        today = datetime.now().strftime("%Y-%m-%d")
        prompt = (
            f"Hari ini adalah {today}.\n"
            f"Tugas: Ekstrak Nama Mata Pelajaran (mapel), nama tugas, dan tanggal deadline dari kalimat ini: '{text}'.\n"
            f"Catatan: kata waktu seperti besok/lusa/tulat/tubin/cekelong sudah dikonversi menjadi tanggal (YYYY-MM-DD) dalam kalimat di atas.\n"
            f'Format respon: HANYA JSON murni seperti ini: {{"mapel": "...", "tugas": "...", "deadline": "YYYY-MM-DD"}}.'
        )
        
        print(f"DEBUG: Mengirim prompt ke Gemini...")
        
        models_to_try = [
            "gemini-3.1-flash-lite",
            "gemini-3-flash-preview",
            "gemini-2.5-flash", 
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash-lite-preview-06-17"
        ]
        raw_text = None
        
        for model_name in models_to_try:
            try:
                print(f"DEBUG: Mencoba model {model_name}...", flush=True)
                
                config = None
                if "gemini-3" in model_name:
                    config = types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_level="MEDIUM")
                    )
                
                response = gemini_generate(
                    model_name,
                    contents=prompt,
                    config=config
                )
                if response and response.text:
                    raw_text = response.text.strip()
                    print(f"DEBUG: Sukses menggunakan {model_name}", flush=True)
                    break
            except Exception as e:
                print(f"DEBUG: Model {model_name} gagal: {str(e)}", flush=True)
                continue
        
        if not raw_text:
            print("DE: model gagal respon.")
            return None
            
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            json_str = match.group(0)
            try:
                return json.loads(json_str)
            except:
                return None
        return None
    
    except Exception as e:
        print(f"DEBUG CRITICAL: Terjadi Exception: {str(e)}")
        return None

def send_wa(message, target=None):
    """Kirim pesan WhatsApp via Fonnte API."""
    if target is None:
        target = FONNTE_GROUP_ID
    try:
        print(f"DEBUG: Mengirim WA ke {target}...")
        c_code = '0' if '@' in str(target) else '62'
        
        response = requests.post('https://api.fonnte.com/send', data={
            'target': target,
            'message': message,
            'countryCode': c_code
        }, headers={'Authorization': FONNTE_TOKEN})
        print(f"DEBUG: Respon Fonnte ({response.status_code}): {response.text}")
        return response.status_code == 200
    except Exception as e:
        print(f"DEBUG ERROR send_wa: {str(e)}")
        return False


def supabase_heartbeat():
    print("Sending Supabase heartbeat...", flush=True)
    try:
        supabase.table('tb_tugas').select("id", count="exact").limit(1).execute()
        print("DEBUG: Heartbeat sent successfully.", flush=True)
    except Exception as e:
        print(f"DEBUG: Heartbeat failed: {e}", flush=True)

def check_pending_deadlines():
    """
    Dijalankan scheduler setiap hari.
    Cek apakah ada pending task yang sudah dikonfirmasi dari alat tapi belum dilengkapi
    deadline/mapel-nya via WA.
    - Hari ke-1 s/d ke-2 : kirim pengingat
    - Hari ke-3           : pengingat terakhir + hapus pending task
    """
    print("DEBUG [check_pending]: Memeriksa pending task...", flush=True)
    try:
        pending = get_pending_task()
        if not pending:
            return
        if not pending.get("confirmed_at"):
            return
        if not pending.get("missing"):
            return

        from datetime import timedelta
        confirmed_at = datetime.fromisoformat(pending["confirmed_at"])
        days_since   = (datetime.now() - confirmed_at).days
        reminder_count = pending.get("reminder_count", 0)

        tugas    = pending.get("tugas", "?")
        missing  = pending.get("missing", [])
        mapel    = pending.get("mapel", "Umum")

        if days_since < 1:
            return

        if reminder_count >= 3:
            clear_pending_task()
            send_wa(
                f"\U0001f5d1\ufe0f *TUGAS DIHAPUS OTOMATIS*\n\n"
                f"\U0001f4da Mapel : {mapel}\n"
                f"\U0001f4dd Tugas : {tugas}\n\n"
                f"Tugas ini dihapus karena {', '.join(missing)} tidak diisi "
                f"dalam 3 hari setelah konfirmasi."
            )
            print(f"DEBUG [check_pending]: Pending task '{tugas}' dihapus setelah 3 hari tanpa respon.", flush=True)
            return

        reminder_count += 1
        pending["reminder_count"] = reminder_count
        save_pending_task(pending)

        missing_str = " dan ".join(missing)
        send_wa(
            f"\u23f0 *PENGINGAT {reminder_count}/3 \u2014 TUGAS BELUM LENGKAP*\n\n"
            f"\U0001f4da Mapel : {mapel}\n"
            f"\U0001f4dd Tugas : {tugas}\n\n"
            f"Segera lengkapi *{missing_str}* agar tugas tersimpan!\n"
            + (
                f"Balas: `/deadline TAHUN-BULAN-TANGGAL`" if "deadline" in missing and "mapel" not in missing
                else f"Balas: `/mapel [nama]`" if "mapel" in missing and "deadline" not in missing
                else f"Balas: `/mapel [nama] /deadline TAHUN-BULAN-TANGGAL`"
            )
            + (f"\n\n\u26a0\ufe0f Tersisa {3 - reminder_count} pengingat sebelum tugas dihapus otomatis." if reminder_count < 3 else "\n\n\u26a0\ufe0f Ini pengingat terakhir!")
        )
        print(f"DEBUG [check_pending]: Pengingat {reminder_count}/3 terkirim untuk '{tugas}'.", flush=True)

    except Exception as e:
        print(f"DEBUG [check_pending]: Error: {e}", flush=True)

def send_reminders():
    print("Checking for reminders...", flush=True)
    try:
        cleanup_old_tasks()
        today = datetime.now().date().isoformat()
        tasks = get_all_tasks()
        
        for task in tasks:
            if task['deadline'] >= today:
                message = (
                    f"\U0001f514 *PENGINGAT TUGAS*\n\n"
                    f"Mapel : {task.get('mapel', '-')}\n"
                    f"Tugas : {task['tugas']}\n"
                    f"Deadline : {task['deadline']}\n\n"
                    f"Semangat belajarnya! \U0001f525\n\n"
                    f"\U0001f4a1 Ketik /tugas untuk mengelola tugas."
                )
                if send_wa(message):
                    print(f"Sent scheduled reminder for: {task['tugas']}", flush=True)
                else:
                    print(f"DEBUG: Gagal kirim reminder untuk: {task['tugas']}", flush=True)
    except Exception as e:
        print(f"Scheduler error: {e}", flush=True)

@app.route('/upload', methods=['POST'])
def upload_audio():
    filepath = os.path.abspath(os.path.join(UPLOAD_FOLDER, "record.wav"))
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)

    try:
        with open(filepath, 'wb') as f:
            while True:
                chunk = request.stream.read(4096)
                if not chunk:
                    break
                f.write(chunk)
        
        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
            return jsonify({"status": "error", "message": "File kosong atau gagal simpan"}), 400

        print(f"DEBUG: File tersimpan di {filepath} ({os.path.getsize(filepath)} bytes)", flush=True)
        gemini_res = process_audio_with_gemini(filepath)
        
        if gemini_res and isinstance(gemini_res, dict):
            tugas    = (gemini_res.get("tugas") or "").strip() or None
            deadline = (gemini_res.get("deadline") or "").strip() or None
            mapel    = (gemini_res.get("mapel") or "").strip() or None
            if not tugas:
                return jsonify({"status": "error", "message": "AI gagal mengekstrak tugas dari audio"}), 500
            missing = []
            if not deadline:
                missing.append("deadline")
            if not mapel:
                missing.append("mapel")

            if missing:
                pending = {
                    "tugas": tugas,
                    "mapel": mapel or "Umum",
                    "deadline": deadline,
                    "missing": missing,
                    "source": "audio"
                }
                save_pending_task(pending)
                print(f"DEBUG /upload: Partial result -- missing {missing}, pending disimpan (WA belum dikirim).", flush=True)

                return jsonify({
                    "status": "pending",
                    "tugas": tugas,
                    "mapel": mapel if mapel else "Tidak terdeteksi",
                    "deadline": deadline if deadline else "Tidak terdeteksi",
                    "missing": missing,
                    "message": "Informasi belum lengkap. Konfirmasi di alat untuk lanjut."
                })

            return jsonify({
                "status": "success",
                "mapel": mapel,
                "tugas": tugas,
                "deadline": deadline
            })
        else:
            return jsonify({"status": "error", "message": "AI gagal mengekstrak data"}), 500

    except Exception as e:
        print(f"ERROR Upload: {str(e)}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/confirm', methods=['POST'])
def confirm_task():
    """
    Endpoint konfirmasi tugas dari ESP32.
    Alur:
    - Jika tugas LENGKAP (ada deadline) -> simpan ke DB, kirim notif WA.
    - Jika tugas PENDING (deadline/mapel kosong) -> tandai confirmed_at di pending_task.json,
      kirim WA meminta info yang kurang. Scheduler mengingatkan 3 hari, lalu hapus.
    """
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"status": "error", "message": "Payload tidak valid atau bukan JSON."}), 400
            
        mapel    = (data.get('mapel') or '').strip()
        tugas    = (data.get('tugas') or '').strip()
        deadline = (data.get('deadline') or '').strip()
        action   = (data.get('action') or '').strip().lower()

        if mapel.lower() in ('tidak terdeteksi', 'null', 'none', ''):
            mapel = ''
        if deadline.lower() in ('tidak terdeteksi', 'null', 'none', ''):
            deadline = ''

        print(f"DEBUG /confirm: mapel='{mapel}' tugas='{tugas}' deadline='{deadline}' action='{action}'", flush=True)

        if action in ('delete', 'cancel', 'tidak'):
            pending = get_pending_task()
            if pending and pending.get("tugas", "").strip().lower() == tugas.lower():
                clear_pending_task()
            return jsonify({"status": "cancelled", "message": "Tugas dibatalkan oleh pengguna."})

        if not tugas:
            return jsonify({"status": "error", "message": "Field 'tugas' tidak boleh kosong."}), 400

        pending = get_pending_task()
        is_pending_match = pending and pending.get("tugas", "").strip().lower() == tugas.lower()

        if is_pending_match and pending.get("missing"):
            pending["confirmed_at"] = datetime.now().isoformat()
            pending["reminder_count"] = 0
            if mapel and mapel != pending.get("mapel", ""):
                pending["mapel"] = mapel
            if deadline:
                pending["deadline"] = deadline
                pending["missing"] = [m for m in pending["missing"] if m != "deadline"]
            save_pending_task(pending)

            missing = pending.get("missing", [])
            mapel_display    = pending.get("mapel") or "Belum disebutkan"
            deadline_display = pending.get("deadline") or "Belum disebutkan"
            msg = (
                f"*Tugas Baru*\n\n"
                f"\U0001f4da Mapel   : {mapel_display}\n"
                f"\U0001f4dd Tugas   : {tugas}\n"
                f"\u23f0 Deadline: {deadline_display}\n\n"
            )
            if "deadline" in missing and "mapel" in missing:
                msg += (
                    "\u2753Mapel dan deadline belum terdeteksi.\n"
                    "Balas dengan:\n"
                    "`/mapel [nama] /deadline TAHUN-BULAN-TANGGAL`\n"
                    "Contoh: `/mapel Kimia /deadline 2025-01-01`\n\n"
                    "\u23f3 Jika tidak dibalas dalam 3 hari, tugas akan dihapus otomatis."
                )
            elif "deadline" in missing:
                msg += (
                    "\u2753Deadline belum diketahui.\n"
                    "Balas dengan: `/deadline TAHUN-BULAN-TANGGAL`\n"
                    "Contoh: `/deadline 2025-01-01`\n\n"
                    "\u23f3 Jika tidak dibalas dalam 3 hari, tugas akan dihapus otomatis."
                )
            else:
                msg += (
                    "\u2753Mapel belum diketahui.\n"
                    "Balas dengan: `/mapel [nama mapel]`\n"
                    "Contoh: `/mapel Kimia`\n\n"
                    "\u23f3 Jika tidak dibalas dalam 3 hari, tugas akan dihapus otomatis."
                )
            send_wa(msg, target=FONNTE_GROUP_ID)
            print(f"DEBUG /confirm: Pending task dikonfirmasi dari alat. WA terkirim, menunggu info: {missing}", flush=True)
            return jsonify({
                "status": "pending",
                "message": "Tugas dikonfirmasi. Bot WA sedang menanyakan info yang kurang.",
                "missing": missing
            })

        if not deadline:
            return jsonify({"status": "error", "message": "Field 'deadline' tidak boleh kosong."}), 400

        try:
            deadline_date = datetime.strptime(deadline, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({
                "status": "error",
                "message": f"Format deadline tidak valid: '{deadline}'. Harus YYYY-MM-DD."
            }), 400

        today = datetime.now().date()
        if deadline_date < today:
            return jsonify({
                "status": "error",
                "message": f"Deadline ({deadline}) sudah lewat! Hari ini: {today.isoformat()}"
            }), 400

        mapel_final = mapel if mapel else "Umum"
        add_task(mapel_final, tugas, deadline)
        print(f"DEBUG /confirm: Tugas berhasil disimpan: {tugas} | {deadline}", flush=True)

        selisih = (deadline_date - today).days
        if selisih == 0:
            info_hari = "*HARI INI!*"
        elif selisih == 1:
            info_hari = "Besok"
        else:
            info_hari = f"{selisih} hari lagi"

        msg = (
            f"\u2705 *TUGAS BARU DISIMPAN ({info_hari})*\n\n"
            f"\U0001f4da Mapel   : {mapel_final}\n"
            f"\U0001f4dd Tugas   : {tugas}\n"
            f"\u23f0 Deadline: {deadline}\n\n"
            f"Mantap!"
        )
        send_wa(msg, target=FONNTE_GROUP_ID)

        return jsonify({
            "status": "success",
            "message": "Tugas berhasil disimpan.",
            "mapel": mapel_final,
            "tugas": tugas,
            "deadline": deadline,
            "days_remaining": selisih
        })

    except Exception as e:
        print(f"ERROR /confirm: {str(e)}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return jsonify({"status": "active", "message": "Webhook Sistem Otomatisasi is running!"})
    try:
        if request.is_json:
            data = request.json
        else:
            data = request.form
            
        message = data.get('message', '').strip()
        sender = data.get('sender', '')
        group  = data.get('group', '')

        print("=== PYTHON WEBHOOK RECEIVED ===")
        print(f"Message: '{message}'")
        print(f"Sender: '{sender}'")
        print(f"Group: '{group}'")
        print(f"Configured FONNTE_GROUP_ID: '{FONNTE_GROUP_ID}'")

        target = group if group else sender

        is_match = (
            not FONNTE_GROUP_ID
            or group == FONNTE_GROUP_ID
            or sender == FONNTE_GROUP_ID
            or target == FONNTE_GROUP_ID
            or FONNTE_GROUP_ID in str(group)
            or FONNTE_GROUP_ID in str(sender)
        )
        print(f"Is Group Match in Python?: {is_match} | target={target}")
        
        if not is_match:
            print(f"DEBUG: Pesan diabaikan. group='{group}' sender='{sender}' expected='{FONNTE_GROUP_ID}'", flush=True)
            return jsonify({"status": "ignored"})
            
        msg_lower = message.lower()
        today_str = str(datetime.now().date())

        def reply_wa(msg_text):
            send_wa(msg_text, target=target)
            return jsonify({"status": "ok", "reply": msg_text})

        if '/help' in msg_lower or '/bantuan' in msg_lower:
            help_msg = (
                "*PANDUAN PENGGUNAAN BOT TUGAS*\n\n"
                "Berikut adalah daftar perintah yang tersedia:\n\n"
                "1. */tugas*\n"
                "   Menampilkan seluruh daftar tugas aktif beserta deadline-nya.\n\n"
                "2. */add [deskripsi tugas dan deadline]*\n"
                "   Menambahkan tugas baru secara otomatis menggunakan AI.\n"
                "   Contoh: `/add Tugas Matematika halaman 50 deadline besok`\n\n"
                "3. */edit [nomor] [perubahan]*\n"
                "   Mengubah informasi tugas yang sudah ada.\n"
                "   Contoh: `/edit 1 Tugas Fisika deadline 2025-05-20`\n\n"
                "4. */hapus [nomor / beberapa nomor / semua]*\n"
                "   Menghapus tugas berdasarkan nomor urut di daftar.\n"
                "   - Satu tugas : `/hapus 1`\n"
                "   - Beberapa tugas: `/hapus 1 2 3` atau `/hapus 1-3`\n"
                "   - Seluruh tugas: `/hapus semua`\n\n"
                "5. */waktu [jam:menit]*\n"
                "   Melihat atau mengatur waktu pengingat otomatis harian.\n"
                "   Contoh: `/waktu 18:30`\n\n"
                "6. */deadline [YYYY-MM-DD]*\n"
                "   Melengkapi tanggal deadline untuk tugas pending.\n\n"
                "7. */mapel [nama mapel]*\n"
                "   Melengkapi nama mata pelajaran untuk tugas pending."
            )
            return reply_wa(help_msg)

        elif '/add' in msg_lower:
            query = re.sub(r'\/add', '', message, flags=re.IGNORECASE).strip()
            if not query:
                return reply_wa("Format tidak sesuai. Gunakan: `/add [tugas] [waktu]`")
            else:
                gemini_res = process_text_with_gemini(query)
                if gemini_res:
                    mapel    = (gemini_res.get("mapel") or "").strip() or None
                    tugas    = (gemini_res.get("tugas") or "").strip() or None
                    deadline = (gemini_res.get("deadline") or "").strip() or None

                    if not tugas:
                        return reply_wa("Gagal mendeteksi informasi tugas. Harap tuliskan deskripsi tugas dengan lebih jelas.")
                    elif not deadline or not mapel:
                        missing = []
                        if not deadline:
                            missing.append("deadline")
                        if not mapel:
                            missing.append("mapel")
                        pending = {
                            "tugas": tugas,
                            "mapel": mapel or "Umum",
                            "deadline": deadline,
                            "missing": missing,
                            "source": "text"
                        }
                        save_pending_task(pending)
                        mapel_display    = mapel or "Belum disebutkan"
                        deadline_display = deadline or "Belum disebutkan"
                        msg = (
                            f"*INFORMASI TUGAS BELUM LENGKAP*\n\n"
                            f"Mata Pelajaran : {mapel_display}\n"
                            f"Deskripsi Tugas: {tugas}\n"
                            f"Deadline       : {deadline_display}\n\n"
                        )
                        if "deadline" in missing and "mapel" in missing:
                            msg += "Silakan balas dengan format: `/mapel [nama] /deadline YYYY-MM-DD`"
                        elif "deadline" in missing:
                            msg += "Informasi deadline belum ada.\nSilakan balas dengan format: `/deadline YYYY-MM-DD`"
                        else:
                            msg += "Informasi mata pelajaran belum ada.\nSilakan balas dengan format: `/mapel [nama mapel]`"
                        return reply_wa(msg)
                    else:
                        if deadline < today_str:
                            return reply_wa(f"Gagal menyimpan tugas. Tanggal deadline ({deadline}) sudah terlewati.")
                        else:
                            add_task(mapel, tugas, deadline)
                            return reply_wa(f"*TUGAS BERHASIL DISIMPAN*\n\nMata Pelajaran : {mapel}\nDeskripsi Tugas: {tugas}\nDeadline       : {deadline}")
                else:
                    return reply_wa("Terjadi kesalahan saat memproses data tugas.")

        elif '/tugas' in msg_lower:
            tasks = get_all_tasks()
            if tasks:
                reply = "*DAFTAR TUGAS AKTIF*\n\n"
                for i, task in enumerate(tasks, 1):
                    reply += f"{i}. *{task.get('mapel', '-')}*\n   Tugas   : {task['tugas']}\n   Deadline: {task['deadline']}\n\n"
                reply += "Petunjuk: Gunakan `/hapus [nomor/semua]` atau `/edit [nomor] [perubahan]` untuk mengelola tugas."
            else:
                reply = "Tidak ada tugas aktif yang tersimpan saat ini."
            return reply_wa(reply)

        elif '/hapus' in msg_lower:
            query_hapus = re.sub(r'\/hapus', '', message, flags=re.IGNORECASE).strip()
            tasks = get_all_tasks()

            if not tasks:
                return reply_wa("Tidak ada tugas aktif yang tersimpan saat ini.")

            if not query_hapus:
                reply = "*HAPUS TUGAS*\n\nPilih nomor tugas yang ingin dihapus:\n\n"
                for i, task in enumerate(tasks, 1):
                    reply += f"{i}. *{task.get('mapel', '-')}*: {task['tugas']}\n"
                reply += "\nSilakan ketik:\n- `/hapus 1` (hapus 1 tugas)\n- `/hapus 1 2 3` (hapus banyak)\n- `/hapus 1-3` (hapus rentang nomor)\n- `/hapus semua` (hapus seluruh tugas)"
                return reply_wa(reply)

            if query_hapus.lower() in ['semua', 'all', 'semesta']:
                deleted_count = len(tasks)
                for task in tasks:
                    delete_task_by_id(task['id'])
                return reply_wa(f"*SELURUH TUGAS BERHASIL DIHAPUS*\n\nTotal tugas yang dihapus: {deleted_count} tugas.")

            indices_to_delete = set()

            range_matches = re.findall(r'(\d+)\s*-\s*(\d+)', query_hapus)
            for start, end in range_matches:
                s, e = int(start), int(end)
                if s > e:
                    s, e = e, s
                for idx in range(s, e + 1):
                    indices_to_delete.add(idx - 1)

            clean_query = re.sub(r'\d+\s*-\s*\d+', '', query_hapus)
            single_numbers = re.findall(r'\b\d+\b', clean_query)
            for num_str in single_numbers:
                indices_to_delete.add(int(num_str) - 1)

            if not indices_to_delete:
                return reply_wa("Nomor tugas tidak valid. Silakan ketik `/hapus` untuk melihat daftar nomor tugas.")

            deleted_items = []
            invalid_numbers = []

            sorted_indices = sorted(list(indices_to_delete), reverse=True)

            for idx in sorted_indices:
                if 0 <= idx < len(tasks):
                    task = tasks[idx]
                    delete_task_by_id(task['id'])
                    deleted_items.append(f"- *{task.get('mapel', '-')}*: {task['tugas']}")
                else:
                    invalid_numbers.append(str(idx + 1))

            if deleted_items:
                deleted_items.reverse()
                reply = f"*BERHASIL MENGHAPUS {len(deleted_items)} TUGAS*\n\nDaftar tugas yang dihapus:\n" + "\n".join(deleted_items)
                if invalid_numbers:
                    reply += f"\n\nCatatan: Nomor {', '.join(invalid_numbers)} tidak ditemukan."
                return reply_wa(reply)
            else:
                return reply_wa(f"Nomor tugas {', '.join(invalid_numbers)} tidak ditemukan di daftar.")

        elif '/edit' in msg_lower:
            match = re.search(r'\/edit\s+(\d+)(.*)', message, re.IGNORECASE | re.DOTALL)
            tasks = get_all_tasks()
            if match:
                idx = int(match.group(1)) - 1
                new_content = match.group(2).strip()
                if 0 <= idx < len(tasks):
                    if not new_content:
                        return reply_wa(f"Gunakan format: `/edit {idx+1} [tugas baru] [deadline]` untuk mengubah tugas '{tasks[idx]['tugas']}'.")
                    else:
                        gemini_res = process_text_with_gemini(new_content)
                        if gemini_res and gemini_res.get("tugas") and gemini_res.get("deadline"):
                            n_mapel = gemini_res.get("mapel", tasks[idx].get("mapel", "Umum"))
                            n_tugas = gemini_res["tugas"]
                            n_dl = gemini_res["deadline"]
                            if n_dl < today_str:
                                return reply_wa(f"Gagal memperbarui tugas. Tanggal deadline ({n_dl}) sudah terlewati.")
                            else:
                                update_task_by_id(tasks[idx]['id'], n_mapel, n_tugas, n_dl)
                                return reply_wa(f"*TUGAS BERHASIL DIPERBARUI*\n\nMata Pelajaran Baru : {n_mapel}\nDeskripsi Tugas Baru: {n_tugas}\nDeadline Baru       : {n_dl}")
                        else:
                            return reply_wa("Gagal memproses perubahan tugas.")
                else:
                    return reply_wa("Nomor tugas tidak ditemukan.")
            else:
                if not tasks:
                    return reply_wa("Tidak ada tugas aktif yang tersimpan saat ini.")
                else:
                    reply = "*EDIT TUGAS*\n\nPilih nomor tugas yang ingin diubah:\n\n"
                    for i, task in enumerate(tasks, 1):
                        reply += f"{i}. {task['tugas']}\n"
                    reply += "\nSilakan ketik: `/edit [nomor] [perubahan]`"
                    return reply_wa(reply)

        elif '/waktu' in msg_lower:
            match = re.search(r'[:/]waktu\s+(\d{1,2})[.:]?(\d{2})?', message, re.IGNORECASE)
            if not match:
                settings = load_settings()
                h = settings['reminder_hour']
                m = settings['reminder_minute']
                reply = (
                    f"*PENGATURAN WAKTU PENGINGAT*\n\n"
                    f"Waktu pengingat harian aktif pada pukul: *{h:02d}:{m:02d} WIB*\n\n"
                    f"Untuk mengubah waktu, ketik:\n`/waktu [Jam:Menit]`"
                )
                return reply_wa(reply)
            else:
                new_hour = int(match.group(1))
                new_minute = int(match.group(2)) if match.group(2) else 0

                if 0 <= new_hour <= 23 and 0 <= new_minute <= 59:
                    settings = load_settings()
                    settings['reminder_hour'] = new_hour
                    settings['reminder_minute'] = new_minute
                    save_settings(settings)

                    try:
                        scheduler.reschedule_job(
                            'reminder_job',
                            trigger='cron',
                            hour=new_hour,
                            minute=new_minute
                        )
                        print(f"DEBUG: Scheduler diperbarui ke {new_hour:02d}:{new_minute:02d}", flush=True)
                        reply = (
                            f"*WAKTU PENGINGAT BERHASIL DIPERBARUI*\n\n"
                            f"Pengingat tugas otomatis telah diubah ke pukul *{new_hour:02d}:{new_minute:02d} WIB*."
                        )
                        return reply_wa(reply)
                    except Exception as sched_e:
                        print(f"DEBUG: Gagal reschedule: {sched_e}")
                        return reply_wa("Gagal memperbarui jadwal pengingat.")
                else:
                    return reply_wa("Format waktu tidak valid. Silakan gunakan format 00-23 untuk jam dan 00-59 untuk menit.\nContoh: `/waktu 18:30`")

        elif '/deadline' in msg_lower:
            pending = get_pending_task()
            if not pending:
                return reply_wa("Tidak ada tugas yang sedang menunggu konfirmasi deadline.")
            else:
                match_mapel = re.search(r'/mapel\s+([^/]+?)(?=\s*/deadline|$)', message, re.IGNORECASE)
                if match_mapel:
                    new_mapel = match_mapel.group(1).strip()
                    pending["mapel"] = new_mapel
                    pending["missing"] = [m for m in pending.get("missing", []) if m != "mapel"]

                match_dl = re.search(r'/deadline\s+(\d{4}-\d{2}-\d{2})', message, re.IGNORECASE)
                if not match_dl:
                    return reply_wa("Format deadline salah.\nGunakan: `/deadline YYYY-MM-DD`\nContoh: `/deadline 2025-05-20`")
                else:
                    new_deadline = match_dl.group(1)
                    try:
                        dl_date = datetime.strptime(new_deadline, "%Y-%m-%d").date()
                        if dl_date < datetime.now().date():
                            return reply_wa(f"Tanggal deadline ({new_deadline}) sudah terlewati. Masukkan tanggal yang valid.")
                        else:
                            pending["deadline"] = new_deadline
                            pending["missing"] = [m for m in pending.get("missing", []) if m != "deadline"]

                            if pending.get("missing"):
                                save_pending_task(pending)
                                reply = (
                                    f"Deadline berhasil disimpan: {new_deadline}\n\n"
                                    f"Informasi Mata Pelajaran belum ada.\nSilakan balas: `/mapel [nama mapel]`"
                                )
                                return reply_wa(reply)
                            else:
                                add_task(pending["mapel"], pending["tugas"], pending["deadline"])
                                clear_pending_task()
                                selisih = (dl_date - datetime.now().date()).days
                                info_hari = "HARI INI" if selisih == 0 else ("Besok" if selisih == 1 else f"{selisih} hari lagi")
                                reply = (
                                    f"*TUGAS BERHASIL DISIMPAN ({info_hari})*\n\n"
                                    f"Mata Pelajaran : {pending['mapel']}\n"
                                    f"Deskripsi Tugas: {pending['tugas']}\n"
                                    f"Deadline       : {pending['deadline']}"
                                )
                                return reply_wa(reply)
                    except ValueError:
                        return reply_wa(f"Format tanggal tidak valid: '{new_deadline}'. Gunakan format YYYY-MM-DD.")

        elif '/mapel' in msg_lower:
            pending = get_pending_task()
            if not pending:
                return reply_wa("\u26a0\ufe0f Tidak ada tugas yang sedang menunggu konfirmasi mapel.")
            else:
                match_dl = re.search(r'/deadline\s+(\d{4}-\d{2}-\d{2})', message, re.IGNORECASE)
                if match_dl:
                    new_deadline = match_dl.group(1)
                    try:
                        dl_date = datetime.strptime(new_deadline, "%Y-%m-%d").date()
                        if dl_date < datetime.now().date():
                            return reply_wa(f"\u274c Deadline ({new_deadline}) sudah lewat!")
                        pending["deadline"] = new_deadline
                        pending["missing"] = [m for m in pending.get("missing", []) if m != "deadline"]
                    except ValueError:
                        return reply_wa(f"\u274c Format deadline salah: '{new_deadline}'.")

                match_mapel = re.search(r'/mapel\s+([^/]+?)(?=\s*/deadline|$)', message, re.IGNORECASE)
                if not match_mapel:
                    return reply_wa("\u274c Format mapel salah.\nGunakan: `/mapel [nama mapel]`\nContoh: `/mapel Matematika`")
                else:
                    new_mapel = match_mapel.group(1).strip()
                    pending["mapel"] = new_mapel
                    pending["missing"] = [m for m in pending.get("missing", []) if m != "mapel"]

                    if pending.get("missing"):
                        save_pending_task(pending)
                        reply = (
                            f"\u2705 Mapel disimpan: {new_mapel}\n\n"
                            f"\u2753 Deadline masih belum ada.\nBalas: `/deadline YYYY-MM-DD`"
                        )
                        return reply_wa(reply)
                    else:
                        dl_date = datetime.strptime(pending["deadline"], "%Y-%m-%d").date()
                        add_task(pending["mapel"], pending["tugas"], pending["deadline"])
                        clear_pending_task()
                        selisih = (dl_date - datetime.now().date()).days
                        info_hari = "*HARI INI!*" if selisih == 0 else ("Besok" if selisih == 1 else f"{selisih} hari lagi")
                        reply = (
                            f"\u2705 *TUGAS DISIMPAN ({info_hari})*\n\n"
                            f"\U0001f4da Mapel   : {pending['mapel']}\n"
                            f"\U0001f4dd Tugas   : {pending['tugas']}\n"
                            f"\u23f0 Deadline: {pending['deadline']}\n\nMantap!"
                        )
                        return reply_wa(reply)

        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"Webhook error: {e}", flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/test-gemini', methods=['GET'])
def test_gemini():
    try:
        models = []
        try:
            for m in client.models.list():
                models.append(m.name)
        except Exception as list_e:
             return jsonify({"status": "error", "message": f"API Key mungkin tidak valid atau tidak punya izin list models: {str(list_e)}"})
            
        candidates = ["gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-2.5-flash"]
        results = {}
        
        for model_name in candidates:
            try:
                response = gemini_generate(model_name, contents="Halo")
                return jsonify({
                    "status": "success",
                    "working_model": model_name,
                    "response": response.text,
                    "backup_client_ready": client_backup is not None,
                    "available_models": models
                })
            except Exception as e:
                results[model_name] = str(e)
                
        return jsonify({
            "status": "fail",
            "message": "Semua model gagal.",
            "errors": results,
            "backup_client_ready": client_backup is not None,
            "available_models": models
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/', methods=['GET'])
def index():
    return "Server Running! <br><br><a href='/debug/last-audio' target='_blank'>Klik di sini untuk dengerin rekaman terakhir</a>"

@app.route('/debug/last-audio', methods=['GET'])
def debug_audio():
    from flask import send_from_directory
    return send_from_directory(UPLOAD_FOLDER, "record.wav", as_attachment=False)

_settings = load_settings()
_reminder_hour = _settings.get('reminder_hour', 18)
_reminder_minute = _settings.get('reminder_minute', 0)

scheduler.add_job(send_reminders,          'cron',     id='reminder_job',      hour=_reminder_hour, minute=_reminder_minute)
scheduler.add_job(supabase_heartbeat,       'interval', id='heartbeat_job',     hours=12)
scheduler.add_job(check_pending_deadlines,  'interval', id='pending_check_job', hours=24)
scheduler.start()
print(f"DEBUG: Scheduler started. Pengingat aktif pukul {_reminder_hour:02d}:{_reminder_minute:02d}", flush=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7860, debug=False)
