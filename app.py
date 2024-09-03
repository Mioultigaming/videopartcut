from flask import Flask, request, redirect, url_for, session, send_file, render_template
from werkzeug.utils import secure_filename
import os
import uuid
import zipfile
import ffmpeg
from datetime import timedelta
from urllib.parse import quote as url_quote
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_talisman import Talisman
import logging

# Configuration de l'application Flask
app = Flask(__name__)
app.secret_key = os.urandom(24)  # Utiliser une clé secrète générée aléatoirement
app.permanent_session_lifetime = timedelta(minutes=30)  # Durée de vie de la session

# Configuration des en-têtes de sécurité HTTP
talisman = Talisman(app, content_security_policy={
    'default-src': ['\'self\''],
    'img-src': ['\'self\'', 'data:'],
    'script-src': ['\'self\''],
    'style-src': ['\'self\'']
})

# Configuration du CSRF
csrf = CSRFProtect(app)

# Configuration de la limitation des requêtes
limiter = Limiter(app, key_func=lambda: request.remote_addr)

app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

# Configurer la journalisation
logging.basicConfig(filename='app.log', level=logging.INFO)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'mp4', 'mov', 'avi'}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
@csrf.exempt
@limiter.limit("5 per minute")
def upload_file():
    if 'file' not in request.files:
        return "Pas de fichier téléchargé", 400

    files = request.files.getlist('file')
    if not files:
        return "Pas de fichier sélectionné", 400

    try:
        part_time = int(request.form.get('duration', 10))
        if part_time <= 0:
            raise ValueError("La durée doit être un nombre entier positif.")
    except ValueError as e:
        return f"Durée invalide : {str(e)}", 400

    session.permanent = True  # Marquer la session comme permanente
    session_id = str(uuid.uuid4())
    uploaded_files = []

    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            unique_filename = f"{session_id}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            file.save(filepath)
            uploaded_files.append(filepath)
        else:
            return "Type de fichier non autorisé", 400

    session['uploaded_files'] = uploaded_files
    session['part_time'] = part_time
    session['session_id'] = session_id  # Ajouter l'ID de session pour les fichiers

    return redirect(url_for('process_video'))

@app.route('/process')
def process_video():
    if 'uploaded_files' not in session:
        return "Pas de fichier à traiter", 400

    uploaded_files = session['uploaded_files']
    part_time = session.get('part_time', 10)

    output_files = []
    for filepath in uploaded_files:
        try:
            output_files.extend(cut_video(
                in_filename=filepath,
                part_time=part_time,
                output_folder=app.config['OUTPUT_FOLDER'],
                session_id=session['session_id']
            ))
        except Exception as e:
            logging.error(f"Erreur lors du découpage de la vidéo : {str(e)}")
            return f"Erreur lors du découpage de la vidéo : {str(e)}", 500

    zip_filename = os.path.join(app.config['OUTPUT_FOLDER'], f"{session['session_id']}_videos.zip")
    with zipfile.ZipFile(zip_filename, 'w') as zipf:
        for file in output_files:
            zipf.write(file, os.path.basename(file))

    session['zip_file'] = zip_filename

    return f"Vidéos traitées avec succès. Téléchargez le fichier ZIP contenant toutes les vidéos <a href='/download_zip'>ici</a>."

@app.route('/download_zip')
def download_zip():
    zip_file = session.get('zip_file')
    if not zip_file or not os.path.exists(zip_file):
        return "Aucun fichier ZIP disponible", 400
    return send_file(zip_file, as_attachment=True)

def cut_video(in_filename, part_time, output_folder, session_id):
    try:
        probe = ffmpeg.probe(in_filename)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        if video_stream is None:
            raise ValueError('Aucun flux vidéo trouvé.')

        videotime = float(video_stream['duration'])
        currenttime = 0
        uploadvideos = 0
        output_files = []

        while currenttime < videotime:
            video = ffmpeg.input(in_filename, ss=currenttime, t=part_time).filter('setpts', 'PTS-STARTPTS')
            audio = ffmpeg.input(in_filename, ss=currenttime, t=part_time).filter('aformat', 'sample_fmts=fltp|sample_rates=44100|channel_layouts=stereo').filter('asetpts', 'PTS-STARTPTS')

            finalvideo = ffmpeg.concat(video, audio, v=1, a=1).node
            output_filename = os.path.join(output_folder, f"{session_id}_part{uploadvideos}.mp4")
            output_files.append(output_filename)
            ffmpeg.output(finalvideo, output_filename).run()
            currenttime += part_time
            uploadvideos += 1

        return output_files
    except Exception as e:
        logging.error(f"Erreur lors du découpage de la vidéo : {str(e)}")
        raise Exception(f"Erreur lors du découpage de la vidéo : {str(e)}")

@app.before_request
def check_session():
    if 'session_id' in session:
        if 'uploaded_files' in session and not session['uploaded_files']:
            clean_up_files(session)
            session.pop('uploaded_files', None)
            session.pop('part_time', None)
            session.pop('zip_file', None)

def clean_up_files(session):
    # Supprimer les fichiers associés à la session expirée
    uploaded_files = session.get('uploaded_files', [])
    zip_file = session.get('zip_file')
    
    # Supprimer les fichiers téléchargés
    for file in uploaded_files:
        if os.path.exists(file):
            os.remove(file)

    # Supprimer les fichiers de sortie
    for file in os.listdir(app.config['OUTPUT_FOLDER']):
        if file.startswith(session.get('session_id', '')):
            os.remove(os.path.join(app.config['OUTPUT_FOLDER'], file))

    # Supprimer le fichier ZIP s'il existe
    if zip_file and os.path.exists(zip_file):
        os.remove(zip_file)

if __name__ == '__main__':
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    if not os.path.exists(app.config['OUTPUT_FOLDER']):
        os.makedirs(app.config['OUTPUT_FOLDER'])
    app.run(debug=True)
