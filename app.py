import traceback
import os
import uuid
import re
from collections import defaultdict
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv
from supabase import create_client, Client
from translations import get_translation

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_voicehire_key')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB max upload
CORS(app, resources={r"/api/*": {"origins": "*"}})

UPLOAD_FOLDER = os.path.join('static', 'uploads')
AUDIO_FOLDER = os.path.join(UPLOAD_FOLDER, 'audio')
VIDEO_FOLDER = os.path.join(UPLOAD_FOLDER, 'video')
ID_FOLDER = os.path.join(UPLOAD_FOLDER, 'ids')

os.makedirs(AUDIO_FOLDER, exist_ok=True)
os.makedirs(VIDEO_FOLDER, exist_ok=True)
os.makedirs(ID_FOLDER, exist_ok=True)

ALLOWED_AUDIO_EXTENSIONS = {'mp3', 'wav', 'ogg', 'm4a', 'aac'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'webm', 'ogg', 'mov'}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png'}

# ---- SUPABASE CLIENT ----
supabase_url = os.environ.get('SUPABASE_URL')
supabase_key = os.environ.get('SUPABASE_KEY')

if not supabase_url or not supabase_key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(supabase_url, supabase_key)

# ---- HELPERS ----
def allowed_file(filename, allowed_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_set

def is_valid_phone(phone):
    return bool(re.match(r'^[6-9]\d{9}$', str(phone).strip()))

def safe_str(val):
    return str(val).strip() if val else ''

# ---- TRANSLATION CONFIG ----
@app.context_processor
def inject_translation():
    def t(text):
        lang = session.get('lang', 'en')
        return get_translation(lang, text)
    return dict(t=t)

@app.route('/set_lang/<lang_code>')
def set_lang(lang_code):
    session['lang'] = lang_code
    return redirect(url_for('home'))

# ---- TEMPLATE ROUTES ----

@app.route('/')
def index():
    return render_template('select_language.html')

@app.route('/home')
def home():
    if 'user_id' in session:
        if session.get('role') == 'user':
            return redirect(url_for('user_dashboard'))
        else:
            return redirect(url_for('worker_dashboard'))
    return render_template('language.html')

@app.route('/role')
def role_selection():
    return render_template('role.html')

@app.route('/login')
def login_page():
    role = request.args.get('role', 'user')
    return render_template('login.html', role=role)

@app.route('/signup/user')
def user_signup_page():
    return render_template('user_signup.html')

@app.route('/signup/worker')
def worker_signup_page():
    return render_template('worker_signup.html')

@app.route('/stitch_worker_dashboard')
def stitch_worker_dashboard():
    return render_template('stitch_worker_dashboard.html')

@app.route('/dashboard/user')
def user_dashboard():
    if 'user_id' not in session or session.get('role') != 'user':
        return redirect(url_for('login_page', role='user'))
    return render_template('user_dashboard.html', name=session.get('name'))

@app.route('/dashboard/worker')
def worker_dashboard():
    if 'user_id' not in session or session.get('role') != 'worker':
        return redirect(url_for('login_page', role='worker'))
    try:
        response = supabase.table("workers").select("*").eq("id", session['user_id']).execute()
        if not response.data:
            session.clear()
            return redirect(url_for('login_page', role='worker'))
        worker_data = response.data[0]
        return render_template('worker_dashboard.html', worker=worker_data)
    except Exception as e:
        return f"Database error: {e}"

# ---- AUTHENTICATION API ENDPOINTS ----

@app.route('/api/auth/signup/user', methods=['POST'])
def signup_user():
    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({'error': 'Invalid JSON data'}), 400

    name = safe_str(data.get('name'))
    phone = safe_str(data.get('phone'))
    password = safe_str(data.get('password'))

    if not all([name, phone, password]):
        return jsonify({'error': 'All fields are required'}), 400
    if not is_valid_phone(phone):
        return jsonify({'error': 'Invalid Indian phone number (10 digits starting with 6-9)'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    hashed_pw = generate_password_hash(password)
    try:
        existing = supabase.table("users").select("id").eq("phone", phone).execute()
        if existing.data:
            return jsonify({'error': 'Phone number already registered'}), 400

        response = supabase.table("users").insert({
            "name": name,
            "phone": phone,
            "password": hashed_pw
        }).execute()

        user_id = response.data[0]['id']
        session['user_id'] = user_id
        session['role'] = 'user'
        session['name'] = name
        session['phone'] = phone

        return jsonify({'message': 'User registered successfully', 'redirect': url_for('user_dashboard')}), 201
    except Exception as e:
        print("Error during user signup:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/auth/signup/worker', methods=['POST'])
def signup_worker():
    name = safe_str(request.form.get('name'))
    work = safe_str(request.form.get('work'))
    location = safe_str(request.form.get('location'))
    phone = safe_str(request.form.get('phone'))
    password = safe_str(request.form.get('password'))

    if not all([name, work, location, phone, password]):
        return jsonify({'error': 'All textual fields are required'}), 400
    if not is_valid_phone(phone):
        return jsonify({'error': 'Invalid Indian phone number (10 digits starting with 6-9)'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    hashed_pw = generate_password_hash(password)
    voice_file = request.files.get('voice_note')
    video_file = request.files.get('video')

    voice_path = None
    video_path = None

    try:
        existing = supabase.table("workers").select("id").eq("phone", phone).execute()
        if existing.data:
            return jsonify({'error': 'Phone number already registered. Please login.'}), 400

        if voice_file and voice_file.filename != '':
            if not allowed_file(voice_file.filename, ALLOWED_AUDIO_EXTENSIONS):
                return jsonify({'error': 'Invalid audio file type'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(voice_file.filename)}"
            voice_file.save(os.path.join(AUDIO_FOLDER, filename))
            voice_path = f'uploads/audio/{filename}'

        if video_file and video_file.filename != '':
            if not allowed_file(video_file.filename, ALLOWED_VIDEO_EXTENSIONS):
                return jsonify({'error': 'Invalid video file type'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(video_file.filename)}"
            video_file.save(os.path.join(VIDEO_FOLDER, filename))
            video_path = f'uploads/video/{filename}'

        id_file = request.files.get('id_proof')
        id_path = None
        if id_file and id_file.filename != '':
            if not allowed_file(id_file.filename, ALLOWED_IMAGE_EXTENSIONS):
                return jsonify({'error': 'Invalid image file type for ID proof'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(id_file.filename)}"
            id_file.save(os.path.join(ID_FOLDER, filename))
            id_path = f'uploads/ids/{filename}'

        lat = request.form.get('latitude')
        lng = request.form.get('longitude')

        response = supabase.table("workers").insert({
            "name": name,
            "work": work,
            "location": location,
            "phone": phone,
            "password": hashed_pw,
            "voice_note": voice_path,
            "video": video_path,
            "id_proof_path": id_path,
            "latitude": float(lat) if lat else None,
            "longitude": float(lng) if lng else None,
            "is_available": True,
            "is_verified": False
        }).execute()

        worker_id = response.data[0]['id']
        session['user_id'] = worker_id
        session['role'] = 'worker'
        session['name'] = name

        return jsonify({'message': 'Worker registered successfully', 'redirect': url_for('worker_dashboard')}), 201
    except Exception as e:
        print("Error during worker signup:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/auth/login', methods=['POST'])
def login():
    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({'error': 'Invalid JSON data'}), 400

    phone = safe_str(data.get('phone'))
    password = safe_str(data.get('password'))
    role = safe_str(data.get('role'))

    if not all([phone, password, role]) or role not in ['user', 'worker']:
        return jsonify({'error': 'Valid phone, password, and role ("user" or "worker") are required'}), 400

    table = 'users' if role == 'user' else 'workers'

    try:
        response = supabase.table(table).select("*").eq("phone", phone).execute()
        rows = response.data

        if rows and check_password_hash(rows[0]['password'], password):
            user = rows[0]
            session['user_id'] = user['id']
            session['role'] = role
            session['name'] = user['name']
            if role == 'user':
                session['phone'] = user['phone']

            redirect_url = url_for('user_dashboard') if role == 'user' else url_for('worker_dashboard')
            return jsonify({'message': 'Login successful', 'redirect': redirect_url}), 200
        else:
            return jsonify({'error': 'Invalid phone or password'}), 401
    except Exception as e:
        print("Error during login:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/auth/logout', methods=['POST'])
def logout_api():
    session.clear()
    return jsonify({'redirect': url_for('home')}), 200

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# ---- DATA API ENDPOINTS ----

@app.route('/api/worker/edit', methods=['POST'])
def edit_worker_profile():
    if 'user_id' not in session or session['role'] != 'worker':
        return jsonify({'error': 'Unauthorized'}), 401

    name = safe_str(request.form.get('name'))
    work = safe_str(request.form.get('work'))
    location = safe_str(request.form.get('location'))
    phone = safe_str(request.form.get('phone'))
    password = safe_str(request.form.get('password'))

    if not all([name, work, location, phone]):
        return jsonify({'error': 'Name, work, location, and phone are required'}), 400
    if not is_valid_phone(phone):
        return jsonify({'error': 'Invalid Indian phone number'}), 400

    worker_id = session['user_id']
    voice_file = request.files.get('voice_note')
    video_file = request.files.get('video')

    try:
        existing_resp = supabase.table("workers").select("voice_note, video, id_proof_path").eq("id", worker_id).execute()
        if not existing_resp.data:
            return jsonify({'error': 'Worker not found'}), 404
        existing = existing_resp.data[0]

        voice_path = existing.get('voice_note')
        video_path = existing.get('video')
        id_path = existing.get('id_proof_path')

        if voice_file and voice_file.filename != '':
            if not allowed_file(voice_file.filename, ALLOWED_AUDIO_EXTENSIONS):
                return jsonify({'error': 'Invalid audio file type'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(voice_file.filename)}"
            voice_file.save(os.path.join(AUDIO_FOLDER, filename))
            voice_path = f'uploads/audio/{filename}'

        if video_file and video_file.filename != '':
            if not allowed_file(video_file.filename, ALLOWED_VIDEO_EXTENSIONS):
                return jsonify({'error': 'Invalid video file type'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(video_file.filename)}"
            video_file.save(os.path.join(VIDEO_FOLDER, filename))
            video_path = f'uploads/video/{filename}'

        id_file = request.files.get('id_proof')
        if id_file and id_file.filename != '':
            if not allowed_file(id_file.filename, ALLOWED_IMAGE_EXTENSIONS):
                return jsonify({'error': 'Invalid image file type for ID proof'}), 400
            filename = f"{uuid.uuid4().hex}_{secure_filename(id_file.filename)}"
            id_file.save(os.path.join(ID_FOLDER, filename))
            id_path = f'uploads/ids/{filename}'

        lat = request.form.get('latitude')
        lng = request.form.get('longitude')

        update_data = {
            "name": name,
            "work": work,
            "location": location,
            "phone": phone,
            "voice_note": voice_path,
            "video": video_path,
            "id_proof_path": id_path,
            "latitude": float(lat) if lat else None,
            "longitude": float(lng) if lng else None,
        }

        if password:
            if len(password) < 6:
                return jsonify({'error': 'Password must be at least 6 characters'}), 400
            update_data["password"] = generate_password_hash(password)

        supabase.table("workers").update(update_data).eq("id", worker_id).execute()
        session['name'] = name
        return jsonify({'message': 'Profile updated successfully', 'redirect': url_for('worker_dashboard')}), 200
    except Exception as e:
        print("Error updating profile:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/worker/availability', methods=['POST'])
def update_availability():
    if 'user_id' not in session or session['role'] != 'worker':
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.get_json() or {}
        is_available = bool(data.get('is_available'))
        worker_id = session['user_id']
        supabase.table("workers").update({"is_available": is_available}).eq("id", worker_id).execute()
        return jsonify({'message': 'Availability updated', 'is_available': is_available}), 200
    except Exception as e:
        print("Error updating availability:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/get_workers', methods=['GET'])
def get_workers():
    work_filter = safe_str(request.args.get('work'))
    location_filter = safe_str(request.args.get('location'))

    try:
        query = supabase.table("workers").select(
            "id, name, work, location, phone, voice_note, video, is_verified, is_available, latitude, longitude"
        )
        if work_filter and work_filter.lower() != 'all':
            query = query.ilike("work", f"%{work_filter}%")
        if location_filter:
            query = query.ilike("location", f"%{location_filter}%")

        workers_resp = query.execute()
        workers = workers_resp.data

        # Fetch all reviews to compute aggregates
        reviews_resp = supabase.table("reviews").select("worker_id, rating").execute()
        review_map = defaultdict(list)
        for r in reviews_resp.data:
            review_map[r['worker_id']].append(r['rating'])

        for w in workers:
            ratings = review_map.get(w['id'], [])
            w['avg_rating'] = round(sum(ratings) / len(ratings), 2) if ratings else 0
            w['review_count'] = len(ratings)

        # Sort: available first, then by rating desc, then review count desc
        workers.sort(key=lambda w: (not w.get('is_available', True), -w['avg_rating'], -w['review_count']))

        return jsonify(workers), 200
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs', methods=['POST'])
def post_job():
    if 'user_id' not in session or session['role'] != 'user':
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({'error': 'Invalid JSON data'}), 400

    service_type = safe_str(data.get('service_type'))
    description = safe_str(data.get('description'))
    location = safe_str(data.get('location'))

    if not service_type or not description or not location:
        return jsonify({'error': 'Service type, description, and location are required'}), 400

    try:
        supabase.table("jobs").insert({
            "user_id": session['user_id'],
            "user_name": session['name'],
            "user_phone": session.get('phone', 'Unknown'),
            "service_type": service_type,
            "description": description,
            "location": location,
            "is_urgent": bool(data.get('is_urgent')),
            "status": "open"
        }).execute()
        return jsonify({'message': 'Job posted successfully!'}), 201
    except Exception as e:
        print("Error posting job:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    if 'user_id' not in session or session['role'] != 'worker':
        return jsonify({'error': 'Unauthorized'}), 401

    work_type = safe_str(request.args.get('work'))
    location = safe_str(request.args.get('location'))
    worker_id = session['user_id']

    try:
        # Get open jobs
        open_query = supabase.table("jobs").select("*").eq("status", "open")
        if work_type:
            open_query = open_query.ilike("service_type", f"%{work_type}%")
        if location:
            open_query = open_query.ilike("location", f"%{location}%")
        open_resp = open_query.execute()

        # Get accepted jobs for this worker
        accepted_resp = supabase.table("jobs").select("*").eq("status", "accepted").eq("worker_id", worker_id).execute()

        jobs = open_resp.data + accepted_resp.data
        # Sort: urgent first, then newest first
        jobs.sort(key=lambda j: (not j.get('is_urgent', False), j.get('created_at', '') ), reverse=False)
        jobs.sort(key=lambda j: j.get('is_urgent', False), reverse=True)

        return jsonify(jobs), 200
    except Exception as e:
        print("Error fetching jobs:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs/customer', methods=['GET'])
def get_customer_jobs():
    if 'user_id' not in session or session['role'] != 'user':
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session['user_id']
    try:
        jobs_resp = supabase.table("jobs").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
        jobs = jobs_resp.data

        # For jobs with a worker_id, fetch worker details
        worker_ids = list({j['worker_id'] for j in jobs if j.get('worker_id')})
        worker_map = {}
        if worker_ids:
            workers_resp = supabase.table("workers").select("id, name, phone").in_("id", worker_ids).execute()
            for w in workers_resp.data:
                worker_map[w['id']] = w

        for j in jobs:
            wid = j.get('worker_id')
            if wid and wid in worker_map:
                j['worker_name'] = worker_map[wid]['name']
                j['worker_phone'] = worker_map[wid]['phone']
            else:
                j['worker_name'] = None
                j['worker_phone'] = None

        return jsonify(jobs), 200
    except Exception as e:
        print("Error fetching customer jobs:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs/<int:job_id>/accept', methods=['POST'])
def accept_job(job_id):
    if 'user_id' not in session or session['role'] != 'worker':
        return jsonify({'error': 'Unauthorized'}), 401

    worker_id = session['user_id']
    try:
        job_resp = supabase.table("jobs").select("status").eq("id", job_id).execute()
        if not job_resp.data:
            return jsonify({'error': 'Job not found'}), 404
        if job_resp.data[0]['status'] != 'open':
            return jsonify({'error': 'Job is no longer open'}), 400

        supabase.table("jobs").update({"status": "accepted", "worker_id": worker_id}).eq("id", job_id).execute()
        return jsonify({'message': 'Job accepted successfully'}), 200
    except Exception as e:
        print("Error accepting job:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/jobs/<int:job_id>/status', methods=['POST'])
def update_job_status(job_id):
    if 'user_id' not in session or session['role'] != 'user':
        return jsonify({'error': 'Unauthorized'}), 401

    user_id = session['user_id']
    try:
        data = request.get_json() or {}
        new_status = safe_str(data.get('status'))
        if new_status not in ['open', 'completed', 'cancelled']:
            return jsonify({'error': 'Invalid status'}), 400

        job_resp = supabase.table("jobs").select("id").eq("id", job_id).eq("user_id", user_id).execute()
        if not job_resp.data:
            return jsonify({'error': 'Job not found or unauthorized'}), 404

        supabase.table("jobs").update({"status": new_status}).eq("id", job_id).execute()
        return jsonify({'message': f'Job marked as {new_status}'}), 200
    except Exception as e:
        print("Error updating job status:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/workers/<int:worker_id>/rate', methods=['POST'])
def rate_worker(worker_id):
    if 'user_id' not in session or session['role'] != 'user':
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        data = request.get_json() or {}
        job_id = data.get('job_id')
        rating = int(data.get('rating', 0))
        review = safe_str(data.get('review', ''))

        if not job_id or not (1 <= rating <= 5):
            return jsonify({'error': 'Valid job ID and rating (1-5) are required'}), 400

        user_id = session['user_id']

        job_resp = supabase.table("jobs").select("status, worker_id").eq("id", job_id).eq("user_id", user_id).execute()
        if not job_resp.data:
            return jsonify({'error': 'Job not found'}), 404
        job = job_resp.data[0]

        if job['status'] != 'completed' or job['worker_id'] != worker_id:
            return jsonify({'error': 'Job must be completed to leave a review'}), 400

        # Prevent duplicate reviews
        dup_resp = supabase.table("reviews").select("id").eq("job_id", job_id).execute()
        if dup_resp.data:
            return jsonify({'error': 'Review already submitted for this job'}), 400

        supabase.table("reviews").insert({
            "job_id": job_id,
            "worker_id": worker_id,
            "user_id": user_id,
            "rating": rating,
            "review": review
        }).execute()

        return jsonify({'message': 'Review submitted successfully'}), 201
    except ValueError:
        return jsonify({'error': 'Rating must be an integer'}), 400
    except Exception as e:
        print("Error submitting review:\n", traceback.format_exc())
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/translate', methods=['POST'])
def translate_api():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    lang = data.get('lang') or session.get('lang') or 'en'
    text = data.get('text')

    if not text:
        return jsonify({'error': 'No text provided'}), 400

    if isinstance(text, list):
        translated = {t: get_translation(lang, t) for t in text}
    else:
        translated = get_translation(lang, text)

    return jsonify({'translated': translated})

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(debug=debug_mode)
