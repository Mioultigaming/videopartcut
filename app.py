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
import logging

# Configuration de l'application Flask
app = Flask(__name__)
app.secret_key = os.urandom(24)  # Utiliser une clé secrète générée aléatoirement
app.permanent_session_lifetime = timedelta(minutes=30)  # Durée de vie de la session


# Configuration du CSRF
csrf = CSRFProtect(app)

# Configuration de la limitation des requêtes

app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
#app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

# Configurer la journalisation
logging.basicConfig(filename='app.log', level=logging.INFO)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'mp4', 'mov', 'avi'}

@app.route('/')
def index():
    return render_template('index.html')

logging.basicConfig(level=logging.DEBUG)

@app.route('/upload', methods=['POST'])
@csrf.exempt
def upload_file():
    try:
        if 'file' not in request.files:
            logging.error("Aucun fichier dans la requête")
            return "Pas de fichier téléchargé", 400

        files = request.files.getlist('file')
        if not files:
            logging.error("Aucun fichier sélectionné")
            return "Pas de fichier sélectionné", 400

        part_time = int(request.form.get('duration', 10))
        if part_time <= 0:
            raise ValueError("La durée doit être un nombre entier positif.")
        
        session.permanent = True  # Marquer la session comme permanente
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id
        uploaded_files = []

        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_filename = f"{session_id}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                logging.debug(f"Enregistrement du fichier : {filepath}")
                file.save(filepath)
                uploaded_files.append(filepath)
            else:
                logging.error(f"Type de fichier non autorisé : {file.filename}")
                return "Type de fichier non autorisé", 400

        session['uploaded_files'] = uploaded_files
        session['part_time'] = part_time

        logging.info("Fichiers téléchargés avec succès")
        return redirect(url_for('process_video'))
    except Exception as e:
        logging.exception(f"Erreur lors du téléchargement du fichier : {e}")
        return f"Erreur lors du téléchargement du fichier : {str(e)}", 500

@app.route('/process')
def process_video():
    if 'uploaded_files' not in session:
        return "Pas de fichier à traiter", 400

    uploaded_files = session['uploaded_files']
    part_time = session.get('part_time', 60)

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

    return redirect("/")

@app.route('/download_zip')
def download_zip():
    zip_file = session.get('zip_file')
    if not zip_file or not os.path.exists(zip_file):
        return "Aucun fichier ZIP disponible", 400
    return send_file(zip_file, as_attachment=True)

def cut_video(in_filename, part_time, output_folder, session_id):
    try:
        # Vérifiez que le fichier d'entrée existe
        if not os.path.exists(in_filename):
            raise FileNotFoundError(f"Le fichier d'entrée {in_filename} est introuvable.")

        # Assurez-vous que le dossier de sortie existe
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        probe = ffmpeg.probe(in_filename)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        if video_stream is None:
            raise ValueError('Aucun flux vidéo trouvé.')

        videotime = float(video_stream['duration'])
        currenttime = 0
        uploadvideos = 0
        output_files = []

        while currenttime < videotime:
            # Créez les entrées vidéo et audio
            input_stream = ffmpeg.input(in_filename, ss=currenttime, t=part_time)
            
            # Utilisation du filtre 'split' pour permettre des sorties multiples sans conflits
            video_split = input_stream.video.filter('split')
            video = video_split.filter('setpts', 'PTS-STARTPTS')
            audio_split = input_stream.audio.filter('asplit')
            audio = audio_split.filter('asetpts', 'PTS-STARTPTS')

            # Créez le nom de fichier de sortie
            output_filename = os.path.join(output_folder, f"{session_id}_part{uploadvideos}.mp4")
            output_files.append(output_filename)

            # Concaténer la vidéo et l'audio en spécifiant explicitement les indices 'v' et 'a'
            finalvideo = ffmpeg.concat(video, audio, v=1, a=1).node
            
            # Sortie de la vidéo finale
            try:
                ffmpeg.output(finalvideo[0], finalvideo[1], output_filename).run(overwrite_output=True)
            except ffmpeg.Error as e:
                raise Exception(f"Erreur lors de l'exécution de ffmpeg : {e.stderr.decode()}")

            currenttime += part_time
            uploadvideos += 1

        return output_files
    except FileNotFoundError as fnf_error:
        print(f"Erreur: {fnf_error}")
        raise Exception(fnf_error)
    except ffmpeg.Error as ffmpeg_error:
        print(f"Erreur lors de l'utilisation de ffmpeg: {ffmpeg_error.stderr.decode()}")
        raise Exception(f"Erreur lors de l'utilisation de ffmpeg: {ffmpeg_error.stderr.decode()}")
    except Exception as e:
        print(f"Erreur générale lors du découpage de la vidéo: {str(e)}")
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
