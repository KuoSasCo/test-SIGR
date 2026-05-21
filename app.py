import os
import uuid
import boto3
import mysql.connector
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)
CORS(app, origins=["https://kuosasco.github.io"])

# ── Configuración AWS ──────────────────────────────────────────
AWS_ACCESS_KEY    = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY    = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION        = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET         = os.getenv("S3_BUCKET")
REKOGNITION_MODEL = os.getenv("REKOGNITION_MODEL_ARN")
MIN_CONFIDENCE    = float(os.getenv("MIN_CONFIDENCE", "50"))

# ── Configuración MySQL ────────────────────────────────────────
DB_HOST = os.getenv("MYSQLHOST")
DB_PORT = int(os.getenv("MYSQLPORT", "3306"))
DB_NAME = os.getenv("MYSQL_DATABASE")
DB_USER = os.getenv("MYSQLUSER")
DB_PASS = os.getenv("MYSQLPASSWORD")


def get_db():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT,
        database=DB_NAME, user=DB_USER, password=DB_PASS
    )


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            nombre     VARCHAR(120),
            email      VARCHAR(180),
            comentario TEXT NOT NULL,
            creado_en  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()
    print("✅ Base de datos lista.")


def get_rekognition_client():
    session = boto3.Session(
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION
    )
    return session.client("rekognition")


def get_s3_client():
    session = boto3.Session(
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION
    )
    return session.client("s3")


LABEL_META = {
    "reciclable": {
        "icono": "♻️", "clase": "res-reciclable",
        "desc": "Este residuo puede ser reciclado."
    },
    "organico": {
        "icono": "🌱", "clase": "res-organico",
        "desc": "Este residuo es de origen orgánico. Puede usarse para compostaje."
    },
    "no reciclable": {
        "icono": "🗑️", "clase": "res-no-reciclable",
        "desc": "Este residuo no es reciclable. Deposítalo en la basura general."
    },
    "posiblemente reciclable": {
        "icono": "🔄", "clase": "res-posible",
        "desc": "Podría ser reciclable dependiendo del municipio."
    },
}

def enriquecer_label(nombre_label):
    key = nombre_label.lower().strip()
    meta = LABEL_META.get(key, {
        "icono": "🔍", "clase": "res-default",
        "desc": "Residuo clasificado por el modelo."
    })
    return {"label": nombre_label, **meta}


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


@app.route("/clasificar", methods=["POST"])
def clasificar():
    if "imagen" not in request.files:
        return jsonify({"error": "No se envió ninguna imagen."}), 400

    archivo = request.files["imagen"]
    extension = archivo.filename.rsplit(".", 1)[-1].lower()
    nombre_s3 = f"uploads/{uuid.uuid4().hex}.{extension}"

    try:
        s3 = get_s3_client()
        s3.upload_fileobj(
            archivo, S3_BUCKET, nombre_s3,
            ExtraArgs={"ContentType": archivo.content_type}
        )

        rek = get_rekognition_client()
        response = rek.detect_custom_labels(
            Image={"S3Object": {"Bucket": S3_BUCKET, "Name": nombre_s3}},
            MinConfidence=MIN_CONFIDENCE,
            ProjectVersionArn=REKOGNITION_MODEL
        )

        labels = response.get("CustomLabels", [])

        if not labels:
            return jsonify({
                "detectado": False,
                "mensaje": "No se detectó ningún residuo con suficiente confianza.",
                "label": None, "confianza": None
            })

        mejor = max(labels, key=lambda x: x["Confidence"])
        meta = enriquecer_label(mejor["Name"])

        return jsonify({
            "detectado": True,
            "label": mejor["Name"],
            "confianza": round(mejor["Confidence"], 2),
            "icono": meta["icono"],
            "clase": meta["clase"],
            "desc": meta["desc"],
            "imagen_key": nombre_s3,
            "todos_los_labels": [
                {"label": l["Name"], "confianza": round(l["Confidence"], 2)}
                for l in labels
            ]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/feedback", methods=["POST"])
def feedback():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    comentario = data.get("comentario", "").strip()
    if not comentario:
        return jsonify({"error": "El comentario es obligatorio."}), 400

    nombre = data.get("nombre", "").strip() or None
    email  = data.get("email", "").strip() or None

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO feedback (nombre, email, comentario) VALUES (%s, %s, %s)",
            (nombre, email, comentario)
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"ok": True, "mensaje": "Feedback guardado correctamente."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/feedback", methods=["GET"])
def listar_feedback():
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM feedback ORDER BY creado_en DESC")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        for row in rows:
            if row.get("creado_en"):
                row["creado_en"] = row["creado_en"].isoformat()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)